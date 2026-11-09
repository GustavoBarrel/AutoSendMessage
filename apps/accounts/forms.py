from django import forms

from apps.accounts.models import Workspace


class WorkspaceForm(forms.ModelForm):
    class Meta:
        model = Workspace
        fields = ("name", "slug", "color")
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Nome", "autofocus": True}),
            "slug": forms.TextInput(attrs={"placeholder": "Slug"}),
            "color": forms.TextInput(
                attrs={
                    "type": "color",
                    "style": "height: 2.5rem; width: 100%; cursor: pointer;",
                }
            ),
        }
        help_texts = {
            "slug": "Identificador único na URL (letras minúsculas, hífens).",
            "color": "Cor do card na tela de escolha de perfil.",
        }
