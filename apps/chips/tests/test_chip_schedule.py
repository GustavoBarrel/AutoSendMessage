from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Workspace
from apps.campaigns.models import Campaign, CampaignSendJob
from apps.campaigns.services.campaign_chip_schedule import clear_future_schedules_for_chip
from apps.chips.models import Chip


class ClearChipScheduleTests(TestCase):
    def setUp(self):
        from apps.campaigns.models import CampaignMessageVariant
        from apps.contacts.models import Contact

        self.workspace = Workspace.objects.create(name="Pc", slug="p-clear")
        self.chip = Chip.objects.create(
            workspace=self.workspace, name="C1", instance_name="i-clear"
        )
        self.campaign = Campaign.objects.create(
            workspace=self.workspace,
            name="Camp",
            audience=Campaign.Audience.ALL,
            audience_config={},
            status=Campaign.Status.ACTIVE,
        )
        for position in (1, 2, 3):
            CampaignMessageVariant.objects.create(
                campaign=self.campaign,
                position=position,
                message_type=CampaignMessageVariant.MessageType.TEXT,
                text=f"M{position}",
            )
        self.contact = Contact.objects.create(name="Z", mobile_phone="11988776655")
        self.future = timezone.now() + timedelta(hours=1)
        self.job = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=self.contact,
            chip=self.chip,
            chip_name_snapshot=self.chip.name,
            status=CampaignSendJob.Status.PENDING,
            scheduled_at=self.future,
            idempotency_key=f"c{self.campaign.pk}-ct{self.contact.pk}-x",
        )

    def test_clear_removes_future_scheduled_at(self):
        n = clear_future_schedules_for_chip(self.chip.pk)
        self.assertEqual(n, 1)
        self.job.refresh_from_db()
        self.assertIsNone(self.job.scheduled_at)


class ClearChipScheduleRedirectTests(TestCase):
    """The ``next`` field of the clear-schedule POST must never leave this site."""

    def setUp(self):
        user = get_user_model().objects.create_user(username="redir", password="senha-teste")
        self.workspace = Workspace.objects.create(name="Redir", slug="redir")
        self.chip = Chip.objects.create(workspace=self.workspace, name="C", instance_name="i-redir")
        self.client.force_login(user)
        session = self.client.session
        session["workspace_id"] = self.workspace.pk
        session.save()
        self.url = reverse("chips:chip_clear_pending_schedule", args=[self.chip.pk])
        self.home = reverse("dashboards:home")

    def test_local_next_is_followed(self):
        target = reverse("campaigns:campaign_list")
        response = self.client.post(self.url, {"next": target})
        self.assertRedirects(response, target, fetch_redirect_response=False)

    def test_external_next_values_fall_back_to_home(self):
        for bad in ("//evil.com", "/\\evil.com", "http://evil.com", "https://evil.com/x", ""):
            with self.subTest(next=bad):
                response = self.client.post(self.url, {"next": bad})
                self.assertRedirects(response, self.home, fetch_redirect_response=False)
