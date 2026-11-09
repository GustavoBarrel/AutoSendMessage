from __future__ import annotations

from collections import Counter
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Workspace
from apps.campaigns.models import (
    Campaign,
    CampaignChip,
    CampaignMessageVariant,
    CampaignSendAttempt,
    CampaignSendJob,
)
from apps.campaigns.services.campaign_chip_offline import (
    ERROR_KIND_CHIP_OFFLINE,
    redistribute_waiting_jobs,
    try_resume_chips_with_waiting_jobs,
)
from apps.campaigns.services.campaign_chips import add_chips_to_campaign
from apps.campaigns.services.campaign_enqueue import enqueue_delivery_jobs
from apps.campaigns.services.campaign_send_execution import claim_and_process_one
from apps.chips.models import Chip
from apps.contacts.models import Contact


class CampaignVolumeTestMixin:
    worker_id = "worker-volume-tests"

    def create_workspace(self, slug: str = "perfil-volume") -> Workspace:
        return Workspace.objects.create(name="Perfil Volume", slug=slug)

    def create_chip(self, workspace: Workspace, suffix: str, **kwargs) -> Chip:
        defaults = {
            "name": f"Chip {suffix}",
            "instance_name": f"inst-{suffix}",
            "max_messages_per_hour": 100,
            "max_messages_per_day": 1000,
            "delay_between_messages_seconds": 30,
        }
        defaults.update(kwargs)
        return Chip.objects.create(workspace=workspace, **defaults)

    def create_campaign(
        self,
        workspace: Workspace,
        *,
        status: str = Campaign.Status.ACTIVE,
        name: str = "Campanha Volume",
    ) -> Campaign:
        return Campaign.objects.create(
            workspace=workspace,
            name=name,
            audience=Campaign.Audience.ALL,
            audience_config={},
            status=status,
        )

    def attach_chips(self, campaign: Campaign, chips: list[Chip]) -> None:
        for chip in chips:
            CampaignChip.objects.create(
                campaign=campaign,
                chip=chip,
                is_active=True,
                campaign_status=campaign.status,
            )

    def create_variants(self, campaign: Campaign) -> list[CampaignMessageVariant]:
        return [
            CampaignMessageVariant.objects.create(
                campaign=campaign,
                position=1,
                message_type=CampaignMessageVariant.MessageType.TEXT,
                text="Texto puro para {primeiro_nome}.",
            ),
            CampaignMessageVariant.objects.create(
                campaign=campaign,
                position=2,
                message_type=CampaignMessageVariant.MessageType.IMAGE,
                text="Legenda da imagem para {primeiro_nome}.",
                media_url="https://example.com/imagem.jpg",
            ),
            CampaignMessageVariant.objects.create(
                campaign=campaign,
                position=3,
                message_type=CampaignMessageVariant.MessageType.VIDEO,
                text="Legenda do vídeo para {primeiro_nome}.",
                media_url="https://example.com/video.mp4",
            ),
        ]

    def create_contacts(self, total: int, *, start: int = 0) -> list[Contact]:
        contacts = [
            Contact(
                name=f"Contato {start + i}",
                mobile_phone=f"009{start + i:08d}",
                has_whatsapp=True,
            )
            for i in range(total)
        ]
        return Contact.objects.bulk_create(contacts, batch_size=1000)

    def create_job(
        self,
        campaign: Campaign,
        contact: Contact,
        chip: Chip,
        *,
        variant: CampaignMessageVariant | None = None,
        status: str = CampaignSendJob.Status.PENDING,
        locked_by: str = "",
    ) -> CampaignSendJob:
        return CampaignSendJob.objects.create(
            campaign=campaign,
            contact=contact,
            chip=chip,
            chip_name_snapshot=chip.name,
            variant=variant,
            status=status,
            locked_by=locked_by,
            locked_at=timezone.now() if status == CampaignSendJob.Status.LOCKED else None,
            lease_expires_at=(
                timezone.now() + timedelta(minutes=5)
                if status == CampaignSendJob.Status.LOCKED
                else None
            ),
            idempotency_key=f"c{campaign.pk}-ct{contact.pk}",
        )


