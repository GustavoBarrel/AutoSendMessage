"""Campaigns: CRUD, dashboard and actions (start, pause, resume, finish)."""

from __future__ import annotations

from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count
from django.db.models import Q as DbQ
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from apps.accounts.decorators import workspace_required
from apps.campaigns.models import Campaign, CampaignChip, CampaignSendAttempt, CampaignSendJob
from apps.campaigns.services import campaign_lifecycle as lifecycle
from apps.campaigns.services.campaign_audience import (
    audience_summary_text,
    count_contacts_for_audience,
)
from apps.campaigns.services.campaign_chip_offline import redistribute_waiting_jobs
from apps.campaigns.services.campaign_chips import add_chips_to_campaign
from apps.campaigns.services.campaign_retry import (
    RETRY_SCOPE_ALL,
    RETRY_SCOPE_CAUSE_CHIP,
    RETRY_SCOPE_CAUSE_MESSAGE,
    RETRY_SCOPE_CHOICES,
    RETRY_SCOPE_SELECTED,
    chip_eligible_for_campaign_retry,
    classify_failure_cause,
    eligible_chips_for_campaign_retry,
    failure_breakdown_for_campaign,
    failure_breakdown_labels,
    retry_failed_jobs_with_chips,
)
from apps.chips.models import Chip
from apps.chips.services.evolution_whatsapp_errors import get_send_error_display
from apps.common.utils.safe import safe_action, safe_user_message


def _campaign_for_workspace(workspace, pk: int) -> Campaign:
    return get_object_or_404(Campaign, pk=pk, workspace=workspace)


def _available_chips_to_link(campaign: Campaign) -> list[Chip]:
    """Workspace chips not yet linked (active) to this campaign and free for another active one."""
    linked_active = set(
        CampaignChip.objects.filter(campaign=campaign, is_active=True).values_list(
            "chip_id", flat=True
        )
    )
    return [
        ch
        for ch in Chip.objects.filter(workspace_id=campaign.workspace_id, is_active=True).order_by(
            "name"
        )
        if ch.pk not in linked_active and chip_eligible_for_campaign_retry(ch, campaign)
    ]


def _redistribute_dest_campaign_chips(
    campaign_chips: list[CampaignChip],
    evolution_ok: bool,
    chip_connection_by_id: dict[int, str | None],
) -> list[CampaignChip]:
    """Redistribution targets: with Evolution configured, only connected chips (open)."""
    if not evolution_ok:
        return list(campaign_chips)
    return [cc for cc in campaign_chips if chip_connection_by_id.get(cc.chip_id) == "open"]


def _can_show_redistribute_waiting_form(
    waiting_by_chip: list[dict],
    campaign_chips: list[CampaignChip],
    redistribute_dest_campaign_chips: list[CampaignChip],
    evolution_ok: bool,
) -> bool:
    if not waiting_by_chip:
        return False
    if not evolution_ok:
        return len(campaign_chips) > 1
    dest_ids = {cc.chip_id for cc in redistribute_dest_campaign_chips}
    if not dest_ids:
        return False
    for row in waiting_by_chip:
        wid = row["chip_id"]
        if dest_ids - {wid}:
            return True
    return False


def _sync_campaign_chips(campaign: Campaign, chip_ids: list[int]) -> None:
    workspace_id = campaign.workspace_id
    valid = set(
        Chip.objects.filter(workspace_id=workspace_id, pk__in=chip_ids, is_active=True).values_list(
            "pk", flat=True
        )
    )
    chosen = [cid for cid in chip_ids if cid in valid]
    CampaignChip.objects.filter(campaign=campaign).exclude(chip_id__in=chosen).delete()
    for cid in chosen:
        CampaignChip.objects.update_or_create(
            campaign=campaign,
            chip_id=cid,
            defaults={"is_active": True, "campaign_status": campaign.status},
        )


