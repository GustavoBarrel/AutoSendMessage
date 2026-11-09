from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import (
    Avg,
    CharField,
    Count,
    F,
    Q,
    Value,
)
from django.db.models.functions import (
    Coalesce,
    ExtractHour,
    TruncDate,
)
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.accounts.decorators import workspace_required
from apps.campaigns.models import Campaign, CampaignSendJob
from apps.chips.models import Chip
from apps.common.utils.csv_safety import neutralize_csv_cell
from apps.contacts.models import Contact
from apps.dashboards.constants import MAX_WIDGET_PERIOD_DAYS
from apps.dashboards.models import StatsDashboard, StatsDashboardWidget
from apps.dashboards.stats_datasets import dataset_help_for_template

_DATE_FIELD_LOOKUP = {
    "created": "created_at",
    "updated": "updated_at",
    "sent": "sent_at",
}

_DEFAULT_DATASET_KEY = "status"
_DEFAULT_CHART_TYPE = "bar"
_VALID_DATASETS = {choice.value for choice in StatsDashboardWidget.Dataset}
_DATASET_DEFAULT_LABELS = dict(StatsDashboardWidget.Dataset.choices)
_VALID_CHART_TYPES = {"bar", "pie", "doughnut", "line"}


def _parse_date(raw_value: str | None) -> date | None:
    raw = (raw_value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _build_base_filters(request, workspace):
    today = timezone.localdate()
    start_default = today - timedelta(days=29)

    start_date = _parse_date(request.GET.get("start")) or start_default
    end_date = _parse_date(request.GET.get("end")) or today
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    date_field = (request.GET.get("date_field") or "created").strip().lower()
    if date_field not in _DATE_FIELD_LOOKUP:
        date_field = "created"
    date_lookup = _DATE_FIELD_LOOKUP[date_field]

    selected_status = (request.GET.get("status") or "").strip().lower()
    valid_statuses = {choice[0] for choice in CampaignSendJob.Status.choices}
    if selected_status not in valid_statuses:
        selected_status = ""

    selected_dataset = (request.GET.get("dataset") or _DEFAULT_DATASET_KEY).strip().lower()
    if selected_dataset not in _VALID_DATASETS:
        selected_dataset = _DEFAULT_DATASET_KEY

    selected_chart_type = (request.GET.get("chart") or _DEFAULT_CHART_TYPE).strip().lower()
    if selected_chart_type not in _VALID_CHART_TYPES:
        selected_chart_type = _DEFAULT_CHART_TYPE

    campaign_options = list(
        Campaign.objects.filter(workspace=workspace, deleted_at__isnull=True)
        .order_by("-created_at")
        .only("id", "name")
    )
    chip_options = list(
        Chip.objects.filter(workspace=workspace).order_by("name").only("id", "name")
    )

    selected_campaign = None
    raw_campaign_id = (request.GET.get("campaign_id") or "").strip()
    if raw_campaign_id.isdigit():
        selected_campaign = next(
            (c for c in campaign_options if c.pk == int(raw_campaign_id)), None
        )

    selected_chip = None
    raw_chip_id = (request.GET.get("chip_id") or "").strip()
    if raw_chip_id.isdigit():
        selected_chip = next((c for c in chip_options if c.pk == int(raw_chip_id)), None)

    jobs_qs = CampaignSendJob.objects.filter(campaign__workspace=workspace).filter(
        **{
            f"{date_lookup}__date__gte": start_date,
            f"{date_lookup}__date__lte": end_date,
        }
    )
    if selected_campaign:
        jobs_qs = jobs_qs.filter(campaign_id=selected_campaign.pk)
    if selected_chip:
        jobs_qs = jobs_qs.filter(chip_id=selected_chip.pk)
    if selected_status:
        jobs_qs = jobs_qs.filter(status=selected_status)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "date_field": date_field,
        "date_lookup": date_lookup,
        "selected_status": selected_status,
        "selected_dataset": selected_dataset,
        "selected_chart_type": selected_chart_type,
        "campaign_options": campaign_options,
        "chip_options": chip_options,
        "selected_campaign": selected_campaign,
        "selected_chip": selected_chip,
        "jobs_qs": jobs_qs,
    }


def _compute_totals(jobs_qs):
    totals = jobs_qs.aggregate(
        total=Count("id"),
        sent=Count("id", filter=Q(status=CampaignSendJob.Status.SENT)),
        failed=Count("id", filter=Q(status=CampaignSendJob.Status.FAILED)),
        pending=Count("id", filter=Q(status=CampaignSendJob.Status.PENDING)),
        waiting_chip=Count("id", filter=Q(status=CampaignSendJob.Status.WAITING_CHIP)),
        locked=Count("id", filter=Q(status=CampaignSendJob.Status.LOCKED)),
        cancelled=Count("id", filter=Q(status=CampaignSendJob.Status.CANCELLED)),
        skipped=Count("id", filter=Q(status=CampaignSendJob.Status.SKIPPED)),
    )
    finalized_total = totals["sent"] + totals["failed"]
    success_rate = (totals["sent"] / finalized_total * 100) if finalized_total else None
    delivery_rate = (totals["sent"] / totals["total"] * 100) if totals["total"] else None
    cancelled_and_skipped = totals["cancelled"] + totals["skipped"]
    return totals, success_rate, delivery_rate, cancelled_and_skipped


