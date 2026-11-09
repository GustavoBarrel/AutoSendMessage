from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Workspace
from apps.campaigns.models import Campaign, CampaignChip, CampaignMessageVariant, CampaignSendJob
from apps.campaigns.services.campaign_audience import default_audience_config
from apps.campaigns.services.campaign_chip_offline import (
    ERROR_KIND_CHIP_OFFLINE,
    redistribute_waiting_jobs,
    suspend_jobs_for_chip_offline,
)
from apps.campaigns.services.campaign_evolution_dispatch import dispatch_campaign_message
from apps.campaigns.services.campaign_humanized_delay import calculate_humanized_delay_seconds
from apps.campaigns.services.campaign_lifecycle import (
    auto_finish_if_complete,
    pause_campaign,
    resume_campaign,
)
from apps.campaigns.services.campaign_rate_limit import (
    defer_if_chip_rate_limited,
    next_slot_after_last_send,
)
from apps.campaigns.services.campaign_retry import (
    RETRY_SCOPE_CAUSE_CHIP,
    RETRY_SCOPE_CAUSE_MESSAGE,
    RETRY_SCOPE_SELECTED,
    chip_eligible_for_campaign_retry,
    classify_failure_cause,
    retry_failed_jobs_with_chip,
    retry_failed_jobs_with_chips,
)
from apps.campaigns.services.campaign_send_execution import (
    claim_and_process_one,
    pick_variant_for_job,
    process_send_job_once,
)
from apps.campaigns.services.campaign_send_queue import fail_locked_permanent_contact_error
from apps.campaigns.services.message_utils import render_text
from apps.campaigns.services.message_variation_quality import (
    find_message_variation_issues,
    message_similarity,
    normalize_message_text,
)
from apps.chips.models import Chip
from apps.contacts.models import Contact


class ClassifyFailureTests(TestCase):
    def test_chip_connection_keywords(self):
        self.assertEqual(
            classify_failure_cause("Instância WhatsApp desconectada (connectionState=close)."),
            "chip_connection",
        )

    def test_api_config(self):
        self.assertEqual(
            classify_failure_cause("Evolution não configurada: defina EVOLUTION_URL_BASE."),
            "api_config",
        )

    def test_message_content(self):
        self.assertEqual(classify_failure_cause("Texto da mensagem vazio."), "message_content")

    def test_message_content_whatsapp_number(self):
        self.assertEqual(classify_failure_cause("Phone is not on whatsapp."), "number_not_exists")

    def test_chip_restriction_463(self):
        from apps.chips.services.evolution_whatsapp_errors import MSG_WHATSAPP_463

        self.assertEqual(classify_failure_cause(MSG_WHATSAPP_463), "chip_connection")


class CampaignMessagePlaceholderTests(TestCase):
    def test_render_text_keeps_name_compatibility(self):
        contact = Contact(name="Maria da Silva", mobile_phone="00999999999")

        self.assertEqual(render_text("Olá {nome}", contact), "Olá Maria da Silva")

    def test_render_text_replaces_contact_attributes(self):
        contact = Contact(
            name="Maria da Silva",
            mobile_phone="00999999999",
            city="São Paulo",
            neighborhood="Centro",
            state="SP",
            category="Cliente",
        )

        rendered = render_text(
            "Olá {primeiro_nome} {ultimo_nome}, cidade {cidade}, bairro {bairro}, {uf}, "
            "{categoria}, {celular}.",
            contact,
        )

        self.assertEqual(
            rendered,
            "Olá Maria Silva, cidade São Paulo, bairro Centro, SP, Cliente, 00999999999.",
        )


@override_settings(EVOLUTION_URL_BASE="https://evolution.test", EVOLUTION_API_KEY="secret")
class CampaignMessageDispatchCaptionTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Dispatch", slug="dispatch")
        self.campaign = Campaign.objects.create(
            workspace=self.workspace,
            name="Campanha Dispatch",
            audience=Campaign.Audience.ALL,
            audience_config=default_audience_config(),
            status=Campaign.Status.ACTIVE,
        )
        self.chip = Chip.objects.create(
            workspace=self.workspace,
            name="Chip Dispatch",
            instance_name="dispatch-instance",
        )
        self.contact = Contact.objects.create(name="Maria Silva", mobile_phone="00999999999")

    def _job_for_variant(self, variant: CampaignMessageVariant) -> CampaignSendJob:
        return CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=self.contact,
            chip=self.chip,
            chip_name_snapshot=self.chip.name,
            variant=variant,
            status=CampaignSendJob.Status.PENDING,
            idempotency_key=f"c{self.campaign.pk}-ct{self.contact.pk}-v{variant.position}",
        )

    def test_image_dispatch_sends_rendered_caption(self):
        variant = CampaignMessageVariant.objects.create(
            campaign=self.campaign,
            position=1,
            message_type=CampaignMessageVariant.MessageType.IMAGE,
            text="Olá {primeiro_nome}, veja esta imagem.",
            media_url="https://example.com/imagem.jpg",
        )
        job = self._job_for_variant(variant)

        with (
            patch(
                "apps.campaigns.services.campaign_evolution_dispatch.connection_state",
                return_value="open",
            ),
            patch(
                "apps.campaigns.services.campaign_evolution_dispatch.send_media",
                return_value=SimpleNamespace(
                    ok=True,
                    raw={"ok": True},
                    message_id="msg-img",
                    http_status=201,
                    error_message="",
                ),
            ) as mocked_send_media,
        ):
            ok, err, external_id, _payload, http_status, _kind = dispatch_campaign_message(
                job, variant
            )

        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(external_id, "msg-img")
        self.assertEqual(http_status, 201)
        mocked_send_media.assert_called_once()
        self.assertEqual(
            mocked_send_media.call_args.kwargs["caption"], "Olá Maria, veja esta imagem."
        )

    def test_video_dispatch_sends_rendered_caption(self):
        variant = CampaignMessageVariant.objects.create(
            campaign=self.campaign,
            position=1,
            message_type=CampaignMessageVariant.MessageType.VIDEO,
            text="Olá {primeiro_nome}, veja este vídeo.",
            media_url="https://example.com/video.mp4",
        )
        job = self._job_for_variant(variant)

        with (
            patch(
                "apps.campaigns.services.campaign_evolution_dispatch.connection_state",
                return_value="open",
            ),
            patch(
                "apps.campaigns.services.campaign_evolution_dispatch.send_media",
                return_value=SimpleNamespace(
                    ok=True,
                    raw={"ok": True},
                    message_id="msg-video",
                    http_status=201,
                    error_message="",
                ),
            ) as mocked_send_media,
        ):
            ok, err, external_id, _payload, http_status, _kind = dispatch_campaign_message(
                job, variant
            )

        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(external_id, "msg-video")
        self.assertEqual(http_status, 201)
        mocked_send_media.assert_called_once()
        self.assertEqual(
            mocked_send_media.call_args.kwargs["caption"], "Olá Maria, veja este vídeo."
        )