@login_required
@workspace_required
def campaign_list(request):
    workspace = request.current_workspace
    show_deleted = request.GET.get("archived") == "1"
    show_finished = request.GET.get("finished") == "1"
    qs = Campaign.objects.filter(workspace=workspace)
    if show_deleted:
        qs = qs.filter(deleted_at__isnull=False)
    else:
        qs = qs.filter(deleted_at__isnull=True)
        if show_finished:
            qs = qs.filter(status=Campaign.Status.FINISHED)
        else:
            qs = qs.exclude(status=Campaign.Status.FINISHED)
    qs = qs.order_by("-created_at")
    rows = []
    for c in qs:
        c.audience_short = audience_summary_text(c)
        rows.append(c)
    deleted_count = Campaign.objects.filter(workspace=workspace, deleted_at__isnull=False).count()
    finished_count = Campaign.objects.filter(
        workspace=workspace,
        deleted_at__isnull=True,
        status=Campaign.Status.FINISHED,
    ).count()
    return render(
        request,
        "campaigns/list.html",
        {
            "current_workspace": workspace,
            "campaigns": rows,
            "show_deleted": show_deleted,
            "show_finished": show_finished,
            "deleted_count": deleted_count,
            "finished_count": finished_count,
        },
    )


@login_required
@workspace_required
@csrf_protect
@safe_action("criar campanha", fallback="campaigns:campaign_list")
def campaign_create(request):
    workspace = request.current_workspace
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Informe o nome da campanha.")
            return render(
                request,
                "campaigns/create.html",
                {"current_workspace": workspace, "name": name},
            )
        from apps.campaigns.services.campaign_audience import default_audience_config

        c = Campaign.objects.create(
            workspace=workspace,
            name=name[:120],
            audience=Campaign.Audience.ALL,
            audience_config=default_audience_config(),
            status=Campaign.Status.DRAFT,
        )
        messages.success(request, "Rascunho criado. Defina o público no assistente.")
        return redirect("campaigns:campaign_wizard", pk=c.pk, step="audience")
    return render(
        request,
        "campaigns/create.html",
        {"current_workspace": workspace},
    )


@login_required
@workspace_required
def campaign_edit(request, pk: int):
    """Compatibility: editing moved to the wizard."""
    return redirect("campaigns:campaign_wizard", pk=pk, step="audience")


_JOBS_PER_PAGE = 50
_ATTEMPTS_PER_PAGE = 10
_ATTEMPT_RESULT_ALL = ""
_ATTEMPT_RESULT_SENT = "sent"
_ATTEMPT_RESULT_FAILED = "failed"
_ATTEMPT_RESULT_OPEN = "open"
_VALID_ATTEMPT_RESULTS = frozenset(
    {_ATTEMPT_RESULT_ALL, _ATTEMPT_RESULT_SENT, _ATTEMPT_RESULT_FAILED, _ATTEMPT_RESULT_OPEN}
)
_STATUS_LABELS = {s.value: s.label for s in CampaignSendJob.Status}


def _campaign_detail_query(
    *,
    status: str | None = None,
    page: int | None = None,
    aresult: str | None = None,
    apage: int | None = None,
) -> str:
    """Build the campaign dashboard query string (filters + two paginations)."""
    p: dict[str, str] = {}
    if status:
        p["status"] = status
    if page and page > 1:
        p["page"] = str(page)
    if aresult:
        p["aresult"] = aresult
    if apage and apage > 1:
        p["apage"] = str(apage)
    return "?" + urlencode(p) if p else "?"


