"""Validators for chip values that end up inside URLs sent to external APIs.

Evolution and Meta URLs are built from user-provided text (instance name, Phone Number ID,
Graph API version). Restricting the alphabet closes path traversal and host-swap tricks
(``../``, ``//host``, ``http://...``) before the value reaches an HTTP client.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from django.core.validators import RegexValidator

INSTANCE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
META_PHONE_NUMBER_ID_RE = re.compile(r"^\d{5,30}$")
META_API_VERSION_RE = re.compile(r"^v\d{1,2}\.\d{1,2}$")

validate_instance_name = RegexValidator(
    INSTANCE_NAME_RE,
    "Use de 1 a 60 caracteres: letras, números, hífen ou sublinhado (sem espaços).",
    code="invalid_instance_name",
)
validate_meta_phone_number_id = RegexValidator(
    META_PHONE_NUMBER_ID_RE,
    "O Phone Number ID da Meta contém apenas dígitos (5 a 30).",
    code="invalid_meta_phone_number_id",
)
validate_meta_api_version = RegexValidator(
    META_API_VERSION_RE,
    "Informe a versão no formato v18.0.",
    code="invalid_meta_api_version",
)


def url_segment(value: str) -> str:
    """Return ``value`` as a single, inert URL path segment.

    Defense in depth for rows saved before the validators existed: percent-encodes everything
    (including ``/``) and neutralizes the dot segments ``.`` and ``..``.
    """
    encoded = quote(value, safe="")
    return encoded.replace(".", "%2E") if set(encoded) == {"."} else encoded