class CampaignHumanizedDelayTests(TestCase):
    def test_calculate_humanized_delay_uses_conservative_range(self):
        with patch(
            "apps.campaigns.services.campaign_humanized_delay.random.triangular",
            return_value=138,
        ) as mocked_random:
            self.assertEqual(calculate_humanized_delay_seconds(120), 138)

        mocked_random.assert_called_once_with(108, 192, 138)

    def test_calculate_humanized_delay_allows_only_small_anticipation(self):
        with patch(
            "apps.campaigns.services.campaign_humanized_delay.random.triangular",
            return_value=108,
        ):
            self.assertEqual(calculate_humanized_delay_seconds(120), 108)

    def test_calculate_humanized_delay_respects_minimum_delay(self):
        with patch(
            "apps.campaigns.services.campaign_humanized_delay.random.triangular",
            return_value=30,
        ) as mocked_random:
            self.assertEqual(calculate_humanized_delay_seconds(10), 30)

        mocked_random.assert_called_once_with(30, 48, 34)

    def test_next_slot_after_last_send_uses_humanized_delay(self):
        workspace = Workspace.objects.create(name="Delay", slug="delay")
        chip = Chip.objects.create(
            workspace=workspace,
            name="Chip delay",
            instance_name="chip-delay",
            delay_between_messages_seconds=120,
        )
        campaign = Campaign.objects.create(
            workspace=workspace,
            name="Camp delay",
            audience=Campaign.Audience.ALL,
            audience_config=default_audience_config(),
            status=Campaign.Status.ACTIVE,
        )
        contact = Contact.objects.create(name="Contato delay", mobile_phone="00911112222")
        now = timezone.now()
        last_sent_at = now - timedelta(seconds=119)
        CampaignSendJob.objects.create(
            campaign=campaign,
            contact=contact,
            chip=chip,
            chip_name_snapshot=chip.name,
            status=CampaignSendJob.Status.SENT,
            sent_at=last_sent_at,
            idempotency_key=f"c{campaign.pk}-ct{contact.pk}",
        )

        with patch(
            "apps.campaigns.services.campaign_rate_limit.calculate_humanized_delay_seconds",
            return_value=160,
        ):
            slot = next_slot_after_last_send(chip, now)

        self.assertEqual(slot, last_sent_at + timedelta(seconds=160))


class MessageVariationQualityTests(TestCase):
    def test_normalize_ignores_accents_punctuation_and_placeholder_names(self):
        normalized = normalize_message_text("Olá, {primeiro_nome}!!! CONFIRME sua consulta amanhã.")

        self.assertEqual(normalized, "ola campo confirme sua consulta amanha")

    def test_similar_messages_are_flagged(self):
        issues = find_message_variation_issues(
            [
                "Oi {nome}, confirme sua consulta amanhã.",
                "Olá {primeiro_nome}, confirme sua consulta amanhã!",
                "Oi {nome}, confirme sua consulta amanhã cedo.",
            ]
        )

        self.assertTrue(issues)
        self.assertEqual(issues[0].first_order, 1)
        self.assertEqual(issues[0].second_order, 2)

    def test_different_messages_are_allowed(self):
        issues = find_message_variation_issues(
            [
                "Oi {primeiro_nome}, tudo bem? Sua consulta na unidade de {bairro} está marcada "
                "para amanhã às 9h.",
                "{primeiro_nome}, o evento de {cidade} começa sábado e a entrada é gratuita para "
                "todos os convidados.",
                "Olá, {primeiro_nome}. Seu cadastro foi atualizado e já é possível consultar o "
                "histórico de atendimentos.",
            ]
        )

        self.assertEqual(issues, [])

    def test_similarity_score_detects_minor_changes(self):
        score = message_similarity(
            "Oi {nome}, confirme sua consulta amanhã.",
            "Oi {primeiro_nome}, confirme sua consulta amanhã!",
        )

        self.assertGreaterEqual(score, 0.85)


class CampaignWizardMessageVariationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="technician",
            password="senha-teste",
        )
        self.workspace = Workspace.objects.create(name="Perfil Msg", slug="perfil-msg")
        self.campaign = Campaign.objects.create(
            workspace=self.workspace,
            name="Campanha Msg",
            audience=Campaign.Audience.ALL,
            audience_config=default_audience_config(),
            status=Campaign.Status.DRAFT,
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["workspace_id"] = self.workspace.pk
        session.save()
        self.url = reverse(
            "campaigns:campaign_wizard",
            kwargs={"pk": self.campaign.pk, "step": "messages"},
        )

    def _post_messages(self, text_1: str, text_2: str = "", text_3: str = ""):
        return self.client.post(
            self.url,
            data={
                "shared_message_type": "",
                "shared_media_url": "",
                "message_type_1": CampaignMessageVariant.MessageType.TEXT,
                "text_1": text_1,
                "media_1": "",
                "message_type_2": CampaignMessageVariant.MessageType.TEXT,
                "text_2": text_2,
                "media_2": "",
                "message_type_3": CampaignMessageVariant.MessageType.TEXT,
                "text_3": text_3,
                "media_3": "",
            },
        )

    def test_single_filled_variation_is_still_copied_to_empty_variations(self):
        response = self._post_messages("Oi {primeiro_nome}, mensagem principal da campanha.")

        self.assertEqual(response.status_code, 302)
        variants = list(
            CampaignMessageVariant.objects.filter(campaign=self.campaign).order_by("position")
        )
        self.assertEqual(len(variants), 3)
        self.assertEqual([variant.text for variant in variants], [variants[0].text] * 3)

    def test_shared_media_keeps_copied_caption_on_empty_variations(self):
        text = "Oi {primeiro_nome}, mensagem principal da campanha."
        media_url = "https://example.com/imagem.jpg"

        response = self.client.post(
            self.url,
            data={
                "shared_message_type": CampaignMessageVariant.MessageType.IMAGE,
                "shared_media_url": media_url,
                "message_type_1": CampaignMessageVariant.MessageType.TEXT,
                "text_1": text,
                "media_1": "",
                "message_type_2": CampaignMessageVariant.MessageType.TEXT,
                "text_2": "",
                "media_2": "",
                "message_type_3": CampaignMessageVariant.MessageType.TEXT,
                "text_3": "",
                "media_3": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        variants = list(
            CampaignMessageVariant.objects.filter(campaign=self.campaign).order_by("position")
        )
        self.assertEqual(len(variants), 3)
        self.assertEqual(
            [variant.message_type for variant in variants],
            [CampaignMessageVariant.MessageType.IMAGE] * 3,
        )
        self.assertEqual([variant.media_url for variant in variants], [media_url] * 3)
        self.assertEqual([variant.text for variant in variants], [text] * 3)

    def test_shared_video_keeps_copied_caption_on_empty_variations(self):
        text = "Oi {primeiro_nome}, veja o vídeo da campanha."
        media_url = "https://example.com/video.mp4"

        response = self.client.post(
            self.url,
            data={
                "shared_message_type": CampaignMessageVariant.MessageType.VIDEO,
                "shared_media_url": media_url,
                "message_type_1": CampaignMessageVariant.MessageType.TEXT,
                "text_1": text,
                "media_1": "",
                "message_type_2": CampaignMessageVariant.MessageType.TEXT,
                "text_2": "",
                "media_2": "",
                "message_type_3": CampaignMessageVariant.MessageType.TEXT,
                "text_3": "",
                "media_3": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        variants = list(
            CampaignMessageVariant.objects.filter(campaign=self.campaign).order_by("position")
        )
        self.assertEqual(
            [variant.message_type for variant in variants],
            [CampaignMessageVariant.MessageType.VIDEO] * 3,
        )
        self.assertEqual([variant.media_url for variant in variants], [media_url] * 3)
        self.assertEqual([variant.text for variant in variants], [text] * 3)

    def test_media_variations_with_blank_caption_copy_text_without_replacing_media(self):
        text = "Oi {primeiro_nome}, legenda padrão."

        response = self.client.post(
            self.url,
            data={
                "shared_message_type": "",
                "shared_media_url": "",
                "message_type_1": CampaignMessageVariant.MessageType.IMAGE,
                "text_1": text,
                "media_1": "https://example.com/imagem-1.jpg",
                "message_type_2": CampaignMessageVariant.MessageType.IMAGE,
                "text_2": "",
                "media_2": "https://example.com/imagem-2.jpg",
                "message_type_3": CampaignMessageVariant.MessageType.VIDEO,
                "text_3": "",
                "media_3": "https://example.com/video-3.mp4",
            },
        )

        self.assertEqual(response.status_code, 302)
        variants = list(
            CampaignMessageVariant.objects.filter(campaign=self.campaign).order_by("position")
        )
        self.assertEqual([variant.text for variant in variants], [text] * 3)
        self.assertEqual(
            [variant.media_url for variant in variants],
            [
                "https://example.com/imagem-1.jpg",
                "https://example.com/imagem-2.jpg",
                "https://example.com/video-3.mp4",
            ],
        )

    def test_three_similar_filled_variations_are_blocked(self):
        response = self._post_messages(
            "Oi {nome}, confirme sua consulta amanhã.",
            "Olá {primeiro_nome}, confirme sua consulta amanhã!",
            "Oi {nome}, confirme sua consulta amanhã cedo.",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "As 3 variações foram preenchidas")
        self.assertContains(response, "risco de bloqueio do chip")
        self.assertEqual(
            CampaignMessageVariant.objects.filter(campaign=self.campaign).count(),
            0,
        )


class ChipEligibleRetryTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="P", slug="p-elig")
        self.chip_a = Chip.objects.create(
            workspace=self.workspace,
            name="A",
            instance_name="inst-a",
        )
        self.chip_b = Chip.objects.create(
            workspace=self.workspace,
            name="B",
            instance_name="inst-b",
        )
        self.campaign_active = Campaign.objects.create(
            workspace=self.workspace,
            name="C1",
            audience=Campaign.Audience.ALL,
            audience_config=default_audience_config(),
            status=Campaign.Status.ACTIVE,
        )
        CampaignChip.objects.create(
            campaign=self.campaign_active,
            chip=self.chip_a,
            is_active=True,
            campaign_status=Campaign.Status.ACTIVE,
        )
        self.campaign_other = Campaign.objects.create(
            workspace=self.workspace,
            name="C2",
            audience=Campaign.Audience.ALL,
            audience_config=default_audience_config(),
            status=Campaign.Status.DRAFT,
        )

    def test_chip_in_active_campaign_blocks_other_campaign(self):
        self.assertFalse(chip_eligible_for_campaign_retry(self.chip_a, self.campaign_other))

    def test_free_chip_ok(self):
        self.assertTrue(chip_eligible_for_campaign_retry(self.chip_b, self.campaign_other))


class RetryFailedWithChipTests(TestCase):
    def setUp(self):
        from apps.campaigns.models import CampaignMessageVariant
        from apps.contacts.models import Contact

        self.workspace = Workspace.objects.create(name="P2", slug="p-retry")
        self.chip_a = Chip.objects.create(
            workspace=self.workspace,
            name="ChipA",
            instance_name="inst-ra",
        )
        self.chip_b = Chip.objects.create(
            workspace=self.workspace,
            name="ChipB",
            instance_name="inst-rb",
        )
        self.campaign = Campaign.objects.create(
            workspace=self.workspace,
            name="Camp",
            audience=Campaign.Audience.ALL,
            audience_config=default_audience_config(),
            status=Campaign.Status.ACTIVE,
        )
        for position in (1, 2, 3):
            CampaignMessageVariant.objects.create(
                campaign=self.campaign,
                position=position,
                message_type=CampaignMessageVariant.MessageType.TEXT,
                text=f"Msg {position}",
            )
        CampaignChip.objects.create(
            campaign=self.campaign,
            chip=self.chip_a,
            is_active=True,
            campaign_status=Campaign.Status.ACTIVE,
        )
        CampaignChip.objects.create(
            campaign=self.campaign,
            chip=self.chip_b,
            is_active=True,
            campaign_status=Campaign.Status.ACTIVE,
        )
        self.contact = Contact.objects.create(
            name="X",
            mobile_phone="00999887766",
        )
        self.job = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=self.contact,
            chip=self.chip_b,
            chip_name_snapshot=self.chip_b.name,
            status=CampaignSendJob.Status.FAILED,
            last_error="falha",
            idempotency_key=f"c{self.campaign.pk}-ct{self.contact.pk}",
        )

    def test_retry_moves_failed_to_pending_and_chip(self):
        n = retry_failed_jobs_with_chip(self.campaign.pk, self.chip_a.pk)
        self.assertEqual(n, 1)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, CampaignSendJob.Status.PENDING)
        self.assertEqual(self.job.chip_id, self.chip_a.pk)
        self.assertEqual(self.job.attempts, 0)

    def test_retry_distributes_round_robin_across_two_chips(self):
        c2 = Contact.objects.create(name="Y", mobile_phone="00999887767")
        c3 = Contact.objects.create(name="Z", mobile_phone="00999887768")
        j2 = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=c2,
            chip=self.chip_b,
            chip_name_snapshot=self.chip_b.name,
            status=CampaignSendJob.Status.FAILED,
            last_error="x",
            idempotency_key=f"c{self.campaign.pk}-ct{c2.pk}",
        )
        j3 = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=c3,
            chip=self.chip_b,
            chip_name_snapshot=self.chip_b.name,
            status=CampaignSendJob.Status.FAILED,
            last_error="y",
            idempotency_key=f"c{self.campaign.pk}-ct{c3.pk}",
        )
        n, names = retry_failed_jobs_with_chips(self.campaign.pk, [self.chip_a.pk, self.chip_b.pk])
        self.assertEqual(n, 3)
        self.assertEqual(names, ["ChipA", "ChipB"])
        for j in (self.job, j2, j3):
            j.refresh_from_db()
        ordered = sorted([self.job, j2, j3], key=lambda j: j.pk)
        self.assertEqual(ordered[0].chip_id, self.chip_a.pk)
        self.assertEqual(ordered[1].chip_id, self.chip_b.pk)
        self.assertEqual(ordered[2].chip_id, self.chip_a.pk)
        for j in ordered:
            self.assertEqual(j.status, CampaignSendJob.Status.PENDING)
            self.assertEqual(j.attempts, 0)

    def test_retry_selected_only_two_jobs(self):
        c2 = Contact.objects.create(name="S2", mobile_phone="00999887772")
        c3 = Contact.objects.create(name="S3", mobile_phone="00999887773")
        j2 = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=c2,
            chip=self.chip_b,
            chip_name_snapshot=self.chip_b.name,
            status=CampaignSendJob.Status.FAILED,
            last_error="x",
            idempotency_key=f"c{self.campaign.pk}-ct{c2.pk}",
        )
        j3 = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=c3,
            chip=self.chip_b,
            chip_name_snapshot=self.chip_b.name,
            status=CampaignSendJob.Status.FAILED,
            last_error="y",
            idempotency_key=f"c{self.campaign.pk}-ct{c3.pk}",
        )
        n, _ = retry_failed_jobs_with_chips(
            self.campaign.pk,
            [self.chip_a.pk],
            scope=RETRY_SCOPE_SELECTED,
            selected_job_ids=[self.job.pk, j3.pk],
        )
        self.assertEqual(n, 2)
        self.job.refresh_from_db()
        j2.refresh_from_db()
        j3.refresh_from_db()
        self.assertEqual(self.job.status, CampaignSendJob.Status.PENDING)
        self.assertEqual(j3.status, CampaignSendJob.Status.PENDING)
        self.assertEqual(j2.status, CampaignSendJob.Status.FAILED)

    def test_retry_cause_chip_only(self):
        self.job.last_error = "Instância WhatsApp desconectada (connectionState=close)."
        self.job.save()
        c2 = Contact.objects.create(name="Api", mobile_phone="00999887774")
        j2 = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=c2,
            chip=self.chip_b,
            chip_name_snapshot=self.chip_b.name,
            status=CampaignSendJob.Status.FAILED,
            last_error="Evolution não configurada: defina EVOLUTION_URL_BASE.",
            idempotency_key=f"c{self.campaign.pk}-ct{c2.pk}",
        )
        n, _ = retry_failed_jobs_with_chips(
            self.campaign.pk,
            [self.chip_a.pk],
            scope=RETRY_SCOPE_CAUSE_CHIP,
        )
        self.assertEqual(n, 1)
        self.job.refresh_from_db()
        j2.refresh_from_db()
        self.assertEqual(self.job.status, CampaignSendJob.Status.PENDING)
        self.assertEqual(j2.status, CampaignSendJob.Status.FAILED)

    def test_retry_cause_message_only(self):
        self.job.last_error = "Phone is not on whatsapp."
        self.job.save()
        c2 = Contact.objects.create(name="Chip", mobile_phone="00999887775")
        j2 = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=c2,
            chip=self.chip_b,
            chip_name_snapshot=self.chip_b.name,
            status=CampaignSendJob.Status.FAILED,
            last_error="401 unauthorized",
            idempotency_key=f"c{self.campaign.pk}-ct{c2.pk}",
        )
        n, _ = retry_failed_jobs_with_chips(
            self.campaign.pk,
            [self.chip_a.pk],
            scope=RETRY_SCOPE_CAUSE_MESSAGE,
        )
        self.assertEqual(n, 1)
        self.job.refresh_from_db()
        j2.refresh_from_db()
        self.assertEqual(self.job.status, CampaignSendJob.Status.PENDING)
        self.assertEqual(j2.status, CampaignSendJob.Status.FAILED)

    def test_retry_preserves_variant_pick_variant_unchanged(self):
        """A retry does not change variant_id; pick_variant_for_job follows the same variant."""
        variants = list(
            CampaignMessageVariant.objects.filter(campaign=self.campaign).order_by("position")
        )
        self.assertEqual(len(variants), 3)
        v_target = variants[2]
        self.job.variant = v_target
        self.job.save()
        retry_failed_jobs_with_chips(self.campaign.pk, [self.chip_a.pk, self.chip_b.pk])
        self.job.refresh_from_db()
        self.assertEqual(self.job.variant_id, v_target.pk)
        job_loaded = CampaignSendJob.objects.select_related("variant").get(pk=self.job.pk)
        self.assertEqual(pick_variant_for_job(job_loaded).pk, v_target.pk)
        self.assertEqual(pick_variant_for_job(job_loaded).text, "Msg 3")

    def test_defer_if_chip_rate_limited_uses_job_assigned_chip(self):
        """After a retry round-robin, the rate limit queries the chip linked to the job."""
        c2 = Contact.objects.create(name="Y2", mobile_phone="00999887771")
        j2 = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=c2,
            chip=self.chip_b,
            chip_name_snapshot=self.chip_b.name,
            status=CampaignSendJob.Status.FAILED,
            last_error="x",
            idempotency_key=f"c{self.campaign.pk}-ct{c2.pk}",
        )
        retry_failed_jobs_with_chips(self.campaign.pk, [self.chip_a.pk, self.chip_b.pk])
        self.job.refresh_from_db()
        j2.refresh_from_db()
        first = self.job if self.job.pk < j2.pk else j2
        second = j2 if first is self.job else self.job
        self.assertEqual(first.chip_id, self.chip_a.pk)
        self.assertEqual(second.chip_id, self.chip_b.pk)

        job_a = CampaignSendJob.objects.select_related("chip").get(pk=first.pk)
        job_b = CampaignSendJob.objects.select_related("chip").get(pk=second.pk)
        with patch(
            "apps.campaigns.services.campaign_rate_limit.compute_defer_until_with_reason"
        ) as m:
            m.return_value = (None, "")
            defer_if_chip_rate_limited(job_a, "worker-test-a")
            self.assertEqual(m.call_args[0][0].pk, self.chip_a.pk)
        with patch(
            "apps.campaigns.services.campaign_rate_limit.compute_defer_until_with_reason"
        ) as m:
            m.return_value = (None, "")
            defer_if_chip_rate_limited(job_b, "worker-test-b")
            self.assertEqual(m.call_args[0][0].pk, self.chip_b.pk)

    def test_retry_does_not_modify_pending_jobs(self):
        ct_pend = Contact.objects.create(name="SóPendente", mobile_phone="00999887799")
        pending = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=ct_pend,
            chip=self.chip_a,
            chip_name_snapshot=self.chip_a.name,
            status=CampaignSendJob.Status.PENDING,
            idempotency_key=f"c{self.campaign.pk}-ct{ct_pend.pk}",
        )
        retry_failed_jobs_with_chips(self.campaign.pk, [self.chip_b.pk])
        pending.refresh_from_db()
        self.assertEqual(pending.status, CampaignSendJob.Status.PENDING)
        self.assertEqual(pending.chip_id, self.chip_a.pk)


