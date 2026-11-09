"""
Contact import service for Excel/CSV: reading, column mapping,
normalization, validation and comparison with the database.
"""

import csv
import io
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from apps.contacts.models import Contact

# Upload limits (the file is read fully in memory by the CSV reader and unzipped for .xlsx).
MAX_IMPORT_FILE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_IMPORT_ROWS = 10_000


class ImportLimitError(ValueError):
    """The spreadsheet has more data rows than MAX_IMPORT_ROWS."""

    def __init__(self) -> None:
        super().__init__(
            f"A planilha tem mais de {MAX_IMPORT_ROWS} linhas. Divida o arquivo em partes menores."
        )


CONTACT_FIELDS = [
    "code",
    "name",
    "gender",
    "postal_code",
    "street",
    "street_number",
    "neighborhood",
    "city",
    "state",
    "mobile_phone",
    "has_whatsapp",
    "birth_date",
    "email",
    "category",
    "subcategory",
]

# Labels shown to the user (column-mapping step and conflicts table). They stay in
# Portuguese because they are UI text.
CONTACT_FIELD_LABELS: dict[str, str] = {
    "code": "codigo",
    "name": "nome",
    "gender": "sexo",
    "postal_code": "cep",
    "street": "logradouro",
    "street_number": "numero",
    "neighborhood": "bairro",
    "city": "cidade",
    "state": "uf",
    "mobile_phone": "celular",
    "has_whatsapp": "whatsapp",
    "birth_date": "data_nascimento",
    "email": "email",
    "category": "categoria",
    "subcategory": "subcategoria",
}

HEADER_ALIASES: dict[str, list[str]] = {
    "code": ["codigo", "código", "id", "cod"],
    "name": ["nome", "nome completo", "contato", "nome do contato"],
    "gender": ["sexo", "genero", "gênero", "género"],
    "postal_code": ["cep"],
    "street": ["logradouro", "endereco", "endereço", "rua"],
    # "no" was removed because it conflicts with "nome"
    "street_number": ["numero", "número", "num", "nº"],
    "neighborhood": ["bairro"],
    "city": ["cidade"],
    "state": ["uf", "estado"],
    "mobile_phone": [
        "celular",
        "cel",
        "telefone",
        "fone",
        "tel",
        "whatsapp numero",
        "fone celular",
    ],
    "has_whatsapp": ["whatsapp", "tem whatsapp", "é whatsapp", "e whatsapp", "wa", "whats"],
    "birth_date": [
        "data de nascimento",
        "nascimento",
        "dt nascimento",
        "dtnasc",
        "data_nascimento",
        "data nascimento",
        "nasc",
    ],
    "email": ["email", "e-mail", "mail", "e mail"],
    "category": ["categoria", "categ"],
    "subcategory": ["subcategoria", "sub-categoria", "sub categoria"],
}


def _normalize_text(s: str) -> str:
    if not s or not isinstance(s, str):
        return ""
    s = str(s).strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s


def _normalize_mobile_phone(val: Any) -> str:
    if val is None:
        return ""
    s = re.sub(r"\D", "", str(val).strip())
    return s


def _parse_whatsapp(val: Any) -> bool:
    if val is None:
        return False
    s = _normalize_text(str(val))
    if s in ("sim", "s", "1", "true", "yes", "x"):
        return True
    return False


def _parse_data(val: Any) -> tuple[datetime | None, str | None]:
    """Return (date, error). The error is None when everything is fine."""
    if val is None or (isinstance(val, str) and not val.strip()):
        return None, None
    if hasattr(val, "date"):
        return val.date() if hasattr(val, "date") else val, None
    s = str(val).strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date(), None
        except ValueError:
            continue
    return None, f"valor inválido: {s}"


def _parse_manual_tags(val: Any) -> list[str]:
    """
    Convert a string into a list of tags.
    Accept common separators: ';' ',' '|' '/' and line breaks.
    """
    if val is None:
        return []
    s = str(val).strip()
    if not s:
        return []
    raw_parts = re.split(r"[;,|/\n\r]+", s)
    out: list[str] = []
    for p in raw_parts:
        t = " ".join(str(p).strip().split())
        if not t:
            continue
        out.append(t[:60])
    # de-duplicate while preserving order
    return list(dict.fromkeys(out))


def apply_manual_tags(contact: Contact, tag_names: list[str]) -> None:
    """Create/find tags and apply them to the contact (set)."""
    if not tag_names:
        contact.manual_tags.clear()
        return
    from apps.contacts.models import ManualTag

    tags = []
    for name in tag_names:
        nm = (name or "").strip()
        if not nm:
            continue
        tag = ManualTag.objects.filter(name__iexact=nm[:60]).first()
        if tag:
            tags.append(tag)
    contact.manual_tags.set(tags)


