"""Contact selection: preset, optional filters, manual exclusions and inclusions."""

from __future__ import annotations

from datetime import date
from typing import Any

from django.db.models import QuerySet
from django.utils import timezone

from apps.campaigns.models import Campaign
from apps.contacts.models import Contact, ManualTag

# Maximum distinct values per field (lists rendered in <select>)
_AUDIENCE_DISTINCT_LIMIT = 500

_FILTER_FIELDS = ("city", "state", "neighborhood", "category", "subcategory")


def distinct_filter_values_for_contacts() -> dict[str, list[str]]:
    """Distinct values in the database for filters (city, state, etc.)."""
    out: dict[str, list[str]] = {}
    for field in _FILTER_FIELDS:
        qs = (
            Contact.objects.filter(deleted_at__isnull=True)
            .exclude(**{f"{field}__exact": ""})
            .values_list(field, flat=True)
            .distinct()
            .order_by(field)
        )
        out[field] = list(qs[:_AUDIENCE_DISTINCT_LIMIT])
    return out


def filter_options_for_wizard(cfg: dict[str, Any]) -> dict[str, list[str]]:
    """
    Options for <select>: distinct values from the database plus the value already saved
    in the campaign if it is not in the list (keeps the selection without retyping).
    """
    out = distinct_filter_values_for_contacts()
    for field in _FILTER_FIELDS:
        cur = (cfg.get(field) or "").strip()
        if cur and cur not in out[field]:
            out[field] = list(out[field]) + [cur]
    return out


def default_audience_config() -> dict[str, Any]:
    return {
        "base": "all",
        "birthday_date": "",
        "manual_tag_include_ids": [],
        "manual_tag_exclude_ids": [],
        "manual_tag_include_mode": "any",  # any|all
        "city": "",
        "state": "",
        "neighborhood": "",
        "category": "",
        "subcategory": "",
        "whatsapp_only": False,
        "exclude_ids": [],
        "include_ids": [],
    }


def get_audience_config(campaign: Campaign) -> dict[str, Any]:
    raw = campaign.audience_config
    if not isinstance(raw, dict):
        return default_audience_config()
    out = default_audience_config()
    out.update({k: v for k, v in raw.items() if k in out})
    if "exclude_ids" in raw and isinstance(raw["exclude_ids"], list):
        out["exclude_ids"] = [int(x) for x in raw["exclude_ids"] if str(x).isdigit()]
    if "include_ids" in raw and isinstance(raw["include_ids"], list):
        out["include_ids"] = [int(x) for x in raw["include_ids"] if str(x).isdigit()]
    if "manual_tag_include_ids" in raw and isinstance(raw["manual_tag_include_ids"], list):
        out["manual_tag_include_ids"] = [
            int(x) for x in raw["manual_tag_include_ids"] if str(x).isdigit()
        ]
    if "manual_tag_exclude_ids" in raw and isinstance(raw["manual_tag_exclude_ids"], list):
        out["manual_tag_exclude_ids"] = [
            int(x) for x in raw["manual_tag_exclude_ids"] if str(x).isdigit()
        ]
    mode = (raw.get("manual_tag_include_mode") or "").strip().lower()
    if mode in ("any", "all"):
        out["manual_tag_include_mode"] = mode
    base = raw.get("base") or campaign.audience
    if base in ("all", "birthday_today", "manual"):
        out["base"] = base
    elif campaign.audience == Campaign.Audience.BIRTHDAY_TODAY:
        out["base"] = "birthday_today"
    else:
        out["base"] = "all"
    return out


def _parse_birthday_date(value: str | None) -> date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def contacts_queryset_for_campaign(campaign: Campaign) -> QuerySet[Contact]:
    """Final QuerySet: base + AND filters, minus exclude_ids, plus include_ids."""
    cfg = get_audience_config(campaign)
    base = cfg.get("base") or "all"
    exclude = set(cfg.get("exclude_ids") or [])
    include = set(cfg.get("include_ids") or [])

    include_tag_ids = [
        int(x) for x in (cfg.get("manual_tag_include_ids") or []) if str(x).isdigit()
    ]
    exclude_tag_ids = [
        int(x) for x in (cfg.get("manual_tag_exclude_ids") or []) if str(x).isdigit()
    ]
    include_mode = (cfg.get("manual_tag_include_mode") or "any").lower()

    if base == "manual":
        final_ids = include - exclude
        if not final_ids:
            return Contact.objects.none()
        qs = Contact.objects.filter(pk__in=final_ids, deleted_at__isnull=True)
        if include_tag_ids:
            if include_mode == "all":
                for tid in include_tag_ids:
                    qs = qs.filter(manual_tags__id=tid)
            else:
                qs = qs.filter(manual_tags__id__in=include_tag_ids)
        if exclude_tag_ids:
            qs = qs.exclude(manual_tags__id__in=exclude_tag_ids)
        return qs.distinct().order_by("pk")

    qs = Contact.objects.filter(deleted_at__isnull=True)
    if base == "birthday_today":
        chosen = _parse_birthday_date(cfg.get("birthday_date"))
        today = chosen or timezone.localdate()
        qs = qs.filter(
            birth_date__month=today.month,
            birth_date__day=today.day,
        )

    city = (cfg.get("city") or "").strip()
    if city:
        qs = qs.filter(city__iexact=city)
    state = (cfg.get("state") or "").strip()
    if state:
        qs = qs.filter(state__iexact=state[:5])
    neighborhood = (cfg.get("neighborhood") or "").strip()
    if neighborhood:
        qs = qs.filter(neighborhood__iexact=neighborhood)
    cat = (cfg.get("category") or "").strip()
    if cat:
        qs = qs.filter(category__iexact=cat)
    sub = (cfg.get("subcategory") or "").strip()
    if sub:
        qs = qs.filter(subcategory__iexact=sub)
    if cfg.get("whatsapp_only"):
        qs = qs.filter(has_whatsapp=True)

    if include_tag_ids:
        if include_mode == "all":
            for tid in include_tag_ids:
                qs = qs.filter(manual_tags__id=tid)
        else:
            qs = qs.filter(manual_tags__id__in=include_tag_ids)
    if exclude_tag_ids:
        qs = qs.exclude(manual_tags__id__in=exclude_tag_ids)
    if include_tag_ids or exclude_tag_ids:
        qs = qs.distinct()

    base_ids = set(qs.values_list("pk", flat=True))
    final_ids = (base_ids - exclude) | include
    if not final_ids:
        return Contact.objects.none()
    return Contact.objects.filter(pk__in=final_ids, deleted_at__isnull=True).order_by("pk")


