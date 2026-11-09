import hmac
import json
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_POST

from apps.accounts.decorators import workspace_required
from apps.campaigns.services.campaign_chip_schedule import (
    clear_future_schedules_for_chip,
    count_pending_future_scheduled_for_chip,
)
from apps.chips.evolution_debug import clear_log, get_log, log_evolution_call
from apps.chips.forms import ChipForm
from apps.chips.models import RECOMMENDED_CHIP_LIMITS, Chip
from apps.chips.services.evolution import (
    connection_state,
    instance_connect,
    instance_create,
    instance_delete,
    webhook_set,
)
from apps.chips.services.evolution_config import (
    evolution_configured,
    evolution_creds,
    evolution_error,
    evolution_missing_message,
    webhook_token,
)
from apps.common.utils.redirects import safe_next_url
from apps.common.utils.safe import safe_action, safe_json_action, safe_user_message


def _chip_active_campaigns(chip: Chip):
    """Return the ACTIVE campaigns that use this chip."""
    from apps.campaigns.models import Campaign

    return list(
        Campaign.objects.filter(
            campaign_chips__chip=chip,
            status=Campaign.Status.ACTIVE,
        ).distinct()
    )


def _render_chip_form(
    request, *, form: ChipForm, title: str, chip: Chip | None = None, blocking_campaigns=None
):
    ctx = {
        "form": form,
        "title": title,
        "recommended_limits": RECOMMENDED_CHIP_LIMITS,
        "blocking_campaigns": blocking_campaigns or [],
    }
    if chip is not None:
        ctx["chip"] = chip
        ctx["chip_deferred_pending"] = count_pending_future_scheduled_for_chip(chip.pk)
    return render(request, "chips/form.html", ctx)


@login_required
@workspace_required
def chip_list(request):
    return redirect("dashboards:home")


@login_required
@workspace_required
@csrf_protect
@safe_action("criar chip", fallback="dashboards:home")
def chip_create(request):
    workspace = request.current_workspace
    if request.method != "POST":
        return _render_chip_form(request, form=ChipForm(), title="Adicionar chip")

    form = ChipForm(request.POST)
    if not form.is_valid():
        return _render_chip_form(request, form=form, title="Adicionar chip")

    instance_name = form.cleaned_data["instance_name"]
    if Chip.objects.filter(workspace=workspace, instance_name=instance_name).exists():
        messages.error(request, "Já existe um chip neste perfil com este nome de instância.")
        return _render_chip_form(request, form=form, title="Adicionar chip")

    # Simulated chips have no Evolution instance, so nothing to create over HTTP.
    uses_evolution = form.instance.uses_evolution
    if uses_evolution:
        evo_url, evo_key = evolution_creds()
        ok, err = instance_create(evo_url, evo_key, instance_name)
        if not ok:
            messages.error(request, evolution_error("criar instância na Evolution", err))
            return _render_chip_form(request, form=form, title="Adicionar chip")

    chip = form.save(commit=False)
    chip.workspace = workspace
    chip.save()
    if uses_evolution:
        messages.success(request, "Chip adicionado e instância criada na Evolution.")
    else:
        messages.success(request, "Chip adicionado.")
    return redirect("dashboards:home")


@login_required
@workspace_required
@csrf_protect
@safe_action("editar chip", fallback="dashboards:home")
def chip_edit(request, pk):
    workspace = request.current_workspace
    chip = get_object_or_404(Chip, pk=pk, workspace=workspace)
    title = f"Editar {chip.name}"

    active_campaigns = _chip_active_campaigns(chip)
    if active_campaigns:
        return _render_chip_form(
            request,
            form=ChipForm(instance=chip),
            title=title,
            chip=chip,
            blocking_campaigns=active_campaigns,
        )

    if request.method != "POST":
        return _render_chip_form(request, form=ChipForm(instance=chip), title=title, chip=chip)

    form = ChipForm(request.POST, instance=chip)
    if not form.is_valid():
        return _render_chip_form(request, form=form, title=title, chip=chip)

    old_instance = (chip.instance_name or "").strip()
    new_instance = form.cleaned_data["instance_name"]

    if old_instance == new_instance or not form.instance.uses_evolution:
        form.save()
        messages.success(request, "Chip atualizado.")
        return redirect("dashboards:home")

    if not request.POST.get("confirm_evolution_rename"):
        form.add_error(
            None,
            ValidationError(
                "Ao alterar o nome da instância, a instância antiga é removida na Evolution "
                "e será necessário conectar de novo (QR). Marque a confirmação abaixo."
            ),
        )
        return _render_chip_form(request, form=form, title=title, chip=chip)

    if not evolution_configured():
        messages.error(request, evolution_missing_message())
        return _render_chip_form(request, form=form, title=title, chip=chip)

    evo_url, evo_key = evolution_creds()
    if old_instance:
        ok_del, err_del = instance_delete(evo_url, evo_key, old_instance)
        if not ok_del:
            messages.error(
                request,
                evolution_error("remover instância antiga na Evolution", err_del),
            )
            return _render_chip_form(request, form=form, title=title, chip=chip)

    ok_create, err_create = instance_create(evo_url, evo_key, new_instance)
    if not ok_create:
        messages.error(
            request,
            evolution_error("criar nova instância na Evolution", err_create)
            + (
                " A instância antiga já foi removida; tente criar de novo ou restaure o nome "
                "anterior."
            ),
        )
        return _render_chip_form(request, form=form, title=title, chip=chip)

    form.save()
    cache.delete(f"evolution_qr:{new_instance}")
    cache.delete(f"evolution_qr:{old_instance}")
    messages.success(
        request,
        "Instância atualizada na Evolution: a antiga foi removida e a nova criada. "
        "Use «Conectar (QR)» para vincular o WhatsApp novamente.",
    )
    return redirect("dashboards:home")