def add_manual_tags_by_ids(contact: Contact, tag_ids: list[int]) -> None:
    """Add existing tags to the contact (does not remove the current ones)."""
    if not tag_ids:
        return
    from apps.contacts.models import ManualTag

    tags = list(ManualTag.objects.filter(pk__in=tag_ids))
    if tags:
        contact.manual_tags.add(*tags)


def auto_map_columns(headers: dict[int, str]) -> dict[str, int]:
    """
    Given { column_index: header_value }, return { model_field: column_index }.
    Use header aliases for matching. The mobile phone may stay empty if not found.
    """
    result: dict[str, int] = {}
    headers_norm: dict[int, str] = {i: _normalize_text(h) for i, h in headers.items()}
    headers_tokens: dict[int, list[str]] = {i: _tokenize(h) for i, h in headers_norm.items()}
    for field, variations in HEADER_ALIASES.items():
        variations_norm = [_normalize_text(v) for v in variations]
        variations_tokens = [_tokenize(v) for v in variations_norm]
        best_idx = None
        best_score = -1
        for idx, cab in headers_norm.items():
            if not cab:
                continue
            score = _match_score(cab, headers_tokens[idx], variations_norm, variations_tokens)
            if score > best_score:
                best_score = score
                best_idx = idx
        if best_idx is not None and best_score > 0:
            result[field] = best_idx
    return result


def _tokenize(s: str) -> list[str]:
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return [t for t in s.strip().split() if t]


def _match_score(
    header: str,
    header_tokens: list[str],
    variations_norm: list[str],
    variations_tokens: list[list[str]],
) -> int:
    """
    Matching score to avoid false positives such as 'no' inside 'nome'.
    0 = no match.
    """
    # exact match
    if header in variations_norm:
        return 100

    # token match (whole alias)
    for vt in variations_tokens:
        if not vt:
            continue
        if len(vt) == 1:
            if vt[0] in header_tokens:
                return 80
        else:
            # token sequence
            for i in range(0, max(0, len(header_tokens) - len(vt) + 1)):
                if header_tokens[i : i + len(vt)] == vt:
                    return 75

    # prefix match, only for aliases with 4+ characters
    for v in variations_norm:
        if len(v) >= 4 and header.startswith(v):
            return 60

    return 0


def resolve_mapping_collisions(mapping: dict[str, int]) -> dict[str, int]:
    """
    Remove collisions when auto-mapping assigns the same column to several fields.
    Keep the field with the highest priority.
    """
    priority = [
        "mobile_phone",
        "has_whatsapp",
        "name",
        "email",
        "birth_date",
        "code",
        "postal_code",
        "street",
        "street_number",
        "neighborhood",
        "city",
        "state",
        "gender",
        "category",
        "subcategory",
    ]
    prio_index = {k: i for i, k in enumerate(priority)}
    by_col: dict[int, list[str]] = {}
    for field, col in mapping.items():
        by_col.setdefault(col, []).append(field)

    out = dict(mapping)
    for fields in by_col.values():
        if len(fields) <= 1:
            continue
        fields_sorted = sorted(fields, key=lambda c: prio_index.get(c, 999))
        for c in fields_sorted[1:]:
            out.pop(c, None)
    return out


def read_excel_header(file_path: str | Path) -> dict[int, str]:
    """Read the first row of the Excel file and return { index: value }."""
    wb = load_workbook(filename=file_path, read_only=True, data_only=True)
    ws = wb.active
    headers: dict[int, str] = {}
    for col_idx, cell in enumerate(ws[1], start=1):
        val = cell.value
        headers[col_idx] = str(val).strip() if val is not None else ""
    wb.close()
    return headers


def read_csv_header(file_path: str | Path) -> dict[int, str]:
    raw = Path(file_path).read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    dialect = _sniff_csv_dialect(sample)
    reader = csv.reader(io.StringIO(text), dialect)
    headers_row = next(reader, [])
    if len(headers_row) == 1 and isinstance(headers_row[0], str):
        # fallback: the sniffer failed and the whole line came as one cell (e.g. "Codigo;Nome;...")
        headers_row = _split_fallback_header(headers_row[0])
    return {i + 1: str(h).strip() for i, h in enumerate(headers_row)}


def _blank_row(row: dict[str, Any]) -> bool:
    """Return True if all values in the row (except metadata) are None or empty strings."""
    return all(
        v is None or (isinstance(v, str) and not v.strip()) for k, v in row.items() if k != "_row"
    )