def _compute_status_rows(jobs_qs):
    status_count_rows_raw = jobs_qs.values("status").annotate(total=Count("id"))
    status_count_map = {row["status"]: row["total"] for row in status_count_rows_raw}
    return [
        {"status": key, "label": label, "total": status_count_map.get(key, 0)}
        for key, label in CampaignSendJob.Status.choices
    ]


def _compute_by_chip(jobs_qs):
    return list(
        jobs_qs.annotate(
            chip_name_display=Coalesce(
                "chip__name",
                "chip_name_snapshot",
                Value("Sem chip", output_field=CharField()),
            )
        )
        .values("chip_name_display")
        .annotate(
            total=Count("id"),
            sent=Count("id", filter=Q(status=CampaignSendJob.Status.SENT)),
            failed=Count("id", filter=Q(status=CampaignSendJob.Status.FAILED)),
            pending=Count("id", filter=Q(status=CampaignSendJob.Status.PENDING)),
            waiting_chip=Count("id", filter=Q(status=CampaignSendJob.Status.WAITING_CHIP)),
            locked=Count("id", filter=Q(status=CampaignSendJob.Status.LOCKED)),
        )
        .order_by("-total", "chip_name_display")[:15]
    )


def _compute_by_campaign(jobs_qs):
    return list(
        jobs_qs.annotate(
            campaign_name_display=Coalesce(
                "campaign__name",
                Value("Sem campanha", output_field=CharField()),
            )
        )
        .values("campaign_name_display")
        .annotate(
            total=Count("id"),
            sent=Count("id", filter=Q(status=CampaignSendJob.Status.SENT)),
            failed=Count("id", filter=Q(status=CampaignSendJob.Status.FAILED)),
            pending=Count("id", filter=Q(status=CampaignSendJob.Status.PENDING)),
        )
        .order_by("-total", "campaign_name_display")[:15]
    )


def _compute_by_day(jobs_qs, date_lookup):
    return list(
        jobs_qs.exclude(**{f"{date_lookup}__isnull": True})
        .annotate(ref_day=TruncDate(date_lookup))
        .values("ref_day")
        .annotate(
            total=Count("id"),
            sent=Count("id", filter=Q(status=CampaignSendJob.Status.SENT)),
            failed=Count("id", filter=Q(status=CampaignSendJob.Status.FAILED)),
        )
        .order_by("ref_day")
    )


def _compute_hourly(jobs_qs):
    """Sends per hour of the day (0-23) based on sent_at."""
    rows = list(
        jobs_qs.filter(sent_at__isnull=False)
        .annotate(hour=ExtractHour("sent_at"))
        .values("hour")
        .annotate(total=Count("id"))
        .order_by("hour")
    )
    hour_map = {row["hour"]: row["total"] for row in rows}
    labels = [f"{h:02d}h" for h in range(24)]
    values = [hour_map.get(h, 0) for h in range(24)]
    return labels, values


def _compute_chip_fail_rate(by_chip_rows):
    """Failure rate per chip (%) ordered from worst to best."""
    result = []
    for row in by_chip_rows:
        finalized = row["sent"] + row["failed"]
        rate = (row["failed"] / finalized * 100) if finalized else 0.0
        result.append(
            {
                "chip_name_display": row["chip_name_display"],
                "total": row["total"],
                "sent": row["sent"],
                "failed": row["failed"],
                "fail_rate": round(rate, 1),
            }
        )
    result.sort(key=lambda x: x["fail_rate"], reverse=True)
    return result


def _compute_chip_success_rate(by_chip_rows):
    """Success rate per chip (%) among finalized sends."""
    result = []
    for row in by_chip_rows:
        finalized = row["sent"] + row["failed"]
        rate = (row["sent"] / finalized * 100) if finalized else 0.0
        result.append(
            {
                "chip_name_display": row["chip_name_display"],
                "success_rate": round(rate, 1),
            }
        )
    result.sort(key=lambda x: x["success_rate"], reverse=True)
    return result


def _compute_campaign_fail_rate(by_campaign_rows):
    """Failure rate per campaign (%)."""
    result = []
    for row in by_campaign_rows:
        finalized = row["sent"] + row["failed"]
        rate = (row["failed"] / finalized * 100) if finalized else 0.0
        result.append(
            {
                "campaign_name_display": row["campaign_name_display"],
                "fail_rate": round(rate, 1),
            }
        )
    result.sort(key=lambda x: x["fail_rate"], reverse=True)
    return result


def _compute_campaign_success_rate(by_campaign_rows):
    """Success rate per campaign (%)."""
    result = []
    for row in by_campaign_rows:
        finalized = row["sent"] + row["failed"]
        rate = (row["sent"] / finalized * 100) if finalized else 0.0
        result.append(
            {
                "campaign_name_display": row["campaign_name_display"],
                "success_rate": round(rate, 1),
            }
        )
    result.sort(key=lambda x: x["success_rate"], reverse=True)
    return result


_CONTACTS_DATASET_FIELDS = {
    "contacts_city": "city",
    "contacts_state": "state",
    "contacts_neighborhood": "neighborhood",
    "contacts_category": "category",
    "contacts_subcategory": "subcategory",
    "contacts_gender": "gender",
}


