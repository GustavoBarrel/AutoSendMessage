"""
Campaign send queue: states, locks with lease, retries and auditing.

claim_job is atomic; mark_sent and schedule_retry require LOCKED + locked_by
(anti double-send / anti race).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

# Short lease: if the process dies, another worker can recover the job.
DEFAULT_LOCK_LEASE_SECONDS = 120
DEFAULT_MAX_ATTEMPTS = 3


def _now():
    return timezone.now()


def reclaim_stale_locks(*, lease_seconds: int = DEFAULT_LOCK_LEASE_SECONDS) -> int:
    """
    Release jobs stuck by a process that died (expired lock).

    Prefer `lease_expires_at` when set (by extend_lease/claim), since it is the source of
    truth after heartbeat renewals. Only jobs without `lease_expires_at` fall back to
    `locked_at + lease_seconds`.
    Return the number of updated jobs.
    """
    from apps.campaigns.models import CampaignSendJob

    now = _now()
    fallback_cutoff = now - timedelta(seconds=lease_seconds)

    qs = CampaignSendJob.objects.filter(
        status=CampaignSendJob.Status.LOCKED,
    ).filter(
        # The explicit lease already expired OR there is no lease and locked_at is too old.
        Q(lease_expires_at__isnull=False, lease_expires_at__lt=now)
        | Q(lease_expires_at__isnull=True, locked_at__lt=fallback_cutoff)
    )
    return qs.update(
        status=CampaignSendJob.Status.PENDING,
        locked_at=None,
        locked_by="",
        lease_expires_at=None,
    )


def claim_job(
    job_id: int,
    worker_id: str,
    *,
    lease_seconds: int = DEFAULT_LOCK_LEASE_SECONDS,
):
    """
    Mark the job as LOCKED if it is still PENDING and scheduled_at <= now.
    Uses an atomic UPDATE (safe on SQLite; also avoids races on Postgres).
    Return the updated instance or None.
    """
    from apps.campaigns.models import CampaignSendJob

    now = _now()
    expires = now + timedelta(seconds=lease_seconds)
    updated = (
        CampaignSendJob.objects.filter(
            pk=job_id,
            status=CampaignSendJob.Status.PENDING,
        )
        .filter(Q(scheduled_at__isnull=True) | Q(scheduled_at__lte=now))
        .update(
            status=CampaignSendJob.Status.LOCKED,
            locked_at=now,
            locked_by=worker_id[:64],
            lease_expires_at=expires,
        )
    )
    if updated == 1:
        return CampaignSendJob.objects.get(pk=job_id)
    return None


def schedule_retry(
    job_id: int,
    worker_id: str,
    *,
    error_message: str,
    backoff_seconds: int,
) -> bool:
    """
    Only changes a LOCKED job owned by this worker, to avoid incrementing attempts
    on a job already released by a reclaim or another process (anti-duplicate / anti-loop).
    A single save() so no intermediate state exists between two UPDATEs.
    """
    from apps.campaigns.models import CampaignSendJob

    wid = worker_id[:64]
    err = (error_message or "")[:2000]
    with transaction.atomic():
        j = (
            CampaignSendJob.objects.select_for_update()
            .filter(
                pk=job_id,
                status=CampaignSendJob.Status.LOCKED,
                locked_by=wid,
            )
            .first()
        )
        if not j:
            return False
        j.attempts += 1
        j.last_error = err
        if j.attempts >= j.max_attempts:
            j.status = CampaignSendJob.Status.FAILED
            j.scheduled_at = None
        else:
            j.status = CampaignSendJob.Status.PENDING
            j.scheduled_at = _now() + timedelta(seconds=max(1, backoff_seconds))
        j.schedule_defer_reason = ""
        j.locked_at = None
        j.locked_by = ""
        j.lease_expires_at = None
        j.save()
    return True


def mark_sent(
    job_id: int,
    worker_id: str,
    *,
    variant_id: int | None = None,
    external_message_id: str = "",
) -> bool:
    """Mark SENT only if still LOCKED by this worker (one real send per lock)."""
    from apps.campaigns.models import CampaignSendJob

    fields = {
        "status": CampaignSendJob.Status.SENT,
        "sent_at": _now(),
        "locked_at": None,
        "locked_by": "",
        "lease_expires_at": None,
        "last_error": "",
        "schedule_defer_reason": "",
    }
    if variant_id is not None:
        fields["variant_id"] = variant_id
    if external_message_id:
        fields["external_message_id"] = external_message_id[:200]
    n = CampaignSendJob.objects.filter(
        pk=job_id,
        status=CampaignSendJob.Status.LOCKED,
        locked_by=worker_id[:64],
    ).update(**fields)
    return n == 1


def release_locked_to_pending_now(job_id: int, worker_id: str) -> bool:
    """
    LOCKED → PENDING with an immediate scheduled_at (recovery if rescheduling fails).
    Avoids a job stuck in LOCKED without processing.
    """
    from apps.campaigns.models import CampaignSendJob

    return (
        CampaignSendJob.objects.filter(
            pk=job_id,
            status=CampaignSendJob.Status.LOCKED,
            locked_by=worker_id[:64],
        ).update(
            status=CampaignSendJob.Status.PENDING,
            scheduled_at=_now(),
            schedule_defer_reason="",
            locked_at=None,
            locked_by="",
            lease_expires_at=None,
        )
        == 1
    )


def fail_locked_configuration_error(
    job_id: int,
    worker_id: str,
    *,
    message: str,
) -> bool:
    """Permanent failure caused by configuration (e.g. variant removed)."""
    from apps.campaigns.models import CampaignSendJob

    err = (message or "")[:2000]
    return (
        CampaignSendJob.objects.filter(
            pk=job_id,
            status=CampaignSendJob.Status.LOCKED,
            locked_by=worker_id[:64],
        ).update(
            status=CampaignSendJob.Status.FAILED,
            last_error=err,
            scheduled_at=None,
            schedule_defer_reason="",
            locked_at=None,
            locked_by="",
            lease_expires_at=None,
        )
        == 1
    )


def fail_locked_permanent_contact_error(
    job_id: int,
    worker_id: str,
    *,
    message: str,
) -> bool:
    """
    Permanent failure caused by the recipient (e.g. number does not exist / not on WhatsApp).

    Rule: after the first real attempt, retrying makes no sense
    (exists=false, invalid jid, etc.).
    Sets max_attempts=1 and attempts=1 to reflect the decision.
    """
    from apps.campaigns.models import CampaignSendJob

    wid = worker_id[:64]
    err = (message or "")[:2000]
    with transaction.atomic():
        j = (
            CampaignSendJob.objects.select_for_update()
            .filter(
                pk=job_id,
                status=CampaignSendJob.Status.LOCKED,
                locked_by=wid,
            )
            .first()
        )
        if not j:
            return False
        j.max_attempts = 1
        j.attempts = 1
        j.status = CampaignSendJob.Status.FAILED
        j.last_error = err
        j.scheduled_at = None
        j.schedule_defer_reason = ""
        j.locked_at = None
        j.locked_by = ""
        j.lease_expires_at = None
        j.save()
    return True


def reschedule_locked_without_retry(
    job_id: int,
    worker_id: str,
    scheduled_at,
    *,
    schedule_defer_reason: str = "",
) -> bool:
    """
    Go back to PENDING with a future scheduled_at, without incrementing attempts
    (e.g. rate limit / time window).
    """
    from apps.campaigns.models import CampaignSendJob

    n = CampaignSendJob.objects.filter(
        pk=job_id,
        status=CampaignSendJob.Status.LOCKED,
        locked_by=worker_id[:64],
    ).update(
        status=CampaignSendJob.Status.PENDING,
        scheduled_at=scheduled_at,
        schedule_defer_reason=schedule_defer_reason or "",
        locked_at=None,
        locked_by="",
        lease_expires_at=None,
    )
    return n == 1


def cancel_locked_job_by_worker(job_id: int, worker_id: str) -> bool:
    """Release this worker's LOCKED job to CANCELLED (e.g. the campaign stopped being active)."""
    from apps.campaigns.models import CampaignSendJob

    n = CampaignSendJob.objects.filter(
        pk=job_id,
        status=CampaignSendJob.Status.LOCKED,
        locked_by=worker_id[:64],
    ).update(
        status=CampaignSendJob.Status.CANCELLED,
        schedule_defer_reason="",
        locked_at=None,
        locked_by="",
        lease_expires_at=None,
    )
    return n == 1


