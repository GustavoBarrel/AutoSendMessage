"""Fictional demo data: workspaces, contacts, simulated chips, campaigns and a send history.

Used by ``manage.py seed_demo``. Generation is deterministic (fixed seed) and idempotent:
rows are looked up by a natural key, and the history of a campaign is only built while the
campaign has no send jobs yet.

Every mobile phone starts with area code 00, which does not exist in Brazil, and all chips use
the simulated provider, so nothing created here can reach a real person.
"""

from __future__ import annotations

import random
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Workspace
from apps.campaigns.models import (
    Campaign,
    CampaignChip,
    CampaignMessageVariant,
    CampaignSendAttempt,
    CampaignSendJob,
)
from apps.campaigns.services.campaign_audience import (
    contacts_queryset_for_campaign,
    default_audience_config,
)
from apps.campaigns.services.demo_seed_data import (
    BIRTHDAY_TODAY_COUNT,
    CATEGORIES,
    CITIES,
    CONTACT_COUNT,
    DASHBOARD_NAME,
    DASHBOARD_WIDGETS,
    DEMO_AREA_CODE,
    FAILURE_NOT_ON_WHATSAPP,
    FAILURE_TIMEOUT,
    FEMALE_FIRST_NAMES,
    LAST_NAMES,
    MALE_FIRST_NAMES,
    MANUAL_TAGS,
    NEIGHBORHOODS,
    SEED,
    WORKSPACES,
    CampaignSpec,
    ChipSpec,
    WorkspaceSpec,
)
from apps.chips.models import Chip
from apps.contacts.models import Contact, ManualTag
from apps.dashboards.models import StatsDashboard, StatsDashboardWidget

# Business hours (local time) used for the generated sends; matches the chips' default window.
_SEND_START_SECONDS = 8 * 3600
_SEND_END_SECONDS = 18 * 3600
# Margin before "now" so a generated send is never in the future.
_NOW_MARGIN = timedelta(minutes=5)
_RETRY_SUCCESS_RATE = 0.1
_MAX_ATTEMPTS = 3


@dataclass
class SeedSummary:
    """How many rows each step created (zero on a repeated run)."""

    workspaces: int = 0
    chips: int = 0
    tags: int = 0
    contacts: int = 0
    campaigns: int = 0
    send_jobs: int = 0
    dashboards: int = 0
    birthday_contacts_today: int = 0


def demo_phone(index: int) -> str:
    """Phone number of the Nth demo contact: area code 00 + 9 + 8 digits (11 digits)."""
    return f"{DEMO_AREA_CODE}9{index + 1:08d}"


def seed_demo(*, today: date | None = None, now: datetime | None = None) -> SeedSummary:
    """Create (or complete) the demo data in a single transaction."""
    now = now or timezone.now()
    today = today or timezone.localdate(now)
    summary = SeedSummary()
    with transaction.atomic():
        tags = _seed_tags(summary)
        _seed_contacts(summary, tags, today)
        for spec in WORKSPACES:
            _seed_workspace(summary, spec, now)
    return summary


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_text.lower())


def _local_datetime(day: date, seconds: int) -> datetime:
    naive = datetime.combine(day, time()) + timedelta(seconds=seconds)
    return timezone.make_aware(naive)


def _birth_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:  # Feb 29 on a non-leap year
        return date(2000, month, day)


# ---------------------------------------------------------------------------
# Tags and contacts
# ---------------------------------------------------------------------------


def _seed_tags(summary: SeedSummary) -> list[ManualTag]:
    tags = []
    for name in MANUAL_TAGS:
        tag, created = ManualTag.objects.get_or_create(name=name)
        summary.tags += int(created)
        tags.append(tag)
    return tags


