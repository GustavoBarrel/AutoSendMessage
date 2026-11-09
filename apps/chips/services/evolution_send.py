"""Send WhatsApp messages through Evolution API v2 (sendText / sendMedia)."""

from __future__ import annotations

import mimetypes
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from apps.chips.services.evolution_whatsapp_errors import (
    MSG_WHATSAPP_463,
    payload_contains_whatsapp_error_463,
    record_indicates_whatsapp_rejection,
)
from apps.chips.validators import url_segment

# The 463 / ERROR usually shows up ~1-2s after HTTP 201 (asynchronous update in Evolution).
_PROBE_463_ATTEMPTS = 3
_PROBE_463_INTERVAL_S = 1.0


def _log_evolution(
    call_type: str,
    url: str,
    request_body=None,
    response_status=None,
    response_body=None,
    error=None,
):
    try:
        from apps.chips.evolution_debug import log_evolution_call

        log_evolution_call(
            call_type,
            url,
            request_body=request_body,
            response_status=response_status,
            response_body=response_body,
            error=error,
        )
    except Exception:
        pass


def format_whatsapp_number_br(mobile_phone: str) -> str:
    """
    Mobile phone with 11 digits (area code + number) → international format 55...
    Already starting with 55 and 13 digits → keep as is.
    """
    raw = re.sub(r"\D", "", str(mobile_phone or ""))
    if len(raw) == 11:
        return f"55{raw}"
    if len(raw) == 12 and raw.startswith("55"):
        return raw
    if len(raw) == 13 and raw.startswith("55"):
        return raw
    return raw


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Content-Type": "application/json"}


def _extract_message_id(data: dict[str, Any]) -> str:
    key = data.get("key") or {}
    mid = key.get("id")
    if mid:
        return str(mid)
    return ""


def _extract_remote_jid(data: dict[str, Any]) -> str:
    key = data.get("key") or {}
    return str(key.get("remoteJid") or "")


