"""
Meta provider: send messages through the Meta Graph API (official WhatsApp).
Supports the Cloud API and on-premises.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import requests

from apps.campaigns.services.message_utils import render_text
from apps.chips.providers.base import ProviderSendResult, WhatsAppProvider
from apps.chips.validators import META_API_VERSION_RE, META_PHONE_NUMBER_ID_RE

if TYPE_CHECKING:
    from apps.campaigns.models import CampaignMessageVariant, CampaignSendJob


class MetaProvider(WhatsAppProvider):
    """Send messages through the Meta WhatsApp Business API."""

    def __init__(self, chip):
        self._chip = chip

    def _messages_url(self, phone_number_id: str) -> str | None:
        """Build the Graph API URL, or None if the chip values are not safe to put in a URL.

        Both values are free text in the database; validating them here keeps the request pinned
        to graph.facebook.com even for rows saved before the form validators existed.
        """
        version = (self._chip.meta_api_version or "v18.0").strip()
        if not META_API_VERSION_RE.fullmatch(version):
            return None
        if not META_PHONE_NUMBER_ID_RE.fullmatch(phone_number_id):
            return None
        return f"https://graph.facebook.com/{version}/{phone_number_id}/messages"

    def _phone_number_id(self) -> str:
        return (self._chip.meta_phone_number_id or "").strip()

    def _access_token(self) -> str:
        return (self._chip.meta_access_token or "").strip()

    def _headers(self) -> dict:
        token = self._access_token()
        if not token:
            raise ValueError("Meta access token não configurado.")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _send_request(self, payload: dict) -> ProviderSendResult:
        pn_id = self._phone_number_id()
        if not pn_id:
            return ProviderSendResult(
                ok=False,
                error_message="Meta Phone Number ID não configurado no chip.",
                error_kind=None,
            )
        url = self._messages_url(pn_id)
        if url is None:
            return ProviderSendResult(
                ok=False,
                error_message=(
                    "Phone Number ID ou versão da API Meta inválidos no chip. Corrija o cadastro "
                    "do chip."
                ),
                error_kind="permanent_config",
            )
        try:
            r = requests.post(url, headers=self._headers(), json=payload, timeout=60)
            data = {}
            try:
                data = r.json()
            except Exception:
                pass
            if r.status_code in (200, 201):
                mid = ""
                try:
                    mid = ((data.get("messages") or [{}])[0]).get("id", "")
                except Exception:
                    mid = ""
                return ProviderSendResult(
                    ok=True,
                    external_id=mid,
                    payload=data,
                    http_status=r.status_code,
                    error_kind=None,
                )
            err_msg = self._parse_error(r, data)
            error_kind = self._classify_error(r.status_code, data)
            return ProviderSendResult(
                ok=False,
                error_message=err_msg,
                payload=data,
                http_status=r.status_code,
                error_kind=error_kind,
            )
        except requests.Timeout:
            return ProviderSendResult(
                ok=False,
                error_message="Timeout ao enviar mensagem pela Meta API (60s).",
                error_kind="timeout",
            )
        except requests.RequestException as e:
            return ProviderSendResult(
                ok=False,
                error_message=f"Erro de conexão com Meta API: {e}",
                error_kind="connection",
            )

    def _parse_error(self, r, data: dict) -> str:
        raw = (r.text or "")[:2000]
        try:
            error = data.get("error", {})
            if isinstance(error, dict):
                msg = error.get("message") or raw
                code = error.get("code")
                subcode = error.get("error_subcode")
                if code and subcode:
                    return f"[{code}/{subcode}] {msg}"
                if code:
                    return f"[{code}] {msg}"
                return str(msg)
        except Exception:
            pass
        return f"HTTP {r.status_code} — {raw}" if raw else f"HTTP {r.status_code}"

    def _classify_error(self, status_code: int, data: dict) -> str | None:
        """Classify an error to decide whether it is permanent or temporary."""
        if status_code in (401, 403):
            return "permanent_config"
        if status_code == 429:
            return "rate_limit"
        if status_code >= 500:
            return "server_error"
        try:
            error = data.get("error", {})
            subcode = error.get("error_subcode")
            code = error.get("code")
            # Number is not on WhatsApp / invalid
            if subcode in (131051, 131026):
                return "permanent_contact"
            # Template not approved
            if subcode in (131047, 131056):
                return "permanent_template"
            # Blocked for unsolicited sending / similar timelock
            if subcode in (131042, 131043, 131044):
                return "permanent_contact"
            # Missing permission / resource unavailable
            if subcode in (131049, 131050):
                return "permanent_config"
            # Rate limit for the number
            if code == 130472:
                return "rate_limit"
            # Generic payload error
            if code == 100 and subcode in (33, 35):
                return "temporary"
        except Exception:
            pass
        return "temporary"

    def send_text(
        self,
        job: CampaignSendJob,
        variant: CampaignMessageVariant,
        contact,
    ) -> ProviderSendResult:
        text = render_text(variant.text or "", contact)
        if not text.strip():
            return ProviderSendResult(
                ok=False,
                error_message="Texto da mensagem vazio.",
                error_kind=None,
            )
        payload = {
            "messaging_product": "whatsapp",
            "to": self.format_number(contact.mobile_phone),
            "type": "text",
            "text": {"body": text},
        }
        return self._send_request(payload)

    def send_media(
        self,
        job: CampaignSendJob,
        variant: CampaignMessageVariant,
        contact,
        mediatype: str,
        caption: str = "",
    ) -> ProviderSendResult:
        raw_url = (variant.media_url or "").strip()
        if not raw_url:
            return ProviderSendResult(
                ok=False,
                error_message=f"URL de {mediatype} não informada.",
                error_kind=None,
            )
        mt = mediatype.lower()
        if mt not in ("image", "video", "document"):
            return ProviderSendResult(
                ok=False,
                error_message=f"Tipo de mídia não suportado pela Meta: {mediatype}",
                error_kind=None,
            )
        # The Meta API accepts a public HTTPS link or a media ID (previous upload).
        # Here we use an HTTPS link, which also works for a local server if Meta can reach its host.
        # Base64 sends would need an upload to /media first.
        media_key = "link" if raw_url.startswith(("http://", "https://")) else "id"
        payload = {
            "messaging_product": "whatsapp",
            "to": self.format_number(contact.mobile_phone),
            "type": mt,
            mt: {media_key: raw_url},
        }
        if caption:
            payload[mt]["caption"] = caption
        return self._send_request(payload)

    def get_connection_state(self) -> str:
        # The Meta API has no "connection state" like Evolution does.
        # If the credentials are filled in, we treat the chip as "open".
        if self._phone_number_id() and self._access_token():
            return "open"
        return None

    def format_number(self, mobile_phone: str) -> str:
        """Format to the international standard 55..."""
        raw = re.sub(r"\D", "", str(mobile_phone or ""))
        if len(raw) == 11 and not raw.startswith("55"):
            return f"55{raw}"
        if len(raw) == 12 and raw.startswith("55"):
            return raw
        if len(raw) == 13 and raw.startswith("55"):
            return raw
        return raw
