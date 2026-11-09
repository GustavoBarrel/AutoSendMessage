from datetime import time

from django.core.validators import MaxLengthValidator, MaxValueValidator, MinValueValidator
from django.db import models

from apps.accounts.models import Workspace
from apps.chips.validators import (
    validate_instance_name,
    validate_meta_api_version,
    validate_meta_phone_number_id,
)
from apps.common.fields import EncryptedTextField

# Recommended limits per account type (to avoid chip blocking)
RECOMMENDED_CHIP_LIMITS = {
    "new": {"per_hour": 5, "per_day": 25},
    "normal": {"per_hour": 10, "per_day": 75},
    "old": {"per_hour": 20, "per_day": 150},
}


class Chip(models.Model):
    """
    Sending instance (number/connection), linked to a workspace.
    Supports the Evolution API, the Meta WhatsApp Business API and a simulated provider
    (demo only, never sends anything).
    """

    class Provider(models.TextChoices):
        EVOLUTION = "evolution", "Evolution API"
        META = "meta", "Meta WhatsApp Business API"
        SIMULATED = "simulated", "Simulado (demonstração, não envia mensagens de verdade)"

    ACCOUNT_TYPE_CHOICES = [
        ("new", "Conta nova (< 7 dias)"),
        ("normal", "Conta normal (1–3 meses)"),
        ("old", "Conta antiga (> 3 meses)"),
    ]
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="chips",
        verbose_name="perfil",
    )
    name = models.CharField(
        max_length=80,
        help_text="Nome para identificar o chip na interface (ex.: Chip atendimento 1).",
        verbose_name="nome",
    )
    provider = models.CharField(
        max_length=20,
        choices=Provider.choices,
        default=Provider.EVOLUTION,
        help_text="Provedor de envio (Evolution, Meta WhatsApp ou simulado).",
    )
    instance_name = models.CharField(
        max_length=120,
        validators=[validate_instance_name],
        help_text="Nome da instância na Evolution API (criada em /instance/create).",
    )
    account_type = models.CharField(
        max_length=20,
        choices=ACCOUNT_TYPE_CHOICES,
        default="normal",
        help_text="Usado para sugerir limites de envio e evitar bloqueio.",
        verbose_name="tipo conta",
    )
    max_messages_per_hour = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Máximo de mensagens por hora (vazio = usar sugestão conforme tipo de conta).",
        verbose_name="msg por hora max",
    )
    max_messages_per_day = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Máximo de mensagens por dia (vazio = usar sugestão conforme tipo de conta).",
        verbose_name="msg por dia max",
    )
    send_window_start = models.TimeField(
        default=time(8, 0),
        help_text="Início do horário em que os envios são permitidos. Ideal: horário comercial.",
        verbose_name="horario inicio",
    )
    send_window_end = models.TimeField(
        default=time(18, 0),
        help_text="Fim do horário em que os envios são permitidos. Ideal: horário comercial.",
        verbose_name="horario fim",
    )
    delay_between_messages_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(30), MaxValueValidator(3600)],
        help_text=(
            "Segundos de espera entre uma mensagem e outra. Mínimo recomendado: 30. Para 5 "
            "mensagens/hora, ideal ~720."
        ),
        verbose_name="delay entre mensagens segundos",
    )
    is_active = models.BooleanField(default=True, verbose_name="ativo")
    evolution_state_next_check_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Antes deste instante o worker evita nova chamada connectionState na Evolution "
            "para este chip (reduz chamadas após detecção de offline)."
        ),
    )
    meta_phone_number_id = models.CharField(
        max_length=120,
        blank=True,
        validators=[validate_meta_phone_number_id],
        help_text="Phone Number ID no Meta (para provedor Meta).",
    )
    meta_access_token = EncryptedTextField(
        blank=True,
        validators=[MaxLengthValidator(500)],
        help_text="Token de acesso permanente do WhatsApp Business Account (Meta).",
    )
    meta_waba_id = models.CharField(
        max_length=120,
        blank=True,
        help_text="WhatsApp Business Account ID (Meta).",
    )
    meta_api_version = models.CharField(
        max_length=10,
        default="v18.0",
        validators=[validate_meta_api_version],
        help_text="Versão da Meta Graph API (ex.: v18.0, v19.0).",
    )

    class Meta:
        verbose_name = "Chip"
        verbose_name_plural = "Chips"
        ordering = ["name"]
        unique_together = [["workspace", "instance_name"]]

    def __str__(self):
        provider_label = self.get_provider_display() if self.provider else "Evolution"
        return f"{self.name} ({self.workspace.name}) — {provider_label}"

    @property
    def uses_evolution(self) -> bool:
        """True when the chip talks to the Evolution API (the default for a blank provider)."""
        return self.provider not in (self.Provider.META, self.Provider.SIMULATED)

    def get_recommended_limits(self):
        return RECOMMENDED_CHIP_LIMITS.get(self.account_type, RECOMMENDED_CHIP_LIMITS["normal"])

    def get_recommended_delay_seconds(self):
        """Ideal delay in seconds (3600 / messages per hour). Minimum 30."""
        per_hour = self.max_messages_per_hour or self.get_recommended_limits()["per_hour"]
        delay = 3600 // per_hour if per_hour else 720
        return max(30, delay)