def _fetch_message_delivery_records(
    url_base: str,
    api_key: str,
    instance_name: str,
    message_id: str,
) -> list[dict[str, Any]]:
    """Query findMessages and findStatusMessage filtering by the message ID."""
    url_base = url_base.rstrip("/")
    headers = _headers(api_key)
    mid = (message_id or "").strip()
    if not mid:
        return []

    records: list[dict[str, Any]] = []

    find_messages_url = urljoin(url_base + "/", f"chat/findMessages/{url_segment(instance_name)}")
    body = {"where": {"key": {"id": mid}}}
    try:
        r = requests.post(find_messages_url, headers=headers, json=body, timeout=10)
        _log_evolution(
            "message_delivery_check",
            find_messages_url,
            request_body=body,
            response_status=r.status_code,
            response_body=r.text[:4000] if r.text else None,
        )
        if r.status_code == 200:
            messages = r.json().get("messages") or {}
            if isinstance(messages, dict):
                for rec in messages.get("records", []):
                    if not isinstance(rec, dict):
                        continue
                    key = rec.get("key") or {}
                    if str(key.get("id") or "") == mid:
                        records.append(rec)
    except requests.RequestException as e:
        _log_evolution("message_delivery_check", find_messages_url, request_body=body, error=e)

    find_status_url = urljoin(
        url_base + "/", f"chat/findStatusMessage/{url_segment(instance_name)}"
    )
    try:
        r = requests.post(find_status_url, headers=headers, json=body, timeout=10)
        _log_evolution(
            "message_delivery_check",
            find_status_url,
            request_body=body,
            response_status=r.status_code,
            response_body=r.text[:4000] if r.text else None,
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and str(item.get("keyId") or "") == mid:
                        records.append(item)
    except requests.RequestException as e:
        _log_evolution("message_delivery_check", find_status_url, request_body=body, error=e)

    return records


def _probe_whatsapp_error_463(
    url_base: str,
    api_key: str,
    instance_name: str,
    message_id: str,
    *,
    remote_jid: str = "",
) -> bool:
    """Light poll after HTTP 201: detect the ERROR/463 that Evolution records asynchronously."""
    mid = (message_id or "").strip()
    if not mid:
        return False
    del (
        remote_jid
    )  # findMessages only uses the id; an alternative jid (@lid) is not part of the REST filter.

    for attempt in range(_PROBE_463_ATTEMPTS):
        if attempt > 0:
            time.sleep(_PROBE_463_INTERVAL_S)
        for record in _fetch_message_delivery_records(url_base, api_key, instance_name, mid):
            if record_indicates_whatsapp_rejection(record, message_id=mid):
                return True
    return False


def _check_evolution_body_error(data: dict[str, Any]) -> str | None:
    """
    Detect error responses disguised as HTTP 200/201.
    Only checks explicit error indicators in the body; it does not assume a specific
    success response format, to stay compatible with different Evolution API versions.
    """
    if payload_contains_whatsapp_error_463(data):
        return MSG_WHATSAPP_463
    error = data.get("error")
    if error and isinstance(error, str):
        return error[:2000]
    if data.get("status") == "error":
        return str(data.get("message") or "Erro desconhecido da Evolution")[:2000]
    try:
        stack: list[Any] = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if cur.get("exists") is False:
                    return "Número não está no WhatsApp (exists=false)."
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    except Exception:
        pass
    return None


def _finalize_send_result(
    url_base: str,
    api_key: str,
    instance_name: str,
    data: dict[str, Any],
    http_status: int,
) -> EvolutionSendResult:
    body_err = _check_evolution_body_error(data)
    if body_err:
        return EvolutionSendResult(False, body_err, http_status, "", data)

    message_id = _extract_message_id(data)
    if message_id and _probe_whatsapp_error_463(
        url_base,
        api_key,
        instance_name,
        message_id,
        remote_jid=_extract_remote_jid(data),
    ):
        return EvolutionSendResult(False, MSG_WHATSAPP_463, http_status, message_id, data)

    return EvolutionSendResult(
        True,
        "",
        http_status,
        message_id,
        data,
    )


def _parse_evolution_error(r) -> str:
    """
    Extract the error message from an Evolution API HTTP error response.
    Include the full body when the JSON message is generic ("Bad Request").
    """
    raw = (r.text or "")[:2000]
    try:
        err_data = r.json()
        msg_val = err_data.get("message")
        if isinstance(msg_val, list):
            msg = "; ".join(str(x) for x in msg_val)
        else:
            msg = str(msg_val or err_data.get("error") or "").strip()
    except Exception:
        msg = ""
    if not msg or msg.lower() in ("bad request", "error", "internal server error"):
        return f"HTTP {r.status_code} — {raw}" if raw else f"HTTP {r.status_code}"
    if raw and raw not in msg and len(msg) < 200:
        return f"{msg} | corpo: {raw[:400]}"
    return msg[:2000]


@dataclass(frozen=True)
class EvolutionSendResult:
    ok: bool
    error_message: str
    http_status: int | None
    message_id: str
    raw: dict[str, Any] | None


def send_text(
    url_base: str,
    api_key: str,
    instance_name: str,
    number: str,
    text: str,
    *,
    timeout: int = 60,
) -> EvolutionSendResult:
    url_base = url_base.rstrip("/")
    url = urljoin(url_base + "/", f"message/sendText/{url_segment(instance_name)}")
    body = {"number": number, "text": text}
    try:
        r = requests.post(
            url,
            headers=_headers(api_key),
            json=body,
            timeout=timeout,
        )
        _log_evolution(
            "message_sendText",
            url,
            request_body=body,
            response_status=r.status_code,
            response_body=r.text[:8000] if r.text else None,
        )
        if r.status_code in (200, 201):
            try:
                data = r.json()
            except ValueError:
                return EvolutionSendResult(
                    False,
                    "Resposta JSON inválida da Evolution",
                    r.status_code,
                    "",
                    None,
                )
            return _finalize_send_result(url_base, api_key, instance_name, data, r.status_code)
        msg = _parse_evolution_error(r)
        return EvolutionSendResult(False, msg, r.status_code, "", None)
    except requests.Timeout as e:
        _log_evolution("message_sendText", url, request_body=body, error=e)
        return EvolutionSendResult(False, f"Timeout após {timeout}s", None, "", None)
    except requests.RequestException as e:
        _log_evolution("message_sendText", url, request_body=body, error=e)
        return EvolutionSendResult(False, str(e)[:2000], None, "", None)


def _guess_mimetype(url: str, mediatype: str) -> str:
    path = urlparse(url).path
    guessed, _ = mimetypes.guess_type(path)
    if guessed:
        return guessed
    if mediatype == "video":
        return "video/mp4"
    return "image/jpeg"


def _safe_filename(url: str, mediatype: str) -> str:
    path = urlparse(url).path
    name = path.rstrip("/").split("/")[-1] or ""
    if name and "." in name:
        return name[:200]
    return "video.mp4" if mediatype == "video" else "image.jpg"


_MAX_BASE64_MEDIA_CHARS = 28_000_000


def send_media(
    url_base: str,
    api_key: str,
    instance_name: str,
    number: str,
    *,
    mediatype: str,
    media_url: str,
    caption: str,
    timeout: int | tuple[float, float] | None = None,
) -> EvolutionSendResult:
    url_base = url_base.rstrip("/")
    url = urljoin(url_base + "/", f"message/sendMedia/{url_segment(instance_name)}")
    mt = mediatype.lower()
    if mt not in ("image", "video"):
        return EvolutionSendResult(
            False,
            f"mediatype inválido: {mediatype}",
            None,
            "",
            None,
        )
    mime = _guess_mimetype(media_url, mt)
    file_name = _safe_filename(media_url, mt)
    media_payload = media_url
    if len(media_payload) > _MAX_BASE64_MEDIA_CHARS:
        return EvolutionSendResult(
            False,
            "Arquivo de mídia muito grande para envio neste formato. "
            "Para vídeo, use uma URL pública (HTTPS) acessível pela Evolution ou reduza o arquivo. "
            f"Limite aproximado: {_MAX_BASE64_MEDIA_CHARS // 1_000_000}M caracteres no payload.",
            None,
            "",
            None,
        )
    body = {
        "number": number,
        "mediatype": mt,
        "mimetype": mime,
        "caption": caption or "",
        "media": media_payload,
        "fileName": file_name,
    }
    if timeout is None:
        timeout = (30.0, 600.0) if mt == "video" else (15.0, 120.0)
    elif isinstance(timeout, (int, float)):
        timeout = (15.0, float(timeout))

    _log_body = {
        **body,
        "media": (media_payload[:120] + "…") if len(media_payload) > 200 else body["media"],
    }

    max_attempts = 3 if mt == "video" else 1
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(
                url,
                headers=_headers(api_key),
                json=body,
                timeout=timeout,
            )
            _log_evolution(
                "message_sendMedia",
                url,
                request_body=_log_body,
                response_status=r.status_code,
                response_body=r.text[:8000] if r.text else None,
            )
            if r.status_code in (200, 201):
                try:
                    data = r.json()
                except ValueError:
                    return EvolutionSendResult(
                        False,
                        "Resposta JSON inválida da Evolution",
                        r.status_code,
                        "",
                        None,
                    )
                return _finalize_send_result(url_base, api_key, instance_name, data, r.status_code)
            msg = _parse_evolution_error(r)
            return EvolutionSendResult(False, msg, r.status_code, "", None)
        except requests.Timeout as e:
            _log_evolution("message_sendMedia", url, request_body=_log_body, error=e)
            read_s = timeout[1] if isinstance(timeout, tuple) else timeout
            return EvolutionSendResult(
                False,
                f"Timeout ao enviar mídia (limite ~{read_s}s). Vídeo grande ou Evolution lenta — "
                f"tente URL pública ou arquivo menor. Detalhe: {e!s}"[:2000],
                None,
                "",
                None,
            )
        except requests.RequestException as e:
            _log_evolution("message_sendMedia", url, request_body=_log_body, error=e)
            if attempt < max_attempts and mt == "video":
                time.sleep(1.5 * attempt)
                continue
            hint = ""
            if mt == "video":
                hint = (
                    " Confirme se a Evolution API está rodando (ex.: localhost:8080). "
                    "Vídeos em base64 são pesados: prefira URL HTTPS acessível pelo servidor da "
                    "Evolution."
                )
            return EvolutionSendResult(False, (str(e)[:1800] + hint)[:2000], None, "", None)

    return EvolutionSendResult(False, "Falha ao enviar mídia.", None, "", None)
