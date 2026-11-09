"""
Evolution provider: wrapper around the existing services.
No behavior change; it only adapts them to the provider interface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.campaigns.services.campaign_chip_offline import ERROR_KIND_CHIP_OFFLINE
from apps.campaigns.services.message_utils import (
    media_unreachable_message,
    render_text,
    resolve_media,
)
from apps.chips.providers.base import ProviderSendResult, WhatsAppProvider
from apps.chips.services.evolution import connection_state
from apps.chips.services.evolution_send import (
    EvolutionSendResult,
    format_whatsapp_number_br,
    send_media,
    send_text,
)

if TYPE_CHECKING:
    from apps.campaigns.models import CampaignMessageVariant, CampaignSendJob


class EvolutionProvider(WhatsAppProvider):
    """Send messages through the Evolution API (current behavior, unchanged)."""

    def __init__(self, chip):
        self._chip = chip

    def send_text(
        self,
        job: CampaignSendJob,
        variant: CampaignMessageVariant,
        contact,
    ) -> ProviderSendResult:
        from django.conf import settings

        base = (settings.EVOLUTION_URL_BASE or "").strip()
        key = (settings.EVOLUTION_API_KEY or "").strip()
        if not base or not key:
            return ProviderSendResult(
                ok=False,
                error_message=(
                    "Evolution não configurada: defina EVOLUTION_URL_BASE e EVOLUTION_API_KEY no "
                    ".env."
                ),
                error_kind=None,
            )

        instance = self._chip.instance_name
        if not instance:
            return ProviderSendResult(
                ok=False,
                error_message="Instância Evolution não configurada no chip.",
                error_kind=None,
            )

        state = connection_state(base, key, instance)
        if state == "close":
            return ProviderSendResult(
                ok=False,
                error_message=(
                    "Instância WhatsApp desconectada na Evolution (connectionState=close). "
                    "Reconecte o chip."
                ),
                error_kind=ERROR_KIND_CHIP_OFFLINE,
            )

        number = self.format_number(contact.mobile_phone)
        text = render_text(variant.text or "", contact)
        if not text.strip():
            return ProviderSendResult(
                ok=False,
                error_message="Texto da mensagem vazio.",
                error_kind=None,
            )
        result: EvolutionSendResult = send_text(base, key, instance, number, text)
        return ProviderSendResult(
            ok=result.ok,
            error_message=result.error_message or "",
            external_id=result.message_id,
            payload=result.raw,
            http_status=result.http_status,
            error_kind=None,
        )

    def send_media(
        self,
        job: CampaignSendJob,
        variant: CampaignMessageVariant,
        contact,
        mediatype: str,
        caption: str = "",
    ) -> ProviderSendResult:
        from django.conf import settings

        base = (settings.EVOLUTION_URL_BASE or "").strip()
        key = (settings.EVOLUTION_API_KEY or "").strip()
        if not base or not key:
            return ProviderSendResult(
                ok=False,
                error_message=(
                    "Evolution não configurada: defina EVOLUTION_URL_BASE e EVOLUTION_API_KEY no "
                    ".env."
                ),
                error_kind=None,
            )

        instance = self._chip.instance_name
        if not instance:
            return ProviderSendResult(
                ok=False,
                error_message="Instância Evolution não configurada no chip.",
                error_kind=None,
            )

        state = connection_state(base, key, instance)
        if state == "close":
            return ProviderSendResult(
                ok=False,
                error_message=(
                    "Instância WhatsApp desconectada na Evolution (connectionState=close). "
                    "Reconecte o chip."
                ),
                error_kind=ERROR_KIND_CHIP_OFFLINE,
            )

        raw_url = (variant.media_url or "").strip()
        if not raw_url:
            return ProviderSendResult(
                ok=False,
                error_message=f"URL de {mediatype} não informada.",
                error_kind=None,
            )
        resolved = resolve_media(raw_url)
        unreachable = media_unreachable_message(resolved)
        if unreachable:
            return ProviderSendResult(
                ok=False,
                error_message=unreachable,
                error_kind=None,
            )
        number = self.format_number(contact.mobile_phone)
        result: EvolutionSendResult = send_media(
            base,
            key,
            instance,
            number,
            mediatype=mediatype,
            media_url=resolved,
            caption=caption,
        )
        return ProviderSendResult(
            ok=result.ok,
            error_message=result.error_message or "",
            external_id=result.message_id,
            payload=result.raw,
            http_status=result.http_status,
            error_kind=None,
        )

    def get_connection_state(self) -> str:
        from django.conf import settings

        base = (settings.EVOLUTION_URL_BASE or "").strip()
        key = (settings.EVOLUTION_API_KEY or "").strip()
        if not base or not key:
            return None
        instance = self._chip.instance_name
        if not instance:
            return None
        return connection_state(base, key, instance)

    def format_number(self, mobile_phone: str) -> str:
        return format_whatsapp_number_br(mobile_phone)