class CampaignLargeEnqueueTests(CampaignVolumeTestMixin, TestCase):
    def test_enqueue_10000_contacts_distributes_chips_variants_and_is_idempotent(self):
        workspace = self.create_workspace()
        campaign = self.create_campaign(workspace)
        chips = [self.create_chip(workspace, str(i)) for i in range(1, 5)]
        self.attach_chips(campaign, chips)
        variants = self.create_variants(campaign)
        contacts = self.create_contacts(10_000)

        result = enqueue_delivery_jobs(campaign.pk, [contact.pk for contact in contacts])

        self.assertEqual(result.attempted, 10_000)
        self.assertEqual(result.created, 10_000)
        self.assertEqual(result.skipped_already_queued, 0)
        self.assertEqual(CampaignSendJob.objects.filter(campaign=campaign).count(), 10_000)
        self.assertEqual(
            CampaignSendJob.objects.filter(campaign=campaign)
            .values("contact_id")
            .distinct()
            .count(),
            10_000,
        )

        chip_counts = Counter(
            CampaignSendJob.objects.filter(campaign=campaign).values_list("chip_id", flat=True)
        )
        self.assertEqual(set(chip_counts), {chip.pk for chip in chips})
        self.assertTrue(all(count == 2500 for count in chip_counts.values()))

        variant_ids = {variant.pk for variant in variants}
        for chip in chips:
            per_chip_variants = Counter(
                CampaignSendJob.objects.filter(campaign=campaign, chip=chip).values_list(
                    "variant_id", flat=True
                )
            )
            self.assertEqual(set(per_chip_variants), variant_ids)
            self.assertEqual(sum(per_chip_variants.values()), 2500)
            self.assertLessEqual(
                max(per_chip_variants.values()) - min(per_chip_variants.values()),
                1,
            )

        second = enqueue_delivery_jobs(campaign.pk, [contact.pk for contact in contacts])
        self.assertEqual(second.attempted, 0)
        self.assertEqual(second.created, 0)
        self.assertEqual(second.skipped_already_queued, 10_000)
        self.assertEqual(CampaignSendJob.objects.filter(campaign=campaign).count(), 10_000)

    def test_enqueue_1200_shared_media_jobs_never_uses_blank_copied_caption(self):
        workspace = self.create_workspace("perfil-shared-media-load")
        campaign = self.create_campaign(workspace, name="Campanha Mídia Compartilhada")
        chips = [self.create_chip(workspace, f"shared-{i}") for i in range(1, 4)]
        self.attach_chips(campaign, chips)
        caption = "Oi {primeiro_nome}, legenda copiada da variação 1."
        variants = [
            CampaignMessageVariant.objects.create(
                campaign=campaign,
                position=position,
                message_type=CampaignMessageVariant.MessageType.IMAGE,
                text=caption,
                media_url="https://example.com/imagem-compartilhada.jpg",
            )
            for position in (1, 2, 3)
        ]
        contacts = self.create_contacts(1_200, start=50_000)

        result = enqueue_delivery_jobs(campaign.pk, [contact.pk for contact in contacts])

        self.assertEqual(result.created, 1_200)
        self.assertEqual(CampaignSendJob.objects.filter(campaign=campaign).count(), 1_200)
        self.assertFalse(
            CampaignSendJob.objects.filter(campaign=campaign, variant__text="").exists()
        )
        self.assertEqual(
            CampaignSendJob.objects.filter(
                campaign=campaign,
                variant__message_type=CampaignMessageVariant.MessageType.IMAGE,
                variant__media_url="https://example.com/imagem-compartilhada.jpg",
            ).count(),
            1_200,
        )

        variant_ids = {variant.pk for variant in variants}
        for chip in chips:
            per_chip_variants = Counter(
                CampaignSendJob.objects.filter(campaign=campaign, chip=chip).values_list(
                    "variant_id",
                    flat=True,
                )
            )
            self.assertEqual(set(per_chip_variants), variant_ids)
            self.assertEqual(sum(per_chip_variants.values()), 400)
            self.assertLessEqual(
                max(per_chip_variants.values()) - min(per_chip_variants.values()),
                1,
            )


