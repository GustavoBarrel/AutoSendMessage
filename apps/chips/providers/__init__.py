"""
Factory of WhatsApp providers.
Pick the provider from the ``Chip.provider`` field.
"""

from __future__ import annotations

from django.conf import settings

from apps.chips.models import Chip
from apps.chips.providers.base import WhatsAppProvider
from apps.chips.providers.evolution import EvolutionProvider
from apps.chips.providers.meta import MetaProvider
from apps.chips.providers.simulated import SimulatedProvider

SIMULATED_DISABLED_MESSAGE = (
    "Provedor simulado desativado neste ambiente. Troque este chip por um chip real "
    "(Evolution ou Meta) para enviar mensagens."
)


class ProviderDisabledError(Exception):
    """The chip's provider exists but is turned off in this environment."""


def get_provider_for_chip(chip: Chip) -> WhatsAppProvider:
    provider = (getattr(chip, "provider", None) or "").strip().lower()
    if provider == Chip.Provider.EVOLUTION or not provider:
        return EvolutionProvider(chip)
    if provider == Chip.Provider.META:
        return MetaProvider(chip)
    if provider == Chip.Provider.SIMULATED:
        # Without this guard a leftover simulated chip would mark messages as sent in a real
        # environment without delivering anything.
        if not settings.SIMULATED_PROVIDER_ENABLED:
            raise ProviderDisabledError(SIMULATED_DISABLED_MESSAGE)
        return SimulatedProvider(chip)
    raise ValueError(f"Provider desconhecido para chip: {provider!r}")
