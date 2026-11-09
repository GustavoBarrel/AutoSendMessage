"""Simulated provider: no network access anywhere and a campaign that runs to the end."""

from __future__ import annotations

from datetime import time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Workspace
from apps.campaigns.models import (
    Campaign,
    CampaignChip,
    CampaignMessageVariant,
    CampaignSendAttempt,
    CampaignSendJob,
)
from apps.campaigns.services.campaign_chip_offline import try_resume_chips_with_waiting_jobs
from apps.campaigns.services.campaign_enqueue import enqueue_delivery_jobs
from apps.campaigns.services.campaign_lifecycle import auto_finish_if_complete
from apps.campaigns.services.campaign_provider_dispatch import dispatch_campaign_message
from apps.campaigns.services.campaign_send_execution import claim_and_process_one
from apps.chips.forms import ChipForm
from apps.chips.models import Chip
from apps.chips.providers import ProviderDisabledError, get_provider_for_chip
from apps.chips.providers.evolution import EvolutionProvider
from apps.chips.providers.meta import MetaProvider
from apps.chips.providers.simulated import SimulatedProvider
from apps.contacts.models import Contact

# Every call of the requests library (post/get/delete/...) ends up here.
HTTP_PATCH_TARGET = "requests.sessions.Session.request"


class SimulatedProviderTestMixin:
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="sim-user", password="pass")
        self.workspace = Workspace.objects.create(name="Perfil Sim", slug="perfil-sim")
        self.chip = Chip.objects.create(
            workspace=self.workspace,
            name="Chip Simulado",
            provider=Chip.Provider.SIMULATED,
            instance_name="chip-simulado",
            send_window_start=time(0, 0),
            send_window_end=time(23, 59, 59),
        )
        self.contact = Contact.objects.create(
            name="Maria Silva", mobile_phone="00999990000", has_whatsapp=True
        )

    def make_campaign(self, status: str = Campaign.Status.ACTIVE) -> Campaign:
        campaign = Campaign.objects.create(
            workspace=self.workspace,
            name="Campanha Simulada",
            audience=Campaign.Audience.ALL,
            audience_config={},
            status=status,
        )
        CampaignChip.objects.create(
            campaign=campaign, chip=self.chip, is_active=True, campaign_status=campaign.status
        )
        types = [
            (CampaignMessageVariant.MessageType.TEXT, ""),
            (CampaignMessageVariant.MessageType.IMAGE, "https://example.com/a.jpg"),
            (CampaignMessageVariant.MessageType.TEXT, ""),
        ]
        for position, (message_type, media_url) in enumerate(types, start=1):
            CampaignMessageVariant.objects.create(
                campaign=campaign,
                position=position,
                message_type=message_type,
                text=f"Olá {{primeiro_nome}}, variação {position}.",
                media_url=media_url,
            )
        return campaign

    def login_with_workspace(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["workspace_id"] = self.workspace.pk
        session.save()


@override_settings(SIMULATED_PROVIDER_ENABLED=True)
class SimulatedProviderUnitTests(SimulatedProviderTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.campaign = self.make_campaign()
        self.variants = list(self.campaign.message_variants.order_by("position"))
        self.job = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=self.contact,
            chip=self.chip,
            chip_name_snapshot=self.chip.name,
        )
        self.provider = SimulatedProvider(self.chip)

    def test_send_text_succeeds_without_network(self):
        with patch(HTTP_PATCH_TARGET) as http:
            result = self.provider.send_text(self.job, self.variants[0], self.contact)
        http.assert_not_called()
        self.assertTrue(result.ok)
        self.assertTrue(result.external_id.startswith("sim-"))
        self.assertEqual(result.payload["simulated"], True)
        self.assertEqual(result.payload["to"], "00999990000")

    def test_external_ids_are_unique(self):
        first = self.provider.send_text(self.job, self.variants[0], self.contact)
        second = self.provider.send_text(self.job, self.variants[0], self.contact)
        self.assertNotEqual(first.external_id, second.external_id)

    def test_send_media_succeeds_without_network(self):
        with patch(HTTP_PATCH_TARGET) as http:
            result = self.provider.send_media(
                self.job, self.variants[1], self.contact, mediatype="image", caption="legenda"
            )
        http.assert_not_called()
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["type"], "image")

    def test_send_media_without_url_fails(self):
        result = self.provider.send_media(
            self.job, self.variants[0], self.contact, mediatype="image"
        )
        self.assertFalse(result.ok)
        self.assertIn("URL", result.error_message)

    def test_send_text_with_blank_text_fails(self):
        self.variants[0].text = "   "
        result = self.provider.send_text(self.job, self.variants[0], self.contact)
        self.assertFalse(result.ok)

    def test_connection_state_is_always_open_without_network(self):
        with patch(HTTP_PATCH_TARGET) as http:
            self.assertEqual(self.provider.get_connection_state(), "open")
        http.assert_not_called()

    def test_dispatch_goes_through_the_simulated_provider(self):
        with patch(HTTP_PATCH_TARGET) as http:
            ok, error, external_id, payload, http_status, error_kind = dispatch_campaign_message(
                self.job, self.variants[0]
            )
        http.assert_not_called()
        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertTrue(external_id.startswith("sim-"))
        self.assertTrue(payload["simulated"])
        self.assertEqual(http_status, 200)
        self.assertIsNone(error_kind)