def _compute_defer_reasons(jobs_qs):
    """Count per deferral reason (rate limit / chip offline)."""
    reason_labels = dict(CampaignSendJob.ScheduleDeferReason.choices)
    rows = list(
        jobs_qs.exclude(schedule_defer_reason="")
        .values("schedule_defer_reason")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    labels = [
        reason_labels.get(row["schedule_defer_reason"], row["schedule_defer_reason"])
        for row in rows
    ]
    values = [row["total"] for row in rows]
    return labels, values


def _compute_attempts_distribution(jobs_qs):
    """How many sends finished on the 1st, 2nd or 3rd+ attempt."""
    finalized = jobs_qs.filter(
        status__in=[CampaignSendJob.Status.SENT, CampaignSendJob.Status.FAILED],
    )
    buckets = [
        ("1 tentativa", Q(attempts=1)),
        ("2 tentativas", Q(attempts=2)),
        ("3+ tentativas", Q(attempts__gte=3)),
    ]
    labels: list[str] = []
    values: list[int] = []
    for label, q in buckets:
        n = finalized.filter(q).count()
        if n:
            labels.append(label)
            values.append(n)
    return labels, values


def _compute_by_variant(jobs_qs):
    rows = list(
        jobs_qs.filter(variant__isnull=False)
        .values("variant__position")
        .annotate(total=Count("id"))
        .order_by("variant__position")
    )
    labels = [f"Variação {row['variant__position']}" for row in rows]
    values = [row["total"] for row in rows]
    return labels, values


def _compute_by_message_type(jobs_qs):
    from apps.campaigns.models import CampaignMessageVariant

    message_type_labels = dict(CampaignMessageVariant.MessageType.choices)
    rows = list(
        jobs_qs.filter(variant__isnull=False, status=CampaignSendJob.Status.SENT)
        .values("variant__message_type")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    labels = [
        message_type_labels.get(row["variant__message_type"], row["variant__message_type"])
        for row in rows
    ]
    values = [row["total"] for row in rows]
    return labels, values


def _compute_contacts_by_field(jobs_qs, field: str, *, limit: int = 15):
    """Successful sends grouped by contact field (city, state...)."""
    rows = list(
        jobs_qs.filter(status=CampaignSendJob.Status.SENT)
        .exclude(**{f"contact__{field}": ""})
        .values(f"contact__{field}")
        .annotate(total=Count("id"))
        .order_by("-total")[:limit]
    )
    labels = [row[f"contact__{field}"] for row in rows]
    values = [row["total"] for row in rows]
    return labels, values


def _chart_payload_for_dataset(
    dataset_key: str,
    jobs_qs,
    *,
    default_label: str = "",
    date_lookup: str = "created_at",
) -> dict:
    """Build Chart.js labels/values for a dataset."""
    ds = dataset_key
    label = default_label or _DATASET_DEFAULT_LABELS.get(ds, "Dados")
    labels: list[str] = []
    values: list[int | float] = []

    if ds == "status":
        rows = _compute_status_rows(jobs_qs)
        labels = [r["label"] for r in rows if r["total"] > 0]
        values = [r["total"] for r in rows if r["total"] > 0]
    elif ds in (
        "chip_total",
        "chip_sent",
        "chip_failed",
        "chip_pending",
        "chip_waiting",
        "chip_locked",
    ):
        by_chip = _compute_by_chip(jobs_qs)
        labels = [r["chip_name_display"] for r in by_chip]
        field_map = {
            "chip_total": "total",
            "chip_sent": "sent",
            "chip_failed": "failed",
            "chip_pending": "pending",
            "chip_waiting": "waiting_chip",
            "chip_locked": "locked",
        }
        values = [r[field_map[ds]] for r in by_chip]
    elif ds == "chip_fail_rate":
        rates = _compute_chip_fail_rate(_compute_by_chip(jobs_qs))
        labels = [r["chip_name_display"] for r in rates]
        values = [r["fail_rate"] for r in rates]
    elif ds == "chip_success_rate":
        rates = _compute_chip_success_rate(_compute_by_chip(jobs_qs))
        labels = [r["chip_name_display"] for r in rates]
        values = [r["success_rate"] for r in rates]
    elif ds == "chip_avg_time":
        rows = _compute_chip_avg_time(jobs_qs)
        labels = [r["chip_name_display"] for r in rows]
        values = [r["avg_minutes"] for r in rows]
    elif ds in ("campaign_total", "campaign_sent", "campaign_failed", "campaign_pending"):
        by_camp = _compute_by_campaign(jobs_qs)
        labels = [r["campaign_name_display"] for r in by_camp]
        field_map = {
            "campaign_total": "total",
            "campaign_sent": "sent",
            "campaign_failed": "failed",
            "campaign_pending": "pending",
        }
        values = [r[field_map[ds]] for r in by_camp]
    elif ds == "campaign_fail_rate":
        rates = _compute_campaign_fail_rate(_compute_by_campaign(jobs_qs))
        labels = [r["campaign_name_display"] for r in rates]
        values = [r["fail_rate"] for r in rates]
    elif ds == "campaign_success_rate":
        rates = _compute_campaign_success_rate(_compute_by_campaign(jobs_qs))
        labels = [r["campaign_name_display"] for r in rates]
        values = [r["success_rate"] for r in rates]
    elif ds in ("daily_total", "daily_sent", "daily_failed"):
        by_day = _compute_by_day(jobs_qs, date_lookup)
        labels = [r["ref_day"].strftime("%d/%m") for r in by_day]
        field_map = {
            "daily_total": "total",
            "daily_sent": "sent",
            "daily_failed": "failed",
        }
        values = [r[field_map[ds]] for r in by_day]
    elif ds == "hourly":
        labels, values = _compute_hourly(jobs_qs)
    elif ds == "top_errors":
        rows = _compute_top_errors(jobs_qs, limit=12)
        labels = [r["error"] for r in rows]
        values = [r["count"] for r in rows]
    elif ds == "defer_reason":
        labels, values = _compute_defer_reasons(jobs_qs)
    elif ds == "attempts":
        labels, values = _compute_attempts_distribution(jobs_qs)
    elif ds == "variant_total":
        labels, values = _compute_by_variant(jobs_qs)
    elif ds == "message_type":
        labels, values = _compute_by_message_type(jobs_qs)
    elif ds in _CONTACTS_DATASET_FIELDS:
        labels, values = _compute_contacts_by_field(
            jobs_qs,
            _CONTACTS_DATASET_FIELDS[ds],
        )

    return {"label": label, "labels": labels, "values": values}


def _compute_chip_avg_time(jobs_qs):
    """Average time between job creation and the actual send, per chip (in minutes)."""
    rows = list(
        jobs_qs.filter(
            status=CampaignSendJob.Status.SENT,
            sent_at__isnull=False,
        )
        .annotate(
            chip_name_display=Coalesce(
                "chip__name",
                "chip_name_snapshot",
                Value("Sem chip", output_field=CharField()),
            ),
            delta=F("sent_at") - F("created_at"),
        )
        .values("chip_name_display")
        .annotate(avg_delta=Avg("delta"))
        .order_by("chip_name_display")
    )
    result = []
    for row in rows:
        avg = row["avg_delta"]
        minutes = avg.total_seconds() / 60 if avg else 0
        result.append(
            {
                "chip_name_display": row["chip_name_display"],
                "avg_minutes": round(minutes, 1),
            }
        )
    result.sort(key=lambda x: x["avg_minutes"])
    return result


_FRIENDLY_ERROR_RULES: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"exists.*false|not on whatsapp|not registered|recipient not found", re.I),
        "Número não existe no WhatsApp",
    ),
    (re.compile(r"invalid jid|invalid wid", re.I), "Número inválido (formato incorreto)"),
    (
        re.compile(r"connectionState.*close|desconectad|chip offline|Evolution.*desconectad", re.I),
        "Chip desconectado (WhatsApp offline)",
    ),
    (
        re.compile(r"erro 463|timelock|tctoken|restri[cç][aã]o de alcance", re.I),
        "Chip restrito pelo WhatsApp (erro 463 — mensagem não enviada)",
    ),
    (re.compile(r"Timeout|timed?\s*out", re.I), "Timeout — Evolution demorou para responder"),
    (
        re.compile(r"owned media must be a url|mídia em url só acessível|localhost", re.I),
        "Mídia inacessível (URL local/localhost)",
    ),
    (re.compile(r"Texto da mensagem vazio|empty.*text", re.I), "Mensagem com texto vazio"),
    (re.compile(r"URL d[ao] (imagem|vídeo) não informada", re.I), "URL da mídia não informada"),
    (
        re.compile(r"Variação de mensagem não encontrada", re.I),
        "Variação de mensagem removida ou inválida",
    ),
    (
        re.compile(r"Chip excluído|Reconfigure a campanha", re.I),
        "Chip excluído — campanha precisa ser reconfigurada",
    ),
    (re.compile(r"Contato excluído", re.I), "Contato excluído manualmente"),
    (
        re.compile(r"Evolution não configurada|EVOLUTION_URL_BASE|EVOLUTION_API_KEY", re.I),
        "Evolution API não configurada (.env)",
    ),
    (re.compile(r"muito grande|payload|OOM", re.I), "Arquivo de mídia muito grande para envio"),
    (
        re.compile(r"Connection(Error|Refused|Reset)|ECONNREFUSED|cannot connect", re.I),
        "Evolution API inacessível (conexão recusada)",
    ),
    (
        re.compile(r"HTTP\s*4\d\d|Bad Request|Internal Server Error|HTTP\s*5\d\d", re.I),
        "Erro HTTP na Evolution API",
    ),
]


