"""
Simulated provider: demo/test only. It never touches the network; every send "succeeds".
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

from apps.campaigns.services.message_utils import render_text
from apps.chips.providers.base import ProviderSendResult, WhatsAppProvider

if TYPE_CHECKING:
    from apps.campaigns.models import CampaignMessageVariant, CampaignSendJob


class SimulatedProvider(WhatsAppProvider):
    """Pretend to send messages, so the whole campaign flow can run without a real chip."""

    def __init__(self, chip):
        self._chip = chip

    def send_text(
        self,
        job: CampaignSendJob,
        variant: CampaignMessageVariant,
        contact,
    ) -> ProviderSendResult:
        text = render_text(variant.text or "", contact)
        if not text.strip():
            return ProviderSendResult(ok=False, error_message="Texto da mensagem vazio.")
        return self._fake_ok(contact, kind="text")

    def send_media(
        self,
        job: CampaignSendJob,
        variant: CampaignMessageVariant,
        contact,
        mediatype: str,
        caption: str = "",
    ) -> ProviderSendResult:
        if not (variant.media_url or "").strip():
            return ProviderSendResult(
                ok=False,
                error_message=f"URL de {mediatype} não informada.",
            )
        return self._fake_ok(contact, kind=mediatype)

    def get_connection_state(self) -> str:
        return "open"

    def format_number(self, mobile_phone: str) -> str:
        return re.sub(r"\D", "", str(mobile_phone or ""))

    def _fake_ok(self, contact, *, kind: str) -> ProviderSendResult:
        return ProviderSendResult(
            ok=True,
            external_id=f"sim-{uuid.uuid4().hex}",
            payload={
                "simulated": True,
                "type": kind,
                "to": self.format_number(contact.mobile_phone),
            },
            http_status=200,
        )
