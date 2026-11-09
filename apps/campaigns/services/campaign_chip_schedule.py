"""
Per-chip campaign queue: clear future deferrals (rate limit / retry)
so they are re-evaluated with new limits.
"""

from __future__ import annotations

from django.db.models import Count
from django.utils import timezone

from apps.campaigns.models import CampaignSendJob


def count_pending_future_scheduled_for_chip(chip_id: int) -> int:
    """PENDING jobs with scheduled_at in the future (not yet eligible for the worker)."""
    now = timezone.now()
    return CampaignSendJob.objects.filter(
        chip_id=chip_id,
        status=CampaignSendJob.Status.PENDING,
        scheduled_at__gt=now,
    ).count()


def count_pending_future_scheduled_for_chips(chip_ids: list[int]) -> dict[int, int]:
    """Map chip_id -> number of future deferrals."""
    if not chip_ids:
        return {}
    now = timezone.now()
    rows = (
        CampaignSendJob.objects.filter(
            chip_id__in=chip_ids,
            status=CampaignSendJob.Status.PENDING,
            scheduled_at__gt=now,
        )
        .values("chip_id")
        .annotate(n=Count("id"))
    )
    return {row["chip_id"]: row["n"] for row in rows}


def clear_future_schedules_for_chip(chip_id: int) -> int:
    """
    Remove scheduled_at from PENDING jobs whose send was scheduled for after now.

    Useful after raising chip limits or fixing its configuration: the worker applies
    the rate limit again with the chip's current values.
    """
    now = timezone.now()
    return CampaignSendJob.objects.filter(
        chip_id=chip_id,
        status=CampaignSendJob.Status.PENDING,
        scheduled_at__gt=now,
    ).update(scheduled_at=None, schedule_defer_reason="")
