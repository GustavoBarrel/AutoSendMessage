from django import forms

from apps.contacts.models import ManualTag
from apps.contacts.services.contacts_import import (
    CONTACT_FIELD_LABELS,
    CONTACT_FIELDS,
    MAX_IMPORT_FILE_BYTES,
)


class UploadExcelForm(forms.Form):
    file = forms.FileField(
        label="Arquivo (Excel ou CSV)",
        help_text="Arquivo .xlsx ou .csv. A primeira linha deve conter os títulos das colunas.",
        widget=forms.FileInput(attrs={"accept": ".xlsx,.xls,.csv"}),
    )

    def clean_file(self):
        data = self.cleaned_data.get("file")
        if not data:
            return data
        name = getattr(data, "name", "") or ""
        if not name.lower().endswith((".xlsx", ".xls", ".csv")):
            raise forms.ValidationError("Envie um arquivo .xlsx, .xls ou .csv.")
        if data.size > MAX_IMPORT_FILE_BYTES:
            raise forms.ValidationError(
                f"Arquivo muito grande. O limite é {MAX_IMPORT_FILE_BYTES // (1024 * 1024)} MB."
            )
        return data


class MappingColumnsForm(forms.Form):
    """Dynamic form: one field per internal contact field -> file column."""

    def __init__(self, *args, headers=None, mapping_initial=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not headers:
            headers = {}
        col_choices = [("", "— Não importar")] + [
            (str(idx), f"{label or f'Coluna {idx}'}") for idx, label in headers.items()
        ]
        for field in CONTACT_FIELDS:
            initial = ""
            if mapping_initial and field in mapping_initial:
                initial = str(mapping_initial[field])
            self.fields[field] = forms.ChoiceField(
                label=CONTACT_FIELD_LABELS[field],
                choices=col_choices,
                required=False,
                initial=initial,
            )

    def get_mapping(self):
        """Return a dict { model_field: column_index }."""
        mapping = {}
        for field, col in self.cleaned_data.items():
            if field in CONTACT_FIELDS and col:
                mapping[field] = int(col)
        return mapping

    def clean(self):
        cleaned = super().clean()
        mobile_phone_col = cleaned.get("mobile_phone")
        if not mobile_phone_col:
            raise forms.ValidationError(
                "É obrigatório mapear uma coluna para o campo interno 'celular'."
            )
        chosen = [col for field, col in cleaned.items() if field in CONTACT_FIELDS and col]
        repeated = sorted({c for c in chosen if chosen.count(c) > 1})
        if repeated:
            raise forms.ValidationError(
                "A mesma coluna do arquivo foi selecionada para mais de um campo interno. "
                "Escolha colunas diferentes ou marque '— Não importar'."
            )
        return cleaned

    def as_json_selected(self):
        """Return {field: column} (as strings) for use in the template/JS."""
        if not hasattr(self, "cleaned_data"):
            return {}
        return {k: v for k, v in self.cleaned_data.items() if k in CONTACT_FIELDS and v}


class ResolveDuplicatesForm(forms.Form):
    """For each duplicated mobile phone, a choice of which row to keep (index in the list)."""

    def __init__(self, *args, duplicate_groups=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not duplicate_groups:
            duplicate_groups = {}
        for mobile_phone, rows in duplicate_groups.items():
            field_name = f"phone_{mobile_phone}"
            choices = [
                (
                    str(i),
                    f"Linha {rows[i].get('_row', i + 2)} (planilha): "
                    f"{rows[i].get('name', '') or '(sem nome)'} — {mobile_phone}",
                )
                for i in range(len(rows))
            ]
            self.fields[field_name] = forms.ChoiceField(
                label=f"Celular {mobile_phone}",
                choices=choices,
                required=True,
                widget=forms.RadioSelect,
            )

    def get_chosen_rows(self, duplicate_groups):
        """Return the set of (mobile_phone, row_index) to keep."""
        chosen = set()
        for mobile_phone in duplicate_groups:
            key = f"phone_{mobile_phone}"
            val = self.cleaned_data.get(key)
            if val is not None:
                chosen.add((mobile_phone, int(val)))
        return chosen


class DecisionConflictsForm(forms.Form):
    """For each conflicting mobile phone: keep the current data or update it with the Excel data."""

    def __init__(self, *args, conflicts=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not conflicts:
            conflicts = []
        for i, (excel_data, _current_data, _diff) in enumerate(conflicts):
            field_name = f"conflict_{i}"
            self.fields[field_name] = forms.ChoiceField(
                label=excel_data.get("mobile_phone", ""),
                choices=[
                    ("keep", "Manter dados atuais"),
                    ("update", "Atualizar com dados do Excel"),
                ],
                required=True,
                initial="keep",
                widget=forms.RadioSelect,
            )

    def get_decisions(self, conflicts):
        """Return a list of ('keep'|'update') in the same order as the conflicts."""
        return [self.cleaned_data.get(f"conflict_{i}", "keep") for i in range(len(conflicts))]


class ContactFilterForm(forms.Form):
    """Filters of the contact list (GET). Every field is optional."""

    q = forms.CharField(required=False, max_length=200)
    tags = forms.ModelMultipleChoiceField(queryset=ManualTag.objects.all(), required=False)
    exclude_tags = forms.ModelMultipleChoiceField(queryset=ManualTag.objects.all(), required=False)
    category = forms.CharField(required=False, max_length=100)
    subcategory = forms.CharField(required=False, max_length=100)
    city = forms.CharField(required=False, max_length=100)
    neighborhood = forms.CharField(required=False, max_length=100)
    state = forms.CharField(required=False, max_length=5)