def _friendly_error(raw_error: str) -> str:
    for pattern, friendly in _FRIENDLY_ERROR_RULES:
        if pattern.search(raw_error):
            return friendly
    truncated = raw_error[:80].strip()
    if len(raw_error) > 80:
        truncated += "…"
    return truncated


def _compute_top_errors(jobs_qs, limit: int = 10):
    """Most frequent errors, grouped by a friendly category."""
    errors = list(
        jobs_qs.filter(
            status=CampaignSendJob.Status.FAILED,
            last_error__gt="",
        ).values_list("last_error", flat=True)
    )
    counter: Counter[str] = Counter()
    for err in errors:
        raw = (err or "").strip()
        if raw:
            counter[_friendly_error(raw)] += 1
    return [{"error": err, "count": count} for err, count in counter.most_common(limit)]


def _compute_contacts_no_whatsapp(workspace, limit: int = 50):
    """Contacts with a permanent failure (exists=false) in the period."""
    job_ids = list(
        CampaignSendJob.objects.filter(
            campaign__workspace=workspace,
            status=CampaignSendJob.Status.FAILED,
            last_error__icontains="exists",
        )
        .values_list("contact_id", flat=True)
        .distinct()[:limit]
    )
    if not job_ids:
        return []
    contacts = Contact.objects.filter(pk__in=job_ids).only(
        "id", "name", "mobile_phone", "deleted_at"
    )
    return [
        {
            "name": c.name or "—",
            "mobile_phone": c.mobile_phone,
            "archived": c.deleted_at is not None,
        }
        for c in contacts
    ]


