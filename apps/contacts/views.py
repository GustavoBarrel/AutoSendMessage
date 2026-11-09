import logging
import os
import tempfile
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import DataError, IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from apps.common.utils.redirects import safe_next_url
from apps.common.utils.safe import safe_action
from apps.contacts.constants import MAX_BULK_SELECTION
from apps.contacts.forms import (
    DecisionConflictsForm,
    MappingColumnsForm,
    ResolveDuplicatesForm,
    UploadExcelForm,
)
from apps.contacts.models import Contact, ManualTag
from apps.contacts.services import contacts_import as contacts_import_svc
from apps.contacts.services.contact_archive import (
    active_contacts_by_ids,
    archive_contacts,
    summarize_pending_sends,
)
from apps.contacts.services.contact_filters import (
    active_panel_filter_count,
    bind_filter_form,
    filter_contacts,
    filter_options,
    filters_querystring,
)

logger = logging.getLogger(__name__)

# Same color as --color-primary in static/css/tokens.css
# (RGB hex without '#', as openpyxl expects).
BRAND_COLOR_HEX = "15803D"

# One-shot session flag: tells the list page to clear the persisted bulk selection.
BULK_CLEAR_SESSION_KEY = "contacts_bulk_clear_selection"
BULK_FORGET_SESSION_KEY = "contacts_bulk_forget_ids"
BULK_PREVIEW_SIZE = 10
MAX_ID_DIGITS = 18  # keeps int() cheap and well inside the 64-bit range


def _row_to_session(row):
    """Convert a row dict to a session-serializable format (date -> str)."""
    out = dict(row)
    if out.get("birth_date") and hasattr(out["birth_date"], "isoformat"):
        out["birth_date"] = out["birth_date"].isoformat()
    return out


def _row_from_session(row):
    """Convert a session dict back (str -> date)."""
    out = dict(row)
    if out.get("birth_date") and isinstance(out["birth_date"], str):
        try:
            out["birth_date"] = datetime.strptime(out["birth_date"], "%Y-%m-%d").date()
        except ValueError:
            out["birth_date"] = None
    return out


def _get_import_session(request):
    """Session prefix of the import wizard."""
    return request.session.get("import_contacts") or {}


def _set_import_session(request, data):
    request.session["import_contacts"] = data


def _clear_import_session(request):
    request.session.pop("import_contacts", None)
    fpath = request.session.pop("import_contacts_file", None)
    if fpath and os.path.isfile(fpath):
        try:
            os.unlink(fpath)
        except OSError:
            logger.warning("Could not remove temporary import file %s", fpath, exc_info=True)


@login_required
def contact_confirm_delete(request, pk: int):
    contact = get_object_or_404(Contact, pk=pk)
    campaigns_affected = summarize_pending_sends([contact.pk])
    return render(
        request,
        "contacts/confirm_delete.html",
        {
            "contact": contact,
            "campaigns_affected": campaigns_affected,
            "total_pending": sum(c.count for c in campaigns_affected),
        },
    )


@login_required
@require_POST
def contact_delete(request, pk: int):
    contact = get_object_or_404(Contact, pk=pk)
    name = contact.name or contact.mobile_phone
    if contact.deleted_at is not None:
        messages.info(request, f'Contato "{name}" já estava arquivado.')
        _forget_from_bulk_selection(request, contact.pk)
        return redirect("contacts:contact_list")
    result = archive_contacts([contact])
    msg = f'Contato "{name}" arquivado.'
    if result.cancelled_sends:
        msg += f" {result.cancelled_sends} envio(s) pendente(s) cancelado(s) nas campanhas."
    messages.success(request, msg)
    _forget_from_bulk_selection(request, contact.pk)
    return redirect("contacts:contact_list")


def _forget_from_bulk_selection(request, pk: int) -> None:
    """Tell the list's JS to drop a contact archived one by one from the bulk selection.

    The selection lives in the browser, so the server can only pass the id along;
    without this the counter would keep a contact that no longer exists.
    """
    pending = request.session.get(BULK_FORGET_SESSION_KEY, [])
    if pk not in pending:
        request.session[BULK_FORGET_SESSION_KEY] = [*pending, pk]


