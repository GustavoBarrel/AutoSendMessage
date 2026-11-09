"""
Detection of a disconnected Evolution instance: suspends jobs in bulk (without hammering
the API) and allows automatic resume or redistribution to another chip.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.campaigns.models import Campaign, CampaignChip, CampaignSendJob
from apps.chips.models import Chip
from apps.chips.services.evolution import connection_state

logger = logging.getLogger(__name__)

ERROR_KIND_CHIP_OFFLINE = "chip_offline"
"""Valor de error_kind em dispatch_campaign_message quando connectionState=close."""

CHIP_CONNECTION_CHECK_INTERVAL_SEC = 30


def _now():
    return timezone.now()


@transaction.atomic
def suspend_jobs_for_chip_offline(
    *,
    campaign_id: int,
    chip_id: int,
    current_job_id: int,
    worker_id: str,
    last_error_for_trigger: str,
) -> tuple[int, int]:
    """
    Move every PENDING job of the same chip in the campaign, and the current LOCKED job,
    to WAITING_CHIP.

    Return (number of PENDING jobs updated, 0 or 1 if the current job was updated).
    """
    wid = worker_id[:64]
    reason = CampaignSendJob.ScheduleDeferReason.CHIP_OFFLINE
    bulk_err = ""
    n_pending = CampaignSendJob.objects.filter(
        campaign_id=campaign_id,
        chip_id=chip_id,
        status=CampaignSendJob.Status.PENDING,
    ).update(
        status=CampaignSendJob.Status.WAITING_CHIP,
        scheduled_at=None,
        schedule_defer_reason=reason,
        last_error=bulk_err,
        locked_at=None,
        locked_by="",
        lease_expires_at=None,
    )
    n_locked = CampaignSendJob.objects.filter(
        pk=current_job_id,
        status=CampaignSendJob.Status.LOCKED,
        locked_by=wid,
    ).update(
        status=CampaignSendJob.Status.WAITING_CHIP,
        scheduled_at=None,
        schedule_defer_reason=reason,
        last_error=(last_error_for_trigger or "")[:2000],
        locked_at=None,
        locked_by="",
        lease_expires_at=None,
    )
    next_check = _now() + timedelta(seconds=CHIP_CONNECTION_CHECK_INTERVAL_SEC)
    Chip.objects.filter(pk=chip_id).update(evolution_state_next_check_at=next_check)
    logger.warning(
        "chip_offline_suspend campaign=%s chip=%s pending_moved=%s trigger_locked_updated=%s",
        campaign_id,
        chip_id,
        n_pending,
        n_locked,
    )
    return n_pending, n_locked


def try_resume_chips_with_waiting_jobs() -> int:
    """
    For each chip with jobs in WAITING_CHIP, query connectionState (respecting the throttle).

    Return the number of chips that came back online and had jobs reactivated.
    """
    # Local import: the Evolution provider imports this module (circular otherwise).
    from apps.chips.providers.simulated import SimulatedProvider

    base = (settings.EVOLUTION_URL_BASE or "").strip()
    key = (settings.EVOLUTION_API_KEY or "").strip()
    evolution_ok = bool(base and key)

    chip_ids = list(
        CampaignSendJob.objects.filter(
            status=CampaignSendJob.Status.WAITING_CHIP,
            chip_id__isnull=False,
        )
        .values_list("chip_id", flat=True)
        .distinct()
    )
    if not chip_ids:
        return 0

    now = _now()
    resumed = 0
    for chip in Chip.objects.filter(pk__in=chip_ids).order_by("pk"):
        if chip.evolution_state_next_check_at and chip.evolution_state_next_check_at > now:
            continue
        if chip.provider == Chip.Provider.SIMULATED:
            # Never queries Evolution: a simulated chip is always connected. The provider is built
            # directly (not via the factory) so that, if it is disabled, the jobs go back to the
            # queue and fail with a clear message instead of waiting here forever.
            state = SimulatedProvider(chip).get_connection_state()
        elif evolution_ok:
            state = connection_state(base, key, chip.instance_name)
        else:
            continue
        if state == "open":
            n = CampaignSendJob.objects.filter(
                chip_id=chip.pk,
                status=CampaignSendJob.Status.WAITING_CHIP,
                campaign__status=Campaign.Status.ACTIVE,
            ).update(
                status=CampaignSendJob.Status.PENDING,
                scheduled_at=now,
                schedule_defer_reason="",
                last_error="",
            )
            Chip.objects.filter(pk=chip.pk).update(evolution_state_next_check_at=None)
            if n:
                logger.info("chip_online_resume chip=%s jobs_reactivated=%s", chip.pk, n)
                resumed += 1
        elif state == "close":
            Chip.objects.filter(pk=chip.pk).update(
                evolution_state_next_check_at=now
                + timedelta(seconds=CHIP_CONNECTION_CHECK_INTERVAL_SEC)
            )
        else:
            Chip.objects.filter(pk=chip.pk).update(
                evolution_state_next_check_at=now
                + timedelta(seconds=CHIP_CONNECTION_CHECK_INTERVAL_SEC)
            )
    return resumed


def clear_chip_throttle_if_no_waiting_jobs(chip_id: int) -> None:
    """Remove the chip throttle if no WAITING_CHIP jobs remain for it."""
    still = CampaignSendJob.objects.filter(
        chip_id=chip_id,
        status=CampaignSendJob.Status.WAITING_CHIP,
    ).exists()
    if not still:
        Chip.objects.filter(pk=chip_id).update(evolution_state_next_check_at=None)


def redistribute_waiting_jobs(
    campaign: Campaign,
    *,
    source_chip_id: int,
    destination_chip_ids: list[int],
) -> int:
    """
    Move WAITING_CHIP jobs from the source chip to the destination chips (round-robin).

    Validate that the chips are active in the campaign and in the same workspace.
    Return the number of updated jobs.
    """
    if not destination_chip_ids:
        raise ValueError("Selecione ao menos um chip de destino.")
    dest_unique: list[int] = []
    seen: set[int] = set()
    for cid in destination_chip_ids:
        if cid not in seen and cid != source_chip_id:
            seen.add(cid)
            dest_unique.append(cid)
    if not dest_unique:
        raise ValueError("Os chips de destino devem ser diferentes do chip de origem.")

    allowed = set(
        CampaignChip.objects.filter(
            campaign=campaign,
            is_active=True,
            chip__is_active=True,
            chip_id__in=dest_unique,
        ).values_list("chip_id", flat=True)
    )
    if not allowed or not allowed.issuperset(set(dest_unique)):
        raise ValueError(
            "Todos os chips de destino devem estar ativos e vinculados a esta campanha."
        )

    chip_names = dict(
        Chip.objects.filter(workspace_id=campaign.workspace_id, pk__in=dest_unique).values_list(
            "pk", "name"
        )
    )
    if len(chip_names) != len(dest_unique):
        raise ValueError("Chip de destino inválido para o perfil desta campanha.")

    qs = CampaignSendJob.objects.filter(
        campaign=campaign,
        chip_id=source_chip_id,
        status=CampaignSendJob.Status.WAITING_CHIP,
    ).order_by("pk")
    ids = list(qs.values_list("pk", flat=True))
    if not ids:
        return 0

    n_dest = len(dest_unique)
    now = _now()
    updates: list[CampaignSendJob] = []
    for i, job_id in enumerate(ids):
        dest_id = dest_unique[i % n_dest]
        updates.append(
            CampaignSendJob(
                pk=job_id,
                chip_id=dest_id,
                chip_name_snapshot=chip_names.get(dest_id, ""),
                status=CampaignSendJob.Status.PENDING,
                scheduled_at=now,
                schedule_defer_reason="",
                last_error="",
                attempts=0,
                locked_at=None,
                locked_by="",
                lease_expires_at=None,
            )
        )
    CampaignSendJob.objects.bulk_update(
        updates,
        [
            "chip_id",
            "chip_name_snapshot",
            "status",
            "scheduled_at",
            "schedule_defer_reason",
            "last_error",
            "attempts",
            "locked_at",
            "locked_by",
            "lease_expires_at",
        ],
    )
    clear_chip_throttle_if_no_waiting_jobs(source_chip_id)
    return len(ids)