@login_required
@workspace_required
def campaign_detail(request, pk: int):
    workspace = request.current_workspace
    campaign = _campaign_for_workspace(workspace, pk)

    jobs_base = CampaignSendJob.objects.filter(campaign=campaign)
    stats = {
        "total": jobs_base.count(),
        "pending": jobs_base.filter(status=CampaignSendJob.Status.PENDING).count(),
        "locked": jobs_base.filter(status=CampaignSendJob.Status.LOCKED).count(),
        "waiting_chip": jobs_base.filter(status=CampaignSendJob.Status.WAITING_CHIP).count(),
        "sent": jobs_base.filter(status=CampaignSendJob.Status.SENT).count(),
        "failed": jobs_base.filter(status=CampaignSendJob.Status.FAILED).count(),
        "cancelled": jobs_base.filter(status=CampaignSendJob.Status.CANCELLED).count(),
        "skipped": jobs_base.filter(status=CampaignSendJob.Status.SKIPPED).count(),
    }

    by_chip = (
        jobs_base.values("chip__name", "chip_id")
        .annotate(
            total=Count("id"),
            sent=Count("id", filter=DbQ(status=CampaignSendJob.Status.SENT)),
            failures=Count("id", filter=DbQ(status=CampaignSendJob.Status.FAILED)),
            aguardando_chip=Count("id", filter=DbQ(status=CampaignSendJob.Status.WAITING_CHIP)),
        )
        .order_by("chip__name")
    )

    waiting_by_chip = list(
        jobs_base.filter(status=CampaignSendJob.Status.WAITING_CHIP, chip_id__isnull=False)
        .values("chip_id", "chip__name")
        .annotate(n=Count("id"))
        .order_by("chip__name")
    )

    campaign_chips = list(
        CampaignChip.objects.filter(campaign=campaign).select_related("chip").order_by("chip__name")
    )

    status_filter = request.GET.get("status", "")
    valid_statuses = {s.value for s in CampaignSendJob.Status}
    if status_filter not in valid_statuses:
        status_filter = ""

    jobs_qs = jobs_base.select_related("contact", "chip", "variant").order_by("-updated_at", "pk")
    if status_filter:
        jobs_qs = jobs_qs.filter(status=status_filter)

    paginator = Paginator(jobs_qs, _JOBS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    attempt_result = (request.GET.get("aresult") or "").strip().lower()
    if attempt_result not in _VALID_ATTEMPT_RESULTS:
        attempt_result = _ATTEMPT_RESULT_ALL

    attempts_base = CampaignSendAttempt.objects.filter(job__campaign=campaign)
    attempt_counts = attempts_base.aggregate(
        total=Count("id"),
        sent=Count("id", filter=DbQ(success=True)),
        failed=Count("id", filter=DbQ(success=False)),
        open=Count("id", filter=DbQ(finished_at__isnull=True)),
    )

    attempts_qs = attempts_base.select_related("job", "job__contact", "job__chip").order_by(
        "-finished_at", "-pk"
    )
    if attempt_result == _ATTEMPT_RESULT_SENT:
        attempts_qs = attempts_qs.filter(success=True)
    elif attempt_result == _ATTEMPT_RESULT_FAILED:
        attempts_qs = attempts_qs.filter(success=False)
    elif attempt_result == _ATTEMPT_RESULT_OPEN:
        attempts_qs = attempts_qs.filter(finished_at__isnull=True)

    attempts_paginator = Paginator(attempts_qs, _ATTEMPTS_PER_PAGE)
    attempts_page_obj = attempts_paginator.get_page(request.GET.get("apage", 1))

    st_cur = status_filter or None
    pg_cur = page_obj.number if page_obj.number > 1 else None
    ar_cur = attempt_result or None
    ap_cur = attempts_page_obj.number if attempts_page_obj.number > 1 else None

    def _q(
        *,
        status: str | None = st_cur,
        page: int | None = pg_cur,
        aresult: str | None = ar_cur,
        apage: int | None = ap_cur,
    ) -> str:
        return _campaign_detail_query(
            status=status or None,
            page=page,
            aresult=aresult or None,
            apage=apage,
        )

    attempt_filter_links = {
        "all": _q(aresult=None, apage=None),
        "sent": _q(aresult=_ATTEMPT_RESULT_SENT, apage=None),
        "failed": _q(aresult=_ATTEMPT_RESULT_FAILED, apage=None),
        "open": _q(aresult=_ATTEMPT_RESULT_OPEN, apage=None),
    }
    attempts_pagination = {
        "prev": _q(apage=attempts_page_obj.previous_page_number())
        if attempts_page_obj.has_previous()
        else None,
        "next": _q(apage=attempts_page_obj.next_page_number())
        if attempts_page_obj.has_next()
        else None,
    }

    job_filter_url_clear = _campaign_detail_query(status=None, aresult=ar_cur, apage=ap_cur)
    job_filter_urls = {
        val: _campaign_detail_query(status=val, aresult=ar_cur, apage=ap_cur)
        for val, _lbl in CampaignSendJob.Status.choices
    }

    job_pagination: dict[str, str] = {}
    if page_obj.has_previous():
        job_pagination["prev"] = _q(page=page_obj.previous_page_number())
    if page_obj.has_next():
        job_pagination["next"] = _q(page=page_obj.next_page_number())

    preview_count = count_contacts_for_audience(campaign)
    audience_desc = audience_summary_text(campaign)

    retry_eligible_chips = eligible_chips_for_campaign_retry(campaign)
    retry_eligible_chips_hidden_offline = 0
    available_chips_to_link = _available_chips_to_link(campaign)
    show_campaign_queue_hub = not campaign.is_deleted and (
        campaign.activated_at is not None or stats["failed"] > 0 or stats["waiting_chip"] > 0
    )
    failure_breakdown = failure_breakdown_for_campaign(campaign.pk)
    job_failed_causes: dict[int, str] = {}
    for j in page_obj.object_list:
        if j.status == CampaignSendJob.Status.FAILED:
            job_failed_causes[j.pk] = classify_failure_cause(j.last_error or "")

    for j in page_obj.object_list:
        j.send_error_display = get_send_error_display(j.last_error or "")
    for att in attempts_page_obj.object_list:
        att.send_error_display = get_send_error_display(
            att.error_text or "",
            att.provider_payload,
            success=att.success,
        )

    evolution_ok = bool(settings.EVOLUTION_URL_BASE and settings.EVOLUTION_API_KEY)
    chip_connection_by_id: dict[int, str | None] = {}
    available_chips_omitted_offline = 0

    # Fill in the connection state: Meta/simulated are always "open", Evolution queries the API.
    from apps.chips.services.evolution import connection_state as evo_connection_state

    for cc in campaign_chips:
        ch = cc.chip
        if not ch.uses_evolution:
            chip_connection_by_id[cc.chip_id] = "open"
        elif evolution_ok and ch.instance_name:
            chip_connection_by_id[cc.chip_id] = evo_connection_state(
                settings.EVOLUTION_URL_BASE, settings.EVOLUTION_API_KEY, ch.instance_name
            )
        else:
            chip_connection_by_id[cc.chip_id] = None

    # Filter the chips available to link (Meta is always available when configured).
    def _chip_is_available_for_linking(ch: Chip) -> bool:
        if ch.provider == Chip.Provider.META:
            return bool(ch.meta_phone_number_id and ch.meta_access_token)
        if ch.provider == Chip.Provider.SIMULATED:
            return True
        if not evolution_ok:
            return False
        return (
            evo_connection_state(
                settings.EVOLUTION_URL_BASE, settings.EVOLUTION_API_KEY, ch.instance_name
            )
            == "open"
        )

    n_before = len(available_chips_to_link)
    available_chips_to_link = [
        ch for ch in available_chips_to_link if _chip_is_available_for_linking(ch)
    ]
    available_chips_omitted_offline = n_before - len(available_chips_to_link)

    # In the retry modal, Meta is always available; Evolution only when "open".
    def _chip_is_available_for_retry(ch: Chip) -> bool:
        if ch.provider == Chip.Provider.META:
            return bool(ch.meta_phone_number_id and ch.meta_access_token)
        if ch.provider == Chip.Provider.SIMULATED:
            return True
        if not evolution_ok:
            return False
        return (
            evo_connection_state(
                settings.EVOLUTION_URL_BASE, settings.EVOLUTION_API_KEY, ch.instance_name
            )
            == "open"
        )

    before_retry = len(retry_eligible_chips)
    retry_eligible_chips = [ch for ch in retry_eligible_chips if _chip_is_available_for_retry(ch)]
    retry_eligible_chips_hidden_offline = before_retry - len(retry_eligible_chips)

    redistribute_dest_campaign_chips = _redistribute_dest_campaign_chips(
        campaign_chips, evolution_ok, chip_connection_by_id
    )
    can_show_redistribute_form = _can_show_redistribute_waiting_form(
        waiting_by_chip,
        campaign_chips,
        redistribute_dest_campaign_chips,
        evolution_ok,
    )

    return render(
        request,
        "campaigns/detail.html",
        {
            "current_workspace": workspace,
            "campaign": campaign,
            "stats": stats,
            "by_chip": by_chip,
            "campaign_chips": campaign_chips,
            "preview_count": preview_count,
            "audience_desc": audience_desc,
            "page_obj": page_obj,
            "status_filter": status_filter,
            "status_choices": list(CampaignSendJob.Status.choices),
            "attempts_page_obj": attempts_page_obj,
            "attempt_result": attempt_result,
            "attempt_counts": attempt_counts,
            "attempt_filter_links": attempt_filter_links,
            "attempts_pagination": attempts_pagination,
            "job_filter_url_clear": job_filter_url_clear,
            "job_filter_urls": job_filter_urls,
            "job_pagination": job_pagination,
            "retry_eligible_chips": retry_eligible_chips,
            "retry_eligible_chips_hidden_offline": retry_eligible_chips_hidden_offline,
            "failure_breakdown": failure_breakdown,
            "failure_labels": failure_breakdown_labels(),
            "job_failed_causes": job_failed_causes,
            "waiting_by_chip": waiting_by_chip,
            "available_chips_to_link": available_chips_to_link,
            "show_campaign_queue_hub": show_campaign_queue_hub,
            "evolution_ok": evolution_ok,
            "chip_connection_by_id": chip_connection_by_id,
            "available_chips_omitted_offline": available_chips_omitted_offline,
            "redistribute_dest_campaign_chips": redistribute_dest_campaign_chips,
            "can_show_redistribute_form": can_show_redistribute_form,
        },
    )


@login_required
@workspace_required
@require_POST
@csrf_protect
@safe_action("ação da campanha", fallback="campaigns:campaign_list")
def campaign_action(request, pk: int):
    workspace = request.current_workspace
    campaign = _campaign_for_workspace(workspace, pk)
    if campaign.is_deleted:
        messages.error(request, "Não é possível realizar ações em uma campanha arquivada.")
        return redirect("campaigns:campaign_detail", pk=pk)
    action = request.POST.get("action")

    if action == "set_start_at":
        if campaign.status not in (Campaign.Status.DRAFT, Campaign.Status.PAUSED):
            messages.warning(request, "Só é possível agendar em campanha em rascunho ou pausada.")
            return redirect("campaigns:campaign_detail", pk=pk)
        if request.POST.get("clear") == "1":
            campaign.start_at = None
            campaign.save()
            messages.success(request, "Agendamento removido.")
            return redirect("campaigns:campaign_detail", pk=pk)

        raw = (request.POST.get("start_at") or "").strip()
        if not raw:
            messages.error(request, "Informe a data/hora do agendamento (ou clique em Limpar).")
            return redirect("campaigns:campaign_detail", pk=pk)
        # datetime-local comes without a timezone; we interpret it as the system's local time.
        dt = parse_datetime(raw)
        if dt is None:
            # Some browsers omit the seconds; parse_datetime may fail depending on the format.
            try:
                dt = timezone.datetime.fromisoformat(raw)
            except ValueError:
                dt = None
        if dt is None:
            messages.error(request, "Data/hora inválida.")
            return redirect("campaigns:campaign_detail", pk=pk)
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())

        campaign.start_at = dt
        campaign.save()
        if dt > timezone.now():
            messages.success(
                request,
                "Agendamento salvo. Ao iniciar, os envios só começam a partir do horário definido.",
            )
        else:
            messages.success(
                request,
                "Agendamento salvo. Como o horário já passou, ao iniciar os envios começam "
                "imediatamente.",
            )
        return redirect("campaigns:campaign_detail", pk=pk)

    if action == "activate":
        if campaign.status not in (Campaign.Status.DRAFT, Campaign.Status.PAUSED):
            messages.warning(request, "Só é possível iniciar campanha em rascunho ou pausada.")
            return redirect("campaigns:campaign_detail", pk=pk)
        try:
            r = lifecycle.activate_and_enqueue(campaign)
            messages.success(
                request,
                f"Campanha ativa. Jobs criados: {r.created} (tentados {r.attempted}, já na fila: "
                f"{r.skipped_already_queued}).",
            )
        except IntegrityError:
            messages.error(
                request,
                "Não foi possível ativar: um dos chips já está em outra campanha ativa.",
            )
        except Exception as exc:
            messages.error(request, safe_user_message("iniciar campanha", str(exc)))
        return redirect("campaigns:campaign_detail", pk=pk)

    if action == "pause":
        if campaign.status != Campaign.Status.ACTIVE:
            messages.warning(request, "A campanha não está ativa.")
            return redirect("campaigns:campaign_detail", pk=pk)
        lifecycle.pause_campaign(campaign)
        messages.success(request, "Campanha pausada. A fila não será processada até retomar.")
        return redirect("campaigns:campaign_detail", pk=pk)

    if action == "resume":
        if campaign.status != Campaign.Status.PAUSED:
            messages.warning(request, "Só é possível retomar uma campanha pausada.")
            return redirect("campaigns:campaign_detail", pk=pk)
        lifecycle.resume_campaign(campaign)
        messages.success(request, "Campanha retomada.")
        return redirect("campaigns:campaign_detail", pk=pk)

    if action == "finish":
        if campaign.status == Campaign.Status.FINISHED:
            messages.warning(request, "Campanha já finalizada.")
            return redirect("campaigns:campaign_detail", pk=pk)
        n = lifecycle.finish_campaign(campaign)
        messages.success(request, f"Campanha finalizada. {n} job(s) pendente(s) cancelado(s).")
        return redirect("campaigns:campaign_detail", pk=pk)

    if action == "retry_failed":
        if campaign.status not in (
            Campaign.Status.ACTIVE,
            Campaign.Status.PAUSED,
            Campaign.Status.FINISHED,
        ):
            messages.warning(
                request,
                "Só é possível reenviar falhas em campanhas ativas, pausadas ou finalizadas.",
            )
            return redirect("campaigns:campaign_detail", pk=pk)
        chips_ok = eligible_chips_for_campaign_retry(campaign)
        if not chips_ok:
            messages.error(
                request,
                "Não é possível reenviar: nenhum chip disponível "
                "(inativos ou todos vinculados a outra campanha ativa).",
            )
            return redirect("campaigns:campaign_detail", pk=pk)
        raw_list = request.POST.getlist("chip_ids")
        chip_ids: list[int] = []
        seen: set[int] = set()
        for raw in raw_list:
            s = (raw or "").strip()
            if s.isdigit():
                cid = int(s)
                if cid not in seen:
                    seen.add(cid)
                    chip_ids.append(cid)
        if not chip_ids:
            messages.error(request, "Selecione ao menos um chip para o reenvio.")
            return redirect("campaigns:campaign_detail", pk=pk)
        chips_ok_ids = {c.pk for c in chips_ok}
        if not all(cid in chips_ok_ids for cid in chip_ids):
            messages.error(
                request, "Um ou mais chips selecionados não estão disponíveis para esta campanha."
            )
            return redirect("campaigns:campaign_detail", pk=pk)

        scope = (request.POST.get("retry_scope") or RETRY_SCOPE_ALL).strip()
        if scope not in RETRY_SCOPE_CHOICES:
            scope = RETRY_SCOPE_ALL
        selected_raw = request.POST.getlist("retry_job_ids")
        retry_kw = {"scope": scope}
        if scope == RETRY_SCOPE_SELECTED:
            retry_kw["selected_job_ids"] = selected_raw

        if campaign.status == Campaign.Status.FINISHED:
            campaign.status = Campaign.Status.ACTIVE
            campaign.save()
        try:
            n, chip_names = retry_failed_jobs_with_chips(campaign.pk, chip_ids, **retry_kw)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("campaigns:campaign_detail", pk=pk)
        except Exception as exc:
            messages.error(request, safe_user_message("reenviar falhas", str(exc)))
            return redirect("campaigns:campaign_detail", pk=pk)
        if n == 0:
            messages.info(request, "Nenhuma mensagem com falha encontrada para reenvio.")
        else:
            chips_part = " · ".join(f"«{nm}»" for nm in chip_names)
            scope_hint = ""
            if scope == RETRY_SCOPE_SELECTED:
                scope_hint = " (somente as marcadas na tabela)."
            elif scope == RETRY_SCOPE_CAUSE_CHIP:
                scope_hint = " (somente falhas classificadas como chip/conexão)."
            elif scope == RETRY_SCOPE_CAUSE_MESSAGE:
                scope_hint = " (somente falhas classificadas como conteúdo ou número)."
            messages.success(
                request,
                f"{n} mensagen{'s' if n > 1 else ''} reagendada{'s' if n > 1 else ''} em rodízio "
                f"entre {chips_part}. "
                f"Cada chip aplica seus próprios limites na fila.{scope_hint}",
            )
        return redirect("campaigns:campaign_detail", pk=pk)

    if action == "add_campaign_chips":
        if campaign.status not in (
            Campaign.Status.DRAFT,
            Campaign.Status.ACTIVE,
            Campaign.Status.PAUSED,
        ):
            messages.warning(
                request,
                "Só é possível adicionar chips em campanha em rascunho, ativa ou pausada.",
            )
            return redirect("campaigns:campaign_detail", pk=pk)
        add_raw = request.POST.getlist("add_chip_ids")
        add_ids: list[int] = []
        seen_add: set[int] = set()
        for raw in add_raw:
            s = (raw or "").strip()
            if s.isdigit():
                d = int(s)
                if d not in seen_add:
                    seen_add.add(d)
                    add_ids.append(d)
        try:
            n, warns = add_chips_to_campaign(campaign, add_ids)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("campaigns:campaign_detail", pk=pk)
        except Exception as exc:
            messages.error(request, safe_user_message("adicionar chips", str(exc)))
            return redirect("campaigns:campaign_detail", pk=pk)
        for w in warns[:8]:
            messages.warning(request, w)
        messages.success(
            request,
            f"{n} chip{'s' if n != 1 else ''} adicionado{'s' if n != 1 else ''} à campanha. "
            "Eles passam a poder receber redistribuições e reenvios conforme a fila.",
        )
        return redirect("campaigns:campaign_detail", pk=pk)

    if action == "redistribute_waiting":
        if campaign.status not in (
            Campaign.Status.ACTIVE,
            Campaign.Status.PAUSED,
        ):
            messages.warning(
                request,
                "Só é possível redistribuir mensagens aguardando chip em campanha ativa ou "
                "pausada.",
            )
            return redirect("campaigns:campaign_detail", pk=pk)
        raw_src = (request.POST.get("source_chip_id") or "").strip()
        if not raw_src.isdigit():
            messages.error(request, "Selecione o chip de origem (mensagens paradas).")
            return redirect("campaigns:campaign_detail", pk=pk)
        source_chip_id = int(raw_src)
        dest_raw = request.POST.getlist("dest_chip_ids")
        dest_ids: list[int] = []
        seen_d: set[int] = set()
        for raw in dest_raw:
            s = (raw or "").strip()
            if s.isdigit():
                d = int(s)
                if d not in seen_d:
                    seen_d.add(d)
                    dest_ids.append(d)
        try:
            n = redistribute_waiting_jobs(
                campaign,
                source_chip_id=source_chip_id,
                destination_chip_ids=dest_ids,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("campaigns:campaign_detail", pk=pk)
        except Exception as exc:
            messages.error(request, safe_user_message("redistribuir mensagens", str(exc)))
            return redirect("campaigns:campaign_detail", pk=pk)
        if n == 0:
            messages.info(
                request, "Nenhuma mensagem em «aguardando chip» para esse chip de origem."
            )
        else:
            messages.success(
                request,
                f"{n} mensagen{'s' if n > 1 else ''} reatribuída{'s' if n > 1 else ''} a outros "
                "chips. O worker voltará a processá-las na fila.",
            )
        return redirect("campaigns:campaign_detail", pk=pk)

    messages.error(request, "Ação inválida.")
    return redirect("campaigns:campaign_detail", pk=pk)


@login_required
@workspace_required
def campaign_confirm_delete(request, pk: int):
    workspace = request.current_workspace
    campaign = _campaign_for_workspace(workspace, pk)
    if campaign.is_deleted:
        messages.info(request, "Esta campanha já foi excluída.")
        return redirect("campaigns:campaign_detail", pk=pk)
    return render(
        request,
        "campaigns/confirm_delete.html",
        {"current_workspace": workspace, "campaign": campaign},
    )


@login_required
@workspace_required
@require_POST
@csrf_protect
@safe_action("excluir campanha", fallback="campaigns:campaign_list")
def campaign_delete(request, pk: int):
    workspace = request.current_workspace
    campaign = _campaign_for_workspace(workspace, pk)
    if campaign.is_deleted:
        messages.info(request, "Esta campanha já foi excluída.")
        return redirect("campaigns:campaign_list")
    # Cancel pending/locked jobs before marking the campaign as deleted.
    cancelled = campaign.send_jobs.filter(
        status__in=[
            CampaignSendJob.Status.PENDING,
            CampaignSendJob.Status.LOCKED,
            CampaignSendJob.Status.WAITING_CHIP,
        ]
    ).update(
        status=CampaignSendJob.Status.CANCELLED,
        schedule_defer_reason="",
        locked_at=None,
        locked_by="",
        lease_expires_at=None,
    )
    # If the campaign was active, finish it so the worker does not try to process it.
    if campaign.status == Campaign.Status.ACTIVE:
        campaign.status = Campaign.Status.FINISHED
    campaign.deleted_at = timezone.now()
    campaign.save()
    messages.success(
        request,
        f"Campanha arquivada. {cancelled} job(s) pendente(s) cancelado(s). "
        "O histórico completo de tentativas está preservado e pode ser consultado a qualquer "
        "momento.",
    )
    return redirect("campaigns:campaign_list")