def _filtered_contacts(request):
    """Filter form (bound to GET) and the active contacts it selects."""
    form = bind_filter_form(request.GET)
    return form, filter_contacts(form.cleaned_data)


def _safe_next_url(request, raw_next: str | None) -> str:
    """`next` if it points to this host, otherwise the contact list."""
    return safe_next_url(request, raw_next) or reverse("contacts:contact_list")


def _parse_contact_ids(raw_csv: str | None, row_values: list[str] | None = None) -> list[int]:
    """
    Parse the IDs of a bulk action: the comma-separated field plus the per-row checkboxes.

    The checkboxes are the no-JavaScript fallback; their IDs are merged into the CSV ones
    (an ID present in both sources counts once). Repeated IDs inside one source are rejected.

    Raises ValueError (with a pt-BR message for the user) on anything that is not a list of
    positive integers whose merged size is within MAX_BULK_SELECTION.
    """
    csv_tokens = [token.strip() for token in (raw_csv or "").split(",") if token.strip()]
    row_tokens = [token.strip() for token in (row_values or []) if token.strip()]
    if len(csv_tokens) + len(row_tokens) > 2 * MAX_BULK_SELECTION:
        raise ValueError(_too_many_selected_message())
    if not all(
        token.isascii() and token.isdigit() and len(token) <= MAX_ID_DIGITS and int(token) > 0
        for token in (*csv_tokens, *row_tokens)
    ):
        raise ValueError("A seleção contém identificadores de contato inválidos.")
    csv_ids = [int(token) for token in csv_tokens]
    row_ids = [int(token) for token in row_tokens]
    if len(set(csv_ids)) != len(csv_ids) or len(set(row_ids)) != len(row_ids):
        raise ValueError("A seleção contém contatos repetidos.")
    merged = list(dict.fromkeys([*csv_ids, *row_ids]))
    if len(merged) > MAX_BULK_SELECTION:
        raise ValueError(_too_many_selected_message())
    return merged


def _too_many_selected_message() -> str:
    return f"Selecione no máximo {MAX_BULK_SELECTION} contatos por vez. Refine o filtro."


