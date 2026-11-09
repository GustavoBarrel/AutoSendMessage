from django.db import models

from apps.accounts.models import Workspace


class StatsDashboard(models.Model):
    """Statistics dashboard customized by the user."""

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="stats_dashboards",
        verbose_name="perfil",
    )
    name = models.CharField(max_length=120, verbose_name="nome")
    is_default = models.BooleanField(
        default=False,
        help_text="Painel exibido ao abrir a página de painéis customizados.",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="criado em")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="atualizado em")

    class Meta:
        verbose_name = "Painel de estatísticas"
        verbose_name_plural = "Painéis de estatísticas"
        ordering = ["-is_default", "-updated_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.workspace.name})"

    def save(self, *args, **kwargs):
        if self.is_default:
            StatsDashboard.objects.filter(
                workspace_id=self.workspace_id,
                is_default=True,
            ).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)


class StatsDashboardWidget(models.Model):
    """
    Widget (chart) inside a custom dashboard.
    Used only by statistics; does not affect campaigns, chips or sending.
    Help texts for each Dataset value: apps.dashboards.stats_datasets.
    """

    class Dataset(models.TextChoices):
        # Overview
        STATUS = "status", "Distribuição por status"
        # Chips
        CHIP_TOTAL = "chip_total", "Total por chip"
        CHIP_SENT = "chip_sent", "Sucesso por chip"
        CHIP_FAILED = "chip_failed", "Falhas por chip"
        CHIP_PENDING = "chip_pending", "Pendentes por chip"
        CHIP_WAITING = "chip_waiting", "Aguardando chip (offline)"
        CHIP_LOCKED = "chip_locked", "Em processamento por chip"
        CHIP_FAIL_RATE = "chip_fail_rate", "Taxa de falha por chip (%)"
        CHIP_SUCCESS_RATE = "chip_success_rate", "Taxa de sucesso por chip (%)"
        CHIP_AVG_TIME = "chip_avg_time", "Tempo médio até envio por chip (min)"
        # Campaigns
        CAMPAIGN_TOTAL = "campaign_total", "Total por campanha"
        CAMPAIGN_SENT = "campaign_sent", "Sucesso por campanha"
        CAMPAIGN_FAILED = "campaign_failed", "Falhas por campanha"
        CAMPAIGN_PENDING = "campaign_pending", "Pendentes por campanha"
        CAMPAIGN_FAIL_RATE = "campaign_fail_rate", "Taxa de falha por campanha (%)"
        CAMPAIGN_SUCCESS_RATE = "campaign_success_rate", "Taxa de sucesso por campanha (%)"
        # Time
        DAILY_TOTAL = "daily_total", "Evolução diária (total)"
        DAILY_SENT = "daily_sent", "Evolução diária (sucesso)"
        DAILY_FAILED = "daily_failed", "Evolução diária (falhas)"
        HOURLY = "hourly", "Envios por hora do dia"
        # Diagnostics
        TOP_ERRORS = "top_errors", "Principais erros (categorias)"
        DEFER_REASON = "defer_reason", "Motivos de adiamento (rate limit)"
        ATTEMPTS = "attempts", "Distribuição de tentativas por envio"
        # Content
        VARIANT_TOTAL = "variant_total", "Volume por variação de mensagem"
        MESSAGE_TYPE = "message_type", "Envios por tipo de mensagem"
        # Audience (successful sends)
        CONTACTS_CITY = "contacts_city", "Envios por cidade do contato"
        CONTACTS_STATE = "contacts_state", "Envios por UF do contato"
        CONTACTS_NEIGHBORHOOD = "contacts_neighborhood", "Envios por bairro do contato"
        CONTACTS_CATEGORY = "contacts_category", "Envios por categoria do contato"
        CONTACTS_SUBCATEGORY = "contacts_subcategory", "Envios por subcategoria do contato"
        CONTACTS_GENDER = "contacts_gender", "Envios por sexo do contato"

    class ChartType(models.TextChoices):
        BAR = "bar", "Barra"
        LINE = "line", "Linha"
        PIE = "pie", "Pizza"
        DOUGHNUT = "doughnut", "Rosca"

    class Size(models.TextChoices):
        SMALL = "small", "Pequeno (1/3)"
        MEDIUM = "medium", "Médio (1/2)"
        LARGE = "large", "Grande (100%)"

    dashboard = models.ForeignKey(
        StatsDashboard,
        on_delete=models.CASCADE,
        related_name="widgets",
    )
    position = models.PositiveSmallIntegerField(default=0, verbose_name="ordem")
    title = models.CharField(max_length=120, blank=True, verbose_name="titulo")
    dataset = models.CharField(
        max_length=40,
        choices=Dataset.choices,
        default=Dataset.STATUS,
    )
    chart_type = models.CharField(
        max_length=10,
        choices=ChartType.choices,
        default=ChartType.BAR,
    )
    size = models.CharField(
        max_length=10,
        choices=Size.choices,
        default=Size.MEDIUM,
    )
    filters_json = models.JSONField(
        default=dict,
        blank=True,
        help_text="Filtros opcionais: campaign_id, chip_id, status, start_days_ago, end_days_ago.",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="criado em")

    class Meta:
        verbose_name = "Widget de estatísticas"
        verbose_name_plural = "Widgets de estatísticas"
        ordering = ["dashboard_id", "position", "pk"]

    def __str__(self) -> str:
        return f"{self.title or self.get_dataset_display()} ({self.dashboard.name})"
