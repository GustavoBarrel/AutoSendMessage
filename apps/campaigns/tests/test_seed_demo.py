"""seed_demo: expected data, idempotency, DEBUG guard and safe (non-real) phone numbers."""

from __future__ import annotations

from datetime import date, timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Workspace
from apps.campaigns.models import Campaign, CampaignChip, CampaignSendAttempt, CampaignSendJob
from apps.campaigns.services.campaign_audience import contacts_queryset_for_campaign
from apps.campaigns.services.demo_seed import _generate_contact_rows, demo_phone, seed_demo
from apps.campaigns.services.demo_seed_data import BIRTHDAY_TODAY_COUNT, CONTACT_COUNT
from apps.chips.models import Chip
from apps.contacts.models import Contact, ManualTag
from apps.dashboards.models import StatsDashboard


def run_seed(*args: str) -> str:
    out = StringIO()
    call_command("seed_demo", *args, stdout=out)
    return out.getvalue()


def snapshot() -> dict[str, int]:
    return {
        "workspaces": Workspace.objects.count(),
        "chips": Chip.objects.count(),
        "tags": ManualTag.objects.count(),
        "contacts": Contact.objects.count(),
        "campaigns": Campaign.objects.count(),
        "campaign_chips": CampaignChip.objects.count(),
        "variants": sum(c.message_variants.count() for c in Campaign.objects.all()),
        "jobs": CampaignSendJob.objects.count(),
        "attempts": CampaignSendAttempt.objects.count(),
        "dashboards": StatsDashboard.objects.count(),
    }


@override_settings(DEBUG=True)
class SeedDemoDataTests(TestCase):
    def test_creates_expected_data(self):
        run_seed()

        self.assertEqual(Workspace.objects.count(), 2)
        self.assertEqual(Chip.objects.count(), 4)
        self.assertFalse(Chip.objects.exclude(provider=Chip.Provider.SIMULATED).exists())
        self.assertEqual(Contact.objects.count(), CONTACT_COUNT)
        self.assertEqual(
            set(Contact.objects.values_list("category", flat=True)),
            {"Cliente", "Lead", "Fornecedor", "Parceiro"},
        )
        self.assertGreater(
            Contact.objects.values("state").distinct().count(), 3, "expected several states"
        )
        self.assertTrue(Contact.objects.filter(manual_tags__isnull=False).exists())
        self.assertEqual(ManualTag.objects.count(), 4)
        self.assertEqual(StatsDashboard.objects.count(), 2)

    def test_each_workspace_has_draft_active_and_finished_campaigns(self):
        run_seed()

        for workspace in Workspace.objects.all():
            statuses = set(workspace.campaigns.values_list("status", flat=True))
            self.assertEqual(
                statuses,
                {Campaign.Status.DRAFT, Campaign.Status.ACTIVE, Campaign.Status.FINISHED},
            )
        for campaign in Campaign.objects.all():
            self.assertEqual(campaign.message_variants.count(), 3)
            self.assertTrue(campaign.campaign_chips.exists())
            self.assertEqual(
                set(campaign.campaign_chips.values_list("campaign_status", flat=True)),
                {campaign.status},
            )

    def test_send_history_has_sent_and_failed_jobs_in_the_recent_past(self):
        run_seed()
        now = timezone.now()

        draft_jobs = CampaignSendJob.objects.filter(campaign__status=Campaign.Status.DRAFT)
        self.assertFalse(draft_jobs.exists())
        finished = CampaignSendJob.objects.filter(campaign__status=Campaign.Status.FINISHED)
        self.assertFalse(finished.exclude(status__in=["sent", "failed"]).exists())
        active = CampaignSendJob.objects.filter(campaign__status=Campaign.Status.ACTIVE)
        self.assertTrue(active.filter(status=CampaignSendJob.Status.PENDING).exists())
        self.assertTrue(
            CampaignSendJob.objects.filter(status=CampaignSendJob.Status.FAILED).exists()
        )

        sent = CampaignSendJob.objects.filter(status=CampaignSendJob.Status.SENT)
        self.assertTrue(sent.exists())
        for job in sent:
            self.assertIsNotNone(job.sent_at)
            self.assertLessEqual(job.sent_at, now)
            self.assertGreaterEqual(job.sent_at, now - timedelta(days=8))
            self.assertTrue(job.external_message_id.startswith("sim-"))
            self.assertLessEqual(job.created_at, job.sent_at)
        for job in CampaignSendJob.objects.filter(status=CampaignSendJob.Status.FAILED):
            self.assertIsNone(job.sent_at)
            self.assertTrue(job.last_error)
            self.assertEqual(job.attempts_log.count(), job.attempts)

    def test_jobs_respect_the_campaign_audience_and_workspace(self):
        run_seed()

        for campaign in Campaign.objects.exclude(status=Campaign.Status.DRAFT):
            audience = set(contacts_queryset_for_campaign(campaign).values_list("pk", flat=True))
            queued = set(campaign.send_jobs.values_list("contact_id", flat=True))
            self.assertEqual(queued, audience)
            chip_workspaces = set(campaign.send_jobs.values_list("chip__workspace_id", flat=True))
            self.assertEqual(chip_workspaces, {campaign.workspace_id})

    def test_every_mobile_phone_uses_the_nonexistent_area_code_00(self):
        run_seed()

        phones = list(Contact.objects.values_list("mobile_phone", flat=True))
        self.assertEqual(len(phones), CONTACT_COUNT)
        for phone in phones:
            self.assertRegex(phone, r"^00\d{9}$")
        self.assertEqual(len(set(phones)), len(phones))

    def test_birthdays_today_feed_the_birthday_filter(self):
        run_seed()
        today = timezone.localdate()

        celebrating = Contact.objects.filter(
            birth_date__month=today.month, birth_date__day=today.day
        )
        self.assertEqual(celebrating.count(), BIRTHDAY_TODAY_COUNT)
        self.assertFalse(celebrating.filter(has_whatsapp=False).exists())
        campaign = Campaign.objects.get(audience=Campaign.Audience.BIRTHDAY_TODAY)
        self.assertEqual(contacts_queryset_for_campaign(campaign).count(), BIRTHDAY_TODAY_COUNT)

    def test_birthdays_follow_the_day_the_seed_is_run_again(self):
        seed_demo(today=date(2024, 3, 10))
        seed_demo(today=date(2024, 2, 29))  # leap day must not break the generation

        self.assertEqual(Contact.objects.count(), CONTACT_COUNT)
        leap_day = Contact.objects.filter(birth_date__month=2, birth_date__day=29)
        self.assertEqual(leap_day.count(), BIRTHDAY_TODAY_COUNT)
        self.assertFalse(Contact.objects.filter(birth_date__month=3, birth_date__day=10).exists())

    def test_does_not_create_users(self):
        run_seed()

        self.assertEqual(get_user_model().objects.count(), 0)

    def test_generation_is_deterministic(self):
        today = date(2025, 5, 20)

        self.assertEqual(_generate_contact_rows(today), _generate_contact_rows(today))
        self.assertEqual(demo_phone(0), "00900000001")
        self.assertEqual(demo_phone(CONTACT_COUNT - 1), f"009{CONTACT_COUNT:08d}")