def extend_lease(job_id: int, worker_id: str, *, extra_seconds: int) -> bool:
    """
    Renew the lease of a LOCKED job owned by this worker.
    Called as a heartbeat before slow operations (e.g. the Evolution API call)
    so reclaim_stale_locks does not release the job while the dispatch is still running.
    Return True if the renewal was applied.
    """
    from apps.campaigns.models import CampaignSendJob

    expires = _now() + timedelta(seconds=max(1, extra_seconds))
    return (
        CampaignSendJob.objects.filter(
            pk=job_id,
            status=CampaignSendJob.Status.LOCKED,
            locked_by=worker_id[:64],
        ).update(lease_expires_at=expires)
        == 1
    )


def force_mark_sent_after_dispatch(
    job_id: int,
    *,
    variant_id: int | None = None,
    external_message_id: str = "",
) -> bool:
    """
    Last resort: mark SENT without checking locked_by, used only when
    the dispatch to Evolution was already confirmed but mark_sent failed (lock lost
    by expiration). Guarantees the job never goes back to PENDING after a real send.
    Only changes the status if the job is not already SENT/FAILED/CANCELLED.
    """
    from apps.campaigns.models import CampaignSendJob

    fields: dict = {
        "status": CampaignSendJob.Status.SENT,
        "sent_at": _now(),
        "locked_at": None,
        "locked_by": "",
        "lease_expires_at": None,
        "last_error": "",
        "schedule_defer_reason": "",
    }
    if variant_id is not None:
        fields["variant_id"] = variant_id
    if external_message_id:
        fields["external_message_id"] = external_message_id[:200]
    return (
        CampaignSendJob.objects.filter(
            pk=job_id,
            status__in=[
                CampaignSendJob.Status.LOCKED,
                CampaignSendJob.Status.PENDING,
            ],
        ).update(**fields)
        == 1
    )