def contact_ids_for_campaign_audience(campaign: Campaign) -> list[int]:
    return list(contacts_queryset_for_campaign(campaign).values_list("pk", flat=True))


def count_contacts_for_audience(campaign: Campaign) -> int:
    return contacts_queryset_for_campaign(campaign).count()


def parse_audience_post(post) -> dict[str, Any]:
    """Build audience_config from the audience wizard POST (exclusions are handled in the view)."""
    base = post.get("base") or "all"
    if base not in ("all", "birthday_today", "manual"):
        base = "all"
    birthday_date = ""
    if base == "birthday_today":
        chosen = _parse_birthday_date(post.get("birthday_date"))
        birthday_date = (chosen or timezone.localdate()).isoformat()
    raw_inc = post.getlist("manual_tag_include_ids") if hasattr(post, "getlist") else []
    raw_exc = post.getlist("manual_tag_exclude_ids") if hasattr(post, "getlist") else []
    manual_tag_include_ids = [int(x) for x in raw_inc if str(x).isdigit()]
    manual_tag_exclude_ids = [int(x) for x in raw_exc if str(x).isdigit()]
    include_mode = (post.get("manual_tag_include_mode") or "any").strip().lower()
    if include_mode not in ("any", "all"):
        include_mode = "any"
    # Conflict: the same tag in both "must have" and "must not have".
    # Rule: "must not have" wins (the tag is removed from the include list).
    exc_set = set(manual_tag_exclude_ids)
    manual_tag_include_ids = [x for x in manual_tag_include_ids if x not in exc_set]
    return {
        "base": base,
        "birthday_date": birthday_date,
        "manual_tag_include_ids": manual_tag_include_ids,
        "manual_tag_exclude_ids": manual_tag_exclude_ids,
        "manual_tag_include_mode": include_mode,
        "city": (post.get("city") or "").strip()[:100],
        "state": (post.get("state") or "").strip()[:5],
        "neighborhood": (post.get("neighborhood") or "").strip()[:100],
        "category": (post.get("category") or "").strip()[:100],
        "subcategory": (post.get("subcategory") or "").strip()[:100],
        "whatsapp_only": post.get("whatsapp_only") == "on",
    }


def audience_summary_text(campaign: Campaign) -> str:
    """Short text for the dashboard / lists."""
    cfg = get_audience_config(campaign)
    parts = []
    base = cfg.get("base") or "all"
    if base == "birthday_today":
        chosen = _parse_birthday_date(cfg.get("birthday_date"))
        if chosen:
            parts.append(f"Aniversariantes ({chosen.strftime('%d/%m')})")
        else:
            parts.append("Aniversariantes do dia")
    elif base == "manual":
        parts.append("Somente incluídos manualmente")
    else:
        parts.append("Todos")
    if cfg.get("city"):
        parts.append(f"Cidade {cfg['city']}")
    if cfg.get("state"):
        parts.append(f"UF {cfg['state']}")
    if cfg.get("neighborhood"):
        parts.append(f"Bairro {cfg['neighborhood']}")
    if cfg.get("category"):
        parts.append(f"Cat. {cfg['category']}")
    if cfg.get("subcategory"):
        parts.append(f"Subcat. {cfg['subcategory']}")
    if cfg.get("whatsapp_only"):
        parts.append("Só WhatsApp")
    inc_ids = cfg.get("manual_tag_include_ids") or []
    exc_ids = cfg.get("manual_tag_exclude_ids") or []
    if inc_ids:
        names = list(
            ManualTag.objects.filter(pk__in=inc_ids).values_list("name", flat=True).order_by("name")
        )
        if names:
            mode = cfg.get("manual_tag_include_mode") or "any"
            label = "Tags +" + ("(todas): " if mode == "all" else ": ")
            parts.append(label + ", ".join(names[:6]) + ("…" if len(names) > 6 else ""))
    if exc_ids:
        names = list(
            ManualTag.objects.filter(pk__in=exc_ids).values_list("name", flat=True).order_by("name")
        )
        if names:
            parts.append("Tags −: " + ", ".join(names[:6]) + ("…" if len(names) > 6 else ""))
    ex = len(cfg.get("exclude_ids") or [])
    inc = len(cfg.get("include_ids") or [])
    if ex:
        parts.append(f"−{ex} exclusão(ões)")
    if inc:
        parts.append(f"+{inc} inclusão(ões)")
    return " · ".join(parts) if parts else "—"


def sync_campaign_audience_field(campaign: Campaign) -> None:
    """Keep campaign.audience aligned with the main preset (legacy listings)."""
    cfg = get_audience_config(campaign)
    campaign.audience = (
        Campaign.Audience.BIRTHDAY_TODAY
        if cfg.get("base") == "birthday_today"
        else Campaign.Audience.ALL
    )