@override_settings(DEBUG=True)
class SeedDemoIdempotencyTests(TestCase):
    def test_second_run_creates_nothing_and_changes_no_count(self):
        run_seed()
        before = snapshot()
        job_keys = set(CampaignSendJob.objects.values_list("idempotency_key", flat=True))

        output = run_seed()

        self.assertEqual(snapshot(), before)
        self.assertEqual(
            set(CampaignSendJob.objects.values_list("idempotency_key", flat=True)), job_keys
        )
        self.assertIn("0 contacts", output)
        self.assertIn("0 send jobs", output)

    def test_recreates_a_deleted_contact_without_duplicating_the_others(self):
        run_seed()
        before = snapshot()
        Contact.objects.filter(code="DEMO-001").delete()

        run_seed()

        after = snapshot()
        self.assertEqual(after["contacts"], before["contacts"])
        # The send history is only built while a campaign has no jobs, so it is not redone.
        self.assertLessEqual(after["jobs"], before["jobs"])

    def test_does_not_overwrite_edits_made_to_demo_contacts(self):
        run_seed()
        Contact.objects.filter(code="DEMO-002").update(name="Nome editado")

        run_seed()

        self.assertTrue(Contact.objects.filter(code="DEMO-002", name="Nome editado").exists())


class SeedDemoDebugGuardTests(TestCase):
    @override_settings(DEBUG=False)
    def test_refuses_to_run_with_debug_off(self):
        with self.assertRaisesMessage(CommandError, "--force"):
            run_seed()

        self.assertEqual(Contact.objects.count(), 0)
        self.assertEqual(Workspace.objects.count(), 0)

    @override_settings(DEBUG=False)
    def test_force_overrides_the_guard(self):
        run_seed("--force")

        self.assertEqual(Contact.objects.count(), CONTACT_COUNT)

    @override_settings(DEBUG=True)
    def test_runs_without_force_when_debug_is_on(self):
        run_seed()

        self.assertEqual(Contact.objects.count(), CONTACT_COUNT)