def _generate_contact_rows(today: date) -> list[dict[str, object]]:
    """Deterministic contact attributes, independent of what already exists in the database."""
    rng = random.Random(f"{SEED}-contacts")
    city_choices = [(city, state) for city, state, _ in CITIES]
    city_weights = [weight for _, _, weight in CITIES]
    category_weights = [weight for _, weight, _ in CATEGORIES]
    used_names: set[str] = set()
    rows: list[dict[str, object]] = []

    for index in range(CONTACT_COUNT):
        is_female = rng.random() < 0.5
        pool = FEMALE_FIRST_NAMES if is_female else MALE_FIRST_NAMES
        while True:
            first, last = rng.choice(pool), rng.choice(LAST_NAMES)
            if f"{first} {last}" not in used_names:
                break
        used_names.add(f"{first} {last}")

        city, state = rng.choices(city_choices, weights=city_weights)[0]
        category, _, subcategories = rng.choices(CATEGORIES, weights=category_weights)[0]
        has_whatsapp = rng.random() < 0.9
        tag_count = rng.choice((0, 0, 1, 1, 2))
        tag_picks = rng.sample(range(len(MANUAL_TAGS)), tag_count)
        # Birth date is always drawn (keeps the sequence stable); birthday contacts override it.
        year, month, day = rng.randint(1960, 2005), rng.randint(1, 12), rng.randint(1, 28)
        if (month, day) == (today.month, today.day):
            day = day + 1 if day < 28 else day - 1  # only the chosen contacts have a birthday today
        birth_date = _birth_date(year, month, day)
        is_birthday_today = index % 13 == 5 and index // 13 < BIRTHDAY_TODAY_COUNT
        if is_birthday_today:
            birth_date = _birth_date(year, today.month, today.day)
            has_whatsapp = True

        rows.append(
            {
                "mobile_phone": demo_phone(index),
                "is_birthday_today": is_birthday_today,
                "tag_indexes": tag_picks,
                "defaults": {
                    "code": f"DEMO-{index + 1:03d}",
                    "name": f"{first} {last}",
                    "gender": "Feminino" if is_female else "Masculino",
                    "neighborhood": rng.choice(NEIGHBORHOODS),
                    "city": city,
                    "state": state,
                    "has_whatsapp": has_whatsapp,
                    "birth_date": birth_date,
                    "email": f"{_slug(first)}.{_slug(last)}{index + 1}@example.com",
                    "category": category,
                    "subcategory": rng.choice(subcategories),
                },
            }
        )
    return rows


def _seed_contacts(summary: SeedSummary, tags: list[ManualTag], today: date) -> None:
    for row in _generate_contact_rows(today):
        contact, created = Contact.objects.get_or_create(
            mobile_phone=row["mobile_phone"],
            deleted_at__isnull=True,
            defaults=row["defaults"],
        )
        if created:
            summary.contacts += 1
            contact.manual_tags.set([tags[i] for i in row["tag_indexes"]])
        if row["is_birthday_today"]:
            # Keep "birthdays of the day" meaningful on whatever day the seed is run again.
            expected = row["defaults"]["birth_date"]
            if contact.birth_date != expected:
                contact.birth_date = expected
                contact.save(update_fields=["birth_date"])
            summary.birthday_contacts_today += 1


# ---------------------------------------------------------------------------
# Workspaces, chips and campaigns
# ---------------------------------------------------------------------------


def _seed_workspace(summary: SeedSummary, spec: WorkspaceSpec, now: datetime) -> None:
    workspace, created = Workspace.objects.get_or_create(
        slug=spec.slug, defaults={"name": spec.name, "color": spec.color}
    )
    summary.workspaces += int(created)
    chips = [_seed_chip(summary, workspace, chip_spec) for chip_spec in spec.chips]
    for campaign_spec in spec.campaigns:
        _seed_campaign(summary, workspace, chips, campaign_spec, now)
    _seed_dashboard(summary, workspace)


def _seed_chip(summary: SeedSummary, workspace: Workspace, spec: ChipSpec) -> Chip:
    chip, created = Chip.objects.get_or_create(
        workspace=workspace,
        instance_name=spec.instance_name,
        defaults={
            "name": spec.name,
            "provider": Chip.Provider.SIMULATED,
            "account_type": spec.account_type,
        },
    )
    summary.chips += int(created)
    return chip


