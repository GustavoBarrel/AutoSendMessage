from django import forms

from apps.dashboards.constants import MAX_WIDGET_PERIOD_DAYS
from apps.dashboards.models import StatsDashboard, StatsDashboardWidget


class StatsDashboardForm(forms.ModelForm):
    class Meta:
        model = StatsDashboard
        fields = ["name", "is_default"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Nome do painel"}),
        }
        labels = {
            "name": "Nome do painel",
            "is_default": "Painel padrão",
        }


class StatsDashboardWidgetForm(forms.ModelForm):
    start_days_ago = forms.IntegerField(
        required=False,
        min_value=0,
        max_value=MAX_WIDGET_PERIOD_DAYS,
        label="Período: últimos N dias (início)",
        help_text="Deixe vazio para usar os últimos 30 dias.",
        widget=forms.NumberInput(attrs={"placeholder": "30"}),
    )
    end_days_ago = forms.IntegerField(
        required=False,
        min_value=0,
        max_value=MAX_WIDGET_PERIOD_DAYS,
        label="Período: até N dias atrás (fim)",
        help_text="0 = hoje. Deixe vazio para hoje.",
        widget=forms.NumberInput(attrs={"placeholder": "0"}),
    )
    filter_campaign_id = forms.IntegerField(
        required=False,
        label="Campanha (ID)",
        widget=forms.HiddenInput(),
    )
    filter_chip_id = forms.IntegerField(
        required=False,
        label="Chip (ID)",
        widget=forms.HiddenInput(),
    )

    class Meta:
        model = StatsDashboardWidget
        fields = ["title", "dataset", "chart_type", "size"]
        labels = {
            "title": "Título do gráfico",
            "dataset": "Dados",
            "chart_type": "Tipo de gráfico",
            "size": "Tamanho",
        }
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "Ex.: Falhas por chip"}),
        }

    def __init__(self, *args, workspace=None, **kwargs):
        instance = kwargs.get("instance")
        initial = kwargs.get("initial", {})
        if instance and instance.pk and instance.filters_json:
            fj = instance.filters_json
            initial.setdefault("start_days_ago", fj.get("start_days_ago"))
            initial.setdefault("end_days_ago", fj.get("end_days_ago"))
            initial.setdefault("filter_campaign_id", fj.get("campaign_id"))
            initial.setdefault("filter_chip_id", fj.get("chip_id"))
        kwargs["initial"] = initial
        super().__init__(*args, **kwargs)

        if workspace:
            from apps.campaigns.models import Campaign
            from apps.chips.models import Chip

            campaigns = (
                Campaign.objects.filter(
                    workspace=workspace,
                    deleted_at__isnull=True,
                )
                .order_by("-created_at")
                .only("id", "name")
            )
            campaign_choices = [("", "Todas")] + [(c.pk, c.name) for c in campaigns]
            self.fields["filter_campaign_id"] = forms.ChoiceField(
                choices=campaign_choices,
                required=False,
                label="Campanha",
            )

            chips = Chip.objects.filter(workspace=workspace).order_by("name").only("id", "name")
            chip_choices = [("", "Todos")] + [(c.pk, c.name) for c in chips]
            self.fields["filter_chip_id"] = forms.ChoiceField(
                choices=chip_choices,
                required=False,
                label="Chip",
            )

    def save(self, commit=True):
        widget = super().save(commit=False)
        fj: dict = {}
        campaign_id = self.cleaned_data.get("filter_campaign_id")
        chip_id = self.cleaned_data.get("filter_chip_id")
        start = self.cleaned_data.get("start_days_ago")
        end = self.cleaned_data.get("end_days_ago")
        if campaign_id:
            fj["campaign_id"] = int(campaign_id)
        if chip_id:
            fj["chip_id"] = int(chip_id)
        if start is not None and start != "":
            fj["start_days_ago"] = int(start)
        if end is not None and end != "":
            fj["end_days_ago"] = int(end)
        widget.filters_json = fj
        if commit:
            widget.save()
        return widget
