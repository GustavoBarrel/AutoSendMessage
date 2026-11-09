"""Campaign creation/edit wizard (audience → chips → messages)."""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect

from apps.accounts.decorators import workspace_required
from apps.campaigns.models import Campaign, CampaignMessageVariant
from apps.campaigns.services import campaign_lifecycle as lifecycle
from apps.campaigns.services.campaign_audience import (
    audience_summary_text,
    contacts_queryset_for_campaign,
    default_audience_config,
    filter_options_for_wizard,
    get_audience_config,
    parse_audience_post,
    sync_campaign_audience_field,
)
from apps.campaigns.services.message_variation_quality import (
    find_message_variation_issues,
    format_message_variation_feedback,
)
from apps.campaigns.views import _campaign_for_workspace, _sync_campaign_chips
from apps.chips.models import Chip
from apps.common.utils.safe import safe_action, safe_user_message
from apps.contacts.models import Contact, ManualTag

logger = logging.getLogger(__name__)

ALLOWED_STEPS = frozenset({"audience", "chips", "messages"})

_IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".jfif", ".heic", ".heif", ".bmp"}
)
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp"})
_ALLOWED_MEDIA_EXTENSIONS = _IMAGE_EXTENSIONS | _VIDEO_EXTENSIONS
_MEDIA_SUBDIR = "campaign_media"

# A local upload becomes base64 for Evolution (see resolve_media in message_utils).
# Base64 grows ~33%; a conservative limit avoids failures/OOM in sendMedia.
_MAX_UPLOAD_VIDEO_BYTES = 20 * 1024 * 1024  # 20 MB
_MAX_UPLOAD_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB

# QuickTime/MP4 files start with a size followed by one of these atom names.
_ISO_BASE_ATOMS = (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip")


def _has_media_signature(header: bytes, ext: str) -> bool:
    """Check the file's magic bytes against its extension (the client-sent type is not trusted)."""
    if ext in {".jpg", ".jpeg", ".jfif"}:
        return header.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".gif":
        return header.startswith(b"GIF8")
    if ext == ".bmp":
        return header.startswith(b"BM")
    if ext == ".webp":
        return header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    if ext == ".avi":
        return header[:4] == b"RIFF" and header[8:12] == b"AVI "
    if ext in {".webm", ".mkv"}:
        return header.startswith(b"\x1a\x45\xdf\xa3")
    # .heic .heif .mp4 .m4v .mov .3gp: ISO base media / QuickTime
    return header[4:8] in _ISO_BASE_ATOMS


def _save_uploaded_media(request, field: str, saved: list[Path] | None = None) -> str | None:
    """Save the uploaded file and return its absolute URL, or None if there was no upload.

    Type and size are decided by the server (extension, magic bytes and the real size), never
    by the client-sent Content-Type. Every written path is appended to ``saved`` so the caller
    can discard the files if the rest of the form is rejected.
    """
    f = request.FILES.get(field)
    if not f:
        return None
    content_type = (getattr(f, "content_type", "") or "").lower()
    if not content_type.startswith(("image/", "video/")):
        raise ValidationError("Arquivo inválido: envie uma imagem ou um vídeo.")
    ext = Path(f.name).suffix.lower()
    if ext not in _ALLOWED_MEDIA_EXTENSIONS:
        allowed = ", ".join(sorted(_ALLOWED_MEDIA_EXTENSIONS))
        raise ValidationError(f"Formato de arquivo não suportado ({ext}). Permitidos: {allowed}.")
    size = int(getattr(f, "size", 0) or 0)
    if ext in _VIDEO_EXTENSIONS and size > _MAX_UPLOAD_VIDEO_BYTES:
        raise ValidationError(
            f"Vídeo muito grande para envio via upload neste ambiente. "
            f"Use no máximo {_MAX_UPLOAD_VIDEO_BYTES // (1024 * 1024)} MB "
            f"ou envie por URL pública (HTTPS)."
        )
    if ext in _IMAGE_EXTENSIONS and size > _MAX_UPLOAD_IMAGE_BYTES:
        raise ValidationError(
            f"Imagem muito grande para envio via upload neste ambiente. "
            f"Use no máximo {_MAX_UPLOAD_IMAGE_BYTES // (1024 * 1024)} MB "
            f"ou envie por URL pública (HTTPS)."
        )
    f.seek(0)
    header = f.read(16)
    f.seek(0)
    if not _has_media_signature(header, ext):
        raise ValidationError(f"O conteúdo do arquivo não corresponde ao formato {ext}.")
    fname = f"{uuid.uuid4().hex}{ext}"
    dest = Path(settings.MEDIA_ROOT) / _MEDIA_SUBDIR / fname
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as out:
        if saved is not None:
            saved.append(dest)
        for chunk in f.chunks():
            out.write(chunk)
    return request.build_absolute_uri(f"{settings.MEDIA_URL}{_MEDIA_SUBDIR}/{fname}")


def _discard_files(paths: list[Path]) -> None:
    """Remove uploads that were written for a submission that was not accepted."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove discarded upload %s", path, exc_info=True)


def _preview_q(request) -> str:
    if request.method == "POST":
        return (request.POST.get("retain_q") or request.GET.get("q") or "").strip()
    return (request.GET.get("q") or "").strip()


def _redirect_wizard_audience(campaign_pk: int, preview_q: str = ""):
    url = reverse("campaigns:campaign_wizard", kwargs={"pk": campaign_pk, "step": "audience"})
    if preview_q:
        url = f"{url}?{urlencode({'q': preview_q})}"
    return redirect(url)


def _apply_audience_from_post(campaign: Campaign, post) -> None:
    new_cfg = parse_audience_post(post)
    prev_cfg = get_audience_config(campaign)
    prev_inc = prev_cfg.get("include_ids") or []
    full = default_audience_config()
    full.update(new_cfg)
    full["exclude_ids"] = list(dict.fromkeys(int(x) for x in (prev_cfg.get("exclude_ids") or [])))
    full["include_ids"] = list(dict.fromkeys(int(x) for x in prev_inc))
    campaign.audience_config = full
    sync_campaign_audience_field(campaign)
    campaign.save()


@login_required
@workspace_required
@csrf_protect
@safe_action("buscar contato", fallback="campaigns:campaign_list")
def campaign_audience_search_contact(request, pk: int):
    """
    JSON endpoint to search contacts (name or mobile phone) in the audience wizard.
    Return up to 10 results.
    """
    workspace = request.current_workspace
    campaign = _campaign_for_workspace(workspace, pk)
    if campaign.activated_at is not None:
        return JsonResponse(
            {"results": [], "wizard_locked": True},
            status=403,
        )

    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})

    digits = re.sub(r"\D", "", q)
    cond = Q(name__icontains=q)
    if digits:
        cond |= Q(mobile_phone__icontains=digits)

    qs = (
        Contact.objects.filter(cond)
        .order_by("name", "mobile_phone")
        .values("id", "name", "mobile_phone")[:10]
    )
    return JsonResponse({"results": list(qs)})


@login_required
@workspace_required
@csrf_protect
@safe_action("assistente da campanha", fallback="campaigns:campaign_list")
def campaign_wizard(request, pk: int, step: str):
    workspace = request.current_workspace
    campaign = _campaign_for_workspace(workspace, pk)
    if step not in ALLOWED_STEPS:
        return redirect("campaigns:campaign_wizard", pk=pk, step="audience")

    if campaign.activated_at is not None:
        messages.warning(
            request,
            "Esta campanha já foi iniciada ao menos uma vez. O assistente não está disponível — "
            "use o painel da campanha para pausar, retomar, finalizar ou redistribuir envios.",
        )
        return redirect("campaigns:campaign_detail", pk=pk)
    if campaign.status == Campaign.Status.ACTIVE:
        messages.warning(request, "Pause a campanha para usar o assistente.")
        return redirect("campaigns:campaign_detail", pk=pk)
    if campaign.status == Campaign.Status.FINISHED:
        messages.error(request, "Campanha finalizada.")
        return redirect("campaigns:campaign_detail", pk=pk)

    if step == "audience":
        return _wizard_audience(request, workspace, campaign)
    if step == "chips":
        return _wizard_chips(request, workspace, campaign)
    return _wizard_messages(request, workspace, campaign)


def _wizard_audience(request, workspace, campaign: Campaign):
    cfg = get_audience_config(campaign)
    retain = _preview_q(request)

    if request.method == "POST":
        action = request.POST.get("action") or "apply_filters"

        if action == "add_include_by_id":
            rid = request.POST.get("include_id")
            if not (rid and str(rid).isdigit()):
                messages.error(request, "Selecione um contato válido para incluir.")
                return _redirect_wizard_audience(campaign.pk, retain)
            ct = Contact.objects.filter(pk=int(rid)).first()
            if not ct:
                messages.error(request, "Contato não encontrado.")
                return _redirect_wizard_audience(campaign.pk, retain)
            cfg = get_audience_config(campaign)
            inc = list(cfg.get("include_ids") or [])
            if ct.pk not in inc:
                inc.append(ct.pk)
            cfg["include_ids"] = inc
            campaign.audience_config = cfg
            sync_campaign_audience_field(campaign)
            campaign.save()
            messages.success(request, f"Incluído: {ct.name or ct.mobile_phone}.")
            return _redirect_wizard_audience(campaign.pk, retain)

        if action == "add_include":
            raw = re.sub(r"\D", "", request.POST.get("mobile_phone_include", ""))
            if len(raw) != 11:
                messages.error(request, "Celular com 11 dígitos (DDD + número).")
                return _redirect_wizard_audience(campaign.pk, retain)
            ct = Contact.objects.filter(mobile_phone=raw).first()
            if not ct:
                messages.error(request, "Contato não encontrado com esse celular.")
                return _redirect_wizard_audience(campaign.pk, retain)
            cfg = get_audience_config(campaign)
            inc = list(cfg.get("include_ids") or [])
            if ct.pk not in inc:
                inc.append(ct.pk)
            cfg["include_ids"] = inc
            campaign.audience_config = cfg
            sync_campaign_audience_field(campaign)
            campaign.save()
            messages.success(request, f"Incluído: {ct.name or ct.mobile_phone}.")
            return _redirect_wizard_audience(campaign.pk, retain)

        if action == "remove_include":
            rid = request.POST.get("remove_include_id")
            if rid and str(rid).isdigit():
                cfg = get_audience_config(campaign)
                inc = [x for x in (cfg.get("include_ids") or []) if int(x) != int(rid)]
                cfg["include_ids"] = inc
                campaign.audience_config = cfg
                sync_campaign_audience_field(campaign)
                campaign.save()
            return _redirect_wizard_audience(campaign.pk, retain)

        if action == "add_exclude":
            rid = request.POST.get("pk")
            if rid and str(rid).isdigit():
                cfg = get_audience_config(campaign)
                ex = set(cfg.get("exclude_ids") or [])
                ex.add(int(rid))
                cfg["exclude_ids"] = list(ex)
                campaign.audience_config = cfg
                sync_campaign_audience_field(campaign)
                campaign.save()
                messages.success(request, "Contato excluído do público.")
            return _redirect_wizard_audience(campaign.pk, retain)

        if action == "add_exclude_bulk":
            raw_ids = request.POST.getlist("exclude_ids")
            ids = [int(x) for x in raw_ids if str(x).isdigit()]
            if not ids:
                messages.warning(request, "Selecione ao menos um contato para excluir.")
                return _redirect_wizard_audience(campaign.pk, retain)
            cfg = get_audience_config(campaign)
            ex = set(cfg.get("exclude_ids") or [])
            ex.update(ids)
            cfg["exclude_ids"] = list(ex)
            campaign.audience_config = cfg
            sync_campaign_audience_field(campaign)
            campaign.save()
            messages.success(request, f"{len(ids)} contato(s) excluído(s) do público.")
            return _redirect_wizard_audience(campaign.pk, retain)

        if action == "remove_exclude":
            rid = request.POST.get("pk")
            if rid and str(rid).isdigit():
                cfg = get_audience_config(campaign)
                ex = [x for x in (cfg.get("exclude_ids") or []) if int(x) != int(rid)]
                cfg["exclude_ids"] = ex
                campaign.audience_config = cfg
                sync_campaign_audience_field(campaign)
                campaign.save()
                messages.success(request, "Exclusão revertida.")
            return _redirect_wizard_audience(campaign.pk, retain)

        if action in ("apply_filters", "continue_to_chips", "save"):
            if action == "save":
                action = "continue_to_chips"
            _apply_audience_from_post(campaign, request.POST)
            if action == "apply_filters":
                messages.success(request, "Filtros aplicados. Confira a lista abaixo.")
                rq = (request.POST.get("retain_q") or "").strip()
                return _redirect_wizard_audience(campaign.pk, rq)
            messages.success(request, "Público salvo.")
            return redirect("campaigns:campaign_wizard", pk=campaign.pk, step="chips")

    base_qs = contacts_queryset_for_campaign(campaign)
    recipients_total = base_qs.count()
    qs = base_qs
    preview_q = (request.GET.get("q") or "").strip()
    if preview_q:
        digits = re.sub(r"\D", "", preview_q)
        cond = Q(name__icontains=preview_q)
        if digits:
            cond |= Q(mobile_phone__icontains=digits)
        qs = qs.filter(cond)
    total = qs.count()
    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get("page") or 1)
    ex_ids = cfg.get("exclude_ids") or []
    excluded_contacts = (
        list(Contact.objects.filter(pk__in=ex_ids).order_by("name")) if ex_ids else []
    )

    include_ids = cfg.get("include_ids") or []
    included = (
        list(Contact.objects.filter(pk__in=include_ids).order_by("name")) if include_ids else []
    )

    filter_opts = filter_options_for_wizard(cfg)
    manual_tags = list(ManualTag.objects.all().order_by("name"))

    return render(
        request,
        "campaigns/wizard_audience.html",
        {
            "current_workspace": workspace,
            "campaign": campaign,
            "cfg": cfg,
            "audience_summary": audience_summary_text(campaign),
            "recipients_total": recipients_total,
            "total": total,
            "page_obj": page,
            "excluded_contacts": excluded_contacts,
            "included": included,
            "step": "audience",
            "wizard_step": 1,
            "filter_opts": filter_opts,
            "manual_tags": manual_tags,
            "preview_q": preview_q,
        },
    )


def _wizard_chips(request, workspace, campaign: Campaign):
    available_chips = Chip.objects.filter(workspace=workspace, is_active=True).order_by("name")
    chips_sel = list(
        campaign.campaign_chips.filter(is_active=True).values_list("chip_id", flat=True)
    )
    evolution_ok = bool(
        (settings.EVOLUTION_URL_BASE or "").strip() and (settings.EVOLUTION_API_KEY or "").strip()
    )
    evo_url = (settings.EVOLUTION_URL_BASE or "").strip()
    evo_key = (settings.EVOLUTION_API_KEY or "").strip()
    from apps.chips.services.evolution import connection_state as evo_connection_state

    def _chip_ok(ch):
        if ch.provider == Chip.Provider.META:
            return bool(ch.meta_phone_number_id and ch.meta_access_token)
        if ch.provider == Chip.Provider.SIMULATED:
            return True
        if not evolution_ok:
            return True
        return evo_connection_state(evo_url, evo_key, ch.instance_name) == "open"

    chip_states: dict[int, str | None] = {}
    for c in available_chips:
        if not c.uses_evolution:
            chip_states[c.pk] = "open"
        elif evolution_ok and c.instance_name:
            chip_states[c.pk] = evo_connection_state(evo_url, evo_key, c.instance_name)
        else:
            chip_states[c.pk] = None

    chips_with_state = [
        {
            "chip": c,
            "state": chip_states.get(c.pk),
            "is_connected": _chip_ok(c),
        }
        for c in available_chips
    ]

    if request.method == "POST":
        raw_ids = request.POST.getlist("chip_ids")
        chip_ids = [int(x) for x in raw_ids if str(x).isdigit()]
        chip_ids = [cid for cid in chip_ids if _chip_ok(Chip.objects.get(pk=cid))]
        _sync_campaign_chips(campaign, chip_ids)
        if not chip_ids:
            messages.error(request, "Selecione ao menos um chip conectado e válido.")
            return redirect("campaigns:campaign_wizard", pk=campaign.pk, step="chips")
        messages.success(request, "Chips salvos.")
        return redirect("campaigns:campaign_wizard", pk=campaign.pk, step="messages")

    return render(
        request,
        "campaigns/wizard_chips.html",
        {
            "current_workspace": workspace,
            "campaign": campaign,
            "available_chips": available_chips,
            "chips_with_state": chips_with_state,
            "chips_sel": chips_sel,
            "evolution_ok": evolution_ok,
            "step": "chips",
            "wizard_step": 2,
        },
    )


def _variant_display_for_template(
    n: int,
    v: CampaignMessageVariant | None,
    pd: dict,
) -> dict[str, str]:
    """Merge POST with the database without touching attributes on None.

    Avoids a template error with |default:v.field.
    """
    message_type = (pd.get(f"message_type_{n}") or "").strip()
    if not message_type and v is not None:
        message_type = str(v.message_type or "")
    text = pd.get(f"text_{n}", "")
    text_s = text if isinstance(text, str) else str(text or "")
    if not text_s.strip() and v is not None:
        text_s = v.text or ""
    media = (pd.get(f"media_{n}") or "").strip()
    if not media and v is not None:
        media = (v.media_url or "").strip()
    return {"message_type": message_type, "text": text_s, "media": media}


def _messages_render(request, workspace, campaign: Campaign, post_data: dict | None = None):
    """Render the messages step, optionally refilling it with POST data."""
    variants = {
        v.position: v
        for v in CampaignMessageVariant.objects.filter(campaign=campaign).order_by("position")
    }
    pd = post_data or {}

    return render(
        request,
        "campaigns/wizard_messages.html",
        {
            "current_workspace": workspace,
            "campaign": campaign,
            "message_type_choices": CampaignMessageVariant.MessageType.choices,
            "variants_data": [
                (1, "Variação 1", True, _variant_display_for_template(1, variants.get(1), pd)),
                (2, "Variação 2", False, _variant_display_for_template(2, variants.get(2), pd)),
                (3, "Variação 3", False, _variant_display_for_template(3, variants.get(3), pd)),
            ],
            "shared_message_type_post": pd.get("shared_message_type", ""),
            "shared_media_url_post": pd.get("shared_media_url", ""),
            "step": "messages",
            "wizard_step": 3,
            "has_post_data": bool(pd),
        },
    )


def _wizard_messages(request, workspace, campaign: Campaign):
    if request.method == "POST":
        saved_files: list[Path] = []
        accepted = False
        try:
            shared_url = (
                _save_uploaded_media(request, "shared_media_file", saved_files)
                or (request.POST.get("shared_media_url") or "").strip()
            )
            shared_message_type = (request.POST.get("shared_message_type") or "").strip()
            using_shared = bool(shared_message_type and shared_message_type in ("image", "video"))

            if using_shared:
                # Shared media: type and media are fixed for all variants
                message_type_1 = message_type_2 = message_type_3 = shared_message_type
                media_1 = media_2 = media_3 = shared_url
            else:

                def _media(n: int) -> str:
                    uploaded = _save_uploaded_media(request, f"media_file_{n}", saved_files)
                    if uploaded:
                        return uploaded
                    return (request.POST.get(f"media_{n}") or "").strip()

                message_type_1 = request.POST.get("message_type_1") or "text"
                media_1 = _media(1)
                message_type_2 = request.POST.get("message_type_2") or "text"
                media_2 = _media(2)
                message_type_3 = request.POST.get("message_type_3") or "text"
                media_3 = _media(3)

            text_1 = request.POST.get("text_1", "")
            text_2 = request.POST.get("text_2", "")
            text_3 = request.POST.get("text_3", "")
            all_variation_texts_filled = all(text.strip() for text in (text_1, text_2, text_3))

            copied: list[str] = []

            def _copy_missing_from_first(
                label: str,
                message_type: str,
                text: str,
                media: str,
            ) -> tuple[str, str, str]:
                copied_any = False
                if not text.strip() and text_1.strip():
                    text = text_1
                    copied_any = True
                if (message_type == "text" and not text.strip()) or (
                    message_type != "text" and not media.strip()
                ):
                    message_type, text, media = message_type_1, text_1, media_1
                    copied_any = True
                if copied_any:
                    copied.append(label)
                return message_type, text, media

            message_type_2, text_2, media_2 = _copy_missing_from_first(
                "Variação 2",
                message_type_2,
                text_2,
                media_2,
            )
            message_type_3, text_3, media_3 = _copy_missing_from_first(
                "Variação 3",
                message_type_3,
                text_3,
                media_3,
            )
            if copied:
                names = " e ".join(copied)
                messages.warning(
                    request,
                    f"{names} {'foi copiada' if len(copied) == 1 else 'foram copiadas'} da "
                    "Variação 1 "
                    f"por estar{'em' if len(copied) > 1 else ''} "
                    f"vazia{'s' if len(copied) > 1 else ''}. "
                    "Para maior diversidade e menor risco de bloqueio, preencha variações "
                    "distintas.",
                )

            if not copied and all_variation_texts_filled:
                variation_issues = find_message_variation_issues([text_1, text_2, text_3])
                if variation_issues:
                    messages.error(
                        request,
                        format_message_variation_feedback(variation_issues),
                    )
                    return _messages_render(request, workspace, campaign, post_data=request.POST)

            lifecycle.save_variants_from_post(
                campaign,
                message_type_1=message_type_1,
                text_1=text_1,
                media_1=media_1,
                message_type_2=message_type_2,
                text_2=text_2,
                media_2=media_2,
                message_type_3=message_type_3,
                text_3=text_3,
                media_3=media_3,
            )
            accepted = True
        except ValidationError as exc:
            messages.error(request, " ".join(str(m) for m in exc.messages))
            return _messages_render(request, workspace, campaign, post_data=request.POST)
        except Exception as exc:
            messages.error(
                request,
                safe_user_message("validar mensagens", str(exc)),
            )
            return _messages_render(request, workspace, campaign, post_data=request.POST)
        finally:
            if not accepted:
                _discard_files(saved_files)
        messages.success(
            request, "Mensagens salvas. Revise o painel e inicie o envio quando estiver pronto."
        )
        return redirect("campaigns:campaign_detail", pk=campaign.pk)

    return _messages_render(request, workspace, campaign)
