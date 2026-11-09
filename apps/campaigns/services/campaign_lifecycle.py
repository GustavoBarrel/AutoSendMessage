"""Activate, pause and finish campaigns, and enqueue contacts."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.campaigns.models import Campaign, CampaignChip, CampaignMessageVariant
from apps.campaigns.services.campaign_audience import contact_ids_for_campaign_audience
from apps.campaigns.services.campaign_enqueue import EnqueueResult, enqueue_delivery_jobs
from apps.campaigns.services.campaign_send_queue import cancel_pending_for_campaign


def _validate_ready(campaign: Campaign) -> None:
    if campaign.message_variants.count() != 3:
        raise ValueError("Cadastre exatamente 3 variações de mensagem.")
    if not CampaignChip.objects.filter(
        campaign_id=campaign.pk, is_active=True, chip__is_active=True
    ).exists():
        raise ValueError("Selecione ao menos um chip ativo na campanha.")


@transaction.atomic
def activate_and_enqueue(campaign: Campaign) -> EnqueueResult:
    """DRAFT/PAUSED → ACTIVE, and create jobs for the current audience (idempotent)."""
    _validate_ready(campaign)
    campaign.status = Campaign.Status.ACTIVE
    if campaign.activated_at is None:
        campaign.activated_at = timezone.now()
    campaign.save()
    ids = contact_ids_for_campaign_audience(campaign)
    return enqueue_delivery_jobs(campaign.pk, ids)


def pause_campaign(campaign: Campaign) -> None:
    campaign.status = Campaign.Status.PAUSED
    campaign.save()


def resume_campaign(campaign: Campaign) -> None:
    campaign.status = Campaign.Status.ACTIVE
    campaign.save()


@transaction.atomic
def finish_campaign(campaign: Campaign) -> int:
    """Finish the campaign and cancel pending/locked jobs."""
    n = cancel_pending_for_campaign(campaign.pk)
    campaign.status = Campaign.Status.FINISHED
    campaign.save()
    return n


def auto_finish_if_complete(campaign_id: int) -> bool:
    """
    Automatically finish the campaign when there are no more PENDING, LOCKED or WAITING_CHIP
    (waiting for the chip to reconnect) jobs and ALL jobs were sent successfully (SENT).

    Called by the worker after job status transitions.
    Return True if the campaign was finished.
    """
    import logging

    from apps.campaigns.models import CampaignSendJob

    logger = logging.getLogger(__name__)

    campaign = Campaign.objects.filter(pk=campaign_id, status=Campaign.Status.ACTIVE).first()
    if not campaign:
        return False

    jobs = CampaignSendJob.objects.filter(campaign_id=campaign_id)
    if not jobs.exists():
        return False

    has_pending = jobs.filter(
        status__in=[
            CampaignSendJob.Status.PENDING,
            CampaignSendJob.Status.LOCKED,
            CampaignSendJob.Status.WAITING_CHIP,
        ]
    ).exists()
    if has_pending:
        return False

    # Only finish automatically when no job is in a status other than SENT.
    # That keeps permanent failures visible and makes finishing a manual step.
    any_not_sent = jobs.exclude(status=CampaignSendJob.Status.SENT).exists()
    if any_not_sent:
        return False

    finish_campaign(campaign)
    logger.info(
        "campaign_auto_finished campaign_id=%s — all sends succeeded",
        campaign_id,
    )
    return True


@transaction.atomic
def save_variants_from_post(
    campaign: Campaign,
    *,
    message_type_1,
    text_1,
    media_1,
    message_type_2,
    text_2,
    media_2,
    message_type_3,
    text_3,
    media_3,
) -> None:
    """
    Create/update the 3 variants (position 1..3) atomically.

    Validate every instance before persisting any of them, avoiding a partial
    state in the database if one variant is invalid.
    """
    rows = [
        (1, message_type_1, text_1, media_1),
        (2, message_type_2, text_2, media_2),
        (3, message_type_3, text_3, media_3),
    ]

    # First pass: build instances and validate without saving.
    instances: list[CampaignMessageVariant] = []
    for position, message_type, text, media in rows:
        try:
            obj = CampaignMessageVariant.objects.get(campaign=campaign, position=position)
        except CampaignMessageVariant.DoesNotExist:
            obj = CampaignMessageVariant(campaign=campaign, position=position)
        obj.message_type = message_type
        obj.text = (text or "").strip()
        obj.media_url = (media or "").strip()
        obj.full_clean()
        instances.append(obj)

    # Second pass: persist only if every instance passed validation.
    for obj in instances:
        obj.save()