class CampaignDispatchPipelineTests(CampaignVolumeTestMixin, TestCase):
    def setUp(self):
        self.workspace = self.create_workspace("perfil-pipeline")
        self.campaign = self.create_campaign(self.workspace)
        self.chip = self.create_chip(self.workspace, "pipeline")
        self.attach_chips(self.campaign, [self.chip])
        self.variants = self.create_variants(self.campaign)
        self.contacts = self.create_contacts(20, start=20_000)

    def test_pipeline_sends_text_image_and_video_variants_with_fake_dispatch(self):
        jobs = [
            self.create_job(
                self.campaign,
                self.contacts[index],
                self.chip,
                variant=variant,
            )
            for index, variant in enumerate(self.variants)
        ]
        seen: list[tuple[str, str, str]] = []

        def dispatch_ok(job, variant):
            seen.append((variant.message_type, variant.text, variant.media_url))
            return True, "", f"fake-{variant.message_type}-{job.pk}", {"ok": True}, 200, None

        with patch(
            "apps.campaigns.services.campaign_send_execution.defer_if_chip_rate_limited",
            return_value=False,
        ):
            results = [
                claim_and_process_one(
                    job.pk, self.worker_id, lease_seconds=120, dispatch=dispatch_ok
                )
                for job in jobs
            ]

        self.assertEqual(results, ["sent", "sent", "sent"])
        self.assertEqual(
            [item[0] for item in seen],
            [
                CampaignMessageVariant.MessageType.TEXT,
                CampaignMessageVariant.MessageType.IMAGE,
                CampaignMessageVariant.MessageType.VIDEO,
            ],
        )
        self.assertEqual(seen[0][2], "")
        self.assertEqual(seen[1][2], "https://example.com/imagem.jpg")
        self.assertEqual(seen[2][2], "https://example.com/video.mp4")
        self.assertEqual(
            list(
                CampaignSendJob.objects.filter(pk__in=[j.pk for j in jobs]).values_list(
                    "status", flat=True
                )
            ),
            [CampaignSendJob.Status.SENT] * 3,
        )
        self.assertEqual(CampaignSendAttempt.objects.filter(success=True).count(), 3)

    def test_temporary_failure_is_retried_without_marking_failed_immediately(self):
        job = self.create_job(self.campaign, self.contacts[3], self.chip, variant=self.variants[0])

        def dispatch_timeout(_job, _variant):
            return False, "timeout upstream", "", {"error": "timeout"}, 504, None

        with patch(
            "apps.campaigns.services.campaign_send_execution.defer_if_chip_rate_limited",
            return_value=False,
        ):
            result = claim_and_process_one(
                job.pk,
                self.worker_id,
                lease_seconds=120,
                dispatch=dispatch_timeout,
            )

        self.assertEqual(result, "retry_scheduled")
        job.refresh_from_db()
        self.assertEqual(job.status, CampaignSendJob.Status.PENDING)
        self.assertEqual(job.attempts, 1)
        self.assertIsNotNone(job.scheduled_at)
        self.assertIn("timeout", job.last_error)
        attempt = job.attempts_log.get()
        self.assertFalse(attempt.success)
        self.assertEqual(attempt.http_status, 504)

    def test_permanent_number_failure_fails_after_one_attempt(self):
        job = self.create_job(self.campaign, self.contacts[4], self.chip, variant=self.variants[0])

        def dispatch_number_not_found(_job, _variant):
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
            result = claim_and_process_one(
                job.pk,
                self.worker_id,
                lease_seconds=120,
                dispatch=dispatch_number_not_found,
            )

        self.assertEqual(result, "failed")
        job.refresh_from_db()
        self.assertEqual(job.status, CampaignSendJob.Status.FAILED)
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.max_attempts, 1)
        self.assertIsNone(job.scheduled_at)
        self.assertFalse(job.attempts_log.get().success)


