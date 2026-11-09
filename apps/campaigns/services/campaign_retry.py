"""Retry of failed jobs with a chosen chip, and heuristic analysis of the failure type."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from typing import TypedDict

from django.db import transaction
from django.utils import timezone

from apps.campaigns.models import Campaign, CampaignChip, CampaignSendJob
from apps.chips.models import Chip

# Retry scope (POST retry_scope)
RETRY_SCOPE_ALL = "all"
RETRY_SCOPE_SELECTED = "selected"
RETRY_SCOPE_CAUSE_CHIP = "cause_chip"
RETRY_SCOPE_CAUSE_MESSAGE = "cause_message"
RETRY_SCOPE_CHOICES = frozenset(
    {RETRY_SCOPE_ALL, RETRY_SCOPE_SELECTED, RETRY_SCOPE_CAUSE_CHIP, RETRY_SCOPE_CAUSE_MESSAGE}
)


class FailureBreakdown(TypedDict):
    chip_connection: int
    api_config: int
    number_not_exists: int
    message_content: int
    unknown: int


_FAILURE_LABELS_PT = {
    "chip_connection": "Provável problema no chip / conexão WhatsApp",
    "api_config": "Configuração da Evolution API / credenciais",
    "number_not_exists": "Número não existe / não está no WhatsApp",
    "message_content": "Conteúdo da mensagem / mídia",
    "unknown": "Outros ou não classificado",
}


def classify_failure_cause(error_text: str) -> str:
    """
    Classify an error text heuristically (does not replace manual diagnosis).

    Return one of the keys: chip_connection, api_config, message_content, unknown.
    """
    t = (error_text or "").lower()

    # Chip / Evolution instance
    if any(
        x in t
        for x in (
            "desconectad",
            "connectionstate",
            "connection_state",
            "reconecte o chip",
            "instância whatsapp",
            "instancia whatsapp",
            "erro 463",
            "timelock",
            "tctoken",
            "restrição de alcance",
            "restricao de alcance",
        )
    ):
        return "chip_connection"
    if re.search(r"\bclose\b", t) and ("evolution" in t or "whatsapp" in t or "instância" in t):
        return "chip_connection"

    # Server / credentials
    if any(
        x in t
        for x in (
            "evolution não configurada",
            "evolution nao configurada",
            "evolution_url",
            "api_key",
            "401",
            "403",
            "timeout",
            "timed out",
            "connection refused",
            "name or service not known",
        )
    ):
        return "api_config"

    # Number does not exist / not on WhatsApp (permanent error)
    if (
        re.search(r'"exists"\s*:\s*false', t)
        or "is not on whatsapp" in t
        or "not registered" in t
        or "recipient not found" in t
        or "invalid jid" in t
        or "invalid wid" in t
    ):
        return "number_not_exists"

    # Invalid message / number (generic) / media
    if any(
        x in t
        for x in (
            "texto da mensagem vazio",
            "mensagem vazia",
            "url da imagem",
            "url do vídeo",
            "url do video",
            "tipo de mensagem não suportado",
            "mídia",
            "número inválido",
            "numero invalido",
            "não existe",
            "nao existe",
            "phone number",
            "número não",
            "numero nao",
        )
    ):
        return "message_content"

    return "unknown"


def failure_breakdown_for_campaign(campaign_id: int) -> FailureBreakdown:
    """Count per category among FAILED jobs (uses last_error)."""
    keys = ("chip_connection", "api_config", "number_not_exists", "message_content", "unknown")
    errors = CampaignSendJob.objects.filter(
        campaign_id=campaign_id,
        status=CampaignSendJob.Status.FAILED,
    ).values_list("last_error", flat=True)
    ctr: Counter[str] = Counter(classify_failure_cause(e or "") for e in errors)
    return FailureBreakdown(**{k: ctr.get(k, 0) for k in keys})


def failure_breakdown_labels() -> dict[str, str]:
    """Portuguese labels for display."""
    return dict(_FAILURE_LABELS_PT)


def chip_eligible_for_campaign_retry(chip: Chip, campaign: Campaign) -> bool:
    """Same workspace, active chip and not reserved for another active campaign."""
    if chip.workspace_id != campaign.workspace_id or not chip.is_active:
        return False
    blocked = CampaignChip.objects.filter(
        chip_id=chip.pk,
        is_active=True,
        campaign_status=Campaign.Status.ACTIVE,
    ).exclude(campaign_id=campaign.pk)
    return not blocked.exists()


def eligible_chips_for_campaign_retry(campaign: Campaign) -> list[Chip]:
    """Chips that can take over retries of this campaign."""
    qs = Chip.objects.filter(workspace_id=campaign.workspace_id, is_active=True).order_by("name")
    return [c for c in qs if chip_eligible_for_campaign_retry(c, campaign)]


def _dedupe_int_ids(raw: Sequence[object] | None) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    if not raw:
        return out
    for x in raw:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _failed_job_pks_matching_cause(campaign_id: int, cause: str) -> list[int]:
    pks: list[int] = []
    qs = CampaignSendJob.objects.filter(
        campaign_id=campaign_id,
        status=CampaignSendJob.Status.FAILED,
    ).values_list("pk", "last_error")
    for pk, err in qs.iterator(chunk_size=500):
        if classify_failure_cause(err or "") == cause:
            pks.append(pk)
    return pks


@transaction.atomic
def retry_failed_jobs_with_chips(
    campaign_id: int,
    chip_ids: Sequence[int],
    *,
    scope: str = RETRY_SCOPE_ALL,
    selected_job_ids: Sequence[int] | None = None,
) -> tuple[int, list[str]]:
    """
    Put FAILED jobs back to PENDING, distributing them round-robin across the given chips.

    ``scope``: all failures; only pks in ``selected_job_ids``; only those classified as
    chip/connection; or only as content/number (heuristic in ``classify_failure_cause``).

    Ensure a CampaignChip for each chosen chip. Each chip keeps following its own
    limits in the worker (interval, hours, messages/hour, etc.).
    """
    if scope not in RETRY_SCOPE_CHOICES:
        scope = RETRY_SCOPE_ALL

    campaign = Campaign.objects.select_for_update().get(pk=campaign_id)

    seen: set[int] = set()
    ordered_ids: list[int] = []
    for raw in chip_ids:
        cid = int(raw)
        if cid in seen:
            continue
        seen.add(cid)
        ordered_ids.append(cid)

    if not ordered_ids:
        raise ValueError("Selecione ao menos um chip para o reenvio.")

    chips: list[Chip] = []
    for cid in ordered_ids:
        chip = Chip.objects.select_for_update().get(pk=cid)
        if not chip_eligible_for_campaign_retry(chip, campaign):
            raise ValueError(
                f"Chip «{chip.name}» não está disponível "
                "(perfil diferente, inativo ou em uso em outra campanha ativa)."
            )
        chips.append(chip)

    for chip in chips:
        CampaignChip.objects.update_or_create(
            campaign_id=campaign_id,
            chip_id=chip.pk,
            defaults={
                "is_active": True,
                "campaign_status": campaign.status,
            },
        )

    base = CampaignSendJob.objects.filter(
        campaign_id=campaign_id,
        status=CampaignSendJob.Status.FAILED,
    )
    if scope == RETRY_SCOPE_SELECTED:
        ids = _dedupe_int_ids(selected_job_ids)
        if not ids:
            raise ValueError(
                "Para reenviar só mensagens escolhidas, marque ao menos uma falha na tabela "
                "«Controle de mensagens» (use o filtro «Falha») ou altere o escopo acima."
            )
        jobs_qs = base.filter(pk__in=ids)
    elif scope == RETRY_SCOPE_CAUSE_CHIP:
        ids = _failed_job_pks_matching_cause(campaign_id, "chip_connection")
        if not ids:
            raise ValueError(
                "Nenhuma falha desta campanha foi classificada como chip/conexão. "
                "Confira a análise automática de falhas ou marque manualmente na tabela."
            )
        jobs_qs = base.filter(pk__in=ids)
    elif scope == RETRY_SCOPE_CAUSE_MESSAGE:
        ids = _failed_job_pks_matching_cause(
            campaign_id, "message_content"
        ) + _failed_job_pks_matching_cause(campaign_id, "number_not_exists")
        if not ids:
            raise ValueError(
                "Nenhuma falha foi classificada como conteúdo/número (heurística). "
                "Use «Todas» ou marque na tabela."
            )
        jobs_qs = base.filter(pk__in=ids)
    else:
        jobs_qs = base

    jobs = list(jobs_qs.select_for_update().order_by("pk"))
    n_chips = len(chips)
    now = timezone.now()
    chip_names = [(c.name or "")[:80] for c in chips]

    for i, job in enumerate(jobs):
        c = chips[i % n_chips]
        job.status = CampaignSendJob.Status.PENDING
        job.chip_id = c.pk
        job.chip_name_snapshot = chip_names[i % n_chips]
        job.attempts = 0
        job.last_error = ""
        job.scheduled_at = now
        job.schedule_defer_reason = ""
        job.locked_at = None
        job.locked_by = ""
        job.lease_expires_at = None
        job.updated_at = now

    if jobs:
        CampaignSendJob.objects.bulk_update(
            jobs,
            [
                "status",
                "chip_id",
                "chip_name_snapshot",
                "attempts",
                "last_error",
                "scheduled_at",
                "schedule_defer_reason",
                "locked_at",
                "locked_by",
                "lease_expires_at",
                "updated_at",
            ],
            batch_size=500,
        )

    return len(jobs), [c.name for c in chips]


def retry_failed_jobs_with_chip(campaign_id: int, chip_id: int) -> int:
    """Compatibility: a single chip, all failures."""
    n, _names = retry_failed_jobs_with_chips(
        campaign_id, [chip_id], scope=RETRY_SCOPE_ALL, selected_job_ids=None
    )
    return n