def read_excel_rows(
    file_path: str | Path,
    mapping: dict[str, int],
    *,
    max_row: int | None = None,
) -> list[dict[str, Any]]:
    """
    Read every row (from row 2) and return a list with one dict per row.
    Key = model field, value = raw cell value.
    Completely blank rows are discarded.
    """
    wb = load_workbook(filename=file_path, read_only=True, data_only=True)
    ws = wb.active
    idx_to_col: dict[str, int] = {field: col for field, col in mapping.items()}
    rows: list[dict[str, Any]] = []
    for row_idx, sheet_row in enumerate(ws.iter_rows(min_row=2), start=2):
        if max_row and row_idx > max_row:
            break
        if row_idx > MAX_IMPORT_ROWS + 1:
            wb.close()
            raise ImportLimitError
        row: dict[str, Any] = {}
        for field, col in idx_to_col.items():
            try:
                cell = sheet_row[col - 1]
                row[field] = cell.value
            except IndexError:
                row[field] = None
        if _blank_row(row):
            continue
        row["_row"] = row_idx
        rows.append(row)
    wb.close()
    return rows


def read_csv_rows(
    file_path: str | Path,
    mapping: dict[str, int],
    *,
    max_row: int | None = None,
) -> list[dict[str, Any]]:
    raw = Path(file_path).read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    dialect = _sniff_csv_dialect(sample)
    reader = csv.reader(io.StringIO(text), dialect)
    header = next(reader, None)
    if header and len(header) == 1 and isinstance(header[0], str):
        # if the header came "glued" in one cell, the delimiter is wrong: re-parse using a heuristic
        delim = _guess_delimiter(header[0])
        dialect = csv.excel
        dialect.delimiter = delim
        reader = csv.reader(io.StringIO(text), dialect)
        next(reader, None)

    rows: list[dict[str, Any]] = []
    for row_idx, csv_row in enumerate(reader, start=2):
        if max_row and row_idx > max_row:
            break
        if row_idx > MAX_IMPORT_ROWS + 1:
            raise ImportLimitError
        row: dict[str, Any] = {}
        for field, col in mapping.items():
            idx = col - 1
            row[field] = csv_row[idx] if idx < len(csv_row) else None
        if _blank_row(row):
            continue
        row["_row"] = row_idx
        rows.append(row)
    return rows


def _guess_delimiter(header_line: str) -> str:
    delimiters = [";", ",", "\t"]
    counts = {c: header_line.count(c) for c in delimiters}
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] > 0 else ";"


def _sniff_csv_dialect(sample: str) -> csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        delim = _guess_delimiter(sample.splitlines()[0] if sample.splitlines() else "")
        dialect = csv.excel
        dialect.delimiter = delim
        return dialect


def _split_fallback_header(header_line: str) -> list[str]:
    delim = _guess_delimiter(header_line)
    return [h.strip() for h in header_line.split(delim)]


def read_file_header(file_path: str | Path) -> dict[int, str]:
    p = Path(file_path)
    suf = p.suffix.lower()
    if suf == ".csv":
        return read_csv_header(p)
    return read_excel_header(p)


def read_file_rows(
    file_path: str | Path,
    mapping: dict[str, int],
    *,
    max_row: int | None = None,
) -> list[dict[str, Any]]:
    p = Path(file_path)
    suf = p.suffix.lower()
    if suf == ".csv":
        return read_csv_rows(p, mapping, max_row=max_row)
    return read_excel_rows(p, mapping, max_row=max_row)


def parse_row(row: dict[str, Any], row_number: int) -> tuple[dict[str, Any], list[str]]:
    """
    Convert a raw row into data ready for Contact.
    Return (dict of normalized values, list of errors).
    """
    errors: list[str] = []
    out: dict[str, Any] = {}

    mobile_phone_raw = row.get("mobile_phone")
    mobile_phone = _normalize_mobile_phone(mobile_phone_raw)
    if not mobile_phone:
        errors.append("celular: obrigatório ausente ou inválido (use DDD+número, ex: 11999999999)")
    elif len(mobile_phone) != 11:
        errors.append("celular: deve conter 11 dígitos (DDD+número), ex: 11999999999")
    out["mobile_phone"] = mobile_phone

    name = (row.get("name") or "").strip()[:200]
    if not name:
        errors.append("nome: obrigatório ausente")
    out["name"] = name

    out["manual_tags"] = _parse_manual_tags(row.get("manual_filters"))

    for field in CONTACT_FIELDS:
        if field in ("mobile_phone", "name"):
            continue
        if field == "has_whatsapp":
            out["has_whatsapp"] = _parse_whatsapp(row.get("has_whatsapp"))
            continue
        if field == "birth_date":
            val = row.get("birth_date")
            data, _err = _parse_data(val)
            out["birth_date"] = data
            continue
        if field == "email":
            val = row.get("email")
            out["email"] = (str(val).strip() if val else "")[:254]
            continue
        val = row.get(field)
        if val is None:
            out[field] = ""
        else:
            out[field] = str(val).strip()[
                : getattr(Contact._meta.get_field(field), "max_length", 255)
            ]

    return out, errors


