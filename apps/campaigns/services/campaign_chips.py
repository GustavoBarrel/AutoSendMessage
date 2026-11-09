"""Linking chips to a campaign (dashboard), without going through the wizard."""

from __future__ import annotations

from django.db import transaction

from apps.campaigns.models import Campaign, CampaignChip
from apps.campaigns.services.campaign_retry import chip_eligible_for_campaign_retry
from apps.chips.models import Chip


@transaction.atomic
def add_chips_to_campaign(campaign: Campaign, chip_ids: list[int]) -> tuple[int, list[str]]:
    """
    Add chips to the campaign (CampaignChip), or reactivate an inactive existing link.

    Only chips of the same workspace, active and eligible (not blocked by another active
    campaign).

    Return (number linked or reactivated, warnings).
    """
    if campaign.is_deleted:
        raise ValueError("Campanha arquivada.")
    if campaign.status not in (
        Campaign.Status.DRAFT,
        Campaign.Status.ACTIVE,
        Campaign.Status.PAUSED,
    ):
        raise ValueError("Só é possível alterar chips em campanha em rascunho, ativa ou pausada.")

    seen: set[int] = set()
    ordered: list[int] = []
    for raw in chip_ids:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)

    if not ordered:
        raise ValueError("Selecione ao menos um chip para adicionar.")

    chips = list(
        Chip.objects.filter(
            workspace_id=campaign.workspace_id,
            pk__in=ordered,
            is_active=True,
        ).order_by("name")
    )
    found_ids = {c.pk for c in chips}
    warnings: list[str] = []
    if any(i not in found_ids for i in ordered):
        warnings.append("Alguns itens não são chips ativos deste perfil e foram ignorados.")

    n = 0
    for chip in chips:
        if not chip_eligible_for_campaign_retry(chip, campaign):
            warnings.append(f"«{chip.name}» ignorado: chip já usado em outra campanha ativa.")
            continue
        cc, created = CampaignChip.objects.get_or_create(
            campaign=campaign,
            chip=chip,
            defaults={
                "is_active": True,
                "campaign_status": campaign.status,
            },
        )
        if created:
            n += 1
            continue
        to_update: list[str] = []
        if not cc.is_active:
            cc.is_active = True
            to_update.append("is_active")
        if cc.campaign_status != campaign.status:
            cc.campaign_status = campaign.status
            to_update.append("campaign_status")
        if to_update:
            cc.save(update_fields=to_update)
            n += 1

    if n == 0:
        msg = "Nenhum chip novo foi vinculado."
        if warnings:
            msg = " ".join(warnings[:6]) + ("…" if len(warnings) > 6 else "")
        raise ValueError(msg)

    return n, warnings
