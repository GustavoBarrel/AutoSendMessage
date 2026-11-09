"""Contact list filtering, shared by the list view and the bulk selection endpoint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from django.db.models import Q, QuerySet

from apps.campaigns.services.campaign_audience import distinct_filter_values_for_contacts
from apps.contacts.forms import ContactFilterForm
from apps.contacts.models import Contact, ManualTag

# Text filters matched case-insensitively against the field with the same name.
TEXT_FILTER_FIELDS = ("category", "subcategory", "city", "neighborhood", "state")


def bind_filter_form(data: Mapping[str, Any]) -> ContactFilterForm:
    """
    Build the filter form from GET data and validate it.

    Invalid fields are simply absent from `cleaned_data`, so they behave as "no filter"
    instead of raising.
    """
    form = ContactFilterForm(data)
    form.is_valid()
    return form


def filter_contacts(cleaned_data: Mapping[str, Any]) -> QuerySet[Contact]:
    """
    Active contacts matching the cleaned filters, without duplicates.

    Tags use "has any of them" semantics; `exclude_tags` removes contacts having any of them.
    """
    qs = Contact.objects.filter(deleted_at__isnull=True)

    search = cleaned_data.get("q")
    if search:
        qs = qs.filter(
            Q(name__icontains=search)
            | Q(mobile_phone__icontains=search)
            | Q(email__icontains=search)
        )
    for field in TEXT_FILTER_FIELDS:
        value = cleaned_data.get(field)
        if value:
            qs = qs.filter(**{f"{field}__iexact": value})

    tags = cleaned_data.get("tags")
    if tags:
        qs = qs.filter(manual_tags__in=tags).distinct()
    exclude_tags = cleaned_data.get("exclude_tags")
    if exclude_tags:
        qs = qs.exclude(manual_tags__in=exclude_tags)

    return qs.order_by("name", "mobile_phone")


def filters_querystring(cleaned_data: Mapping[str, Any]) -> str:
    """Querystring with the effective filters (never `page`), for links and redirects."""
    pairs: list[tuple[str, str]] = []
    if cleaned_data.get("q"):
        pairs.append(("q", cleaned_data["q"]))
    for field in ("tags", "exclude_tags"):
        pairs.extend((field, str(tag.pk)) for tag in cleaned_data.get(field) or [])
    for field in TEXT_FILTER_FIELDS:
        if cleaned_data.get(field):
            pairs.append((field, cleaned_data[field]))
    return urlencode(pairs)


def active_panel_filter_count(cleaned_data: Mapping[str, Any]) -> int:
    """Filters set in the collapsible panel (the search box `q` is not counted).

    The list keeps the panel open while any of them is active, so the user sees
    why the result is narrower than the plain search.
    """
    count = sum(1 for field in ("tags", "exclude_tags") if cleaned_data.get(field))
    return count + sum(1 for field in TEXT_FILTER_FIELDS if cleaned_data.get(field))


def filter_options() -> dict[str, Any]:
    """Options for the filter controls: distinct values of active contacts plus all tags."""
    options: dict[str, Any] = dict(distinct_filter_values_for_contacts())
    options["tags"] = list(ManualTag.objects.all())
    return options