class ProviderFactoryTests(SimulatedProviderTestMixin, TestCase):
    @override_settings(SIMULATED_PROVIDER_ENABLED=True)
    def test_factory_returns_simulated_provider_when_enabled(self):
        self.assertIsInstance(get_provider_for_chip(self.chip), SimulatedProvider)

    @override_settings(SIMULATED_PROVIDER_ENABLED=False)
    def test_factory_refuses_simulated_provider_when_disabled(self):
        with self.assertRaises(ProviderDisabledError) as ctx:
            get_provider_for_chip(self.chip)
        self.assertIn("Provedor simulado desativado neste ambiente", str(ctx.exception))

    def test_factory_still_returns_other_providers(self):
        self.chip.provider = Chip.Provider.EVOLUTION
        self.assertIsInstance(get_provider_for_chip(self.chip), EvolutionProvider)
        self.chip.provider = Chip.Provider.META
        self.assertIsInstance(get_provider_for_chip(self.chip), MetaProvider)

    def test_factory_rejects_unknown_provider(self):
        self.chip.provider = "telegram"
        with self.assertRaises(ValueError):
            get_provider_for_chip(self.chip)

    def test_uses_evolution_only_for_evolution_or_blank_provider(self):
        self.assertFalse(self.chip.uses_evolution)
        self.chip.provider = Chip.Provider.META
        self.assertFalse(self.chip.uses_evolution)
        self.chip.provider = Chip.Provider.EVOLUTION
        self.assertTrue(self.chip.uses_evolution)
        self.chip.provider = ""
        self.assertTrue(self.chip.uses_evolution)


