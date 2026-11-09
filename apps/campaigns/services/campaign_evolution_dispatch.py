"""Campaign send through Evolution: uses settings + Chip.instance_name + Contact."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings

from apps.campaigns.models import CampaignMessageVariant
from apps.campaigns.services.campaign_chip_offline import ERROR_KIND_CHIP_OFFLINE
from apps.campaigns.services.message_utils import (
    media_unreachable_message,
    render_text,
    resolve_media,
)
from apps.chips.services.evolution import connection_state
from apps.chips.services.evolution_send import (
    EvolutionSendResult,
    format_whatsapp_number_br,
    send_media,
    send_text,
)

if TYPE_CHECKING:
    from apps.campaigns.models import CampaignSendJob


def dispatch_campaign_message(
    job: CampaignSendJob,
    variant: CampaignMessageVariant,
) -> tuple[bool, str, str, dict | None, int | None, str | None]:
    """
    Return (success, error, external_message_id, provider_payload, http_status, error_kind).

    error_kind is ``chip_offline`` when connectionState=close; otherwise None.
    """
    base = (settings.EVOLUTION_URL_BASE or "").strip()
    key = (settings.EVOLUTION_API_KEY or "").strip()
    if not base or not key:
        return (
            False,
            "Evolution não configurada: defina EVOLUTION_URL_BASE e EVOLUTION_API_KEY no .env.",
            "",
            None,
            None,
            None,
        )

    chip = job.chip
    instance = chip.instance_name
    number = format_whatsapp_number_br(job.contact.mobile_phone)

    state = connection_state(base, key, instance)
    if state == "close":
        return (
            False,
            "Instância WhatsApp desconectada na Evolution (connectionState=close). Reconecte o "
            "chip.",
            "",
            None,
            None,
            ERROR_KIND_CHIP_OFFLINE,
        )

    message_type = variant.message_type
    text = render_text(variant.text or "", job.contact)

    result: EvolutionSendResult
    if message_type == CampaignMessageVariant.MessageType.TEXT:
        if not text.strip():
            return False, "Texto da mensagem vazio.", "", None, None, None
        result = send_text(base, key, instance, number, text)
    elif message_type in (
        CampaignMessageVariant.MessageType.IMAGE,
        CampaignMessageVariant.MessageType.VIDEO,
        CampaignMessageVariant.MessageType.DOCUMENT,
    ):
        raw_url = (variant.media_url or "").strip()
        if not raw_url:
            return False, "URL da mídia não informada.", "", None, None, None
        resolved = resolve_media(raw_url)
        unreachable = media_unreachable_message(resolved)
        if unreachable:
            return False, unreachable, "", None, None, None
        media_type = (
            "image"
            if message_type == CampaignMessageVariant.MessageType.IMAGE
            else (
                "video" if message_type == CampaignMessageVariant.MessageType.VIDEO else "document"
            )
        )
        result = send_media(
            base,
            key,
            instance,
            number,
            mediatype=media_type,
            media_url=resolved,
            caption=text,
        )
    else:
        return False, f"Tipo de mensagem não suportado: {message_type}", "", None, None, None

    payload = result.raw
    if result.ok:
        return True, "", result.message_id, payload, result.http_status, None
    return (
        False,
        result.error_message or "Falha no envio pela Evolution.",
        "",
        payload,
        result.http_status,
        None,
    )