def _compute_active_campaign_progress(workspace):
    """Progress of active campaigns (sent/total bar)."""
    active = Campaign.objects.filter(
        workspace=workspace,
        status=Campaign.Status.ACTIVE,
        deleted_at__isnull=True,
    ).only("id", "name")

    result = []
    for camp in active:
        agg = CampaignSendJob.objects.filter(campaign=camp).aggregate(
            total=Count("id"),
            sent=Count("id", filter=Q(status=CampaignSendJob.Status.SENT)),
            failed=Count("id", filter=Q(status=CampaignSendJob.Status.FAILED)),
            pending=Count(
                "id",
                filter=Q(
                    status__in=[
                        CampaignSendJob.Status.PENDING,
                        CampaignSendJob.Status.LOCKED,
                        CampaignSendJob.Status.WAITING_CHIP,
                    ]
                ),
            ),
        )
        total = agg["total"] or 0
        sent = agg["sent"] or 0
        failed = agg["failed"] or 0
        pct = (sent / total * 100) if total else 0
        result.append(
            {
                "name": camp.name,
                "total": total,
                "sent": sent,
                "failed": failed,
                "pending": agg["pending"] or 0,
                "pct": round(pct, 1),
            }
        )
    return result


def _compute_chip_health(workspace):
    """Chip health: current-day metrics per chip."""
    today = timezone.localdate()
    chips = Chip.objects.filter(workspace=workspace, is_active=True).order_by("name")
    result = []
    for chip in chips:
        day_qs = CampaignSendJob.objects.filter(
            chip=chip,
            sent_at__date=today,
        )
        agg = day_qs.aggregate(
            sent_today=Count("id", filter=Q(status=CampaignSendJob.Status.SENT)),
            failed_today=Count("id", filter=Q(status=CampaignSendJob.Status.FAILED)),
        )
        total_qs = CampaignSendJob.objects.filter(chip=chip)
        total_agg = total_qs.aggregate(
            total_sent=Count("id", filter=Q(status=CampaignSendJob.Status.SENT)),
            total_failed=Count("id", filter=Q(status=CampaignSendJob.Status.FAILED)),
        )
        total_finalized = total_agg["total_sent"] + total_agg["total_failed"]
        success_pct = (total_agg["total_sent"] / total_finalized * 100) if total_finalized else None

        last_sent = (
            CampaignSendJob.objects.filter(chip=chip, status=CampaignSendJob.Status.SENT)
            .order_by("-sent_at")
            .values_list("sent_at", flat=True)
            .first()
        )
        pending = CampaignSendJob.objects.filter(
            chip=chip,
            status__in=[
                CampaignSendJob.Status.PENDING,
                CampaignSendJob.Status.WAITING_CHIP,
            ],
        ).count()
        result.append(
            {
                "name": chip.name,
                "instance_name": chip.instance_name,
                "account_type": chip.get_account_type_display(),
                "sent_today": agg["sent_today"] or 0,
                "failed_today": agg["failed_today"] or 0,
                "total_sent": total_agg["total_sent"] or 0,
                "total_failed": total_agg["total_failed"] or 0,
                "success_pct": round(success_pct, 1) if success_pct is not None else None,
                "last_sent": last_sent,
                "pending": pending,
            }
        )
    return result


def _compute_alerts(workspace, totals, chip_health):
    """Automatic alerts based on the current state."""
    alerts: list[dict[str, str]] = []

    chips_offline = [c for c in chip_health if c["failed_today"] > 5 and c["sent_today"] == 0]
    for c in chips_offline:
        alerts.append(
            {
                "level": "danger",
                "message": (
                    f"Chip «{c['name']}» com {c['failed_today']} falhas hoje "
                    "e 0 envios — possível desconexão."
                ),
            }
        )

    for c in chip_health:
        finalized = c["sent_today"] + c["failed_today"]
        if finalized >= 5 and c["failed_today"] / finalized > 0.5:
            if c not in chips_offline:
                alerts.append(
                    {
                        "level": "warning",
                        "message": (
                            f"Chip «{c['name']}» com taxa de falha alta hoje: "
                            f"{c['failed_today']}/{finalized} "
                            f"({c['failed_today'] / finalized * 100:.0f}%)."
                        ),
                    }
                )

    active_count = Campaign.objects.filter(
        workspace=workspace,
        status=Campaign.Status.ACTIVE,
        deleted_at__isnull=True,
    ).count()
    active_chips = Chip.objects.filter(workspace=workspace, is_active=True).count()
    if active_count > 0 and active_chips == 0:
        alerts.append(
            {
                "level": "danger",
                "message": "Há campanhas ativas mas nenhum chip ativo cadastrado.",
            }
        )

    if totals.get("waiting_chip", 0) > 20:
        alerts.append(
            {
                "level": "warning",
                "message": (
                    f"{totals['waiting_chip']} mensagens aguardando chip (offline). "
                    "Verifique a conexão dos chips."
                ),
            }
        )

    return alerts