class ChipFormSimulatedTests(SimulatedProviderTestMixin, TestCase):
    def _post_data(self, **overrides) -> dict:
        data = {
            "name": "Novo chip",
            "provider": Chip.Provider.SIMULATED,
            "instance_name": "novo-chip",
            "account_type": "normal",
            "max_messages_per_hour": 10,
            "max_messages_per_day": 75,
            "send_window_start": "08:00",
            "send_window_end": "18:00",
            "delay_between_messages_seconds": 360,
            "meta_api_version": "v18.0",
        }
        data.update(overrides)
        return data

    @staticmethod
    def _provider_values(form: ChipForm) -> list[str]:
        return [value for value, _label in form.fields["provider"].choices]

    @override_settings(SIMULATED_PROVIDER_ENABLED=True)
    def test_option_is_offered_when_setting_is_on(self):
        self.assertIn(Chip.Provider.SIMULATED, self._provider_values(ChipForm()))

    @override_settings(SIMULATED_PROVIDER_ENABLED=False)
    def test_option_is_hidden_when_setting_is_off(self):
        values = self._provider_values(ChipForm())
        self.assertNotIn(Chip.Provider.SIMULATED, values)
        self.assertIn(Chip.Provider.EVOLUTION, values)
        self.assertIn(Chip.Provider.META, values)

    @override_settings(SIMULATED_PROVIDER_ENABLED=False)
    def test_simulated_value_is_rejected_when_setting_is_off(self):
        form = ChipForm(self._post_data())
        self.assertFalse(form.is_valid())
        self.assertIn("provider", form.errors)

    @override_settings(SIMULATED_PROVIDER_ENABLED=True)
    def test_simulated_value_is_accepted_when_setting_is_on(self):
        form = ChipForm(self._post_data())
        self.assertTrue(form.is_valid(), form.errors)

    @override_settings(SIMULATED_PROVIDER_ENABLED=False)
    def test_existing_simulated_chip_keeps_its_option_when_setting_is_off(self):
        form = ChipForm(instance=self.chip)
        self.assertIn(Chip.Provider.SIMULATED, self._provider_values(form))

    @override_settings(SIMULATED_PROVIDER_ENABLED=True)
    def test_simulated_chip_still_requires_instance_name(self):
        form = ChipForm(self._post_data(instance_name=""))
        self.assertFalse(form.is_valid())
        self.assertIn("instance_name", form.errors)


@override_settings(SIMULATED_PROVIDER_ENABLED=True, EVOLUTION_URL_BASE="", EVOLUTION_API_KEY="")
class SimulatedChipViewsTests(SimulatedProviderTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.login_with_workspace()

    def test_create_view_makes_no_http_call_and_saves_chip(self):
        data = {
            "name": "Chip via formulário",
            "provider": Chip.Provider.SIMULATED,
            "instance_name": "chip-form",
            "account_type": "normal",
            "max_messages_per_hour": 10,
            "max_messages_per_day": 75,
            "send_window_start": "08:00",
            "send_window_end": "18:00",
            "delay_between_messages_seconds": 360,
            "meta_api_version": "v18.0",
        }
        with patch(HTTP_PATCH_TARGET) as http:
            response = self.client.post(reverse("chips:chip_create"), data)
        http.assert_not_called()
        self.assertEqual(response.status_code, 302)
        created = Chip.objects.get(instance_name="chip-form")
        self.assertEqual(created.provider, Chip.Provider.SIMULATED)
        self.assertEqual(created.workspace, self.workspace)

    def test_home_shows_chip_as_open_without_http_call(self):
        with patch(HTTP_PATCH_TARGET) as http:
            response = self.client.get(reverse("dashboards:home"))
        http.assert_not_called()
        self.assertEqual(response.status_code, 200)
        states = {
            i["chip"].name: i["connection_state"] for i in response.context["chips_with_state"]
        }
        self.assertEqual(states["Chip Simulado"], "open")
        self.assertIsNone(response.context["evolution_warning"])
        self.assertEqual(response.context["connected_chips"], 1)

    def test_connect_view_redirects_without_http_call(self):
        with patch(HTTP_PATCH_TARGET) as http:
            response = self.client.get(reverse("chips:chip_connect", args=[self.chip.pk]))
        http.assert_not_called()
        self.assertRedirects(response, reverse("dashboards:home"), fetch_redirect_response=False)

    def test_connect_json_endpoints_report_open_without_http_call(self):
        with patch(HTTP_PATCH_TARGET) as http:
            status = self.client.get(reverse("chips:chip_connect_status", args=[self.chip.pk]))
            qr = self.client.get(reverse("chips:chip_connect_json", args=[self.chip.pk]))
        http.assert_not_called()
        self.assertEqual(status.json()["state"], "open")
        self.assertEqual(qr.json()["state"], "open")

    def test_delete_view_makes_no_http_call(self):
        with patch(HTTP_PATCH_TARGET) as http:
            response = self.client.post(reverse("chips:chip_delete", args=[self.chip.pk]))
        http.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Chip.objects.filter(pk=self.chip.pk).exists())

    def test_campaign_wizard_lists_chip_as_selectable_without_http_call(self):
        campaign = self.make_campaign(status=Campaign.Status.DRAFT)
        with patch(HTTP_PATCH_TARGET) as http:
            response = self.client.get(
                reverse("campaigns:campaign_wizard", args=[campaign.pk, "chips"])
            )
        http.assert_not_called()
        self.assertEqual(response.status_code, 200)
        row = response.context["chips_with_state"][0]
        self.assertEqual(row["state"], "open")
        self.assertTrue(row["is_connected"])

    def test_campaign_detail_makes_no_http_call(self):
        campaign = self.make_campaign()
        with patch(HTTP_PATCH_TARGET) as http:
            response = self.client.get(reverse("campaigns:campaign_detail", args=[campaign.pk]))
        http.assert_not_called()
        self.assertEqual(response.status_code, 200)


