from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from apps.accounts.models import Workspace
from apps.chips.models import Chip
from apps.contacts.models import Contact


class Campaign(models.Model):
    """Send campaign (per workspace)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Rascunho"
        ACTIVE = "active", "Ativa"
        PAUSED = "paused", "Pausada"
        FINISHED = "finished", "Finalizada"

    class Audience(models.TextChoices):
        ALL = "all", "Todos os contatos"
        BIRTHDAY_TODAY = "birthday_today", "Aniversariantes do dia"

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="campaigns",
        verbose_name="perfil",
    )
    name = models.CharField(max_length=120, verbose_name="nome")
    audience = models.CharField(
        max_length=30,
        choices=Audience.choices,
        default=Audience.ALL,
        help_text="Resumo legado (todos / aniversariantes); detalhes em audience_config.",
    )
    audience_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Preset base, filtros (cidade, UF…), exclude_ids e include_ids.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    start_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Início agendado (opcional). Se preenchido no futuro, os envios só começam a partir "
            "deste horário."
        ),
    )
    activated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Data/hora em que a campanha foi iniciada (status mudou para Ativa pela primeira vez)."
        ),
    )
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Preenchido ao excluir (soft delete). Nulo = campanha ativa.",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="criado em")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="atualizado em")

    class Meta:
        verbose_name = "Campanha"
        verbose_name_plural = "Campanhas"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workspace", "status"]),
            models.Index(fields=["workspace", "deleted_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.workspace.name})"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Mirror the status so CampaignChip can enforce constraints on it.
        CampaignChip.objects.filter(campaign_id=self.pk).update(campaign_status=self.status)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def clean(self):
        super().clean()
        if self.status in {self.Status.ACTIVE, self.Status.PAUSED, self.Status.FINISHED}:
            if self.pk and self.message_variants.count() != 3:
                raise ValidationError("A campanha precisa ter exatamente 3 variações de mensagem.")


class CampaignChip(models.Model):
    """Chips used in a campaign."""

    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="campaign_chips",
    )
    chip = models.ForeignKey(
        Chip,
        on_delete=models.CASCADE,
        related_name="campaign_chips",
    )
    is_active = models.BooleanField(default=True, verbose_name="ativo")
    campaign_status = models.CharField(
        max_length=20,
        choices=Campaign.Status.choices,
        default=Campaign.Status.DRAFT,
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="criado em")

    class Meta:
        verbose_name = "Chip da campanha"
        verbose_name_plural = "Chips da campanha"
        unique_together = [["campaign", "chip"]]
        constraints = [
            models.UniqueConstraint(
                fields=["chip"],
                condition=models.Q(campaign_status=Campaign.Status.ACTIVE, is_active=True),
                name="uniq_chip_in_one_active_campaign",
            )
        ]
        indexes = [
            models.Index(fields=["chip", "is_active"]),
            models.Index(fields=["campaign", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.campaign.name} - {self.chip.name}"

    def clean(self):
        super().clean()
        if (
            self.campaign_id
            and self.chip_id
            and self.campaign.workspace_id != self.chip.workspace_id
        ):
            raise ValidationError("O chip deve pertencer ao mesmo perfil da campanha.")
        if self.campaign_id:
            self.campaign_status = self.campaign.status


class CampaignMessageVariant(models.Model):
    """Message variant (1..3) used in the campaign."""

    class MessageType(models.TextChoices):
        TEXT = "text", "Texto"
        IMAGE = "image", "Imagem"
        VIDEO = "video", "Vídeo"
        DOCUMENT = "document", "Documento"

    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="message_variants",
    )
    position = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(3)],
        help_text="1, 2 ou 3.",
        verbose_name="ordem",
    )
    message_type = models.CharField(
        max_length=10,
        choices=MessageType.choices,
        default=MessageType.TEXT,
        verbose_name="tipo",
    )
    text = models.TextField(
        blank=True,
        help_text="Texto da mensagem. Para imagem/vídeo, funciona como legenda (opcional).",
        verbose_name="texto",
    )
    media_url = models.URLField(
        blank=True,
        max_length=800,
        help_text=(
            "URL pública do arquivo (imagem/vídeo). Obrigatório quando tipo for imagem/vídeo."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="criado em")

    class Meta:
        verbose_name = "Variação de mensagem"
        verbose_name_plural = "Variações de mensagem"
        ordering = ["campaign_id", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "position"], name="uniq_variant_position_per_campaign"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.campaign.name} - Variação {self.position}"

    def clean(self):
        super().clean()
        text_ok = bool(self.text and str(self.text).strip())
        media_ok = bool(self.media_url and str(self.media_url).strip())
        if self.message_type == self.MessageType.TEXT:
            if not text_ok:
                raise ValidationError({"text": "Mensagem não pode ficar vazia."})
            if media_ok:
                raise ValidationError({"media_url": "Para tipo 'Texto', não informe mídia."})
        else:
            if not media_ok:
                raise ValidationError({"media_url": "Informe a URL da mídia (imagem/vídeo)."})


class CampaignSendJob(models.Model):
    """One row in the queue: one send per contact and campaign (idempotent per campaign+contact)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pendente"
        LOCKED = "locked", "Em processamento"
        SENT = "sent", "Enviado"
        FAILED = "failed", "Falha permanente"
        CANCELLED = "cancelled", "Cancelado"
        SKIPPED = "skipped", "Ignorado"
        WAITING_CHIP = "waiting_chip", "Aguardando chip (offline)"

    class ScheduleDeferReason(models.TextChoices):
        """Reason for a rate-limit deferral (shown on the campaign dashboard)."""

        BUSINESS_HOURS = (
            "business_hours",
            "Fora do horário de envio do chip; aguardando a próxima janela permitida.",
        )
        MIN_INTERVAL = (
            "min_interval",
            "Aguardando o intervalo mínimo entre envios neste chip.",
        )
        BATCH_PAUSE = (
            "batch_pause",
            "Pausa entre lotes de envios (proteção do chip).",
        )
        HOURLY_LIMIT = (
            "hourly_limit",
            "Limite de mensagens por hora atingido; aguardando a próxima janela.",
        )
        DAILY_LIMIT = (
            "daily_limit",
            "Esta mensagem será enviada no próximo dia: limite de mensagens por dia do chip "
            "atingido.",
        )
        DEFER_RACE = (
            "defer_race",
            "Reagendamento interno; tentativa em instantes.",
        )
        CHIP_OFFLINE = (
            "chip_offline",
            "WhatsApp desconectado na Evolution; reconecte o chip ou redistribua as mensagens.",
        )

    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="send_jobs",
    )
    contact = models.ForeignKey(
        Contact,
        on_delete=models.CASCADE,
        related_name="campaign_send_jobs",
        verbose_name="contato",
    )
    chip = models.ForeignKey(
        Chip,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="campaign_send_jobs",
    )
    chip_name_snapshot = models.CharField(
        max_length=80,
        blank=True,
        help_text="Nome do chip no momento do enfileiramento (preservado se chip for excluído).",
        verbose_name="chip nome snapshot",
    )
    variant = models.ForeignKey(
        CampaignMessageVariant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="send_jobs",
        help_text="Variação usada no envio (preenchido ao enviar ou ao enfileirar).",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    scheduled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Não processar antes deste instante (retentativas com backoff).",
    )
    schedule_defer_reason = models.CharField(
        max_length=32,
        blank=True,
        choices=ScheduleDeferReason.choices,
        help_text="Motivo do último adiamento por limite do chip (rate limit).",
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.CharField(max_length=64, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(
        max_length=80,
        unique=True,
        help_text="Chave estável para evitar duplicata (ex.: campanha+contato).",
    )
    external_message_id = models.CharField(
        max_length=200,
        blank=True,
        help_text="ID retornado pela Evolution (rastreio / idempotência na API).",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="criado em")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="atualizado em")

    class Meta:
        verbose_name = "Job de envio da campanha"
        verbose_name_plural = "Jobs de envio da campanha"
        ordering = ["campaign_id", "scheduled_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "contact"],
                name="uniq_send_job_campaign_contact",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "scheduled_at"]),
            models.Index(fields=["chip", "status", "scheduled_at"]),
            models.Index(fields=["campaign", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.campaign_id} → {self.contact_id} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        if self.campaign_id and self.contact_id and not self.idempotency_key:
            self.idempotency_key = f"c{self.campaign_id}-ct{self.contact_id}"
        super().save(*args, **kwargs)

    def get_schedule_warning_display(self) -> str:
        """
        Explicit warning only for next-day sends (daily limit).
        Other chip limits do not produce text here (they are implicit in the operation).
        """
        daily = self.ScheduleDeferReason.DAILY_LIMIT.value
        if self.schedule_defer_reason == daily:
            return self.get_schedule_defer_reason_display()
        if self.status != self.Status.PENDING or not self.scheduled_at:
            return ""
        sched = self.scheduled_at
        if sched <= timezone.now():
            return ""
        local_sched = timezone.localtime(sched)
        local_now = timezone.localtime(timezone.now())
        if local_sched.date() > local_now.date():
            return (
                "Envio agendado para o próximo dia devido ao limite de mensagens por dia do chip "
                "(ou início do horário de envio no dia seguinte)."
            )
        return ""

    def clean(self):
        super().clean()
        if (
            self.campaign_id
            and self.chip_id
            and self.campaign.workspace_id != self.chip.workspace_id
        ):
            raise ValidationError("O chip deve pertencer ao mesmo perfil da campanha.")
        if self.campaign_id and self.contact_id and self.variant_id:
            if self.variant.campaign_id != self.campaign_id:
                raise ValidationError("A variação deve pertencer à mesma campanha.")


class CampaignSendAttempt(models.Model):
    """Record of each send attempt (audit and diagnostics)."""

    job = models.ForeignKey(
        CampaignSendJob,
        on_delete=models.CASCADE,
        related_name="attempts_log",
    )
    attempt_no = models.PositiveSmallIntegerField()
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    success = models.BooleanField(default=False)
    error_text = models.TextField(blank=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    provider_payload = models.JSONField(null=True, blank=True, verbose_name="evolution payload")

    class Meta:
        verbose_name = "Tentativa de envio"
        verbose_name_plural = "Tentativas de envio"
        ordering = ["job_id", "attempt_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "attempt_no"],
                name="uniq_attempt_no_per_job",
            ),
        ]

    def __str__(self) -> str:
        return f"Job {self.job_id} tentativa {self.attempt_no}"
