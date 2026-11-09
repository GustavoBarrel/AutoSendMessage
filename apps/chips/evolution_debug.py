"""
Log of the latest Evolution API and webhook requests/responses,
for debugging (see /debug/evolution/).
"""

import json

from django.core.cache import cache

KEY = "evolution_debug_log"
MAX_ENTRIES = 20


def _truncate(obj, max_len=1500):
    s = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    if len(s) > max_len:
        return s[:max_len] + "...[truncado]"
    return s


def log_evolution_call(
    call_type: str,
    url: str,
    *,
    request_body=None,
    response_status=None,
    response_body=None,
    error=None,
):
    entry = {
        "type": call_type,
        "url": url,
        "request_body": _truncate(request_body) if request_body is not None else None,
        "response_status": response_status,
        "response_body": _truncate(response_body) if response_body is not None else None,
        "error": str(error)[:500] if error else None,
    }
    try:
        import datetime

        entry["time"] = datetime.datetime.now().isoformat()
    except Exception:
        entry["time"] = ""
    log = cache.get(KEY) or []
    log.append(entry)
    cache.set(KEY, log[-MAX_ENTRIES:], 3600)


def get_log():
    return cache.get(KEY) or []


def clear_log():
    cache.delete(KEY)