@login_required
@workspace_required
@require_POST
@csrf_protect
@safe_action("excluir chip", fallback="dashboards:home")
def chip_delete(request, pk):
    workspace = request.current_workspace
    chip = get_object_or_404(Chip, pk=pk, workspace=workspace)
    name = chip.name
    if chip.uses_evolution and chip.instance_name:
        evo_url, evo_key = evolution_creds()
        if not (evo_url and evo_key):
            messages.error(request, evolution_missing_message())
            return redirect("dashboards:home")
        try:
            ok, err = instance_delete(evo_url, evo_key, chip.instance_name)
            if not ok:
                messages.error(
                    request,
                    evolution_error("excluir instância na Evolution", err),
                )
                return redirect("dashboards:home")
        except Exception as exc:
            messages.error(request, safe_user_message("excluir instância na Evolution", exc))
            return redirect("dashboards:home")
    chip.delete()
    messages.success(request, f'Chip "{name}" excluído.')
    return redirect("dashboards:home")


@login_required
@workspace_required
@require_POST
@csrf_protect
@safe_action("limpar adiamentos do chip", fallback="dashboards:home")
def chip_clear_pending_schedule(request, pk):
    """Remove the future scheduled_at from this chip's PENDING jobs (reset deferrals)."""
    workspace = request.current_workspace
    chip = get_object_or_404(Chip, pk=pk, workspace=workspace)
    n = clear_future_schedules_for_chip(chip.pk)
    if n:
        messages.success(
            request,
            f"{n} envio(s) neste chip não estão mais adiados. O worker aplicará os limites atuais "
            "do chip.",
        )
    else:
        messages.info(request, "Não havia envios pendentes adiados para depois neste chip.")
    next_url = safe_next_url(request, request.POST.get("next"))
    return redirect(next_url or "dashboards:home")


@login_required
@workspace_required
def chip_confirm_delete(request, pk):
    workspace = request.current_workspace
    chip = get_object_or_404(Chip, pk=pk, workspace=workspace)
    return render(request, "chips/confirm_delete.html", {"chip": chip})


@login_required
@workspace_required
@safe_action("conectar chip", fallback="dashboards:home")
def chip_connect(request, pk):
    """Show the QR code / pairing to connect the chip to the Evolution API.

    Configure the webhook and trigger the connect.
    """
    workspace = request.current_workspace
    chip = get_object_or_404(Chip, pk=pk, workspace=workspace)
    if chip.provider == Chip.Provider.META:
        messages.info(
            request, "Chips Meta não precisam de conexão QR. Eles estão sempre prontos para enviar."
        )
        return redirect("dashboards:home")
    if chip.provider == Chip.Provider.SIMULATED:
        messages.info(request, "Chips simulados não precisam de conexão QR e não enviam nada.")
        return redirect("dashboards:home")
    evo_url, evo_key = evolution_creds()
    webhook_url = request.build_absolute_uri(
        reverse("chips:evolution_webhook_qrcode", kwargs={"token": webhook_token()})
    )
    cache.delete(f"evolution_qr:{chip.instance_name}")
    webhook_set(evo_url, evo_key, chip.instance_name, webhook_url)
    data = instance_connect(evo_url, evo_key, chip.instance_name)
    connect_error = None
    if data is None:
        ok, err = instance_create(evo_url, evo_key, chip.instance_name)
        if ok:
            data = instance_connect(evo_url, evo_key, chip.instance_name)
        if not ok:
            connect_error = evolution_error("conectar na Evolution", err)
    if data and (data.get("code") or data.get("base64")):
        cache.set(f"evolution_qr:{chip.instance_name}", data, 120)
    return render(
        request,
        "chips/connect.html",
        {
            "chip": chip,
            "connect_data": data,
            "connect_error": connect_error,
        },
    )


@login_required
@workspace_required
@safe_json_action("carregar QR do chip", status=500)
def chip_connect_json(request, pk):
    """Return JSON with the QR (webhook cache or instance_connect) for polling on the frontend."""
    workspace = request.current_workspace
    chip = get_object_or_404(Chip, pk=pk, workspace=workspace)
    if not chip.uses_evolution:
        return JsonResponse({"state": "open", "meta": chip.provider == Chip.Provider.META})
    evo_url, evo_key = evolution_creds()
    cached = cache.get(f"evolution_qr:{chip.instance_name}")
    if cached and (cached.get("code") or cached.get("base64")):
        return JsonResponse(cached)
    data = instance_connect(evo_url, evo_key, chip.instance_name)
    if data and (data.get("code") or data.get("base64")):
        cache.set(f"evolution_qr:{chip.instance_name}", data, 120)
    return JsonResponse(data or {})


