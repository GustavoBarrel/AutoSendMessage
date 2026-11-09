"""
Dispatch of campaign messages through a WhatsApp provider.
Pick the provider (Evolution or Meta) from Chip.provider.
"""

from __future__ import annotations

from apps.campaigns.models import CampaignMessageVariant
from apps.campaigns.services.campaign_chip_offline import ERROR_KIND_CHIP_OFFLINE
from apps.campaigns.services.message_utils import render_text
from apps.chips.providers import get_provider_for_chip
from apps.chips.providers.base import ProviderSendResult


def dispatch_campaign_message(
    job,
    variant: CampaignMessageVariant,
) -> tuple[bool, str, str, dict | None, int | None, str | None]:
    """
    Return (success, error, external_message_id, payload, http_status, error_kind).
    Delegates to the provider configured on the chip.
    """
    if job.chip is None:
        return False, "Chip não configurado.", "", None, None, None

    provider = get_provider_for_chip(job.chip)
    contact = job.contact
    if contact is None:
        return False, "Contato não encontrado.", "", None, None, None

    message_type = variant.message_type
    text = render_text(variant.text or "", contact)

    result: ProviderSendResult
    if message_type == CampaignMessageVariant.MessageType.TEXT:
        result = provider.send_text(job, variant, contact)
    elif message_type in (
        CampaignMessageVariant.MessageType.IMAGE,
        CampaignMessageVariant.MessageType.VIDEO,
        CampaignMessageVariant.MessageType.DOCUMENT,
    ):
        result = provider.send_media(job, variant, contact, mediatype=message_type, caption=text)
    else:
        return False, f"Tipo de mensagem não suportado: {message_type}", "", None, None, None

    if result.ok:
        return True, "", result.external_id, result.payload, result.http_status, None

    error_kind = result.error_kind
    if (
        provider.__class__.__name__ == "EvolutionProvider"
        and provider.get_connection_state() == "close"
    ):
        error_kind = ERROR_KIND_CHIP_OFFLINE

    return (
        False,
        result.error_message or "Falha no envio.",
        "",
        result.payload,
        result.http_status,
        error_kind,
    )