class RetryFailedTripleChipTests(TestCase):
    """Round-robin with 3 chips in the explicit order passed to the service."""

    def setUp(self):
        self.workspace = Workspace.objects.create(name="P3", slug="p-triple")
        self.chip_z = Chip.objects.create(
            workspace=self.workspace, name="Zeta", instance_name="inst-z"
        )
        self.chip_a = Chip.objects.create(
            workspace=self.workspace, name="Alfa", instance_name="inst-a"
        )
        self.chip_b = Chip.objects.create(
            workspace=self.workspace, name="Beta", instance_name="inst-b"
        )
        self.campaign = Campaign.objects.create(
            workspace=self.workspace,
            name="Camp3",
            audience=Campaign.Audience.ALL,
            audience_config=default_audience_config(),
            status=Campaign.Status.ACTIVE,
        )
        for position in (1, 2, 3):
            CampaignMessageVariant.objects.create(
                campaign=self.campaign,
                position=position,
                message_type=CampaignMessageVariant.MessageType.TEXT,
                text=f"V{position}",
            )
        for ch in (self.chip_z, self.chip_a, self.chip_b):
            CampaignChip.objects.create(
                campaign=self.campaign,
                chip=ch,
                is_active=True,
                campaign_status=Campaign.Status.ACTIVE,
            )
        variants = list(
            CampaignMessageVariant.objects.filter(campaign=self.campaign).order_by("position")
        )
        self.jobs: list[CampaignSendJob] = []
        for i in range(6):
            ct = Contact.objects.create(name=f"T{i}", mobile_phone=f"8299988800{i}")
            j = CampaignSendJob.objects.create(
                campaign=self.campaign,
                contact=ct,
                chip=self.chip_b,
                variant=variants[i % 3],
                chip_name_snapshot=self.chip_b.name,
                status=CampaignSendJob.Status.FAILED,
                last_error="f",
                idempotency_key=f"c{self.campaign.pk}-ct{ct.pk}",
            )
            self.jobs.append(j)

    def test_six_failures_round_robin_three_chips_order(self):
        order = [self.chip_z.pk, self.chip_a.pk, self.chip_b.pk]
        n, names = retry_failed_jobs_with_chips(self.campaign.pk, order)
        self.assertEqual(n, 6)
        self.assertEqual(names, ["Zeta", "Alfa", "Beta"])
        ordered = sorted(self.jobs, key=lambda j: j.pk)
        expected_chip_ids = [
            self.chip_z.pk,
            self.chip_a.pk,
            self.chip_b.pk,
            self.chip_z.pk,
            self.chip_a.pk,
            self.chip_b.pk,
        ]
        for j, exp_chip in zip(ordered, expected_chip_ids, strict=True):
            j.refresh_from_db()
            self.assertEqual(j.chip_id, exp_chip, msg=f"job pk={j.pk}")
            self.assertEqual(j.status, CampaignSendJob.Status.PENDING)
        # Variants fixed at enqueue time are kept
        variants = list(
            CampaignMessageVariant.objects.filter(campaign=self.campaign).order_by("position")
        )
        for i, j in enumerate(ordered):
            j.refresh_from_db()
            self.assertEqual(j.variant_id, variants[i % 3].pk)


class PermanentContactFailureTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Pperm", slug="p-perm")
        self.chip = Chip.objects.create(workspace=self.workspace, name="C", instance_name="inst-c")
        self.campaign = Campaign.objects.create(
            workspace=self.workspace,
            name="CampPerm",
            audience=Campaign.Audience.ALL,
            audience_config=default_audience_config(),
            status=Campaign.Status.ACTIVE,
        )
        for position in (1, 2, 3):
            CampaignMessageVariant.objects.create(
                campaign=self.campaign,
                position=position,
                message_type=CampaignMessageVariant.MessageType.TEXT,
                text=f"V{position}",
            )
        self.contact = Contact.objects.create(name="N", mobile_phone="00999999999")
        self.worker_id = "worker-perm"
        self.job = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=self.contact,
            chip=self.chip,
            chip_name_snapshot=self.chip.name,
            status=CampaignSendJob.Status.LOCKED,
            locked_by=self.worker_id,
            locked_at=timezone.now(),
            attempts=0,
            max_attempts=3,
            idempotency_key=f"c{self.campaign.pk}-ct{self.contact.pk}",
        )

    def test_fail_locked_permanent_contact_error_marks_failed_after_one(self):
        ok = fail_locked_permanent_contact_error(
            self.job.pk,
            self.worker_id,
            message='{"exists": false, "message": "not registered"}',
        )
        self.assertTrue(ok)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, CampaignSendJob.Status.FAILED)
        self.assertEqual(self.job.attempts, 1)
        self.assertEqual(self.job.max_attempts, 1)
        self.assertIsNone(self.job.scheduled_at)


class AutoFinishRulesTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Paf", slug="p-af")
        self.chip = Chip.objects.create(
            workspace=self.workspace, name="Caf", instance_name="inst-af"
        )
        self.campaign = Campaign.objects.create(
            workspace=self.workspace,
            name="CampAF",
            audience=Campaign.Audience.ALL,
            audience_config=default_audience_config(),
            status=Campaign.Status.ACTIVE,
        )
        for position in (1, 2, 3):
            CampaignMessageVariant.objects.create(
                campaign=self.campaign,
                position=position,
                message_type=CampaignMessageVariant.MessageType.TEXT,
                text=f"V{position}",
            )

    def test_auto_finish_only_when_all_sent(self):
        c1 = Contact.objects.create(name="A", mobile_phone="11911110000")
        c2 = Contact.objects.create(name="B", mobile_phone="11911110001")
        CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=c1,
            chip=self.chip,
            chip_name_snapshot=self.chip.name,
            status=CampaignSendJob.Status.SENT,
            idempotency_key=f"c{self.campaign.pk}-ct{c1.pk}",
        )
        CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=c2,
            chip=self.chip,
            chip_name_snapshot=self.chip.name,
            status=CampaignSendJob.Status.FAILED,
            last_error="exists=false",
            idempotency_key=f"c{self.campaign.pk}-ct{c2.pk}",
        )
        finished = auto_finish_if_complete(self.campaign.pk)
        self.assertFalse(finished)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.ACTIVE)

        CampaignSendJob.objects.filter(
            campaign=self.campaign, status=CampaignSendJob.Status.FAILED
        ).update(status=CampaignSendJob.Status.SENT, last_error="")
        finished2 = auto_finish_if_complete(self.campaign.pk)
        self.assertTrue(finished2)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.FINISHED)


class ChipOfflineSuspendTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Poff", slug="p-off")
        self.chip = Chip.objects.create(workspace=self.workspace, name="Cx", instance_name="ix-off")
        self.campaign = Campaign.objects.create(
            workspace=self.workspace,
            name="Coff",
            audience=Campaign.Audience.ALL,
            audience_config=default_audience_config(),
            status=Campaign.Status.ACTIVE,
        )
        for position in (1, 2, 3):
            CampaignMessageVariant.objects.create(
                campaign=self.campaign,
                position=position,
                message_type=CampaignMessageVariant.MessageType.TEXT,
                text=f"T{position}",
            )
        self.ct1 = Contact.objects.create(name="A", mobile_phone="11911112222")
        self.ct2 = Contact.objects.create(name="B", mobile_phone="11911112223")
        self.ct3 = Contact.objects.create(name="C", mobile_phone="11911112224")
        self.j1 = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=self.ct1,
            chip=self.chip,
            chip_name_snapshot=self.chip.name,
            status=CampaignSendJob.Status.PENDING,
            idempotency_key=f"c{self.campaign.pk}-ct{self.ct1.pk}",
        )
        self.j2 = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=self.ct2,
            chip=self.chip,
            chip_name_snapshot=self.chip.name,
            status=CampaignSendJob.Status.PENDING,
            idempotency_key=f"c{self.campaign.pk}-ct{self.ct2.pk}",
        )
        self.j3 = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=self.ct3,
            chip=self.chip,
            chip_name_snapshot=self.chip.name,
            status=CampaignSendJob.Status.LOCKED,
            locked_by="worker-uuid-test",
            locked_at=timezone.now(),
            idempotency_key=f"c{self.campaign.pk}-ct{self.ct3.pk}",
        )

    def test_suspend_moves_pending_and_locked_to_waiting_chip(self):
        suspend_jobs_for_chip_offline(
            campaign_id=self.campaign.pk,
            chip_id=self.chip.pk,
            current_job_id=self.j3.pk,
            worker_id="worker-uuid-test",
            last_error_for_trigger="Instância desconectada.",
        )
        for j in (self.j1, self.j2, self.j3):
            j.refresh_from_db()
            self.assertEqual(j.status, CampaignSendJob.Status.WAITING_CHIP)
            self.assertEqual(
                j.schedule_defer_reason, CampaignSendJob.ScheduleDeferReason.CHIP_OFFLINE
            )
        self.j3.refresh_from_db()
        self.assertIn("desconectada", (self.j3.last_error or "").lower())
        self.chip.refresh_from_db()
        self.assertIsNotNone(self.chip.evolution_state_next_check_at)


