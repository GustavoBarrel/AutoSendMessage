"""
Per-chip limits: interval between sends, messages per hour/day, batch pause and time window.
Does not increment attempts; it only reschedules the job (rate limit, not a failure).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from django.utils import timezone

from apps.campaigns.models import CampaignSendJob
from apps.campaigns.services.campaign_humanized_delay import calculate_humanized_delay_seconds
from apps.chips.models import Chip

_DEFER_RACE = CampaignSendJob.ScheduleDeferReason.DEFER_RACE.value

_BATCH_SIZE = 25
_BATCH_PAUSE_MIN_S = 120
_BATCH_PAUSE_MAX_S = 180


def _delay_seconds(chip: Chip) -> int:
    return chip.delay_between_messages_seconds or chip.get_recommended_delay_seconds()


def _last_sent_at(chip: Chip) -> datetime | None:
    return (
        CampaignSendJob.objects.filter(
            chip_id=chip.pk,
            status=CampaignSendJob.Status.SENT,
            sent_at__isnull=False,
        )
        .order_by("-sent_at")
        .values_list("sent_at", flat=True)
        .first()
    )


def next_slot_after_last_send(chip: Chip, now: datetime) -> datetime | None:
    """If the interval since the last send on this chip has not passed, return when it can send."""
    last = _last_sent_at(chip)
    if not last:
        return None
    need = calculate_humanized_delay_seconds(_delay_seconds(chip))
    elapsed = (now - last).total_seconds()
    if elapsed >= need:
        return None
    return last + timedelta(seconds=need)


def _hourly_sent(chip: Chip, now: datetime) -> tuple[int, datetime | None]:
    """Return (count, oldest sent_at) in the rolling 1-hour window."""
    since = now - timedelta(hours=1)
    qs = CampaignSendJob.objects.filter(
        chip_id=chip.pk,
        status=CampaignSendJob.Status.SENT,
        sent_at__gte=since,
    )
    count = qs.count()
    oldest = qs.order_by("sent_at").values_list("sent_at", flat=True).first() if count else None
    return count, oldest


def _daily_count(chip: Chip) -> int:
    return CampaignSendJob.objects.filter(
        chip_id=chip.pk,
        status=CampaignSendJob.Status.SENT,
        sent_at__date=timezone.localdate(),
    ).count()


def _within_business_hours(chip: Chip, now: datetime) -> bool:
    t = timezone.localtime(now).time()
    return chip.send_window_start <= t <= chip.send_window_end


def next_open_window_start(chip: Chip, now: datetime) -> datetime:
    """Next instant inside the chip's business-hours window."""
    local = timezone.localtime(now)
    start = datetime.combine(local.date(), chip.send_window_start)
    if timezone.is_naive(start):
        start = timezone.make_aware(start, timezone.get_current_timezone())
    end = datetime.combine(local.date(), chip.send_window_end)
    if timezone.is_naive(end):
        end = timezone.make_aware(end, timezone.get_current_timezone())
    if now < start:
        return start
    if now > end:
        next_day = local.date() + timedelta(days=1)
        ns = datetime.combine(next_day, chip.send_window_start)
        if timezone.is_naive(ns):
            ns = timezone.make_aware(ns, timezone.get_current_timezone())
        return ns
    return now


def _batch_pause_until(chip: Chip, now: datetime) -> datetime | None:
    """Every ~25 messages sent in the day, pause for 2-3 minutes."""
    daily = _daily_count(chip)
    if daily == 0 or daily % _BATCH_SIZE != 0:
        return None
    last = _last_sent_at(chip)
    if not last:
        return None
    pause = random.randint(_BATCH_PAUSE_MIN_S, _BATCH_PAUSE_MAX_S)
    resume_at = last + timedelta(seconds=pause)
    return resume_at if resume_at > now else None


def compute_defer_until_with_reason(chip: Chip, now: datetime) -> tuple[datetime | None, str]:
    """
    If it cannot send now, return (next datetime, reason code).
    Reason: values of CampaignSendJob.ScheduleDeferReason.
    """
    R = CampaignSendJob.ScheduleDeferReason
    if not _within_business_hours(chip, now):
        nxt = next_open_window_start(chip, now)
        if nxt > now:
            return nxt, R.BUSINESS_HOURS.value

    slot = next_slot_after_last_send(chip, now)
    if slot and slot > now:
        return slot, R.MIN_INTERVAL.value

    batch = _batch_pause_until(chip, now)
    if batch and batch > now:
        return batch, R.BATCH_PAUSE.value

    lim_h = chip.max_messages_per_hour or chip.get_recommended_limits()["per_hour"]
    hourly_count, oldest_in_window = _hourly_sent(chip, now)
    if hourly_count >= lim_h:
        if oldest_in_window:
            return oldest_in_window + timedelta(hours=1), R.HOURLY_LIMIT.value
        return now + timedelta(minutes=5), R.HOURLY_LIMIT.value

    lim_d = chip.max_messages_per_day or chip.get_recommended_limits()["per_day"]
    if _daily_count(chip) >= lim_d:
        local = timezone.localtime(now)
        tomorrow = local.date() + timedelta(days=1)
        ns = datetime.combine(tomorrow, chip.send_window_start)
        if timezone.is_naive(ns):
            ns = timezone.make_aware(ns, timezone.get_current_timezone())
        return ns, R.DAILY_LIMIT.value

    return None, ""


def compute_defer_until(chip: Chip, now: datetime) -> datetime | None:
    """If it cannot send now, return the next datetime to try."""
    until, _ = compute_defer_until_with_reason(chip, now)
    return until


_DEFER_FALLBACK_MIN_SECONDS = 60
"""Delay mínimo aplicado ao fallback do defer para evitar loop imediato."""


def defer_if_chip_rate_limited(job: CampaignSendJob, worker_id: str) -> bool:
    """
    If the chip cannot send now, reschedule the job without incrementing attempts.
    Return True if the Evolution send must be skipped in this cycle.

    When the main reschedule fails (race condition), use a fallback with a minimum
    delay (_DEFER_FALLBACK_MIN_SECONDS) so the job does not go back to PENDING
    immediately and cause a CPU loop.
    """
    import logging

    from apps.campaigns.services.campaign_send_queue import reschedule_locked_without_retry

    logger = logging.getLogger(__name__)
    now = timezone.now()
    until, reason = compute_defer_until_with_reason(job.chip, now)
    if until is None:
        return False
    ok = reschedule_locked_without_retry(job.pk, worker_id, until, schedule_defer_reason=reason)
    if ok:
        logger.info(
            "campaign_job_deferred_rate_limit job_id=%s chip_id=%s until=%s",
            job.pk,
            job.chip_id,
            until.isoformat(),
        )
        return True

    # Fallback: reschedule failed (race with reclaim or another worker).
    # Apply a minimum delay so the job is not reclaimed immediately
    # and caught in a defer → claim → defer busy loop.
    fallback_until = now + timedelta(seconds=_DEFER_FALLBACK_MIN_SECONDS)
    fallback_ok = reschedule_locked_without_retry(
        job.pk, worker_id, fallback_until, schedule_defer_reason=_DEFER_RACE
    )
    if fallback_ok:
        logger.warning(
            "campaign_job_defer_fallback job_id=%s — rescheduled with minimum delay %ss until %s",
            job.pk,
            _DEFER_FALLBACK_MIN_SECONDS,
            fallback_until.isoformat(),
        )
    else:
        logger.error(
            "campaign_job_defer_failed_stale_lock job_id=%s worker=%s — lock already lost, "
            "job will be picked up by the reclaim",
            job.pk,
            worker_id[:16],
        )
    return True
