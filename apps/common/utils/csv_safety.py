"""Protection against CSV (formula) injection in exported spreadsheets."""

from __future__ import annotations

# Characters that make Excel/LibreOffice read a cell as a formula (OWASP "CSV Injection").
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def neutralize_csv_cell(value: object) -> object:
    """Prefix text that a spreadsheet would run as a formula with an apostrophe.

    Only strings are changed; numbers and ``None`` pass through untouched.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value
