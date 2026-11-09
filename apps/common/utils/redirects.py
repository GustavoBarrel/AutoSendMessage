"""Helpers for redirecting to a user-supplied ``next`` target."""

from __future__ import annotations

from django.http import HttpRequest
from django.utils.http import url_has_allowed_host_and_scheme


def safe_next_url(request: HttpRequest, raw_next: str | None) -> str | None:
    """Return ``raw_next`` if it points to this host (same scheme rules), otherwise ``None``.

    A hand-written ``startswith("/")`` check is not enough: browsers treat ``/\\evil.com`` as
    ``//evil.com``. Django's validator knows those cases.
    """
    candidate = (raw_next or "").strip()
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return None
