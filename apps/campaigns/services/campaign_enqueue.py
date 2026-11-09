"""Idempotent creation of CampaignSendJob. Never duplicates (campaign, contact) in the database."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.campaigns.models import Campaign, CampaignChip, CampaignMessageVariant, CampaignSendJob
from apps.contacts.models import Contact

# Avoids accidentally enqueuing millions of rows in a single call.
DEFAULT_MAX_NEW_JOBS = 50_000


@dataclass(frozen=True)
class EnqueueResult:
    attempted: int
    created: int
    skipped_already_queued: int


def enqueue_delivery_jobs(
    campaign_id: int,
    contact_ids: Sequence[int] | Iterable[int],
    *,
    max_new_jobs: int = DEFAULT_MAX_NEW_JOBS,
) -> EnqueueResult:
    """
    Create PENDING jobs for contacts that have no job in this campaign yet.
    Round-robin across the campaign's active chips.
    Message variant (1/2/3) rotates **per chip**: each chip gets V1, V2, V3, V1... in the order
    of its own sends, independently of the global contact rotation.
    Idempotent: running it again does not duplicate (DB + exclusion of already enqueued ids).
    """
    ids = list(dict.fromkeys(int(x) for x in contact_ids))
    if not ids:
        return EnqueueResult(0, 0, 0)

    if len(ids) > max_new_jobs:
        ids = ids[:max_new_jobs]

    campaign = Campaign.objects.get(pk=campaign_id)
    if campaign.status != Campaign.Status.ACTIVE:
        raise ValueError("Só é possível enfileirar com a campanha ativa.")

    if campaign.message_variants.count() != 3:
        raise ValueError("A campanha precisa ter exatamente 3 variações de mensagem.")

    cc_qs = (
        CampaignChip.objects.filter(campaign_id=campaign_id, is_active=True)
        .select_related("chip")
        .order_by("pk")
    )
    chip_rows = [cc for cc in cc_qs if cc.chip.is_active]
    if not chip_rows:
        raise ValueError("Nenhum chip ativo vinculado à campanha.")

    valid_contact = set(
        Contact.objects.filter(pk__in=ids, deleted_at__isnull=True).values_list("pk", flat=True)
    )
    ids = [i for i in ids if i in valid_contact]
    if not ids:
        return EnqueueResult(0, 0, 0)

    existing = set(
        CampaignSendJob.objects.filter(campaign_id=campaign_id).values_list("contact_id", flat=True)
    )
    to_create_ids = [i for i in ids if i not in existing]
    skipped = len(ids) - len(to_create_ids)

    if not to_create_ids:
        return EnqueueResult(0, 0, skipped)

    chip_ids = [r.chip_id for r in chip_rows]
    chip_names = {r.chip_id: r.chip.name for r in chip_rows}
    n_chips = len(chip_ids)

    variant_rows = list(
        CampaignMessageVariant.objects.filter(campaign_id=campaign_id).order_by("position")
    )
    if len(variant_rows) != 3:
        raise ValueError("A campanha precisa ter exatamente 3 variações de mensagem.")
    variant_ids = [v.id for v in variant_rows]

    scheduled_default = None
    if campaign.start_at and campaign.start_at > timezone.now():
        scheduled_default = campaign.start_at

    with transaction.atomic():
        before = CampaignSendJob.objects.filter(campaign_id=campaign_id).count()
        batch: list[CampaignSendJob] = []
        chip_variant_rot: defaultdict[int, int] = defaultdict(int)
        for idx, cid in enumerate(to_create_ids):
            chip_id = chip_ids[idx % n_chips]
            rot = chip_variant_rot[chip_id]
            variant_id = variant_ids[rot % 3]
            chip_variant_rot[chip_id] = rot + 1
            batch.append(
                CampaignSendJob(
                    campaign_id=campaign_id,
                    contact_id=cid,
                    chip_id=chip_id,
                    chip_name_snapshot=chip_names.get(chip_id, ""),
                    variant_id=variant_id,
                    status=CampaignSendJob.Status.PENDING,
                    scheduled_at=scheduled_default,
                    idempotency_key=f"c{campaign_id}-ct{cid}",
                )
            )
        CampaignSendJob.objects.bulk_create(
            batch,
            batch_size=500,
            ignore_conflicts=True,
        )
        after = CampaignSendJob.objects.filter(campaign_id=campaign_id).count()
        created = after - before

    return EnqueueResult(
        attempted=len(to_create_ids), created=created, skipped_already_queued=skipped
    )
