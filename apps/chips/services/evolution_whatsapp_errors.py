"""Mapping of WhatsApp/Evolution errors to user-facing messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ERROR_CODE_WHATSAPP_463 = "whatsapp_463"

MSG_WHATSAPP_463 = (
    "WhatsApp bloqueou o envio via API (erro 463 — timelock do chip). "
    "Aguarde 24–48h sem enviar ou use outro chip."
)

MSG_WHATSAPP_463_SHORT = "Não enviado — erro 463 (timelock WhatsApp)"


@dataclass(frozen=True)
class SendErrorDisplay:
    code: str
    title: str
    detail: str
    not_sent: bool


def _stubs_contain_463(stubs: Any) -> bool:
    if not stubs:
        return False
    if isinstance(stubs, (list, tuple)):
        return any(str(x).strip() == "463" for x in stubs)
    return str(stubs).strip() == "463"


def _is_error_status_value(status: Any) -> bool:
    if status is None:
        return False
    normalized = str(status).strip().upper()
    return normalized in {"0", "ERROR", "FAILED"}


def record_indicates_whatsapp_rejection(record: dict[str, Any], *, message_id: str = "") -> bool:
    """
    Tell whether WhatsApp rejected an outbound send.

    Evolution only persists ``messageStubParameters: ["463"]`` in real time (logs/webhook).
    REST queries (findMessages / findStatusMessage) usually show just
    ``status: ERROR``; we treat both as a delivery failure (463 / tcToken).
    """
    if not isinstance(record, dict):
        return False
    mid = (message_id or "").strip()
    if mid:
        key = record.get("key") or {}
        key_id = str(record.get("keyId") or key.get("id") or "")
        if key_id and key_id != mid:
            return False
    if _stubs_contain_463(record.get("messageStubParameters")):
        return True
    if _is_error_status_value(record.get("status")):
        return True
    updates = record.get("MessageUpdate")
    if isinstance(updates, list):
        for upd in updates:
            if isinstance(upd, dict) and _is_error_status_value(upd.get("status")):
                return True
    return False


def payload_contains_whatsapp_error_463(payload: Any) -> bool:
    if payload is None:
        return False
    try:
        if isinstance(payload, dict):
            messages = payload.get("messages")
            if isinstance(messages, dict):
                for rec in messages.get("records", []):
                    if isinstance(rec, dict) and record_indicates_whatsapp_rejection(rec):
                        return True
            if record_indicates_whatsapp_rejection(payload):
                return True
        stack: list[Any] = [payload]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if record_indicates_whatsapp_rejection(cur):
                    return True
                stack.extend(cur.values())
            elif isinstance(cur, list):
                for item in cur:
                    if isinstance(item, str) and item.strip() == "463":
                        return True
                    stack.append(item)
            elif isinstance(cur, str) and cur.strip() == "463":
                return True
    except Exception:
        return False
    return False


def error_text_indicates_whatsapp_463(error_text: str) -> bool:
    t = (error_text or "").lower()
    return (
        "erro 463" in t
        or "whatsapp 463" in t
        or "timelock" in t
        or "tctoken" in t
        or "restrição de alcance" in t
        or "restricao de alcance" in t
    )


def is_whatsapp_463_error(error_text: str = "", payload: Any = None) -> bool:
    return error_text_indicates_whatsapp_463(error_text) or payload_contains_whatsapp_error_463(
        payload
    )


def get_send_error_display(
    error_text: str = "",
    payload: Any = None,
    *,
    success: bool = False,
) -> SendErrorDisplay | None:
    """Return metadata for display in the send history."""
    if success:
        return None
    if is_whatsapp_463_error(error_text, payload):
        return SendErrorDisplay(
            code=ERROR_CODE_WHATSAPP_463,
            title=MSG_WHATSAPP_463_SHORT,
            detail="API bloqueada pelo WhatsApp. Aguarde ou troque o chip.",
            not_sent=True,
        )
    return None
