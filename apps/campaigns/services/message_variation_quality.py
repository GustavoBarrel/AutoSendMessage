"""Validation of diversity among message variants."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import combinations

SIMILARITY_THRESHOLD = 0.85

_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")
_SPACES_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class MessageVariationIssue:
    first_order: int
    second_order: int
    similarity: float

    @property
    def percent(self) -> int:
        return round(self.similarity * 100)


def normalize_message_text(text: str) -> str:
    """Normalize text to compare content, ignoring accents and punctuation."""
    value = unicodedata.normalize("NFKD", (text or "").lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = _PLACEHOLDER_RE.sub(" campo ", value)
    value = _NON_ALNUM_RE.sub(" ", value)
    return _SPACES_RE.sub(" ", value).strip()


def message_similarity(first: str, second: str) -> float:
    first_normalized = normalize_message_text(first)
    second_normalized = normalize_message_text(second)
    if not first_normalized or not second_normalized:
        return 0.0
    return SequenceMatcher(None, first_normalized, second_normalized).ratio()


def find_message_variation_issues(
    messages: list[str],
    *,
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[MessageVariationIssue]:
    """Return pairs of variants with similarity above the threshold."""
    issues: list[MessageVariationIssue] = []
    ordered_messages = list(enumerate(messages, start=1))
    for (first_order, first), (second_order, second) in combinations(ordered_messages, 2):
        similarity = message_similarity(first, second)
        if similarity >= threshold:
            issues.append(
                MessageVariationIssue(
                    first_order=first_order,
                    second_order=second_order,
                    similarity=similarity,
                )
            )
    return issues


def format_message_variation_feedback(issues: list[MessageVariationIssue]) -> str:
    pairs = ", ".join(
        f"Variação {issue.first_order} e {issue.second_order} ({issue.percent}% parecidas)"
        for issue in issues
    )
    return (
        "As 3 variações foram preenchidas, mas alguns textos estão muito próximos entre si: "
        f"{pairs}. Para reduzir risco de bloqueio do chip, reescreva as variações com estruturas, "
        "frases e chamadas diferentes. Você pode manter a mesma ideia da campanha, mas evite mudar "
        "apenas pontuação, saudação ou pequenos trechos."
    )
