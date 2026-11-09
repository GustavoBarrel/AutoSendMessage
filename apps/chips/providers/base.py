"""Abstract WhatsApp provider interface and normalized result."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.campaigns.models import CampaignMessageVariant, CampaignSendJob


@dataclass
class ProviderSendResult:
    """Normalized result of a send, independent of the provider."""

    ok: bool
    error_message: str = ""
    external_id: str = ""
    payload: dict | None = None
    http_status: int | None = None
    error_kind: str | None = None  # e.g.: chip_offline, permanent_contact, timeout ...


class WhatsAppProvider(ABC):
    """Contract that every WhatsApp send provider must implement."""

    @abstractmethod
    def send_text(
        self,
        job: CampaignSendJob,
        variant: CampaignMessageVariant,
        contact,
    ) -> ProviderSendResult:
        """Send a text message."""

    @abstractmethod
    def send_media(
        self,
        job: CampaignSendJob,
        variant: CampaignMessageVariant,
        contact,
        mediatype: str,
        caption: str = "",
    ) -> ProviderSendResult:
        """Send an image, video or document."""

    @abstractmethod
    def get_connection_state(self) -> str:
        """Return 'open', 'close' or None."""

    @abstractmethod
    def format_number(self, mobile_phone: str) -> str:
        """Format the mobile phone to the standard required by the provider."""
