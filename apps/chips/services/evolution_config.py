from django.conf import settings
from django.utils.crypto import salted_hmac

from apps.common.utils.safe import safe_user_message


def evolution_configured() -> bool:
    return bool(settings.EVOLUTION_URL_BASE and settings.EVOLUTION_API_KEY)


def evolution_creds() -> tuple[str, str]:
    return settings.EVOLUTION_URL_BASE, settings.EVOLUTION_API_KEY


def evolution_missing_message() -> str:
    return (
        "Configuração da Evolution API ausente. Defina EVOLUTION_URL_BASE e EVOLUTION_API_KEY no "
        ".env."
    )


def evolution_error(action: str, detail: str | None = None) -> str:
    if getattr(settings, "DEBUG", False) and detail:
        return f"{safe_user_message(action)} Detalhe: {detail}"
    return safe_user_message(action)


def webhook_token() -> str:
    """Secret path segment of the QR webhook URL, derived from SECRET_KEY (never stored).

    The webhook is reachable without a session, so the token is the only thing that stops a
    stranger from planting a QR code on the "Conectar chip" screen.
    """
    return salted_hmac("chips.evolution_webhook", "qrcode").hexdigest()[:40]


WEBHOOK_TOKEN_PLACEHOLDER = "<token>"


def mask_webhook_token(text: str) -> str:
    """Replace the webhook token in ``text`` so the secret never reaches logs or the debug page."""
    return text.replace(webhook_token(), WEBHOOK_TOKEN_PLACEHOLDER)
