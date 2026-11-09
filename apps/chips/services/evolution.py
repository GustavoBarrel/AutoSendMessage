"""Calls to the Evolution API (create, connect/QR and delete instance)."""

import time
from urllib.parse import urljoin

import requests

from apps.chips.services.evolution_config import mask_webhook_token
from apps.chips.validators import url_segment


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


def instance_create(url_base: str, api_key: str, instance_name: str) -> tuple[bool, str | None]:
    """
    POST /instance/create: create the instance in the Evolution API.
    Return (True, None) if it was created or already existed, (False, error_message) on error.
    """
    url_base = url_base.rstrip("/")
    url = urljoin(url_base + "/", "instance/create")
    body = {"instanceName": instance_name, "integration": "WHATSAPP-BAILEYS"}
    try:
        r = requests.post(
            url,
            headers={"apikey": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
        _log_evolution(
            "instance_create",
            url,
            request_body=body,
            response_status=r.status_code,
            response_body=r.text,
        )
        if r.status_code in (200, 201):
            return True, None
        if r.status_code == 409:
            return True, None
        try:
            resp = r.json()
            msg = resp.get("message") or resp.get("error") or r.text
        except Exception:
            msg = r.text or f"HTTP {r.status_code}"
        return False, (msg if isinstance(msg, str) else str(msg))
    except requests.RequestException as e:
        _log_evolution("instance_create", url, request_body=body, error=e)
        return False, str(e)
    except ValueError:
        _log_evolution("instance_create", url, request_body=body, error="Resposta inválida da API")
        return False, "Resposta inválida da API"


def instance_delete(url_base: str, api_key: str, instance_name: str) -> tuple[bool, str | None]:
    """
    DELETE /instance/delete/{instance}: remove the instance in the Evolution API.
    Return (True, None) on success, (False, error_message) on error.
    """
    url_base = url_base.rstrip("/")
    url = urljoin(url_base + "/", f"instance/delete/{url_segment(instance_name)}")
    try:
        r = requests.delete(
            url,
            headers={"apikey": api_key},
            timeout=15,
        )
        _log_evolution("instance_delete", url, response_status=r.status_code, response_body=r.text)
        if r.status_code in (200, 204):
            return True, None
        if r.status_code == 404:
            return True, None
        try:
            body = r.json()
            msg = body.get("message") or body.get("error") or r.text
        except Exception:
            msg = r.text or f"HTTP {r.status_code}"
        return False, (msg if isinstance(msg, str) else str(msg))
    except requests.RequestException as e:
        _log_evolution("instance_delete", url, error=e)
        return False, str(e)
    except ValueError:
        _log_evolution("instance_delete", url, error="Resposta inválida da API")
        return False, "Resposta inválida da API"


def instance_connect(url_base: str, api_key: str, instance_name: str) -> dict | None:
    """
    GET /instance/connect/{instance}
    Return a dict with pairingCode, code (QR string), count, or None on error.
    Try up to 3 times with a 2s interval (the API sometimes returns code only on the 2nd/3rd call).
    """
    url_base = url_base.rstrip("/")
    url = urljoin(url_base + "/", f"instance/connect/{url_segment(instance_name)}")
    last_response = None
    for attempt in range(3):
        try:
            r = requests.get(
                url,
                headers={"apikey": api_key},
                timeout=15,
            )
            data = r.json()
            last_response = data
            _log_evolution(
                "instance_connect", url, response_status=r.status_code, response_body=r.text
            )
            if data.get("code") or data.get("pairingCode") or data.get("base64"):
                return data
            if attempt < 2:
                time.sleep(2)
        except requests.RequestException as e:
            resp = getattr(e, "response", None)
            _log_evolution(
                "instance_connect",
                url,
                response_status=resp.status_code if resp else None,
                response_body=resp.text[:2000] if resp else None,
                error=e,
            )
            if attempt < 2:
                time.sleep(2)
            continue
        except ValueError as e:
            _log_evolution("instance_connect", url, error=e)
            last_response = None
            continue
    return last_response


def webhook_set(
    url_base: str,
    api_key: str,
    instance_name: str,
    webhook_url: str,
    events: list[str] | None = None,
) -> tuple[bool, str | None]:
    """
    POST /webhook/set/{instance}: configure the instance webhook (e.g. QRCODE_UPDATED).
    Return (True, None) on success, (False, error_message) on error.
    """
    url_base = url_base.rstrip("/")
    url = urljoin(url_base + "/", f"webhook/set/{url_segment(instance_name)}")
    if events is None:
        events = ["QRCODE_UPDATED"]
    body = {
        "webhook": {
            "enabled": True,
            "url": webhook_url,
            "webhookByEvents": False,
            "webhookBase64": True,
            "events": events,
        }
    }
    # The webhook URL carries a secret token: only a masked copy of the body may be logged.
    # The response is masked too, since Evolution echoes the configured URL back.
    log_body = {"webhook": {**body["webhook"], "url": mask_webhook_token(webhook_url)}}
    try:
        r = requests.post(
            url,
            headers={"apikey": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
        _log_evolution(
            "webhook_set",
            url,
            request_body=log_body,
            response_status=r.status_code,
            response_body=mask_webhook_token(r.text),
        )
        if r.status_code in (200, 201):
            return True, None
        try:
            resp = r.json()
            msg = resp.get("message") or resp.get("error") or r.text
        except Exception:
            msg = r.text or f"HTTP {r.status_code}"
        return False, (msg if isinstance(msg, str) else str(msg))
    except requests.RequestException as e:
        _log_evolution("webhook_set", url, request_body=log_body, error=e)
        return False, str(e)
    except ValueError:
        _log_evolution("webhook_set", url, request_body=log_body, error="Resposta inválida da API")
        return False, "Resposta inválida da API"


def connection_state(url_base: str, api_key: str, instance_name: str) -> str | None:
    """
    GET /instance/connectionState/{instance}
    Return 'open' (connected, QR scanned), 'close' (disconnected) or None on error.
    """
    url_base = url_base.rstrip("/")
    url = urljoin(url_base + "/", f"instance/connectionState/{url_segment(instance_name)}")
    try:
        r = requests.get(
            url,
            headers={"apikey": api_key},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        inst = data.get("instance") or data
        state = (inst.get("state") or data.get("state") or "").lower()
        return state if state in ("open", "close") else None
    except Exception:
        return None