@login_required
@workspace_required
def stats_dashboard(request):
    workspace = request.current_workspace
    f = _build_base_filters(request, workspace)
    jobs_qs = f["jobs_qs"]

    totals, success_rate, delivery_rate, cancelled_and_skipped = _compute_totals(jobs_qs)
    status_rows = _compute_status_rows(jobs_qs)
    by_chip_rows = _compute_by_chip(jobs_qs)
    by_campaign_rows = _compute_by_campaign(jobs_qs)
    by_day_rows = _compute_by_day(jobs_qs, f["date_lookup"])

    chip_fail_rate = _compute_chip_fail_rate(by_chip_rows)
    chip_avg_time = _compute_chip_avg_time(jobs_qs)
    top_errors = _compute_top_errors(jobs_qs)
    contacts_no_wpp = _compute_contacts_no_whatsapp(workspace)
    active_progress = _compute_active_campaign_progress(workspace)
    chip_health = _compute_chip_health(workspace)
    alerts = _compute_alerts(workspace, totals, chip_health)

    date_lookup = f["date_lookup"]
    chart_payload = {
        key: _chart_payload_for_dataset(key, jobs_qs, date_lookup=date_lookup)
        for key in _VALID_DATASETS
    }

    context = {
        "current_workspace": workspace,
        "campaign_options": f["campaign_options"],
        "chip_options": f["chip_options"],
        "selected_campaign": f["selected_campaign"],
        "selected_chip": f["selected_chip"],
        "selected_status": f["selected_status"],
        "selected_dataset": f["selected_dataset"],
        "selected_chart_type": f["selected_chart_type"],
        "selected_date_field": f["date_field"],
        "start_date": f["start_date"],
        "end_date": f["end_date"],
        "totals": totals,
        "success_rate": success_rate,
        "delivery_rate": delivery_rate,
        "cancelled_and_skipped": cancelled_and_skipped,
        "status_rows": status_rows,
        "by_chip_rows": by_chip_rows,
        "by_campaign_rows": by_campaign_rows,
        "by_day_rows": by_day_rows,
        "chart_payload": chart_payload,
        "dataset_options": StatsDashboardWidget.Dataset.choices,
        "dataset_help": dataset_help_for_template(),
        "status_choices": CampaignSendJob.Status.choices,
        # New sections
        "chip_fail_rate": chip_fail_rate,
        "chip_avg_time": chip_avg_time,
        "top_errors": top_errors,
        "contacts_no_wpp": contacts_no_wpp,
        "active_progress": active_progress,
        "chip_health": chip_health,
        "alerts": alerts,
    }
    return render(request, "dashboards/dashboard.html", context)


@login_required
@workspace_required
def stats_export_csv(request):
    """Export the filtered jobs as CSV."""
    workspace = request.current_workspace
    f = _build_base_filters(request, workspace)
    jobs_qs = f["jobs_qs"]

    rows = jobs_qs.select_related("campaign", "chip", "contact").order_by("pk")[:10_000]

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="estatisticas_envios.csv"'
    response.write("\ufeff")

    writer = csv.writer(response, delimiter=";")
    writer.writerow(
        [
            "ID",
            "Campanha",
            "Chip",
            "Contato",
            "Celular",
            "Status",
            "Tentativas",
            "Erro",
            "Criado em",
            "Enviado em",
        ]
    )
    for job in rows:
        writer.writerow(
            [
                job.pk,
                neutralize_csv_cell(job.campaign.name if job.campaign else "—"),
                neutralize_csv_cell(job.chip.name if job.chip else (job.chip_name_snapshot or "—")),
                neutralize_csv_cell(job.contact.name if job.contact else "—"),
                job.contact.mobile_phone if job.contact else "—",
                job.get_status_display(),
                job.attempts,
                neutralize_csv_cell((job.last_error or "")[:200]),
                job.created_at.strftime("%d/%m/%Y %H:%M") if job.created_at else "",
                job.sent_at.strftime("%d/%m/%Y %H:%M") if job.sent_at else "",
            ]
        )

    return response


# ═══════════════════════════════════════════════════════════════════════════════
# Custom dashboards
# ═══════════════════════════════════════════════════════════════════════════════


def _parse_period_days(raw: object) -> int:
    """Parse a "days ago" value; raise ValueError unless it is an int within the window."""
    days = int(raw)  # TypeError/ValueError for anything that is not an integer
    if not 0 <= days <= MAX_WIDGET_PERIOD_DAYS:
        raise ValueError(f"days out of range: {days}")
    return days


def _clamped_period_days(raw: object, default: int) -> int:
    """Lenient version for stored filters (e.g. edited in the admin): never raises."""
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return default
    return min(max(days, 0), MAX_WIDGET_PERIOD_DAYS)


def _filters_json_from_preview_params(params) -> dict:
    """Build filters_json from the preview query string (widget form).

    Raises ValueError if the period is not an integer within 0..MAX_WIDGET_PERIOD_DAYS.
    """
    fj: dict = {}
    campaign_id = params.get("campaign_id") or params.get("filter_campaign_id")
    chip_id = params.get("chip_id") or params.get("filter_chip_id")
    if campaign_id not in (None, ""):
        try:
            fj["campaign_id"] = int(campaign_id)
        except (TypeError, ValueError):
            pass
    if chip_id not in (None, ""):
        try:
            fj["chip_id"] = int(chip_id)
        except (TypeError, ValueError):
            pass
    for key in ("start_days_ago", "end_days_ago"):
        raw = params.get(key)
        if raw not in (None, ""):
            fj[key] = _parse_period_days(raw)
    return fj


def _widget_jobs_queryset(workspace, filters_json: dict | None):
    """Filtered jobs for custom widgets (period + campaign/chip)."""
    today = timezone.localdate()
    fj = filters_json or {}
    start_days = _clamped_period_days(fj.get("start_days_ago", 29), default=29)
    end_days = _clamped_period_days(fj.get("end_days_ago", 0), default=0)
    start_date = today - timedelta(days=start_days)
    end_date = today - timedelta(days=end_days)
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    jobs_qs = CampaignSendJob.objects.filter(
        campaign__workspace=workspace,
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
    )
    if fj.get("campaign_id"):
        jobs_qs = jobs_qs.filter(campaign_id=fj["campaign_id"])
    if fj.get("chip_id"):
        jobs_qs = jobs_qs.filter(chip_id=fj["chip_id"])
    return jobs_qs