def cancel_pending_for_campaign(campaign_id: int) -> int:
    """Mark PENDING, LOCKED or WAITING_CHIP jobs as CANCELLED (finish campaign / archive)."""
    from apps.campaigns.models import CampaignSendJob

    return CampaignSendJob.objects.filter(
        campaign_id=campaign_id,
        status__in=[
            CampaignSendJob.Status.PENDING,
            CampaignSendJob.Status.LOCKED,
            CampaignSendJob.Status.WAITING_CHIP,
        ],
    ).update(
        status=CampaignSendJob.Status.CANCELLED,
        schedule_defer_reason="",
        locked_at=None,
        locked_by="",
        lease_expires_at=None,
    )


def retry_failed_jobs(campaign_id: int) -> int:
    """
    Put the campaign's FAILED jobs back to PENDING, resetting attempts
    and error. The worker picks them up normally in the next cycle.
    Return the number of rescheduled jobs.
    """
    from apps.campaigns.models import CampaignSendJob

    return CampaignSendJob.objects.filter(
        campaign_id=campaign_id,
        status=CampaignSendJob.Status.FAILED,
    ).update(
        status=CampaignSendJob.Status.PENDING,
        attempts=0,
        last_error="",
        scheduled_at=_now(),
        schedule_defer_reason="",
        locked_at=None,
        locked_by="",
        lease_expires_at=None,
    )


def new_worker_id() -> str:
    return str(uuid.uuid4())