def _resolve_bulk_selection(request):
    """
    Validate the POSTed selection.

    Returns (ids, contacts, None) when there is something to archive, or
    (None, None, redirect_response) after flashing the reason.
    """
    next_url = _safe_next_url(request, request.POST.get("next"))
    try:
        ids = _parse_contact_ids(
            request.POST.get("contact_ids"), request.POST.getlist("contact_ids_row")
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return None, None, redirect(next_url)
    contacts = active_contacts_by_ids(ids)
    if not contacts:
        messages.warning(request, "Nenhum contato ativo selecionado para arquivar.")
        return None, None, redirect(next_url)
    return ids, contacts, None


@login_required
@require_GET
def contact_ids(request):
    """IDs of the active contacts matching the list filters (for "select all results")."""
    _, qs = _filtered_contacts(request)
    count = qs.count()
    if count > MAX_BULK_SELECTION:
        return JsonResponse(
            {
                "error": (
                    f"Há {count} contatos nesse filtro e o limite por ação é "
                    f"{MAX_BULK_SELECTION}. Refine o filtro para selecionar todos."
                ),
                "count": count,
            },
            status=400,
        )
    return JsonResponse({"ids": list(qs.values_list("pk", flat=True)), "count": count})


@login_required
@require_POST
def contact_bulk_archive(request):
    """Confirmation page of the bulk archive."""
    ids, contacts, early_response = _resolve_bulk_selection(request)
    if early_response:
        return early_response
    valid_ids = [c.pk for c in contacts]
    campaigns_affected = summarize_pending_sends(valid_ids)
    total_active = Contact.objects.filter(deleted_at__isnull=True).count()
    preview = [
        f"{c.name} — {c.mobile_phone}" if c.name else c.mobile_phone
        for c in contacts[:BULK_PREVIEW_SIZE]
    ]
    return render(
        request,
        "contacts/bulk_archive_confirm.html",
        {
            "total": len(valid_ids),
            "ignored": len(ids) - len(valid_ids),
            "preview": preview,
            "remaining": len(valid_ids) - len(preview),
            "covers_all_active": len(valid_ids) == total_active,
            "campaigns_affected": campaigns_affected,
            "total_pending": sum(c.count for c in campaigns_affected),
            "contact_ids_csv": ",".join(str(pk) for pk in valid_ids),
            "next_url": _safe_next_url(request, request.POST.get("next")),
        },
    )


@login_required
@require_POST
def contact_bulk_archive_confirm(request):
    """Archive the selection (re-validated) and cancel its pending sends."""
    _, contacts, early_response = _resolve_bulk_selection(request)
    if early_response:
        return early_response
    result = archive_contacts(contacts)
    msg = f"{result.archived} contato(s) arquivado(s)."
    if result.cancelled_sends:
        msg += f" {result.cancelled_sends} envio(s) pendente(s) cancelado(s)."
    messages.success(request, msg)
    request.session[BULK_CLEAR_SESSION_KEY] = True
    return redirect(_safe_next_url(request, request.POST.get("next")))


@login_required
def contact_list(request):
    """List of global contacts with filters and pagination."""
    form, qs = _filtered_contacts(request)
    cleaned = form.cleaned_data
    querystring = filters_querystring(cleaned)
    paginator = Paginator(qs, 20)
    page = request.GET.get("page", 1)
    page_obj = paginator.get_page(page)
    total_contacts = paginator.count  # cached by the paginator: no second COUNT query

    last_page = page_obj.paginator.num_pages
    current = page_obj.number
    window_size = 5
    if current <= window_size:
        start = 1
    else:
        start = current
    end = min(start + window_size - 1, last_page)
    page_window = list(range(start, end + 1))
    show_last = last_page not in page_window
    show_ellipsis = show_last and (last_page - page_window[-1] > 1)

    return render(
        request,
        "contacts/list.html",
        {
            "page_obj": page_obj,
            "total_contacts": total_contacts,
            "search_query": cleaned.get("q", ""),
            "filter_form": form,
            "filter_options": filter_options(),
            "filters_querystring": querystring,
            "has_active_filters": bool(querystring),
            "active_filter_count": active_panel_filter_count(cleaned),
            "page_window": page_window,
            "show_last_page": show_last,
            "show_ellipsis": show_ellipsis,
            "clear_bulk_selection": request.session.pop(BULK_CLEAR_SESSION_KEY, False),
            "forget_bulk_ids": request.session.pop(BULK_FORGET_SESSION_KEY, []),
            "contact_ids_url": reverse("contacts:contact_ids"),
            "bulk_archive_url": reverse("contacts:contact_bulk_archive"),
            "max_bulk_selection": MAX_BULK_SELECTION,
        },
    )


@login_required
@csrf_protect
@safe_action("editar tags do contato", fallback="contacts:contact_list")
def contact_tags(request, pk: int):
    contact = get_object_or_404(Contact, pk=pk)
    if request.method == "POST":
        raw_ids = request.POST.getlist("tag_ids")
        ids = [int(x) for x in raw_ids if str(x).isdigit()]
        tags = list(ManualTag.objects.filter(pk__in=ids).order_by("name"))
        contact.manual_tags.set(tags)
        messages.success(request, "Tags atualizadas.")
        return redirect("contacts:contact_list")

    tags_all = list(ManualTag.objects.all().order_by("name"))
    cur_ids = set(contact.manual_tags.values_list("pk", flat=True))
    return render(
        request,
        "contacts/tags.html",
        {"contact": contact, "tags_all": tags_all, "cur_ids": cur_ids},
    )


@login_required
@safe_action("listar tags", fallback="dashboards:home")
def manual_tag_list(request):
    q = (request.GET.get("q") or "").strip()
    qs = ManualTag.objects.all().order_by("name")
    if q:
        qs = qs.filter(name__icontains=q)
    return render(
        request,
        "contacts/tag_list.html",
        {"tags": list(qs[:500]), "q": q},
    )


@login_required
@csrf_protect
@safe_action("criar tag", fallback="contacts:manual_tag_list")
def manual_tag_create(request):
    if request.method != "POST":
        return render(request, "contacts/tag_form.html", {"tag": None, "name": ""})
    name = (request.POST.get("name") or "").strip()[:60]
    if not name:
        messages.error(request, "Informe o nome da tag.")
        return render(request, "contacts/tag_form.html", {"tag": None, "name": name})
    try:
        ManualTag.objects.create(name=name)
    except IntegrityError:
        messages.error(request, "Já existe uma tag com esse nome.")
        return render(request, "contacts/tag_form.html", {"tag": None, "name": name})
    messages.success(request, "Tag criada.")
    return redirect("contacts:manual_tag_list")


@login_required
@csrf_protect
@safe_action("editar tag", fallback="contacts:manual_tag_list")
def manual_tag_edit(request, pk: int):
    tag = get_object_or_404(ManualTag, pk=pk)
    if request.method != "POST":
        return render(request, "contacts/tag_form.html", {"tag": tag, "name": tag.name})
    name = (request.POST.get("name") or "").strip()[:60]
    if not name:
        messages.error(request, "Informe o nome da tag.")
        return render(request, "contacts/tag_form.html", {"tag": tag, "name": name})
    tag.name = name
    try:
        tag.save()
    except IntegrityError:
        messages.error(request, "Já existe uma tag com esse nome.")
        return render(request, "contacts/tag_form.html", {"tag": tag, "name": name})
    messages.success(request, "Tag atualizada.")
    return redirect("contacts:manual_tag_list")


@login_required
@csrf_protect
@safe_action("confirmar exclusão da tag", fallback="contacts:manual_tag_list")
def manual_tag_confirm_delete(request, pk: int):
    tag = get_object_or_404(ManualTag, pk=pk)
    return render(request, "contacts/tag_confirm_delete.html", {"tag": tag})


@login_required
@require_POST
@csrf_protect
@safe_action("excluir tag", fallback="contacts:manual_tag_list")
def manual_tag_delete(request, pk: int):
    tag = get_object_or_404(ManualTag, pk=pk)
    name = tag.name
    tag.delete()
    messages.success(request, f'Tag "{name}" excluída.')
    return redirect("contacts:manual_tag_list")


@login_required
@require_GET
def import_template(request):
    """Generate and send a template Excel file for importing contacts."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Contatos"

    headers = ["Celular*", "Nome", "Email", "DataNascimento", "Sexo", "Categoria"]
    ws.append(headers)

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(
        start_color=BRAND_COLOR_HEX, end_color=BRAND_COLOR_HEX, fill_type="solid"
    )
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    required_fill = PatternFill(start_color="FFE0E0", end_color="FFE0E0", fill_type="solid")
    optional_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
    cell_align = Alignment(vertical="center")
    thin = Side(style="thin", color="D1D5DB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col_idx, _ in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = border

    example_rows = [
        ["00999990001", "João Silva", "joao@exemplo.com", "1990-05-20", "Masculino", "Cliente"],
        ["00999990002", "Maria Souza", "maria@exemplo.com", "1985-11-03", "Feminino", "Lead"],
    ]
    for row in example_rows:
        ws.append(row)

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        for idx, cell in enumerate(row, start=1):
            cell.alignment = cell_align
            cell.border = border
            if idx == 1:
                cell.fill = required_fill
            else:
                cell.fill = optional_fill

    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 26
    ws.column_dimensions["C"].width = 28
    ws.column_dimensions["D"].width = 18
    ws.column_dimensions["E"].width = 16
    ws.column_dimensions["F"].width = 18

    legend = wb.create_sheet("Obrigatoriedade")
    legend.append(["Campo", "Obrigatoriedade", "Observação"])
    legend_rows = [
        ["Celular", "Obrigatório", "11 dígitos, sem formatação. Ex: 11999999999"],
        ["Nome", "Opcional", "Nome completo do contato."],
        ["Email", "Opcional", "E-mail válido."],
        ["DataNascimento", "Opcional", "Formato ISO: AAAA-MM-DD."],
        ["Sexo", "Opcional", "Livre, ex: Masculino/Feminino/Outro."],
        ["Categoria", "Opcional", "Livre, ex: Cliente, Lead."],
    ]
    for row in legend_rows:
        legend.append(row)
    for row in legend.iter_rows():
        for cell in row:
            cell.border = border
            cell.alignment = Alignment(vertical="center", wrap_text=True)
    legend.column_dimensions["A"].width = 18
    legend.column_dimensions["B"].width = 18
    legend.column_dimensions["C"].width = 52

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="modelo-importacao-contatos.xlsx"'
    wb.save(response)
    return response


@login_required
@csrf_protect
def import_upload(request):
    """Step 1: Excel upload."""
    if request.method == "POST":
        form = UploadExcelForm(request.POST, request.FILES)
        if form.is_valid():
            file = form.cleaned_data["file"]
            lower = file.name.lower()
            if lower.endswith(".csv"):
                suffix = ".csv"
            elif lower.endswith(".xlsx"):
                suffix = ".xlsx"
            else:
                suffix = ".xls"
            fd, path = tempfile.mkstemp(suffix=suffix)
            try:
                with os.fdopen(fd, "wb") as f:
                    for chunk in file.chunks():
                        f.write(chunk)
            except Exception:
                os.unlink(path)
                raise
            request.session["import_contacts_file"] = path
            try:
                headers = contacts_import_svc.read_file_header(path)
            except Exception as e:
                os.unlink(path)
                request.session.pop("import_contacts_file", None)
                messages.error(request, f"Erro ao ler planilha: {e}")
                return render(request, "contacts/import_upload.html", {"form": form})
            mapping = contacts_import_svc.auto_map_columns(headers)
            mapping = contacts_import_svc.resolve_mapping_collisions(mapping)
            if "mobile_phone" not in mapping:
                messages.warning(
                    request,
                    "Nenhuma coluna foi reconhecida como 'Celular'. Será necessário mapear "
                    "manualmente.",
                )
            _set_import_session(
                request,
                {
                    "headers": {str(k): v for k, v in headers.items()},
                    "mapping": mapping,
                },
            )
            return redirect("contacts:contacts_import_map")
        return render(request, "contacts/import_upload.html", {"form": form})
    form = UploadExcelForm()
    return render(request, "contacts/import_upload.html", {"form": form})


@login_required
@csrf_protect
def import_map(request):
    """Step 2: column mapping."""
    fpath = request.session.get("import_contacts_file")
    if not fpath or not os.path.isfile(fpath):
        messages.warning(request, "Sessão de importação expirada. Envie o arquivo novamente.")
        return redirect("contacts:contacts_import")
    data = _get_import_session(request)
    headers_raw = data.get("headers") or {}
    headers = {int(k): v for k, v in headers_raw.items()}
    mapping = data.get("mapping") or {}
    if request.method == "POST":
        form = MappingColumnsForm(
            request.POST,
            headers=headers,
            mapping_initial=mapping,
        )
        if form.is_valid():
            new_map = form.get_mapping()
            data["mapping"] = new_map
            _set_import_session(request, data)
            return redirect("contacts:contacts_import_preview")
        return render(
            request,
            "contacts/import_map.html",
            {"form": form, "headers": headers},
        )
    form = MappingColumnsForm(
        headers=headers,
        mapping_initial=mapping,
    )
    return render(
        request,
        "contacts/import_map.html",
        {"form": form, "headers": headers},
    )


@login_required
@csrf_protect
def import_preview(request):
    """Step 3: preview and validation; after confirming, go to duplicates or the summary."""
    fpath = request.session.get("import_contacts_file")
    if not fpath or not os.path.isfile(fpath):
        messages.warning(request, "Sessão de importação expirada.")
        return redirect("contacts:contacts_import")
    data = _get_import_session(request)
    mapping = data.get("mapping")
    if not mapping or "mobile_phone" not in mapping:
        return redirect("contacts:contacts_import_map")

    if request.method != "POST":
        try:
            rows_raw = contacts_import_svc.read_file_rows(fpath, mapping)
        except contacts_import_svc.ImportLimitError as exc:
            _clear_import_session(request)
            messages.error(request, str(exc))
            return redirect("contacts:contacts_import")
        valid_rows = []
        errors = []
        for i, row in enumerate(rows_raw):
            row_number = row.get("_row", i + 2)
            parsed, row_errors = contacts_import_svc.parse_row(row, row_number)
            if row_errors:
                errors.append(
                    {
                        "mobile_phone": parsed.get("mobile_phone", ""),
                        "reason": "; ".join(row_errors),
                        "row": row_number,
                    }
                )
                continue
            if not parsed.get("mobile_phone"):
                errors.append(
                    {
                        "mobile_phone": "",
                        "reason": "Celular obrigatório ausente.",
                        "row": row_number,
                    }
                )
                continue
            parsed["_row"] = row_number
            valid_rows.append(parsed)

        data["preview_rows"] = [_row_to_session(r) for r in valid_rows[:20]]
        data["preview_count_total"] = len(rows_raw)
        data["preview_valid_count"] = len(valid_rows)
        data["preview_error_count"] = len(errors)
        data["preview_errors"] = errors[:200]
        data["processed_rows"] = [_row_to_session(r) for r in valid_rows]
        data["not_imported"] = errors
        _set_import_session(request, data)
        dup_groups = contacts_import_svc.group_duplicates_in_file(valid_rows)
        dup_count = sum(len(v) for v in dup_groups.values())
        if dup_groups:
            duplicate_groups = [
                {
                    "mobile_phone": phone,
                    "occurrences": [
                        {
                            "row": r.get("_row"),
                            "name": r.get("name") or "—",
                            "has_whatsapp": r.get("has_whatsapp"),
                        }
                        for r in rows
                    ],
                    "count": len(rows),
                }
                for phone, rows in dup_groups.items()
            ]
            return render(
                request,
                "contacts/import_preview.html",
                {
                    "rows": [_row_from_session(r) for r in data.get("preview_rows", [])],
                    "errors": data.get("preview_errors", []),
                    "count_total": data.get("preview_count_total", 0),
                    "valid_count": data.get("preview_valid_count", 0),
                    "error_count": data.get("preview_error_count", 0),
                    "new_count": None,
                    "ignored_count": None,
                    "divergences_count": None,
                    "dup_count": dup_count,
                    "duplicate_groups": duplicate_groups,
                },
            )

        to_import, ignored, _, conflicts = contacts_import_svc.compare_with_database(valid_rows)
        conflicts_simple = [(dex, dat, diff) for (dex, dat, diff) in conflicts]
        data["preview_new_count"] = len(to_import)
        data["preview_ignored_count"] = len(ignored)
        data["preview_divergences_count"] = len(conflicts_simple)
        _set_import_session(request, data)
        return render(
            request,
            "contacts/import_preview.html",
            {
                "rows": [_row_from_session(r) for r in data.get("preview_rows", [])],
                "errors": data.get("preview_errors", []),
                "count_total": data.get("preview_count_total", 0),
                "valid_count": data.get("preview_valid_count", 0),
                "error_count": data.get("preview_error_count", 0),
                "new_count": data.get("preview_new_count", 0),
                "ignored_count": data.get("preview_ignored_count", 0),
                "divergences_count": data.get("preview_divergences_count", 0),
                "dup_count": 0,
                "duplicate_groups": [],
            },
        )

    processed_rows = [_row_from_session(r) for r in (data.get("processed_rows") or [])]
    not_imported = data.get("not_imported") or []
    if not processed_rows:
        messages.error(request, "Nenhuma linha válida encontrada para importar.")
        return redirect("contacts:contacts_import_preview")
    dup_groups = contacts_import_svc.group_duplicates_in_file(processed_rows)
    if dup_groups:
        data["duplicate_groups"] = {
            phone: [_row_to_session(r) for r in rows] for phone, rows in dup_groups.items()
        }
        data["import_wizard_passed_duplicates"] = True
        _set_import_session(request, data)
        return redirect("contacts:contacts_import_duplicates")

    to_import, ignored, not_imported_base, conflicts = contacts_import_svc.compare_with_database(
        processed_rows
    )
    data.pop("preview_rows", None)
    data.pop("preview_count_total", None)
    data.pop("preview_valid_count", None)
    data.pop("preview_error_count", None)
    data.pop("preview_errors", None)
    data.pop("processed_rows", None)
    data["to_import"] = [_row_to_session(r) for r in to_import]
    data["ignored_count"] = len(ignored)
    data["not_imported"] = not_imported + [
        {
            "mobile_phone": r.get("mobile_phone", ""),
            "reason": r.get("reason", "Inválido"),
            "row": "",
        }
        for r in not_imported_base
    ]
    data["conflicts"] = []
    for dex, dat, diff in conflicts:
        c = Contact.objects.filter(mobile_phone=dat["mobile_phone"]).first()
        dat_dict = contacts_import_svc.contact_to_dict(c) if c else dat
        data["conflicts"].append(
            (_row_to_session(dex), _row_to_session(dat_dict), [(d[0], d[1], d[2]) for d in diff])
        )
    data["import_wizard_passed_duplicates"] = False
    _set_import_session(request, data)
    return redirect("contacts:contacts_import_summary")


@login_required
@csrf_protect
def import_duplicates(request):
    """Step 3b: resolve duplicates inside the file."""
    data = _get_import_session(request)
    dup_groups = data.get("duplicate_groups")
    if not dup_groups:
        return redirect("contacts:contacts_import_preview")
    dup_groups_deserialized = {
        phone: [_row_from_session(r) for r in rows] for phone, rows in dup_groups.items()
    }
    if request.method == "POST":
        form = ResolveDuplicatesForm(
            request.POST,
            duplicate_groups=dup_groups_deserialized,
        )
        if form.is_valid():
            chosen = form.get_chosen_rows(dup_groups_deserialized)
            resolved_rows = []
            for phone, rows in dup_groups_deserialized.items():
                idx = next((i for (c, i) in chosen if c == phone), 0)
                resolved_rows.append(rows[idx])
            all_rows = data.get("processed_rows") or []
            dup_mobile_phones = set(dup_groups_deserialized.keys())
            for row in (_row_from_session(r) for r in all_rows):
                if row.get("mobile_phone") not in dup_mobile_phones:
                    resolved_rows.append(row)
            to_import, ignored, not_imported_base, conflicts = (
                contacts_import_svc.compare_with_database(resolved_rows)
            )
            data.pop("duplicate_groups", None)
            data.pop("processed_rows", None)
            data["to_import"] = [_row_to_session(r) for r in to_import]
            data["ignored_count"] = len(ignored)
            data["not_imported"] = data.get("not_imported", []) + [
                {
                    "mobile_phone": r.get("mobile_phone", ""),
                    "reason": r.get("reason", "Inválido"),
                    "row": "",
                }
                for r in not_imported_base
            ]
            data["conflicts"] = []
            for dex, dat, diff in conflicts:
                c = Contact.objects.filter(mobile_phone=dat["mobile_phone"]).first()
                dat_dict = contacts_import_svc.contact_to_dict(c) if c else dat
                data["conflicts"].append(
                    (
                        _row_to_session(dex),
                        _row_to_session(dat_dict),
                        [(d[0], d[1], d[2]) for d in diff],
                    )
                )
            data["import_wizard_passed_duplicates"] = True
            _set_import_session(request, data)
            return redirect("contacts:contacts_import_summary")
        return render(
            request,
            "contacts/import_duplicates.html",
            {"form": form, "duplicate_groups": dup_groups_deserialized},
        )
    form = ResolveDuplicatesForm(duplicate_groups=dup_groups_deserialized)
    return render(
        request,
        "contacts/import_duplicates.html",
        {"form": form, "duplicate_groups": dup_groups_deserialized},
    )


def _session_conflicts(data: dict) -> list[tuple[dict, dict, list]]:
    """Conflicts stored in the import session, with each row rebuilt from its serialized form."""
    return [
        (_row_from_session(dex), _row_from_session(dat), diff)
        for dex, dat, diff in data.get("conflicts", [])
    ]


def _parse_tag_ids(raw_values: list) -> list[int]:
    return [int(x) for x in raw_values if str(x).isdigit()]


def _render_import_summary(request, data: dict, form, bulk_tag_ids: list[int]):
    """Render step 4 (summary and conflict decisions) for the pending import."""
    to_import = [_row_from_session(r) for r in data.get("to_import", [])]
    conflicts = _session_conflicts(data)
    conflicts_with_fields = [
        (c[0], c[1], c[2], form[f"conflict_{i}"]) for i, c in enumerate(conflicts)
    ]
    return render(
        request,
        "contacts/import_summary.html",
        {
            "form": form,
            "to_import": to_import,
            "to_import_preview": to_import[:20],
            "to_import_rest_count": max(0, len(to_import) - 20),
            "conflicts": conflicts,
            "conflicts_with_fields": conflicts_with_fields,
            "not_imported": data.get("not_imported", []),
            "ignored_count": data.get("ignored_count", 0),
            "wizard_dup_skipped": not data.get("import_wizard_passed_duplicates"),
            "tags_all": list(ManualTag.objects.all().order_by("name")),
            "bulk_tag_ids": bulk_tag_ids,
        },
    )


@login_required
@require_GET
def import_summary(request):
    """Step 4: summary and conflict decisions. The form posts to `import_run`."""
    data = _get_import_session(request)
    if "to_import" not in data and "conflicts" not in data:
        return redirect("contacts:contacts_import")
    form = DecisionConflictsForm(conflicts=_session_conflicts(data))
    return _render_import_summary(request, data, form, bulk_tag_ids=[])


@login_required
@require_POST
@csrf_protect
def import_run(request):
    """
    Step 5: validate the decisions and run the import.

    POST only: it creates and updates contacts, so it needs CSRF protection and must never
    run from a link or a redirect.
    """
    data = _get_import_session(request)
    if not data.get("to_import") and not data.get("conflicts"):
        messages.info(request, "Nenhuma importação pendente.")
        return redirect("contacts:contacts_import")
    bulk_tag_ids = _parse_tag_ids(request.POST.getlist("bulk_tag_ids"))
    form = DecisionConflictsForm(request.POST, conflicts=_session_conflicts(data))
    if not form.is_valid():
        return _render_import_summary(request, data, form, bulk_tag_ids)
    to_import = [_row_from_session(r) for r in data.get("to_import", [])]
    conflicts_raw = data.get("conflicts", [])
    decisions = form.get_decisions(conflicts_raw)
    created = 0
    updated = 0
    failed = 0
    for index, row in enumerate(to_import):
        try:
            # Savepoint per row: a failure rolls back only that contact and its tags.
            with transaction.atomic():
                c = contacts_import_svc.create_contact(row)
                c.save()
                contacts_import_svc.apply_manual_tags(c, row.get("manual_tags") or [])
                contacts_import_svc.add_manual_tags_by_ids(c, bulk_tag_ids)
            created += 1
        except (IntegrityError, DataError, KeyError, ValueError):
            failed += 1
            logger.warning("Contact import: row %d was not saved", index, exc_info=True)
    for i, (dex, _dat, _diff) in enumerate(conflicts_raw):
        if i >= len(decisions) or decisions[i] != "update":
            continue
        contact = Contact.objects.filter(mobile_phone=dex.get("mobile_phone")).first()
        if contact:
            contacts_import_svc.update_contact(contact, _row_from_session(dex))
            contacts_import_svc.apply_manual_tags(
                contact, (_row_from_session(dex).get("manual_tags") or [])
            )
            contacts_import_svc.add_manual_tags_by_ids(contact, bulk_tag_ids)
            updated += 1
    ignored = data.get("ignored_count", 0)
    not_imported_count = len(data.get("not_imported", []))
    _clear_import_session(request)
    msg = (
        f"Importação concluída: {created} importados, {updated} atualizados, "
        f"{ignored} ignorados (já existentes), {not_imported_count} não importados."
    )
    if failed:
        messages.warning(request, f"{failed} contato(s) não puderam ser salvos e foram pulados.")
    messages.success(request, msg)
    return redirect("contacts:contact_list")