def _widget_chart_payload(widget, workspace):
    """Build the Chart.js payload for a widget, applying its filters."""
    jobs_qs = _widget_jobs_queryset(workspace, widget.filters_json)
    label = widget.title or widget.get_dataset_display()
    return _chart_payload_for_dataset(widget.dataset, jobs_qs, default_label=label)


def _widget_has_chart_data(payload: dict) -> bool:
    values = payload.get("values") or []
    return bool(payload.get("labels")) and len(values) > 0


@login_required
@workspace_required
def widget_preview(request, dashboard_pk: int):
    """JSON for the chart preview in the widget form."""
    workspace = request.current_workspace
    get_object_or_404(StatsDashboard, pk=dashboard_pk, workspace=workspace)

    valid_datasets = {c.value for c in StatsDashboardWidget.Dataset}
    valid_charts = {c.value for c in StatsDashboardWidget.ChartType}

    dataset = (request.GET.get("dataset") or StatsDashboardWidget.Dataset.STATUS).strip()
    chart_type = (request.GET.get("chart_type") or StatsDashboardWidget.ChartType.BAR).strip()
    if dataset not in valid_datasets:
        return JsonResponse({"ok": False, "error": "Dataset inválido."}, status=400)
    if chart_type not in valid_charts:
        return JsonResponse({"ok": False, "error": "Tipo de gráfico inválido."}, status=400)

    try:
        filters_json = _filters_json_from_preview_params(request.GET)
    except (TypeError, ValueError):
        return JsonResponse(
            {
                "ok": False,
                "error": f"Período inválido: use números inteiros de 0 a {MAX_WIDGET_PERIOD_DAYS}.",
            },
            status=400,
        )

    title = (request.GET.get("title") or "").strip()
    preview_widget = StatsDashboardWidget(
        dataset=dataset,
        chart_type=chart_type,
        title=title,
        filters_json=filters_json,
    )
    payload = _widget_chart_payload(preview_widget, workspace)
    return JsonResponse(
        {
            "ok": True,
            "payload": payload,
            "has_data": _widget_has_chart_data(payload),
            "chart_type": chart_type,
        }
    )


@login_required
@workspace_required
def custom_dashboard_list(request):
    workspace = request.current_workspace
    dashboards = StatsDashboard.objects.filter(workspace=workspace)
    return render(
        request,
        "dashboards/dashboard_list.html",
        {
            "dashboards": dashboards,
            "current_workspace": workspace,
        },
    )


@login_required
@workspace_required
def custom_dashboard_create(request):
    workspace = request.current_workspace
    from apps.dashboards.forms import StatsDashboardForm

    if request.method == "POST":
        form = StatsDashboardForm(request.POST)
        if form.is_valid():
            dash = form.save(commit=False)
            dash.workspace = workspace
            dash.save()
            return redirect("dashboards:custom_dashboard_view", pk=dash.pk)
    else:
        form = StatsDashboardForm()
    return render(
        request,
        "dashboards/dashboard_form.html",
        {
            "form": form,
            "current_workspace": workspace,
            "editing": False,
        },
    )


@login_required
@workspace_required
def custom_dashboard_edit(request, pk: int):
    workspace = request.current_workspace
    dash = get_object_or_404(StatsDashboard, pk=pk, workspace=workspace)
    from apps.dashboards.forms import StatsDashboardForm

    if request.method == "POST":
        form = StatsDashboardForm(request.POST, instance=dash)
        if form.is_valid():
            form.save()
            return redirect("dashboards:custom_dashboard_view", pk=dash.pk)
    else:
        form = StatsDashboardForm(instance=dash)
    return render(
        request,
        "dashboards/dashboard_form.html",
        {
            "form": form,
            "dashboard": dash,
            "current_workspace": workspace,
            "editing": True,
        },
    )


@login_required
@workspace_required
def custom_dashboard_delete(request, pk: int):
    workspace = request.current_workspace
    dash = get_object_or_404(StatsDashboard, pk=pk, workspace=workspace)
    if request.method == "POST":
        dash.delete()
        return redirect("dashboards:custom_dashboard_list")
    return render(
        request,
        "dashboards/confirm_delete_dashboard.html",
        {
            "dashboard": dash,
            "current_workspace": workspace,
        },
    )


def _friendly_filters(widget, workspace) -> list[str]:
    """Convert filters_json into readable tags (campaign/chip name instead of ID)."""
    fj = widget.filters_json or {}
    tags: list[str] = []
    cid = fj.get("campaign_id")
    if cid:
        name = (
            Campaign.objects.filter(pk=cid, workspace=workspace)
            .values_list("name", flat=True)
            .first()
        )
        tags.append(f"Campanha: {name or f'#{cid}'}")
    chip_id = fj.get("chip_id")
    if chip_id:
        name = (
            Chip.objects.filter(pk=chip_id, workspace=workspace)
            .values_list("name", flat=True)
            .first()
        )
        tags.append(f"Chip: {name or f'#{chip_id}'}")
    start = fj.get("start_days_ago")
    end = fj.get("end_days_ago", 0)
    if start is not None:
        if end:
            tags.append(f"Período: últimos {start} a {end} dias")
        else:
            tags.append(f"Período: últimos {start} dias")
    return tags