class WorkerPipelineRegressionTests(TestCase):
    """Shield for the critical worker/queue flows, to avoid regressions."""

    def setUp(self):
        self.workspace = Workspace.objects.create(name="Pworker", slug="p-worker")
        self.chip_a = Chip.objects.create(
            workspace=self.workspace, name="Chip A", instance_name="inst-worker-a"
        )
        self.chip_b = Chip.objects.create(
            workspace=self.workspace, name="Chip B", instance_name="inst-worker-b"
        )
        self.chip_c = Chip.objects.create(
            workspace=self.workspace, name="Chip C", instance_name="inst-worker-c"
        )
        self.campaign = Campaign.objects.create(
            workspace=self.workspace,
            name="Camp Worker",
            audience=Campaign.Audience.ALL,
            audience_config=default_audience_config(),
            status=Campaign.Status.ACTIVE,
        )
        for position in (1, 2, 3):
            CampaignMessageVariant.objects.create(
                campaign=self.campaign,
                position=position,
                message_type=CampaignMessageVariant.MessageType.TEXT,
                text=f"Msg worker {position}",
            )
        for chip in (self.chip_a, self.chip_b, self.chip_c):
            CampaignChip.objects.create(
                campaign=self.campaign,
                chip=chip,
                is_active=True,
                campaign_status=Campaign.Status.ACTIVE,
            )
        self.worker_id = "worker-regression"

    def _create_contact(self, suffix: int) -> Contact:
        return Contact.objects.create(name=f"Ct{suffix}", mobile_phone=f"11999000{suffix:03d}")

    def _create_job(
        self,
        *,
        contact: Contact,
        chip: Chip,
        status: str,
        locked_by: str = "",
        last_error: str = "",
    ) -> CampaignSendJob:
        return CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=contact,
            chip=chip,
            chip_name_snapshot=chip.name,
            status=status,
            locked_by=locked_by,
            locked_at=timezone.now() if status == CampaignSendJob.Status.LOCKED else None,
            last_error=last_error,
            idempotency_key=f"c{self.campaign.pk}-ct{contact.pk}",
        )

    def test_claim_and_process_one_marks_sent(self):
        contact = self._create_contact(1)
        job = self._create_job(
            contact=contact,
            chip=self.chip_a,
            status=CampaignSendJob.Status.PENDING,
        )

        def dispatch_ok(_job, _variant):
            return True, "", "ext-123", {"ok": True}, 200, None

        with patch(
            "apps.campaigns.services.campaign_send_execution.defer_if_chip_rate_limited",
            return_value=False,
        ):
            result = claim_and_process_one(
                job.pk,
                self.worker_id,
                lease_seconds=120,
                dispatch=dispatch_ok,
            )

        self.assertEqual(result, "sent")
        job.refresh_from_db()
        self.assertEqual(job.status, CampaignSendJob.Status.SENT)
        self.assertIsNotNone(job.sent_at)
        self.assertEqual(job.external_message_id, "ext-123")
        self.assertEqual(job.attempts, 0)
        self.assertEqual(job.attempts_log.count(), 1)
        self.assertTrue(job.attempts_log.first().success)

    def test_process_send_job_once_schedules_retry_on_temporary_error(self):
        contact = self._create_contact(2)
        job = self._create_job(
            contact=contact,
            chip=self.chip_a,
            status=CampaignSendJob.Status.LOCKED,
            locked_by=self.worker_id,
        )

        def dispatch_fail_temp(_job, _variant):
            return False, "timeout upstream", "", {"error": "timeout"}, 504, None

        with patch(
            "apps.campaigns.services.campaign_send_execution.defer_if_chip_rate_limited",
            return_value=False,
        ):
            result = process_send_job_once(job.pk, self.worker_id, dispatch=dispatch_fail_temp)

        self.assertEqual(result, "retry_scheduled")
        job.refresh_from_db()
        self.assertEqual(job.status, CampaignSendJob.Status.PENDING)
        self.assertEqual(job.attempts, 1)
        self.assertIsNotNone(job.scheduled_at)
        self.assertIn("timeout", job.last_error.lower())
        self.assertEqual(job.attempts_log.count(), 1)
        self.assertFalse(job.attempts_log.first().success)

    def test_process_send_job_once_marks_failed_on_permanent_contact_error(self):
        contact = self._create_contact(3)
        job = self._create_job(
            contact=contact,
            chip=self.chip_a,
            status=CampaignSendJob.Status.LOCKED,
            locked_by=self.worker_id,
        )

        def dispatch_permanent(_job, _variant):
            return (
                False,
                '{"exists": false, "message": "not registered"}',
                "",
                {"exists": False},
                400,
                None,
            )

        with patch(
            "apps.campaigns.services.campaign_send_execution.defer_if_chip_rate_limited",
            return_value=False,
        ):
            result = process_send_job_once(job.pk, self.worker_id, dispatch=dispatch_permanent)

        self.assertEqual(result, "failed")
        job.refresh_from_db()
        self.assertEqual(job.status, CampaignSendJob.Status.FAILED)
        self.assertEqual(job.max_attempts, 1)
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.attempts_log.count(), 1)
        self.assertFalse(job.attempts_log.first().success)

    def test_process_send_job_once_moves_batch_to_waiting_chip_when_offline(self):
        contact_locked = self._create_contact(4)
        contact_pending = self._create_contact(5)
        locked_job = self._create_job(
            contact=contact_locked,
            chip=self.chip_a,
            status=CampaignSendJob.Status.LOCKED,
            locked_by=self.worker_id,
        )
        pending_job = self._create_job(
            contact=contact_pending,
            chip=self.chip_a,
            status=CampaignSendJob.Status.PENDING,
        )

        def dispatch_chip_offline(_job, _variant):
            return False, "instância desconectada", "", None, None, ERROR_KIND_CHIP_OFFLINE

        with patch(
            "apps.campaigns.services.campaign_send_execution.defer_if_chip_rate_limited",
            return_value=False,
        ):
            result = process_send_job_once(
                locked_job.pk, self.worker_id, dispatch=dispatch_chip_offline
            )

        self.assertEqual(result, "waiting_chip")
        locked_job.refresh_from_db()
        pending_job.refresh_from_db()
        self.assertEqual(locked_job.status, CampaignSendJob.Status.WAITING_CHIP)
        self.assertEqual(pending_job.status, CampaignSendJob.Status.WAITING_CHIP)
        self.assertEqual(
            locked_job.schedule_defer_reason,
            CampaignSendJob.ScheduleDeferReason.CHIP_OFFLINE,
        )
        self.assertEqual(
            pending_job.schedule_defer_reason,
            CampaignSendJob.ScheduleDeferReason.CHIP_OFFLINE,
        )
        self.assertIn("desconectada", (locked_job.last_error or "").lower())
        self.chip_a.refresh_from_db()
        self.assertIsNotNone(self.chip_a.evolution_state_next_check_at)

    def test_process_send_job_once_marks_failed_on_whatsapp_463(self):
        from apps.chips.services.evolution_whatsapp_errors import MSG_WHATSAPP_463

        contact = self._create_contact(41)
        job = self._create_job(
            contact=contact,
            chip=self.chip_a,
            status=CampaignSendJob.Status.LOCKED,
            locked_by=self.worker_id,
        )

        def dispatch_463(_job, _variant):
            return (
                False,
                MSG_WHATSAPP_463,
                "",
                {"update": {"status": 0, "messageStubParameters": ["463"]}},
                201,
                None,
            )

        with patch(
            "apps.campaigns.services.campaign_send_execution.defer_if_chip_rate_limited",
            return_value=False,
        ):
            result = process_send_job_once(job.pk, self.worker_id, dispatch=dispatch_463)

        self.assertEqual(result, "failed")
        job.refresh_from_db()
        self.assertEqual(job.status, CampaignSendJob.Status.FAILED)
        self.assertIn("463", job.last_error or "")
        self.assertEqual(job.attempts_log.count(), 1)
        self.assertFalse(job.attempts_log.first().success)

    def test_redistribute_waiting_jobs_round_robin_across_destinations(self):
        c1 = self._create_contact(6)
        c2 = self._create_contact(7)
        c3 = self._create_contact(8)
        j1 = self._create_job(
            contact=c1, chip=self.chip_a, status=CampaignSendJob.Status.WAITING_CHIP
        )
        j2 = self._create_job(
            contact=c2, chip=self.chip_a, status=CampaignSendJob.Status.WAITING_CHIP
        )
        j3 = self._create_job(
            contact=c3, chip=self.chip_a, status=CampaignSendJob.Status.WAITING_CHIP
        )

        moved = redistribute_waiting_jobs(
            self.campaign,
            source_chip_id=self.chip_a.pk,
            destination_chip_ids=[self.chip_b.pk, self.chip_c.pk],
        )

        self.assertEqual(moved, 3)
        j1.refresh_from_db()
        j2.refresh_from_db()
        j3.refresh_from_db()
        ordered = sorted([j1, j2, j3], key=lambda j: j.pk)
        self.assertEqual(ordered[0].chip_id, self.chip_b.pk)
        self.assertEqual(ordered[1].chip_id, self.chip_c.pk)
        self.assertEqual(ordered[2].chip_id, self.chip_b.pk)
        for job in ordered:
            self.assertEqual(job.status, CampaignSendJob.Status.PENDING)
            self.assertEqual(job.attempts, 0)
            self.assertEqual(job.schedule_defer_reason, "")
            self.assertIsNotNone(job.scheduled_at)
        self.assertEqual(ordered[0].chip_name_snapshot, self.chip_b.name)
        self.assertEqual(ordered[1].chip_name_snapshot, self.chip_c.name)
        self.assertEqual(ordered[2].chip_name_snapshot, self.chip_b.name)

    def test_pause_and_resume_campaign_keep_processing_safe(self):
        contact_paused = self._create_contact(9)
        job_paused = self._create_job(
            contact=contact_paused,
            chip=self.chip_a,
            status=CampaignSendJob.Status.LOCKED,
            locked_by=self.worker_id,
        )
        pause_campaign(self.campaign)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.PAUSED)

        def dispatch_should_not_run(_job, _variant):
            raise AssertionError("dispatch não deve rodar com campanha pausada")

        result_paused = process_send_job_once(
            job_paused.pk, self.worker_id, dispatch=dispatch_should_not_run
        )
        self.assertEqual(result_paused, "cancelled")
        job_paused.refresh_from_db()
        self.assertEqual(job_paused.status, CampaignSendJob.Status.CANCELLED)

        resume_campaign(self.campaign)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.ACTIVE)

        contact_resumed = self._create_contact(10)
        job_resumed = self._create_job(
            contact=contact_resumed,
            chip=self.chip_a,
            status=CampaignSendJob.Status.PENDING,
        )

        def dispatch_ok(_job, _variant):
            return True, "", "ext-after-resume", {"ok": True}, 200, None

        with patch(
            "apps.campaigns.services.campaign_send_execution.defer_if_chip_rate_limited",
            return_value=False,
        ):
            result_resumed = claim_and_process_one(
                job_resumed.pk,
                self.worker_id,
                lease_seconds=120,
                dispatch=dispatch_ok,
            )

        self.assertEqual(result_resumed, "sent")
        job_resumed.refresh_from_db()
        self.assertEqual(job_resumed.status, CampaignSendJob.Status.SENT)