@login_required
@workspace_required
@safe_json_action("verificar status do chip", status=500)
def chip_connect_status(request, pk):
    """Return JSON with the connection state (open/close) for polling on the frontend."""
    workspace = request.current_workspace
    chip = get_object_or_404(Chip, pk=pk, workspace=workspace)
    if not chip.uses_evolution:
        return JsonResponse({"state": "open", "meta": chip.provider == Chip.Provider.META})
    evo_url, evo_key = evolution_creds()
    state = connection_state(evo_url, evo_key, chip.instance_name)
    return JsonResponse({"state": state})


@login_required
@workspace_required
@safe_action("finalizar conexão do chip", fallback="dashboards:home")
def chip_connect_success(request, pk):
    """Redirect to home with a success message after the chip connects."""
    workspace = request.current_workspace
    chip = get_object_or_404(Chip, pk=pk, workspace=workspace)
    messages.success(request, f'Chip "{chip.name}" conectado com sucesso!')
    return redirect("dashboards:home")


# Logged instead of the real path so the secret token never reaches the debug page.
_WEBHOOK_LOG_URL = "/webhook/evolution/qrcode/<token>/"
_QR_BASE64_PREFIX = "data:image/png;base64,"
_QR_BASE64_BODY = re.compile(r"^[A-Za-z0-9+/=]+$")
_MAX_QR_BASE64_CHARS = 200_000
_MAX_PAIRING_CODE_CHARS = 32


def _clean_qr_base64(value: object) -> str | None:
    """Return the QR base64 (with or without the data URL prefix) if it looks like one."""
    if isinstance(value, dict):  # some Evolution versions send {"base64": "..."}
        value = value.get("base64")
    if not isinstance(value, str) or not value or len(value) > _MAX_QR_BASE64_CHARS:
        return None
    body = value.removeprefix(_QR_BASE64_PREFIX)
    return value if _QR_BASE64_BODY.fullmatch(body) else None


def _qr_payload_from_webhook(body: dict) -> tuple[str | None, dict]:
    """Extract (instance, cache payload) from the webhook body; payload is empty if no QR."""
    data = body.get("data")
    if not isinstance(data, dict):
        data = body
    instance = (
        body.get("instance")
        or body.get("instanceName")
        or data.get("instance")
        or data.get("instanceName")
    )
    if not isinstance(instance, str):
        return None, {}
    payload: dict = {}
    qr = _clean_qr_base64(
        body.get("qrcode") or body.get("base64") or data.get("qrcode") or data.get("base64")
    )
    if qr:
        payload["base64"] = qr
    pairing = body.get("pairingCode") or data.get("pairingCode")
    if isinstance(pairing, str) and 0 < len(pairing) <= _MAX_PAIRING_CODE_CHARS:
        payload["pairingCode"] = pairing
    return instance, payload


@csrf_exempt
def evolution_webhook_qrcode(request, token: str):
    """
    Webhook called by the Evolution API when the QRCODE_UPDATED event fires.
    Store the QR in cache so the frontend can show it while polling chip_connect_json.

    The caller has no session or CSRF token, so it is authenticated by the secret ``token`` in
    the URL (see ``webhook_token``) and may only write for instances that exist as chips.
    """
    if not hmac.compare_digest(token, webhook_token()):
        return HttpResponseForbidden()
    if request.method != "POST":
        return JsonResponse({"status": "ignored"}, status=200)
    try:
        body = json.loads(request.body) if request.body else {}
    except ValueError as exc:
        log_evolution_call(
            "webhook_received",
            _WEBHOOK_LOG_URL,
            response_body=request.body[:500].decode("utf-8", errors="replace"),
            error=exc,
        )
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"status": "error", "message": "Invalid payload"}, status=400)

    log_evolution_call("webhook_received", _WEBHOOK_LOG_URL, request_body=body)
    instance, payload = _qr_payload_from_webhook(body)
    if not instance:
        return JsonResponse({"status": "error", "message": "instance missing"}, status=400)
    if not Chip.objects.filter(instance_name=instance).exists():
        return JsonResponse({"status": "error", "message": "unknown instance"}, status=404)
    if payload:
        cache.set(f"evolution_qr:{instance}", payload, 120)
    return JsonResponse({"status": "ok"})


@login_required
def debug_evolution(request):
    """List the latest Evolution and webhook requests/responses for debugging."""
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "Acesso restrito ao administrador.")
        return redirect("dashboards:home")
    if request.method == "POST" and request.POST.get("clear"):
        clear_log()
        return redirect("chips:debug_evolution")
    log = get_log()
    return render(request, "chips/debug_evolution.html", {"log": reversed(list(log))})