@override_settings(SIMULATED_PROVIDER_ENABLED=True, EVOLUTION_URL_BASE="", EVOLUTION_API_KEY="")
class SimulatedCampaignFlowTests(SimulatedProviderTestMixin, TestCase):
    worker_id = "worker-simulated"

    def test_resume_reactivates_waiting_jobs_without_http_or_evolution_settings(self):
        campaign = self.make_campaign()
        job = CampaignSendJob.objects.create(
            campaign=campaign,
            contact=self.contact,
            chip=self.chip,
            chip_name_snapshot=self.chip.name,
            status=CampaignSendJob.Status.WAITING_CHIP,
        )
        with patch(HTTP_PATCH_TARGET) as http:
            resumed = try_resume_chips_with_waiting_jobs()
        http.assert_not_called()
        self.assertEqual(resumed, 1)
        job.refresh_from_db()
        self.assertEqual(job.status, CampaignSendJob.Status.PENDING)

    def test_resume_ignores_evolution_chips_when_evolution_is_not_configured(self):
        evolution_chip = Chip.objects.create(
            workspace=self.workspace, name="Evo", instance_name="evo", provider="evolution"
        )
        campaign = self.make_campaign()
        job = CampaignSendJob.objects.create(
            campaign=campaign,
            contact=self.contact,
            chip=evolution_chip,
            chip_name_snapshot=evolution_chip.name,
            status=CampaignSendJob.Status.WAITING_CHIP,
        )
        with patch(HTTP_PATCH_TARGET) as http:
            self.assertEqual(try_resume_chips_with_waiting_jobs(), 0)
        http.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, CampaignSendJob.Status.WAITING_CHIP)

    def test_campaign_runs_to_the_end_with_simulated_chip(self):
        campaign = self.make_campaign()
        contacts = [self.contact] + [
            Contact.objects.create(
                name=f"Contato {i}", mobile_phone=f"8299999{i:04d}", has_whatsapp=True
            )
            for i in range(1, 3)
        ]
        result = enqueue_delivery_jobs(campaign.pk, [c.pk for c in contacts])
        self.assertEqual(result.created, 3)

        with patch(HTTP_PATCH_TARGET) as http:
            for job in CampaignSendJob.objects.filter(campaign=campaign).order_by("pk"):
                # Next slot is gated by the chip's minimum interval; pretend it already passed.
                with patch(
                    "apps.campaigns.services.campaign_send_execution.defer_if_chip_rate_limited",
                    return_value=False,
                ):
                    outcome = claim_and_process_one(job.pk, self.worker_id, lease_seconds=120)
                self.assertEqual(outcome, "sent")
        http.assert_not_called()

        jobs = CampaignSendJob.objects.filter(campaign=campaign)
        self.assertEqual(set(jobs.values_list("status", flat=True)), {CampaignSendJob.Status.SENT})
        for job in jobs:
            self.assertTrue(job.external_message_id.startswith("sim-"))
        self.assertEqual(CampaignSendAttempt.objects.filter(success=True).count(), 3)

        self.assertTrue(auto_finish_if_complete(campaign.pk))
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.FINISHED)

    def test_real_rate_limit_window_does_not_defer_the_first_simulated_send(self):
        campaign = self.make_campaign()
        enqueue_delivery_jobs(campaign.pk, [self.contact.pk])
        job = CampaignSendJob.objects.get(campaign=campaign)
        job.scheduled_at = timezone.now() - timedelta(seconds=1)
        job.save(update_fields=["scheduled_at"])
        with patch(HTTP_PATCH_TARGET) as http:
            outcome = claim_and_process_one(job.pk, self.worker_id, lease_seconds=120)
        http.assert_not_called()
        self.assertEqual(outcome, "sent")


