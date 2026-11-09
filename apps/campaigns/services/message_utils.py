"""
Utilities shared by providers and dispatch:
rendering of message placeholders and resolution of local media.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}", re.IGNORECASE)
_MAX_LOCAL_MEDIA_BASE64_BYTES = 20 * 1024 * 1024  # 20 MB


def render_text(template: str, contact) -> str:
    """Replace {nome}, {cidade}, {primeiro_nome}, etc. in the message text.

    The placeholders typed by the user are in Portuguese; the contact attributes are in English.
    """
    full_name = (getattr(contact, "name", None) or "").strip()
    name_parts = full_name.split()
    first_name = name_parts[0] if name_parts else "amigo(a)"
    last_name = name_parts[-1] if len(name_parts) > 1 else first_name
    values = {
        "nome": full_name or "amigo(a)",
        "nome_completo": full_name or "amigo(a)",
        "primeiro_nome": first_name,
        "ultimo_nome": last_name,
        "celular": getattr(contact, "mobile_phone", "") or "",
        "cidade": getattr(contact, "city", "") or "",
        "bairro": getattr(contact, "neighborhood", "") or "",
        "uf": getattr(contact, "state", "") or "",
        "categoria": getattr(contact, "category", "") or "",
        "subcategoria": getattr(contact, "subcategory", "") or "",
        "sexo": getattr(contact, "gender", "") or "",
        "email": getattr(contact, "email", "") or "",
    }

    def _replace(match: re.Match) -> str:
        key = match.group(1).lower()
        return values.get(key, match.group(0))

    return _PLACEHOLDER.sub(_replace, template)


def _local_media_path(media_url: str) -> Path | None:
    """Map a MEDIA_URL-based URL to a file under MEDIA_ROOT, or None if it is not one.

    The URL is free text typed by the user, so the resolved path must stay inside MEDIA_ROOT:
    without that check ``/media/../../.env`` would read arbitrary server files.
    """
    media_prefix = (getattr(settings, "MEDIA_URL", "/media/") or "/media/").rstrip("/")
    path_part = urlparse(media_url).path or ""
    if not path_part.startswith(f"{media_prefix}/"):
        return None
    relative = path_part[len(media_prefix) :].lstrip("/")
    media_root = Path(settings.MEDIA_ROOT).resolve()
    file_path = (media_root / relative).resolve()
    if not file_path.is_relative_to(media_root):
        logger.warning("Media URL escapes MEDIA_ROOT and was ignored: %r", media_url)
        return None
    return file_path


def resolve_media(media_url: str) -> str:
    """
    If the URL points to a local file (MEDIA_ROOT/MEDIA_URL), read the file
    and return its content as plain base64 (for the Evolution API).
    Return the original URL if it is not a local file, the file does not exist, it is too
    large, or the path points outside MEDIA_ROOT.
    """
    if not media_url:
        return media_url
    file_path = _local_media_path(media_url)
    if file_path is None or not file_path.is_file():
        return media_url
    try:
        if file_path.stat().st_size > _MAX_LOCAL_MEDIA_BASE64_BYTES:
            return media_url
        return base64.b64encode(file_path.read_bytes()).decode("ascii")
    except OSError:
        logger.warning("Could not read local media file %s", file_path, exc_info=True)
        return media_url


def media_unreachable_message(resolved_payload: str) -> str | None:
    """Avoid sending a localhost-only URL: Evolution cannot download it."""
    if not resolved_payload or not resolved_payload.startswith(("http://", "https://")):
        return None
    host = (urlparse(resolved_payload).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        mb = _MAX_LOCAL_MEDIA_BASE64_BYTES // (1024 * 1024)
        return (
            f"Mídia em URL só acessível neste servidor ({host}). "
            f"Use HTTPS público ou upload de até {mb} MB em base64."
        )
    return None