def _seed_dashboard(summary: SeedSummary, workspace: Workspace) -> None:
    dashboard, created = StatsDashboard.objects.get_or_create(
        workspace=workspace, name=DASHBOARD_NAME, defaults={"is_default": True}
    )
    if not created:
        return
    summary.dashboards += 1
    StatsDashboardWidget.objects.bulk_create(
        StatsDashboardWidget(
            dashboard=dashboard,
            position=position,
            title=title,
            dataset=dataset,
            chart_type=chart_type,
            size=size,
        )
        for position, (title, dataset, chart_type, size) in enumerate(DASHBOARD_WIDGETS)
    )


def _seed_campaign(
    summary: SeedSummary,
    workspace: Workspace,
    chips: list[Chip],
    spec: CampaignSpec,
    now: datetime,
) -> None:
    campaign = Campaign.objects.filter(
        workspace=workspace, name=spec.name, deleted_at__isnull=True
    ).first()
    if campaign is None:
        campaign = Campaign.objects.create(
            workspace=workspace,
            name=spec.name,
            audience=spec.audience,
            audience_config={
                **default_audience_config(),
                "base": spec.audience,
                **spec.audience_filters,
            },
            status=spec.status,
        )
        summary.campaigns += 1

    for position, text in enumerate(spec.variants, start=1):
        CampaignMessageVariant.objects.get_or_create(
            campaign=campaign,
            position=position,
            defaults={"message_type": CampaignMessageVariant.MessageType.TEXT, "text": text},
        )
    for chip in chips:
        CampaignChip.objects.get_or_create(
            campaign=campaign, chip=chip, defaults={"campaign_status": campaign.status}
        )

    if spec.status == Campaign.Status.DRAFT or campaign.send_jobs.exists():
        return
    summary.send_jobs += _build_history(campaign, chips, spec, workspace.slug, now)


# ---------------------------------------------------------------------------
# Send history
# ---------------------------------------------------------------------------


def _send_moments(
    rng: random.Random, count: int, spec: CampaignSpec, now: datetime
) -> list[datetime]:
    """Sorted business-hours moments spread over the campaign's days, never after ``now``."""
    local_now = timezone.localtime(now)
    days = list(range(spec.oldest_day_ago, spec.newest_day_ago - 1, -1))
    slots: list[tuple[date, int, int]] = []  # (day, first second, last second)
    for days_ago in days:
        day = local_now.date() - timedelta(days=days_ago)
        last = _SEND_END_SECONDS
        if days_ago == 0:
            elapsed = local_now - _NOW_MARGIN - _local_datetime(day, 0)
            last = min(last, int(elapsed.total_seconds()))
        if last > _SEND_START_SECONDS:
            slots.append((day, _SEND_START_SECONDS, last))
    if not slots:  # run before business hours with only "today" configured
        day = local_now.date() - timedelta(days=1)
        slots.append((day, _SEND_START_SECONDS, _SEND_END_SECONDS))

    moments = []
    for _ in range(count):
        day, first, last = rng.choice(slots)
        moments.append(_local_datetime(day, rng.randrange(first, last)))
    return sorted(moments)


