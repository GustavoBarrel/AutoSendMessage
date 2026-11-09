"""
Processing of one job: validations, variant choice, send (Evolution API), attempt log.

Main flow:
  claim_job (worker) → process_send_job_once → extend_lease → dispatch → mark_sent → _write_attempt

Anti-duplicate guarantees:
  - extend_lease renews the lease BEFORE dispatch to avoid a reclaim during the API call.
  - mark_sent requires LOCKED + the right locked_by; only one UPDATE per lock.
  - force_mark_sent_after_dispatch is a last resort: used only when dispatch succeeded but
    mark_sent failed (lock expired during a slow API call); it marks SENT without checking
    locked_by to avoid sending again.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.campaigns.models import (
    Campaign,
    CampaignMessageVariant,
    CampaignSendAttempt,
    CampaignSendJob,
)
from apps.campaigns.services.campaign_chip_offline import (
    ERROR_KIND_CHIP_OFFLINE,
    suspend_jobs_for_chip_offline,
)
from apps.campaigns.services.campaign_rate_limit import defer_if_chip_rate_limited
from apps.campaigns.services.campaign_send_queue import (
    cancel_locked_job_by_worker,
    extend_lease,
    fail_locked_configuration_error,
    fail_locked_permanent_contact_error,
    force_mark_sent_after_dispatch,
    mark_sent,
    schedule_retry,
)
from apps.chips.providers import ProviderDisabledError, get_provider_for_chip
from apps.chips.services.evolution_whatsapp_errors import is_whatsapp_463_error

# Extra lease time granted before dispatch.
# Must cover the maximum timeout of the Evolution API call (send_text/send_media timeout=60-90s)
# with enough margin.
_DISPATCH_LEASE_EXTENSION_S = 180

logger = logging.getLogger(__name__)

_MAX_BACKOFF = 3600

_PERMANENT_CONTACT_ERROR_PATTERNS = (
    r'"exists"\s*:\s*false',
    r"'exists'\s*:\s*false",
    r"\bexists\s*=\s*false\b",
    r"\bis not on whatsapp\b",
    r"\bnot on whatsapp\b",
    r"\bnot registered\b",
    r"\brecipient not found\b",
    r"\binvalid jid\b",
    r"\binvalid wid\b",
)


def _payload_contains_exists_false(payload: dict | None) -> bool:
    if not payload:
        return False
    try:
        stack: list[object] = [payload]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if cur.get("exists") is False:
                    return True
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    except Exception:
        return False
    return False


def _is_permanent_contact_failure(err_text: str, payload: dict | None) -> bool:
    t = (err_text or "").lower()
    if _payload_contains_exists_false(payload):
        return True
    return any(re.search(p, t) for p in _PERMANENT_CONTACT_ERROR_PATTERNS)


def _is_permanent_send_failure(err_text: str, payload: dict | None) -> bool:
    if is_whatsapp_463_error(err_text, payload):
        return True
    return _is_permanent_contact_failure(err_text, payload)


def pick_variant_for_job(job: CampaignSendJob) -> CampaignMessageVariant:
    """Return the right variant for the job (the variant_id fixed at enqueue time, if present)."""
    if job.variant_id:
        v = getattr(job, "variant", None)
        if v is not None:
            return v
        return CampaignMessageVariant.objects.get(pk=job.variant_id)
    # Fallback: distribute by pk % 3 (only when variant_id was not set at enqueue time)
    variants = list(
        CampaignMessageVariant.objects.filter(campaign_id=job.campaign_id).order_by("position")
    )
    if len(variants) != 3:
        raise ValueError("Campanha sem exatamente 3 variações de mensagem.")
    return variants[(job.pk or 0) % 3]


def _backoff_seconds(attempts: int) -> int:
    """Exponential backoff based on the number of attempts already made."""
    exp = min(8, max(0, attempts))
    return max(15, min(_MAX_BACKOFF, 30 * (2**exp)))


DispatchFn = Callable[
    [CampaignSendJob, CampaignMessageVariant],
    tuple[bool, str, str] | tuple[bool, str, str, dict | None, int | None, str | None],
]
"""(job, variant) → tupla curta ou longa com error_kind opcional (ex.: chip_offline)."""


def process_send_job_once(
    job_id: int,
    worker_id: str,
    *,
    dispatch: DispatchFn | None = None,
) -> str:
    """
    Process a job that is already LOCKED (the caller must have done the claim).

    Possible return values:
      sent            — message successfully delivered to the provider API
      retry_scheduled — temporary failure; job rescheduled with backoff
      failed          — permanent failure (max_attempts reached or invalid configuration)
      cancelled       — the campaign is no longer active
      deferred        — rate limit / outside business hours; job rescheduled
      waiting_chip    — Evolution instance disconnected; the chip's queue is suspended in bulk
      noop            — invalid lock or race; no action taken
    """
    # ── 1. Load and validate the lock ──
    try:
        job = CampaignSendJob.objects.select_related("campaign", "chip", "contact", "variant").get(
            pk=job_id
        )
    except CampaignSendJob.DoesNotExist:
        return "noop"

    if job.status != CampaignSendJob.Status.LOCKED:
        return "noop"
    if job.locked_by != worker_id[:64]:
        return "noop"

    # ── 2. Validate campaign, contact and chip ──
    camp = job.campaign
    if camp is None:
        cancel_locked_job_by_worker(job_id, worker_id)
        logger.warning("job=%s no campaign (deleted) — cancelled", job_id)
        return "cancelled"
    if camp.status != Campaign.Status.ACTIVE:
        cancel_locked_job_by_worker(job_id, worker_id)
        logger.info("job=%s campaign status=%s — cancelled", job_id, camp.status)
        return "cancelled"
    contact = job.contact
    if contact is None or contact.deleted_at is not None:
        name = (contact.name if contact else "") or "desconhecido"
        cancel_locked_job_by_worker(job_id, worker_id)
        logger.info("job=%s contact deleted (%s) — cancelled", job_id, name)
        return "cancelled"
    if job.chip is None:
        msg = f"Chip excluído (snapshot: {job.chip_name_snapshot or '—'}). Reconfigure a campanha."
        fail_locked_configuration_error(job_id, worker_id, message=msg)
        logger.warning("job=%s chip=None — cancelled because the chip was deleted", job_id)
        return "failed"

    # A chip whose provider is off in this environment can never send: fail the job now, with a
    # clear message, instead of deferring or retrying it forever.
    try:
        get_provider_for_chip(job.chip)
    except ProviderDisabledError as exc:
        fail_locked_configuration_error(job_id, worker_id, message=str(exc))
        logger.error("job=%s chip=%s provider disabled: %s", job_id, job.chip_id, exc)
        return "failed"

    # ── 3. Rate limit / business-hours window ──
    if defer_if_chip_rate_limited(job, worker_id):
        return "deferred"

    # attempt_no is computed from the current database state to avoid a
    # UNIQUE(job_id, attempt_no) collision when retry_failed_jobs reset attempts.
    current_max_attempt = (
        CampaignSendAttempt.objects.filter(job_id=job_id).aggregate(Max("attempt_no"))[
            "attempt_no__max"
        ]
        or 0
    )
    attempt_no = current_max_attempt + 1

    # ── 4. Resolve the variant ──
    try:
        variant = pick_variant_for_job(job)
    except CampaignMessageVariant.DoesNotExist:
        msg = "Variação de mensagem não encontrada (removida ou id inválido)."
        if fail_locked_configuration_error(job_id, worker_id, message=msg):
            _write_attempt(job_id, attempt_no, False, msg, None, None)
        logger.error("job=%s config failure: %s", job_id, msg)
        return "failed"
    except ValueError as exc:
        msg = str(exc)[:2000]
        if fail_locked_configuration_error(job_id, worker_id, message=msg):
            _write_attempt(job_id, attempt_no, False, msg, None, None)
        logger.error("job=%s config failure: %s", job_id, msg)
        return "failed"

    # ── 5. Renew the lease before dispatch (heartbeat) ──
    if not extend_lease(job_id, worker_id, extra_seconds=_DISPATCH_LEASE_EXTENSION_S):
        logger.warning(
            "job=%s extend_lease failed — lock already lost, aborting without dispatch",
            job_id,
        )
        return "noop"

    # ── 6. Dispatch to the provider ──
    ok, err, ext_id = False, "", ""
    payload: dict | None = None
    http_status: int | None = None
    error_kind: str | None = None
    try:
        if dispatch is not None:
            raw = dispatch(job, variant)
            if isinstance(raw, tuple) and len(raw) >= 6:
                ok, err, ext_id = raw[0], raw[1], raw[2]
                payload, http_status, error_kind = raw[3], raw[4], raw[5]
            elif isinstance(raw, tuple) and len(raw) == 5:
                ok, err, ext_id, payload, http_status = raw  # type: ignore[misc]
            else:
                ok, err, ext_id = raw[0], raw[1], raw[2]  # type: ignore[misc]
        else:
            from apps.campaigns.services.campaign_provider_dispatch import dispatch_campaign_message

            ok, err, ext_id, payload, http_status, error_kind = dispatch_campaign_message(
                job, variant
            )
    except Exception as exc:
        ok = False
        err = f"{type(exc).__name__}: {exc}"
        logger.exception("job=%s dispatch exception", job_id)

    # ── 7a. Success: mark SENT ──
    if ok:
        marked = mark_sent(
            job_id,
            worker_id,
            variant_id=variant.pk,
            external_message_id=ext_id or "",
        )
        if marked:
            _write_attempt(job_id, attempt_no, True, "", http_status, payload)
            logger.info("job=%s SENT chip=%s contact=%s", job_id, job.chip_id, job.contact_id)
            return "sent"

        # mark_sent failed: the lock expired during dispatch (rare window after the 180s heartbeat).
        # The message WAS already sent. Force SENT to avoid sending it again.
        forced = force_mark_sent_after_dispatch(
            job_id,
            variant_id=variant.pk,
            external_message_id=ext_id or "",
        )
        if forced:
            logger.warning(
                "job=%s mark_sent failed but force_mark_sent OK — message already sent ext_id=%s",
                job_id,
                ext_id,
            )
            _write_attempt(job_id, attempt_no, True, "", http_status, payload)
            return "sent"

        # Both failed: inconsistent state. Critical log for manual review.
        logger.critical(
            "job=%s POTENTIAL DUPLICATE — dispatch OK but mark_sent and force_mark_sent failed "
            "ext_id=%s — check manually",
            job_id,
            ext_id,
        )
        return "noop"

    # ── 7b. Failure: schedule a retry with backoff ──
    err_truncated = (err or "Erro desconhecido no dispatch")[:2000]
    logger.warning(
        "job=%s dispatch FAILED attempt=%d http=%s error: %s",
        job_id,
        attempt_no,
        http_status,
        err_truncated,
    )

    if error_kind == ERROR_KIND_CHIP_OFFLINE and job.chip_id and camp.pk is not None:
        suspend_jobs_for_chip_offline(
            campaign_id=camp.pk,
            chip_id=job.chip_id,
            current_job_id=job_id,
            worker_id=worker_id,
            last_error_for_trigger=err_truncated,
        )
        logger.warning(
            "job=%s chip_offline — chip jobs suspended in bulk (no per-contact retry)",
            job_id,
        )
        return "waiting_chip"

    if _is_permanent_send_failure(err_truncated, payload):
        failed = fail_locked_permanent_contact_error(
            job_id,
            worker_id,
            message=err_truncated,
        )
        if not failed:
            logger.warning("job=%s fail_locked_permanent_contact_error refused — lock lost", job_id)
            return "noop"
        _write_attempt(job_id, attempt_no, False, err_truncated, http_status, payload)
        logger.error(
            "job=%s permanent FAILED (contact) after 1 attempt — error: %s", job_id, err_truncated
        )
        return "failed"

    # Read the updated attempts from the database (retry_failed_jobs may have reset it)
    current_attempts = (
        CampaignSendJob.objects.filter(pk=job_id).values_list("attempts", flat=True).first() or 0
    )
    retried = schedule_retry(
        job_id,
        worker_id,
        error_message=err_truncated,
        backoff_seconds=_backoff_seconds(current_attempts),
    )
    if not retried:
        logger.warning("job=%s schedule_retry refused — lock lost", job_id)
        return "noop"

    _write_attempt(job_id, attempt_no, False, err_truncated, http_status, payload)

    # Check whether max_attempts was reached (schedule_retry may have switched it to FAILED)
    final_status = (
        CampaignSendJob.objects.filter(pk=job_id).values_list("status", flat=True).first()
    )
    if final_status == CampaignSendJob.Status.FAILED:
        logger.error(
            "job=%s FAILED after %d attempts — error: %s", job_id, attempt_no, err_truncated
        )
        return "failed"
    return "retry_scheduled"


def _write_attempt(
    job_id: int,
    attempt_no: int,
    success: bool,
    error_text: str,
    http_status: int | None,
    payload: dict | None,
) -> None:
    """
    Write a CampaignSendAttempt. Use the given attempt_no (already computed as max+1).
    On a collision (extreme edge case), try attempt_no+1 before giving up.
    """
    try:
        with transaction.atomic():
            CampaignSendAttempt.objects.create(
                job_id=job_id,
                attempt_no=attempt_no,
                success=success,
                error_text=(error_text or "")[:2000],
                http_status=http_status,
                provider_payload=payload,
                finished_at=timezone.now(),
            )
    except Exception:
        # attempt_no collision (extremely rare race): retry with +1
        try:
            with transaction.atomic():
                fallback_no = (
                    CampaignSendAttempt.objects.filter(job_id=job_id).aggregate(Max("attempt_no"))[
                        "attempt_no__max"
                    ]
                    or 0
                ) + 1
                CampaignSendAttempt.objects.create(
                    job_id=job_id,
                    attempt_no=fallback_no,
                    success=success,
                    error_text=(error_text or "")[:2000],
                    http_status=http_status,
                    provider_payload=payload,
                    finished_at=timezone.now(),
                )
        except Exception:
            logger.exception(
                "_write_attempt failed permanently job=%s attempt=%s", job_id, attempt_no
            )


def claim_and_process_one(
    job_id: int,
    worker_id: str,
    *,
    lease_seconds: int,
    dispatch: DispatchFn | None = None,
) -> str:
    """Claim + process in sequence. Return the result, or 'not_claimed' if the claim failed."""
    from apps.campaigns.services.campaign_send_queue import claim_job

    claimed = claim_job(job_id, worker_id, lease_seconds=lease_seconds)
    if not claimed:
        return "not_claimed"
    return process_send_job_once(claimed.pk, worker_id, dispatch=dispatch)
