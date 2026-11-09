from datetime import time

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError

from apps.chips.models import RECOMMENDED_CHIP_LIMITS, Chip


class ChipForm(forms.ModelForm):
    class Meta:
        model = Chip
        fields = (
            "name",
            "provider",
            "instance_name",
            "account_type",
            "max_messages_per_hour",
            "max_messages_per_day",
            "send_window_start",
            "send_window_end",
            "delay_between_messages_seconds",
            "is_active",
            "meta_phone_number_id",
            "meta_access_token",
            "meta_waba_id",
            "meta_api_version",
        )
        widgets = {
            "name": forms.TextInput(
                attrs={"placeholder": "Ex.: Chip atendimento 1", "autofocus": True}
            ),
            "provider": forms.RadioSelect,
            "instance_name": forms.TextInput(attrs={"placeholder": "nome-da-instancia"}),
            "max_messages_per_hour": forms.NumberInput(
                attrs={
                    "min": 1,
                    "max": 100,
                    "placeholder": "Preenchido ao escolher tipo de conta",
                }
            ),
            "max_messages_per_day": forms.NumberInput(
                attrs={"min": 1, "placeholder": "Preenchido ao escolher tipo de conta"}
            ),
            "send_window_start": forms.TimeInput(attrs={"type": "time"}),
            "send_window_end": forms.TimeInput(attrs={"type": "time"}),
            "delay_between_messages_seconds": forms.NumberInput(
                attrs={
                    "min": 30,
                    "max": 3600,
                    "placeholder": "Ideal calculado conforme mensagens/hora",
                }
            ),
            "meta_phone_number_id": forms.TextInput(attrs={"placeholder": "Ex.: 106540352242922"}),
            # render_value=False: the stored token never goes back to the browser.
            "meta_access_token": forms.PasswordInput(
                render_value=False, attrs={"placeholder": "EAAJB...", "autocomplete": "off"}
            ),
            "meta_waba_id": forms.TextInput(attrs={"placeholder": "Ex.: 123456789012345"}),
            "meta_api_version": forms.TextInput(attrs={"placeholder": "v18.0"}),
        }
        labels = {
            "max_messages_per_hour": "Mensagens por hora (máx.)",
            "max_messages_per_day": "Mensagens por dia (máx.)",
            "send_window_start": "Início do horário de envio",
            "send_window_end": "Fim do horário de envio",
            "delay_between_messages_seconds": "Delay entre mensagens (segundos)",
            "provider": "Provedor de envio",
            "meta_phone_number_id": "Phone Number ID (Meta)",
            "meta_access_token": "Access Token (Meta)",
            "meta_waba_id": "WABA ID (Meta)",
            "meta_api_version": "Versão da API Graph (Meta)",
        }
        help_texts = {
            "instance_name": (
                "Identificador na Evolution (POST /instance/create). "
                "Usado nos provedores Evolution e Simulado."
            ),
            "account_type": "Ao escolher, os limites recomendados são preenchidos automaticamente.",
            "max_messages_per_hour": (
                "Recomendado conforme tipo de conta. Valores maiores aumentam risco de bloqueio."
            ),
            "max_messages_per_day": (
                "Recomendado conforme tipo de conta. Valores maiores aumentam risco de bloqueio."
            ),
            "send_window_start": (
                "Ideal: horário comercial (ex.: 08:00). "
                "Envios fora desse período aumentam risco de bloqueio."
            ),
            "send_window_end": "Ideal: horário comercial (ex.: 18:00).",
            "delay_between_messages_seconds": (
                "Mínimo recomendado: 30 s. Para 5 mensagens/hora o ideal é 720 s (12 min). "
                "O sistema pode sugerir o valor."
            ),
            "meta_phone_number_id": "Phone Number ID do seu número no Meta Business Manager.",
            "meta_access_token": (
                "Token de acesso do WhatsApp Business Account (WABA). Gere no Meta Developers. "
                "Ao editar, deixe em branco para manter o token atual."
            ),
            "meta_waba_id": "ID do WhatsApp Business Account (opcional, para referência).",
            "meta_api_version": "Versão da Graph API do Meta. Padrão: v18.0.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.limits_warning = []
        instance = self.instance
        self._restrict_provider_choices(instance)
        if instance.pk and instance.meta_access_token:
            self.fields["meta_access_token"].widget.attrs.update(
                {"data-has-stored": "1", "placeholder": "Token salvo (deixe em branco para manter)"}
            )
        # Meta chips have no instance; clean_instance_name enforces it for the other providers.
        self.fields["instance_name"].required = False
        if not (instance and instance.pk):
            self.fields.pop("is_active", None)
            rec = RECOMMENDED_CHIP_LIMITS["normal"]
            self.initial.setdefault("max_messages_per_hour", rec["per_hour"])
            self.initial.setdefault("max_messages_per_day", rec["per_day"])
            self.initial.setdefault("send_window_start", time(8, 0))
            self.initial.setdefault("send_window_end", time(18, 0))
            per_hour = int(self.initial.get("max_messages_per_hour") or rec["per_hour"] or 10)
            self.initial.setdefault("delay_between_messages_seconds", max(30, 3600 // per_hour))
        else:
            if not self.initial.get("delay_between_messages_seconds") and instance:
                self.initial["delay_between_messages_seconds"] = (
                    instance.get_recommended_delay_seconds()
                )
            # Default provider for existing chips
            if not self.initial.get("provider"):
                self.initial["provider"] = instance.provider or Chip.Provider.EVOLUTION

    def _restrict_provider_choices(self, instance: Chip) -> None:
        """Offer the simulated provider only when enabled (or already used by this chip)."""
        if settings.SIMULATED_PROVIDER_ENABLED or instance.provider == Chip.Provider.SIMULATED:
            return
        self.fields["provider"].choices = [
            (value, label)
            for value, label in self.fields["provider"].choices
            if value != Chip.Provider.SIMULATED
        ]

    def clean_instance_name(self):
        name = (self.cleaned_data.get("instance_name") or "").strip()
        provider = self.cleaned_data.get("provider") or Chip.Provider.EVOLUTION
        if provider == Chip.Provider.META:
            return name
        if not name:
            raise ValidationError("Informe o nome da instância na Evolution.")
        if self.instance.pk:
            taken = Chip.objects.filter(
                workspace_id=self.instance.workspace_id,
                instance_name=name,
            ).exclude(pk=self.instance.pk)
            if taken.exists():
                raise ValidationError(
                    "Já existe outro chip neste perfil com este nome de instância."
                )
        return name

    def clean_meta_access_token(self):
        """Keep the stored token when the (never re-rendered) field is submitted blank."""
        token = (self.cleaned_data.get("meta_access_token") or "").strip()
        if not token and self.instance.pk:
            return self.instance.meta_access_token
        return token

    def clean(self):
        data = super().clean()
        self.limits_warning = []
        provider = data.get("provider") or Chip.Provider.EVOLUTION

        if provider == Chip.Provider.META:
            pn_id = (data.get("meta_phone_number_id") or "").strip()
            token = (data.get("meta_access_token") or "").strip()
            if not pn_id:
                self.add_error(
                    "meta_phone_number_id", "Informe o Phone Number ID para provedor Meta."
                )
            if not token:
                self.add_error("meta_access_token", "Informe o Access Token para provedor Meta.")
            # Clear fields that do not apply to the chosen provider
            data["instance_name"] = ""

        start = data.get("send_window_start")
        end = data.get("send_window_end")
        if start and end and start >= end:
            self.add_error(
                "send_window_end",
                f"Horário de fim ({end.strftime('%H:%M')}) deve ser maior que o de início "
                f"({start.strftime('%H:%M')}). Janelas noturnas (ex.: 22h–06h) não são "
                "suportadas.",
            )

        account_type = data.get("account_type")
        per_hour = data.get("max_messages_per_hour")
        per_day = data.get("max_messages_per_day")
        if account_type and account_type in RECOMMENDED_CHIP_LIMITS:
            rec = RECOMMENDED_CHIP_LIMITS[account_type]
            if per_hour and per_hour > rec["per_hour"]:
                self.limits_warning.append(
                    f"Mensagens por hora ({per_hour}) acima do recomendado para este tipo de conta "
                    f"({rec['per_hour']} por hora). Risco maior de bloqueio."
                )
            if per_day and per_day > rec["per_day"]:
                self.limits_warning.append(
                    f"Mensagens por dia ({per_day}) acima do recomendado para este tipo de conta "
                    f"({rec['per_day']} por dia). Risco maior de bloqueio."
                )
        return data