@login_required
@workspace_required
def custom_dashboard_view(request, pk: int):
    workspace = request.current_workspace
    dash = get_object_or_404(StatsDashboard, pk=pk, workspace=workspace)
    widgets = list(dash.widgets.all())

    widgets_data = []
    widgets_js = []
    for w in widgets:
        payload = _widget_chart_payload(w, workspace)
        canvas_id = f"widget_chart_{w.pk}"
        has_data = _widget_has_chart_data(payload)
        widgets_data.append(
            {
                "widget": w,
                "payload": payload,
                "canvas_id": canvas_id,
                "has_data": has_data,
                "friendly_filters": _friendly_filters(w, workspace),
            }
        )
        if has_data:
            widgets_js.append(
                {
                    "canvasId": canvas_id,
                    "chartType": w.chart_type,
                    "payload": payload,
                }
            )

    return render(
        request,
        "dashboards/dashboard_custom.html",
        {
            "dashboard": dash,
            "widgets_data": widgets_data,
            "widgets_js": widgets_js,
            "widget_count": len(widgets),
            "current_workspace": workspace,
        },
    )


@login_required
@workspace_required
def widget_create(request, dashboard_pk: int):
    workspace = request.current_workspace
    dash = get_object_or_404(StatsDashboard, pk=dashboard_pk, workspace=workspace)
    from apps.dashboards.forms import StatsDashboardWidgetForm

    if request.method == "POST":
        form = StatsDashboardWidgetForm(request.POST, workspace=workspace)
        if form.is_valid():
            w = form.save(commit=False)
            w.dashboard = dash
            max_position = dash.widgets.count()
            w.position = max_position
            w.save()
            return redirect("dashboards:custom_dashboard_view", pk=dash.pk)
    else:
        form = StatsDashboardWidgetForm(workspace=workspace)
    return render(
        request,
        "dashboards/widget_form.html",
        {
            "form": form,
            "dashboard": dash,
            "current_workspace": workspace,
            "editing": False,
            "widget_preview_url": reverse(
                "dashboards:widget_preview", kwargs={"dashboard_pk": dash.pk}
            ),
            "dataset_help": dataset_help_for_template(),
        },
    )


@login_required
@workspace_required
def widget_edit(request, dashboard_pk: int, widget_pk: int):
    workspace = request.current_workspace
    dash = get_object_or_404(StatsDashboard, pk=dashboard_pk, workspace=workspace)
    widget = get_object_or_404(StatsDashboardWidget, pk=widget_pk, dashboard=dash)
    from apps.dashboards.forms import StatsDashboardWidgetForm

    if request.method == "POST":
        form = StatsDashboardWidgetForm(request.POST, instance=widget, workspace=workspace)
        if form.is_valid():
            form.save()
            return redirect("dashboards:custom_dashboard_view", pk=dash.pk)
    else:
        form = StatsDashboardWidgetForm(instance=widget, workspace=workspace)
    return render(
        request,
        "dashboards/widget_form.html",
        {
            "form": form,
            "dashboard": dash,
            "widget": widget,
            "current_workspace": workspace,
            "editing": True,
            "widget_preview_url": reverse(
                "dashboards:widget_preview", kwargs={"dashboard_pk": dash.pk}
            ),
            "dataset_help": dataset_help_for_template(),
        },
    )


@login_required
@workspace_required
def widget_delete(request, dashboard_pk: int, widget_pk: int):
    workspace = request.current_workspace
    dash = get_object_or_404(StatsDashboard, pk=dashboard_pk, workspace=workspace)
    widget = get_object_or_404(StatsDashboardWidget, pk=widget_pk, dashboard=dash)
    if request.method == "POST":
        widget.delete()
        return redirect("dashboards:custom_dashboard_view", pk=dash.pk)
    return redirect("dashboards:custom_dashboard_view", pk=dash.pk)


@login_required
@workspace_required
def widget_duplicate(request, dashboard_pk: int, widget_pk: int):
    workspace = request.current_workspace
    dash = get_object_or_404(StatsDashboard, pk=dashboard_pk, workspace=workspace)
    widget = get_object_or_404(StatsDashboardWidget, pk=widget_pk, dashboard=dash)
    if request.method == "POST":
        max_position = dash.widgets.count()
        StatsDashboardWidget.objects.create(
            dashboard=dash,
            position=max_position,
            title=f"{widget.title or widget.get_dataset_display()} (cópia)",
            dataset=widget.dataset,
            chart_type=widget.chart_type,
            size=widget.size,
            filters_json=widget.filters_json or {},
        )
    return redirect("dashboards:custom_dashboard_view", pk=dash.pk)


@login_required
@workspace_required
def widget_reorder(request, dashboard_pk: int):
    """AJAX POST: receive the list of widget IDs in the new order."""
    workspace = request.current_workspace
    dash = get_object_or_404(StatsDashboard, pk=dashboard_pk, workspace=workspace)
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        body = json.loads(request.body)
        ids = body.get("order", [])
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if not isinstance(ids, list):
        return JsonResponse({"error": "order must be a list of widget ids"}, status=400)
    try:
        ordered_ids = [int(wid) for wid in ids]
    except (TypeError, ValueError):
        return JsonResponse({"error": "order must contain only integer widget ids"}, status=400)

    widgets = {w.pk: w for w in dash.widgets.all()}
    for i, wid in enumerate(ordered_ids):
        if wid in widgets:
            StatsDashboardWidget.objects.filter(pk=wid, dashboard=dash).update(position=i)

    return JsonResponse({"ok": True})