@override_settings(SIMULATED_PROVIDER_ENABLED=False, EVOLUTION_URL_BASE="", EVOLUTION_API_KEY="")
class SimulatedProviderDisabledFlowTests(SimulatedProviderTestMixin, TestCase):
    """A leftover simulated chip must not pretend to send in an environment that turned it off."""

    worker_id = "worker-simulated-off"

    def _pending_job(self) -> CampaignSendJob:
        campaign = self.make_campaign()
        enqueue_delivery_jobs(campaign.pk, [self.contact.pk])
        job = CampaignSendJob.objects.get(campaign=campaign)
        job.scheduled_at = timezone.now() - timedelta(seconds=1)
        job.save(update_fields=["scheduled_at"])
        return job

    def test_job_fails_with_a_clear_message_and_is_never_marked_sent(self):
        job = self._pending_job()

        with patch(HTTP_PATCH_TARGET) as http:
            outcome = claim_and_process_one(job.pk, self.worker_id, lease_seconds=120)

        http.assert_not_called()
        self.assertEqual(outcome, "failed")
        job.refresh_from_db()
        self.assertEqual(job.status, CampaignSendJob.Status.FAILED)
        self.assertEqual(job.external_message_id, "")
        self.assertIn("Provedor simulado desativado neste ambiente", job.last_error)
        self.assertFalse(CampaignSendAttempt.objects.filter(success=True).exists())

    def test_worker_keeps_processing_other_jobs_after_the_failure(self):
        simulated_job = self._pending_job()
        other_contact = Contact.objects.create(
            name="Outra Pessoa", mobile_phone="00999990001", has_whatsapp=True
        )
        evolution_chip = Chip.objects.create(
            workspace=self.workspace,
            name="Evo",
            instance_name="evo-ok",
            provider=Chip.Provider.EVOLUTION,
        )
        other_job = CampaignSendJob.objects.create(
            campaign=simulated_job.campaign,
            contact=other_contact,
            chip=evolution_chip,
            chip_name_snapshot=evolution_chip.name,
            scheduled_at=timezone.now() - timedelta(seconds=1),
            idempotency_key="disabled-flow-other",
        )

        def fake_dispatch(job, variant):
            return True, "", "ext-1", {}, 200, None

        claim_and_process_one(simulated_job.pk, self.worker_id, lease_seconds=120)
        with patch(
            "apps.campaigns.services.campaign_send_execution.defer_if_chip_rate_limited",
            return_value=False,
        ):
            outcome = claim_and_process_one(
                other_job.pk, self.worker_id, lease_seconds=120, dispatch=fake_dispatch
            )

        self.assertEqual(outcome, "sent")

    def test_resume_requeues_waiting_jobs_so_they_fail_clearly_instead_of_waiting_forever(self):
        job = self._pending_job()
        CampaignSendJob.objects.filter(pk=job.pk).update(status=CampaignSendJob.Status.WAITING_CHIP)

        self.assertEqual(try_resume_chips_with_waiting_jobs(), 1)

        job.refresh_from_db()
        self.assertEqual(job.status, CampaignSendJob.Status.PENDING)
