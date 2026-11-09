"""Bulk archive (soft delete) of contacts and the cancellation of their pending sends."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import batched

from django.db import transaction
from django.db.models import Case, Count, TextField, Value, When
from django.utils import timezone

from apps.campaigns.models import CampaignSendJob
from apps.contacts.models import Contact

# Sends the worker has not picked up yet. LOCKED is deliberately left out: the worker may be
# processing it right now, so it is neither shown nor cancelled.
CANCELLABLE_SEND_STATUSES = (
    CampaignSendJob.Status.PENDING,
    CampaignSendJob.Status.WAITING_CHIP,
)

# Each id costs up to 3 SQL parameters in the cancel UPDATE (IN + CASE/WHEN); 300 keeps a
# single statement under SQLite's historical limit of 999 variables.
BATCH_SIZE = 300


@dataclass(frozen=True)
class ArchiveResult:
    archived: int
    cancelled_sends: int


@dataclass(frozen=True)
class CampaignPendingSends:
    campaign_id: int | None
    name: str
    count: int


def active_contacts_by_ids(contact_ids: Iterable[int]) -> list[Contact]:
    """Active contacts among `contact_ids`, in list order (name, phone). Unknown IDs are dropped."""
    found: list[Contact] = []
    for batch in batched(contact_ids, BATCH_SIZE):
        found.extend(
            Contact.objects.filter(pk__in=batch, deleted_at__isnull=True).only(
                "pk", "name", "mobile_phone"
            )
        )
    found.sort(key=lambda c: (c.name, c.mobile_phone))
    return found


def summarize_pending_sends(contact_ids: Iterable[int]) -> list[CampaignPendingSends]:
    """Cancellable sends of the given contacts, grouped by campaign (same rule as the archive)."""
    counts: dict[int | None, CampaignPendingSends] = {}
    for batch in batched(contact_ids, BATCH_SIZE):
        rows = (
            CampaignSendJob.objects.filter(
                contact_id__in=batch, status__in=CANCELLABLE_SEND_STATUSES
            )
            .order_by()
            .values("campaign_id", "campaign__name")
            .annotate(total=Count("pk"))
        )
        for row in rows:
            campaign_id = row["campaign_id"]
            previous = counts.get(campaign_id)
            counts[campaign_id] = CampaignPendingSends(
                campaign_id=campaign_id,
                name=row["campaign__name"] or f"Campanha #{campaign_id}",
                count=(previous.count if previous else 0) + row["total"],
            )
    return sorted(counts.values(), key=lambda item: item.name)


def archive_contacts(contacts: Iterable[Contact]) -> ArchiveResult:
    """
    Archive the active contacts and cancel their cancellable sends, atomically.

    Contacts already archived are skipped. Nothing is deleted: the send history is kept.
    """
    archived = 0
    cancelled_sends = 0
    now = timezone.now()
    with transaction.atomic():
        for batch in batched((contact.pk for contact in contacts), BATCH_SIZE):
            active = list(
                Contact.objects.filter(pk__in=batch, deleted_at__isnull=True).values_list(
                    "pk", "name", "mobile_phone"
                )
            )
            if not active:
                continue
            labels = {pk: name or phone for pk, name, phone in active}
            archived += Contact.objects.filter(pk__in=labels, deleted_at__isnull=True).update(
                deleted_at=now
            )
            cancelled_sends += _cancel_pending_sends(labels)
    return ArchiveResult(archived=archived, cancelled_sends=cancelled_sends)


def _cancel_pending_sends(labels: dict[int, str]) -> int:
    """Cancel the cancellable sends of the contacts (`labels`: pk -> name shown in the reason)."""
    reason = Case(
        *(
            When(contact_id=pk, then=Value(f"Contato excluído manualmente ({label})."))
            for pk, label in labels.items()
        ),
        output_field=TextField(),
    )
    return CampaignSendJob.objects.filter(
        contact_id__in=labels, status__in=CANCELLABLE_SEND_STATUSES
    ).update(
        status=CampaignSendJob.Status.CANCELLED,
        last_error=reason,
        schedule_defer_reason="",
        locked_at=None,
        locked_by="",
        lease_expires_at=None,
    )