def _build_history(
    campaign: Campaign,
    chips: list[Chip],
    spec: CampaignSpec,
    rng_key: str,
    now: datetime,
) -> int:
    """Create the jobs of an active/finished campaign with attempts and past timestamps."""
    rng = random.Random(f"{SEED}-{rng_key}-{spec.name}")
    contact_ids = list(contacts_queryset_for_campaign(campaign).values_list("pk", flat=True))
    if not contact_ids:
        return 0

    processed = round(len(contact_ids) * spec.processed_ratio)
    moments = _send_moments(rng, processed, spec, now)
    variants = list(campaign.message_variants.order_by("position"))
    activated_at = (moments[0] - timedelta(hours=1)) if moments else now

    variant_turn: defaultdict[int, int] = defaultdict(int)
    jobs: list[CampaignSendJob] = []
    plan: dict[int, tuple[bool, datetime | None]] = {}  # contact_id -> (failed, moment)
    for index, contact_id in enumerate(contact_ids):
        chip = chips[index % len(chips)]
        variant = variants[variant_turn[chip.pk] % len(variants)]
        variant_turn[chip.pk] += 1
        moment = moments[index] if index < processed else None
        failed = moment is not None and rng.random() < spec.failure_rate
        plan[contact_id] = (failed, moment)
        jobs.append(
            CampaignSendJob(
                campaign=campaign,
                contact_id=contact_id,
                chip=chip,
                chip_name_snapshot=chip.name,
                variant=variant,
                status=_job_status(failed, moment),
                idempotency_key=f"c{campaign.pk}-ct{contact_id}",
            )
        )
    CampaignSendJob.objects.bulk_create(jobs, batch_size=500)

    # Timestamps are auto fields, so they are rewritten after the insert.
    saved = list(CampaignSendJob.objects.filter(campaign=campaign))
    attempts: list[CampaignSendAttempt] = []
    for job in saved:
        failed, moment = plan[job.contact_id]
        job.created_at = activated_at
        job.updated_at = moment or activated_at
        if moment is not None:
            attempts.extend(_fill_job_outcome(job, failed, moment, rng))
    CampaignSendJob.objects.bulk_update(
        saved,
        [
            "created_at",
            "updated_at",
            "sent_at",
            "attempts",
            "last_error",
            "external_message_id",
        ],
        batch_size=500,
    )
    # bulk_create overwrites the in-memory started_at (auto_now_add), so capture it first.
    intended_starts = {(a.job.pk, a.attempt_no): a.started_at for a in attempts}
    CampaignSendAttempt.objects.bulk_create(attempts, batch_size=500)
    _rewrite_attempt_starts(intended_starts)

    # update() on purpose: Campaign.save() would reset the auto timestamps.
    Campaign.objects.filter(pk=campaign.pk).update(
        created_at=activated_at - timedelta(days=1),
        updated_at=max(moments) if moments else activated_at,
        activated_at=activated_at,
    )
    return len(jobs)


def _job_status(failed: bool, moment: datetime | None) -> str:
    if moment is None:
        return CampaignSendJob.Status.PENDING
    return CampaignSendJob.Status.FAILED if failed else CampaignSendJob.Status.SENT


def _fill_job_outcome(
    job: CampaignSendJob, failed: bool, moment: datetime, rng: random.Random
) -> list[CampaignSendAttempt]:
    """Set the result fields of a processed job and return its attempt records."""
    if failed:
        error = rng.choice((FAILURE_NOT_ON_WHATSAPP, FAILURE_TIMEOUT))
        job.attempts = _MAX_ATTEMPTS
        job.last_error = error
        return [
            _attempt(job, number, moment, success=False, error=error)
            for number in range(1, _MAX_ATTEMPTS + 1)
        ]

    job.sent_at = moment
    job.external_message_id = f"sim-{rng.getrandbits(128):032x}"
    if rng.random() < _RETRY_SUCCESS_RATE:
        job.attempts = 2
        return [
            _attempt(job, 1, moment - timedelta(minutes=2), success=False, error=FAILURE_TIMEOUT),
            _attempt(job, 2, moment, success=True),
        ]
    job.attempts = 1
    return [_attempt(job, 1, moment, success=True)]


def _attempt(
    job: CampaignSendJob,
    number: int,
    finished_at: datetime,
    *,
    success: bool,
    error: str = "",
) -> CampaignSendAttempt:
    attempt = CampaignSendAttempt(
        job=job,
        attempt_no=number,
        finished_at=finished_at,
        success=success,
        error_text=error,
        http_status=200 if success else 400,
        provider_payload={"simulated": True, "type": "text"} if success else None,
    )
    # Overwritten by auto_now_add on insert; rewritten afterwards by _rewrite_attempt_starts.
    attempt.started_at = finished_at - timedelta(seconds=2)
    return attempt


def _rewrite_attempt_starts(starts: dict[tuple[int, int], datetime]) -> None:
    """Write the intended ``started_at`` back, since the field is auto_now_add."""
    if not starts:
        return
    stored = list(CampaignSendAttempt.objects.filter(job_id__in={job_pk for job_pk, _ in starts}))
    for attempt in stored:
        attempt.started_at = starts[(attempt.job_id, attempt.attempt_no)]
    CampaignSendAttempt.objects.bulk_update(stored, ["started_at"], batch_size=500)