def contact_to_dict(c: Contact) -> dict[str, Any]:
    """Convert a Contact into a dict with the same keys used by the import."""
    return {
        "code": c.code or "",
        "name": c.name or "",
        "gender": c.gender or "",
        "postal_code": c.postal_code or "",
        "street": c.street or "",
        "street_number": c.street_number or "",
        "neighborhood": c.neighborhood or "",
        "city": c.city or "",
        "state": c.state or "",
        "mobile_phone": c.mobile_phone or "",
        "has_whatsapp": c.has_whatsapp,
        "birth_date": c.birth_date,
        "email": c.email or "",
        "category": c.category or "",
        "subcategory": c.subcategory or "",
    }


def compare_with_database(
    processed_rows: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[tuple[dict[str, Any], dict[str, Any], list[tuple[str, Any, Any]]]],
]:
    """
    Compare rows (with normalized mobile phone) with the database.
    Return:
      to_import: new contacts
      ignored: already exist with identical data (only counted, payload not needed)
      not_imported: validation errors (dict with mobile_phone/name and reason)
      conflicts: [(excel_data, current_data, [(field, current_value, new_value), ...]), ...]
    """
    to_import: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    not_imported: list[dict[str, Any]] = []
    conflicts: list[tuple[dict[str, Any], dict[str, Any], list[tuple[str, Any, Any]]]] = []

    existing_mobile_phones = {
        c.mobile_phone: contact_to_dict(c)
        for c in Contact.objects.filter(
            mobile_phone__in=[r["mobile_phone"] for r in processed_rows if r.get("mobile_phone")]
        ).filter(deleted_at__isnull=True)
    }

    for row in processed_rows:
        phone = row.get("mobile_phone")
        if not phone:
            not_imported.append(
                {"mobile_phone": "", "reason": "Celular vazio ou inválido.", "row": row}
            )
            continue

        current = existing_mobile_phones.get(phone)
        if not current:
            to_import.append(row)
            continue

        diff: list[tuple[str, Any, Any]] = []
        for k in CONTACT_FIELDS:
            v_new = row.get(k)
            v_current = current.get(k)
            if k == "birth_date":
                v_current = current.get(k)
                if v_current and hasattr(v_current, "isoformat"):
                    v_current = v_current.isoformat()
                if v_new and hasattr(v_new, "isoformat"):
                    v_new = v_new.isoformat()
            if v_new != v_current:
                diff.append((CONTACT_FIELD_LABELS[k], v_current, v_new))

        if not diff:
            ignored.append(row)
        else:
            conflicts.append((row, current, diff))

    return to_import, ignored, not_imported, conflicts


def group_duplicates_in_file(
    processed_rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group by mobile phone. Return only groups with more than one row."""
    by_phone: dict[str, list[dict[str, Any]]] = {}
    for row in processed_rows:
        phone = row.get("mobile_phone") or ""
        if phone not in by_phone:
            by_phone[phone] = []
        by_phone[phone].append(row)
    return {phone: rows for phone, rows in by_phone.items() if len(rows) > 1}


def create_contact(data: dict[str, Any]) -> Contact:
    """Create a Contact from the dict (values already normalized)."""
    return Contact(
        code=data.get("code", "") or "",
        name=data.get("name", "") or "",
        gender=data.get("gender", "") or "",
        postal_code=data.get("postal_code", "") or "",
        street=data.get("street", "") or "",
        street_number=data.get("street_number", "") or "",
        neighborhood=data.get("neighborhood", "") or "",
        city=data.get("city", "") or "",
        state=data.get("state", "") or "",
        mobile_phone=data["mobile_phone"],
        has_whatsapp=bool(data.get("has_whatsapp", True)),
        birth_date=data.get("birth_date"),
        email=data.get("email", "") or "",
        category=data.get("category", "") or "",
        subcategory=data.get("subcategory", "") or "",
    )


def update_contact(contact: Contact, data: dict[str, Any]) -> None:
    """Update a Contact with the data from the dict."""
    contact.code = data.get("code", "") or ""
    contact.name = data.get("name", "") or ""
    contact.gender = data.get("gender", "") or ""
    contact.postal_code = data.get("postal_code", "") or ""
    contact.street = data.get("street", "") or ""
    contact.street_number = data.get("street_number", "") or ""
    contact.neighborhood = data.get("neighborhood", "") or ""
    contact.city = data.get("city", "") or ""
    contact.state = data.get("state", "") or ""
    contact.has_whatsapp = bool(data.get("has_whatsapp", True))
    contact.birth_date = data.get("birth_date")
    contact.email = data.get("email", "") or ""
    contact.category = data.get("category", "") or ""
    contact.subcategory = data.get("subcategory", "") or ""
    contact.save()