class CampaignChipFailureAndRecoveryTests(CampaignVolumeTestMixin, TestCase):
    def setUp(self):
        self.workspace = self.create_workspace("perfil-chip-failure")
        self.campaign = self.create_campaign(self.workspace)
        self.chip_a = self.create_chip(self.workspace, "a")
        self.chip_b = self.create_chip(self.workspace, "b")
        self.chip_c = self.create_chip(self.workspace, "c")
        self.attach_chips(self.campaign, [self.chip_a, self.chip_b])
        self.variants = self.create_variants(self.campaign)
        self.contacts = self.create_contacts(12, start=30_000)

    def test_chip_offline_suspends_only_jobs_from_that_chip_and_resume_restores_pending(self):
        locked = self.create_job(
            self.campaign,
            self.contacts[0],
            self.chip_a,
            variant=self.variants[0],
        )
        chip_a_pending = [
            self.create_job(
                self.campaign, self.contacts[i], self.chip_a, variant=self.variants[i % 3]
            )
            for i in range(1, 5)
        ]
        chip_b_pending = [
            self.create_job(
                self.campaign, self.contacts[i], self.chip_b, variant=self.variants[i % 3]
            )
            for i in range(5, 8)
        ]

        def dispatch_chip_offline(_job, _variant):
            return False, "instância desconectada", "", None, None, ERROR_KIND_CHIP_OFFLINE

        with patch(
            "apps.campaigns.services.campaign_send_execution.defer_if_chip_rate_limited",
            return_value=False,
        ):
            result = claim_and_process_one(
                locked.pk,
                self.worker_id,
                lease_seconds=120,
                dispatch=dispatch_chip_offline,
            )

        self.assertEqual(result, "waiting_chip")
        self.assertEqual(
            CampaignSendJob.objects.filter(
                chip=self.chip_a, status=CampaignSendJob.Status.WAITING_CHIP
            ).count(),
            5,
        )
        self.assertEqual(
            CampaignSendJob.objects.filter(
                chip=self.chip_b, status=CampaignSendJob.Status.PENDING
            ).count(),
            3,
        )
        for job in [locked, *chip_a_pending]:
            job.refresh_from_db()
            self.assertEqual(
                job.schedule_defer_reason, CampaignSendJob.ScheduleDeferReason.CHIP_OFFLINE
            )
        for job in chip_b_pending:
            job.refresh_from_db()
            self.assertEqual(job.status, CampaignSendJob.Status.PENDING)

        self.chip_a.evolution_state_next_check_at = timezone.now() - timedelta(seconds=1)
        self.chip_a.save(update_fields=["evolution_state_next_check_at"])
        with override_settings(
            EVOLUTION_URL_BASE="https://evolution.example", EVOLUTION_API_KEY="secret"
        ):
            with patch(
                "apps.campaigns.services.campaign_chip_offline.connection_state",
                return_value="open",
            ):
                resumed = try_resume_chips_with_waiting_jobs()

        self.assertEqual(resumed, 1)
        self.assertEqual(
            CampaignSendJob.objects.filter(
                chip=self.chip_a, status=CampaignSendJob.Status.PENDING
            ).count(),
            5,
        )
        self.chip_a.refresh_from_db()
        self.assertIsNone(self.chip_a.evolution_state_next_check_at)

    def test_redistribute_waiting_jobs_round_robins_to_destination_chips(self):
        CampaignChip.objects.create(
            campaign=self.campaign,
            chip=self.chip_c,
            is_active=True,
            campaign_status=self.campaign.status,
        )
        waiting_jobs = [
            self.create_job(
                self.campaign,
                self.contacts[i],
                self.chip_a,
                variant=self.variants[i % 3],
                status=CampaignSendJob.Status.WAITING_CHIP,
            )
            for i in range(6)
        ]
        for job in waiting_jobs:
            job.schedule_defer_reason = CampaignSendJob.ScheduleDeferReason.CHIP_OFFLINE
            job.save(update_fields=["schedule_defer_reason"])

        moved = redistribute_waiting_jobs(
            self.campaign,
            source_chip_id=self.chip_a.pk,
            destination_chip_ids=[self.chip_b.pk, self.chip_c.pk],
        )

        self.assertEqual(moved, 6)
        counts = Counter(
            CampaignSendJob.objects.filter(pk__in=[j.pk for j in waiting_jobs]).values_list(
                "chip_id", flat=True
            )
        )
        self.assertEqual(counts[self.chip_b.pk], 3)
        self.assertEqual(counts[self.chip_c.pk], 3)
        self.assertFalse(
            CampaignSendJob.objects.filter(
                pk__in=[j.pk for j in waiting_jobs], status=CampaignSendJob.Status.WAITING_CHIP
            ).exists()
        )
        for job in CampaignSendJob.objects.filter(pk__in=[j.pk for j in waiting_jobs]):
            self.assertEqual(job.status, CampaignSendJob.Status.PENDING)
            self.assertEqual(job.attempts, 0)
            self.assertEqual(job.schedule_defer_reason, "")
            self.assertIsNotNone(job.scheduled_at)

    def test_add_chip_to_active_campaign_enables_future_redistribution_destination(self):
        self.assertFalse(
            CampaignChip.objects.filter(
                campaign=self.campaign, chip=self.chip_c, is_active=True
            ).exists()
        )

        added, warnings = add_chips_to_campaign(self.campaign, [self.chip_c.pk])

        self.assertEqual(added, 1)
        self.assertEqual(warnings, [])
        link = CampaignChip.objects.get(campaign=self.campaign, chip=self.chip_c)
        self.assertTrue(link.is_active)
        self.assertEqual(link.campaign_status, Campaign.Status.ACTIVE)

        waiting = self.create_job(
            self.campaign,
            self.contacts[8],
            self.chip_a,
            variant=self.variants[0],
            status=CampaignSendJob.Status.WAITING_CHIP,
        )
        moved = redistribute_waiting_jobs(
            self.campaign,
            source_chip_id=self.chip_a.pk,
            destination_chip_ids=[self.chip_c.pk],
        )

        self.assertEqual(moved, 1)
        waiting.refresh_from_db()
        self.assertEqual(waiting.chip_id, self.chip_c.pk)
        self.assertEqual(waiting.status, CampaignSendJob.Status.PENDING)
