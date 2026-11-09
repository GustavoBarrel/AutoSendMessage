from datetime import date
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from apps.accounts.decorators import workspace_required
from apps.campaigns.models import Campaign, CampaignSendJob
from apps.campaigns.services.campaign_chip_schedule import count_pending_future_scheduled_for_chips
from apps.chips.models import Chip
from apps.chips.services.evolution import connection_state
from apps.chips.services.evolution_config import (
    evolution_configured,
    evolution_creds,
)
from apps.common.utils.safe import safe_action
from apps.contacts.models import Contact


def _parse_home_summary_date(request) -> date:
    raw = (request.GET.get("day") or "").strip()
    if not raw:
        return timezone.localdate()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return timezone.localdate()


@login_required
@workspace_required
@safe_action("carregar home", fallback="dashboards:home")
def home(request):
    workspace = request.current_workspace
    chips = Chip.objects.filter(workspace=workspace).order_by("name")
    chips_count = chips.filter(is_active=True).count()
    evolution_ok = evolution_configured()
    evo_url, evo_key = evolution_creds()
    chip_connection_states = {}
    for c in chips:
        if not c.uses_evolution:
            # Meta and simulated chips have no QR/instance to query.
            chip_connection_states[c.pk] = "open"
        elif evolution_ok:
            chip_connection_states[c.pk] = connection_state(evo_url, evo_key, c.instance_name)
        else:
            chip_connection_states[c.pk] = None
    deferred_map = count_pending_future_scheduled_for_chips(
        list(chips.values_list("pk", flat=True))
    )
    chips_with_state = [
        {
            "chip": c,
            "connection_state": chip_connection_states.get(c.pk),
            "deferred_pending": deferred_map.get(c.pk, 0),
        }
        for c in chips
    ]
    evolution_warning = (
        None
        if evolution_ok or settings.SIMULATED_PROVIDER_ENABLED
        else (
            "Configuração da Evolution API ausente. Defina EVOLUTION_URL_BASE e EVOLUTION_API_KEY "
            "no .env para usar chips e campanhas."
        )
    )
    camp_filter = (request.GET.get("camp") or "").strip().lower()
    camp_base = Campaign.objects.filter(workspace=workspace, deleted_at__isnull=True)
    archived_count = Campaign.objects.filter(workspace=workspace, deleted_at__isnull=False).count()
    paused_count = camp_base.filter(status=Campaign.Status.PAUSED).count()
    finished_count = camp_base.filter(status=Campaign.Status.FINISHED).count()
    default_count = camp_base.filter(
        status__in=[Campaign.Status.DRAFT, Campaign.Status.ACTIVE]
    ).count()

    if camp_filter == "paused":
        campaigns = camp_base.filter(status=Campaign.Status.PAUSED).order_by("-created_at")[:12]
    elif camp_filter == "finished":
        campaigns = camp_base.filter(status=Campaign.Status.FINISHED).order_by("-created_at")[:12]
    elif camp_filter == "archived":
        campaigns = Campaign.objects.filter(workspace=workspace, deleted_at__isnull=False).order_by(
            "-created_at"
        )[:12]
    else:
        camp_filter = ""
        campaigns = camp_base.filter(
            status__in=[Campaign.Status.DRAFT, Campaign.Status.ACTIVE]
        ).order_by("-created_at")[:12]

    campaigns_count = Campaign.objects.filter(workspace=workspace).count()

    today = timezone.localdate()
    summary_date = _parse_home_summary_date(request)
    if summary_date > today:
        summary_date = today
    day_q = summary_date.isoformat()
    home_q_active = "?" + urlencode({"day": day_q})
    home_q_paused = "?" + urlencode({"day": day_q, "camp": "paused"})
    home_q_finished = "?" + urlencode({"day": day_q, "camp": "finished"})
    home_q_archived = "?" + urlencode({"day": day_q, "camp": "archived"})
    summary_sent = CampaignSendJob.objects.filter(
        campaign__workspace=workspace,
        status=CampaignSendJob.Status.SENT,
        sent_at__date=summary_date,
    ).count()
    summary_failed = CampaignSendJob.objects.filter(
        campaign__workspace=workspace,
        status=CampaignSendJob.Status.FAILED,
        updated_at__date=summary_date,
    ).count()
    pending_total = CampaignSendJob.objects.filter(
        campaign__workspace=workspace,
        status=CampaignSendJob.Status.PENDING,
        campaign__status=Campaign.Status.ACTIVE,
    ).count()
    waiting_chip_total = CampaignSendJob.objects.filter(
        campaign__workspace=workspace,
        status=CampaignSendJob.Status.WAITING_CHIP,
        campaign__status=Campaign.Status.ACTIVE,
    ).count()
    active_campaigns = Campaign.objects.filter(
        workspace=workspace, status=Campaign.Status.ACTIVE
    ).count()
    paused_campaigns = Campaign.objects.filter(
        workspace=workspace, status=Campaign.Status.PAUSED
    ).count()
    draft_campaigns = Campaign.objects.filter(
        workspace=workspace, status=Campaign.Status.DRAFT
    ).count()
    contacts_total = Contact.objects.count()
    has_chip_without_evolution = any(not c.uses_evolution for c in chips)
    connected_chips = (
        sum(1 for v in chip_connection_states.values() if v == "open")
        if evolution_ok or has_chip_without_evolution
        else None
    )
    deferred_total = sum(deferred_map.values()) if deferred_map else 0
    return render(
        request,
        "dashboards/home.html",
        {
            "chips": chips,
            "chips_with_state": chips_with_state,
            "chips_count": chips_count,
            "connected_chips": connected_chips,
            "deferred_total": deferred_total,
            "evolution_ok": evolution_ok,
            "evolution_warning": evolution_warning,
            "current_workspace": workspace,
            "campaigns": campaigns,
            "camp_filter": camp_filter,
            "campaigns_default_count": default_count,
            "campaigns_paused_count": paused_count,
            "campaigns_finished_count": finished_count,
            "campaigns_archived_count": archived_count,
            "campaigns_count": campaigns_count,
            "summary_date": summary_date,
            "summary_date_iso": day_q,
            "summary_max_date_iso": today.isoformat(),
            "home_q_active": home_q_active,
            "home_q_paused": home_q_paused,
            "home_q_finished": home_q_finished,
            "home_q_archived": home_q_archived,
            "summary_sent": summary_sent,
            "summary_failed": summary_failed,
            "pending_total": pending_total,
            "waiting_chip_total": waiting_chip_total,
            "active_campaigns": active_campaigns,
            "paused_campaigns": paused_campaigns,
            "draft_campaigns": draft_campaigns,
            "contacts_total": contacts_total,
        },
    )
