"""Bulk archive of contacts (confirmation, execution), the service and individual archive."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Workspace
from apps.campaigns.models import Campaign, CampaignSendJob
from apps.contacts.models import Contact
from apps.contacts.services import contact_archive
from apps.contacts.services.contact_archive import (
    archive_contacts,
    summarize_pending_sends,
)

Status = CampaignSendJob.Status


def _phone(n: int) -> str:
    return f"{n:011d}"


class BulkArchiveBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        workspace = Workspace.objects.create(name="Perfil", slug="perfil")
        cls.campaign = Campaign.objects.create(
            workspace=workspace, name="Camp A", status=Campaign.Status.ACTIVE
        )
        cls.other_campaign = Campaign.objects.create(
            workspace=workspace, name="Camp B", status=Campaign.Status.PAUSED
        )
        cls.ana = Contact.objects.create(name="Ana", mobile_phone=_phone(1))
        cls.bia = Contact.objects.create(name="Bia", mobile_phone=_phone(2))
        cls.caio = Contact.objects.create(name="", mobile_phone=_phone(3))

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("u", password="senha-teste"))

    def _job(self, contact, status, campaign=None):
        campaign = campaign or self.campaign
        return CampaignSendJob.objects.create(
            campaign=campaign,
            contact=contact,
            status=status,
            idempotency_key=f"c{campaign.pk}-ct{contact.pk}",
        )

    def _messages(self, response) -> list[tuple[str, str]]:
        return [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]


class BulkArchiveConfirmationTests(BulkArchiveBase):
    url = reverse("contacts:contact_bulk_archive")

    def _post(self, ids, **extra):
        return self.client.post(self.url, {"contact_ids": ids, **extra})

    def test_requires_login(self):
        self.client.logout()
        response = self._post(f"{self.ana.pk}")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_renders_confirmation_with_summary(self):
        self._job(self.ana, Status.PENDING)
        self._job(self.bia, Status.WAITING_CHIP)
        self._job(self.bia, Status.SENT, campaign=self.other_campaign)
        response = self._post(f"{self.ana.pk},{self.bia.pk}", next="/contacts/?city=X")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "contacts/bulk_archive_confirm.html")
        ctx = response.context
        self.assertEqual(ctx["total"], 2)
        self.assertEqual(ctx["ignored"], 0)
        # The phone tells namesakes apart in the preview.
        self.assertEqual(
            ctx["preview"], [f"Ana — {self.ana.mobile_phone}", f"Bia — {self.bia.mobile_phone}"]
        )
        self.assertEqual(ctx["remaining"], 0)
        self.assertFalse(ctx["covers_all_active"])
        self.assertEqual(ctx["total_pending"], 2)
        self.assertEqual([(c.name, c.count) for c in ctx["campaigns_affected"]], [("Camp A", 2)])
        self.assertEqual(ctx["contact_ids_csv"], f"{self.ana.pk},{self.bia.pk}")
        self.assertEqual(ctx["next_url"], "/contacts/?city=X")

    def test_preview_is_limited_to_ten_and_falls_back_to_phone(self):
        ids = [self.caio.pk]
        for n in range(20, 32):
            ids.append(Contact.objects.create(name=f"Z{n}", mobile_phone=_phone(n)).pk)
        response = self._post(",".join(map(str, ids)))
        self.assertEqual(len(response.context["preview"]), 10)
        self.assertEqual(response.context["preview"][0], self.caio.mobile_phone)
        self.assertEqual(response.context["total"], 13)
        self.assertEqual(response.context["remaining"], 3)
        self.assertContains(response, "e mais 3 contatos.")

    def test_flags_selection_covering_every_active_contact(self):
        response = self._post(f"{self.ana.pk},{self.bia.pk},{self.caio.pk}")
        self.assertTrue(response.context["covers_all_active"])

    def test_archived_and_unknown_ids_are_ignored_and_counted(self):
        archived = Contact.objects.create(
            name="Velho", mobile_phone=_phone(9), deleted_at=timezone.now()
        )
        response = self._post(f"{self.ana.pk},{archived.pk},987654")
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(response.context["ignored"], 2)
        self.assertEqual(response.context["contact_ids_csv"], str(self.ana.pk))

    def test_invalid_ids_are_rejected_with_error(self):
        for raw in ["abc", "1,,x", "-3", "0", "1.5", "١٢", "9" * 5000]:
            with self.subTest(raw=raw):
                response = self._post(raw)
                self.assertRedirects(response, reverse("contacts:contact_list"))
                self.assertEqual(self._messages(response)[-1][0], "error")

    def test_row_checkboxes_are_merged_into_the_csv_without_duplicates(self):
        response = self.client.post(
            self.url,
            {"contact_ids": f"{self.ana.pk}", "contact_ids_row": [self.ana.pk, self.bia.pk]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 2)
        self.assertEqual(response.context["contact_ids_csv"], f"{self.ana.pk},{self.bia.pk}")

    def test_row_checkboxes_alone_work_without_javascript(self):
        response = self.client.post(self.url, {"contact_ids_row": [self.bia.pk, self.caio.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 2)

    def test_row_checkboxes_get_the_same_validation_and_limit(self):
        response = self.client.post(self.url, {"contact_ids_row": [self.ana.pk, "abc"]})
        self.assertEqual(self._messages(response)[-1][0], "error")
        response = self.client.post(self.url, {"contact_ids_row": [self.ana.pk, self.ana.pk]})
        self.assertIn("repetidos", self._messages(response)[-1][1])
        with patch("apps.contacts.views.MAX_BULK_SELECTION", 2):
            response = self.client.post(
                self.url,
                {"contact_ids": f"{self.ana.pk}", "contact_ids_row": [self.bia.pk, self.caio.pk]},
            )
        self.assertEqual(self._messages(response)[-1][0], "error")

    def test_repeated_ids_are_rejected(self):
        response = self._post(f"{self.ana.pk},{self.ana.pk}")
        self.assertRedirects(response, reverse("contacts:contact_list"))
        self.assertIn("repetidos", self._messages(response)[-1][1])

    def test_more_than_the_limit_is_rejected(self):
        with patch("apps.contacts.views.MAX_BULK_SELECTION", 1):
            response = self._post(f"{self.ana.pk},{self.bia.pk}")
        self.assertRedirects(response, reverse("contacts:contact_list"))
        self.assertEqual(self._messages(response)[-1][0], "error")

    def test_empty_selection_or_only_archived_warns_and_redirects(self):
        archived = Contact.objects.create(
            name="Velho", mobile_phone=_phone(9), deleted_at=timezone.now()
        )
        for raw in ["", f"{archived.pk}", "987654"]:
            with self.subTest(raw=raw):
                response = self._post(raw, next="/contacts/?city=X")
                self.assertRedirects(response, "/contacts/?city=X")
                self.assertEqual(self._messages(response)[-1][0], "warning")

    def test_external_next_is_replaced_by_the_list(self):
        for evil in ["http://evil.com", "//evil.com", "/\\evil.com", "https://evil.com/contacts/"]:
            with self.subTest(next=evil):
                response = self._post(f"{self.ana.pk}", next=evil)
                self.assertEqual(response.context["next_url"], reverse("contacts:contact_list"))

    def test_nothing_is_archived_by_the_confirmation_page(self):
        self._post(f"{self.ana.pk}")
        self.ana.refresh_from_db()
        self.assertIsNone(self.ana.deleted_at)


class BulkArchiveExecutionTests(BulkArchiveBase):
    url = reverse("contacts:contact_bulk_archive_confirm")

    def _post(self, ids, **extra):
        return self.client.post(self.url, {"contact_ids": ids, **extra})

    def test_requires_login(self):
        self.client.logout()
        response = self._post(f"{self.ana.pk}")
        self.assertIn("login", response["Location"])
        self.ana.refresh_from_db()
        self.assertIsNone(self.ana.deleted_at)

    def test_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(get_user_model().objects.get(username="u"))
        response = client.post(self.url, {"contact_ids": str(self.ana.pk)})
        self.assertEqual(response.status_code, 403)

    def test_archives_and_cancels_only_pending_and_waiting_chip(self):
        pending = self._job(self.ana, Status.PENDING)
        waiting = self._job(self.bia, Status.WAITING_CHIP)
        locked = self._job(self.ana, Status.LOCKED, campaign=self.other_campaign)
        sent = self._job(self.bia, Status.SENT, campaign=self.other_campaign)
        failed = self._job(self.caio, Status.FAILED)
        response = self._post(f"{self.ana.pk},{self.bia.pk}")

        self.assertRedirects(response, reverse("contacts:contact_list"))
        self.ana.refresh_from_db()
        self.bia.refresh_from_db()
        self.caio.refresh_from_db()
        self.assertIsNotNone(self.ana.deleted_at)
        self.assertIsNotNone(self.bia.deleted_at)
        self.assertIsNone(self.caio.deleted_at)
        for job in (pending, waiting, locked, sent, failed):
            job.refresh_from_db()
        self.assertEqual(pending.status, Status.CANCELLED)
        self.assertEqual(pending.last_error, "Contato excluído manualmente (Ana).")
        self.assertEqual(waiting.status, Status.CANCELLED)
        self.assertEqual(locked.status, Status.LOCKED)
        self.assertEqual(sent.status, Status.SENT)
        self.assertEqual(failed.status, Status.FAILED)

    def test_success_message_mentions_cancelled_sends_only_when_any(self):
        self._job(self.ana, Status.PENDING)
        response = self._post(f"{self.ana.pk},{self.bia.pk}")
        self.assertEqual(
            self._messages(response),
            [("success", "2 contato(s) arquivado(s). 1 envio(s) pendente(s) cancelado(s).")],
        )

        response = self._post(f"{self.caio.pk}")
        self.assertEqual(self._messages(response)[-1], ("success", "1 contato(s) arquivado(s)."))

    def test_sets_one_shot_session_flag_and_redirects_to_safe_next(self):
        response = self._post(f"{self.ana.pk}", next="/contacts/?city=X")
        # Do not fetch the target: that GET would consume the one-shot flag.
        self.assertRedirects(response, "/contacts/?city=X", fetch_redirect_response=False)
        self.assertTrue(self.client.session["contacts_bulk_clear_selection"])
        listing = self.client.get(reverse("contacts:contact_list"))
        self.assertTrue(listing.context["clear_bulk_selection"])
        self.assertNotIn("contacts_bulk_clear_selection", self.client.session)

    def test_archives_from_row_checkboxes_alone_and_mixed_with_csv(self):
        self._job(self.ana, Status.PENDING)
        response = self.client.post(self.url, {"contact_ids_row": [self.ana.pk, self.bia.pk]})
        self.assertEqual(
            self._messages(response)[-1],
            ("success", "2 contato(s) arquivado(s). 1 envio(s) pendente(s) cancelado(s)."),
        )
        response = self.client.post(
            self.url,
            {"contact_ids": f"{self.bia.pk}", "contact_ids_row": [self.bia.pk, self.caio.pk]},
        )
        # Bia was already archived above; only Caio is new.
        self.assertEqual(self._messages(response)[-1], ("success", "1 contato(s) arquivado(s)."))

    def test_external_next_redirects_to_the_list(self):
        for evil in ["http://evil.com", "//evil.com", "/\\evil.com"]:
            with self.subTest(next=evil):
                response = self._post(f"{self.bia.pk}", next=evil)
                self.assertEqual(response["Location"], reverse("contacts:contact_list"))

    def test_invalid_selection_changes_nothing(self):
        for raw in ["abc", f"{self.ana.pk},{self.ana.pk}", ""]:
            with self.subTest(raw=raw):
                response = self._post(raw)
                self.assertEqual(response.status_code, 302)
        self.assertEqual(Contact.objects.filter(deleted_at__isnull=True).count(), 3)
        self.assertNotIn("contacts_bulk_clear_selection", self.client.session)

    def test_already_archived_contacts_are_not_counted(self):
        self.ana.deleted_at = timezone.now()
        self.ana.save()
        response = self._post(f"{self.ana.pk},{self.bia.pk}")
        self.assertEqual(self._messages(response)[-1], ("success", "1 contato(s) arquivado(s)."))

    def test_history_is_kept(self):
        self._job(self.ana, Status.SENT)
        self._post(f"{self.ana.pk}")
        self.assertEqual(CampaignSendJob.objects.filter(contact=self.ana).count(), 1)
        self.assertTrue(Contact.objects.filter(pk=self.ana.pk).exists())


class ArchiveServiceTests(BulkArchiveBase):
    def test_batching_covers_selections_larger_than_a_batch(self):
        self.assertTrue(contact_archive.BATCH_SIZE > 0)
        original = contact_archive.BATCH_SIZE
        contact_archive.BATCH_SIZE = 2
        self.addCleanup(setattr, contact_archive, "BATCH_SIZE", original)
        contacts = [self.ana, self.bia, self.caio]
        for contact in contacts:
            self._job(contact, Status.PENDING)

        summary = summarize_pending_sends(c.pk for c in contacts)
        self.assertEqual([(s.name, s.count) for s in summary], [("Camp A", 3)])

        result = archive_contacts(contacts)
        self.assertEqual((result.archived, result.cancelled_sends), (3, 3))
        self.assertEqual(Contact.objects.filter(deleted_at__isnull=True).count(), 0)

    def test_archiving_twice_is_a_no_op(self):
        self._job(self.ana, Status.PENDING)
        archive_contacts([self.ana])
        result = archive_contacts([self.ana])
        self.assertEqual((result.archived, result.cancelled_sends), (0, 0))

    def test_cancel_clears_lock_fields(self):
        job = self._job(self.ana, Status.WAITING_CHIP)
        CampaignSendJob.objects.filter(pk=job.pk).update(
            locked_at=timezone.now(), locked_by="w1", schedule_defer_reason="chip_offline"
        )
        archive_contacts([self.ana])
        job.refresh_from_db()
        self.assertEqual(job.status, Status.CANCELLED)
        self.assertIsNone(job.locked_at)
        self.assertEqual(job.locked_by, "")
        self.assertEqual(job.schedule_defer_reason, "")

    def test_summary_ignores_locked_and_finished_statuses(self):
        self._job(self.ana, Status.LOCKED)
        self._job(self.bia, Status.SENT)
        self.assertEqual(summarize_pending_sends([self.ana.pk, self.bia.pk]), [])

    def test_summary_counts_paused_campaigns_too(self):
        self._job(self.ana, Status.PENDING, campaign=self.other_campaign)
        summary = summarize_pending_sends([self.ana.pk])
        self.assertEqual([(s.name, s.count) for s in summary], [("Camp B", 1)])


class IndividualArchiveTests(BulkArchiveBase):
    def test_confirmation_page_counts_match_what_will_be_cancelled(self):
        self._job(self.ana, Status.PENDING)
        self._job(self.ana, Status.WAITING_CHIP, campaign=self.other_campaign)
        # LOCKED-only campaign must not be listed (it will not be cancelled).
        locked_campaign = Campaign.objects.create(
            workspace=self.campaign.workspace, name="Camp C", status=Campaign.Status.ACTIVE
        )
        self._job(self.ana, Status.LOCKED, campaign=locked_campaign)

        response = self.client.get(reverse("contacts:contact_confirm_delete", args=[self.ana.pk]))
        self.assertEqual(response.context["total_pending"], 2)
        self.assertEqual(
            [(c.name, c.count) for c in response.context["campaigns_affected"]],
            [("Camp A", 1), ("Camp B", 1)],
        )
        self.assertNotContains(response, "Camp C")

    def test_delete_archives_and_cancels_with_message(self):
        pending = self._job(self.ana, Status.PENDING)
        locked = self._job(self.ana, Status.LOCKED, campaign=self.other_campaign)
        response = self.client.post(reverse("contacts:contact_delete", args=[self.ana.pk]))
        self.assertRedirects(response, reverse("contacts:contact_list"))
        self.ana.refresh_from_db()
        pending.refresh_from_db()
        locked.refresh_from_db()
        self.assertIsNotNone(self.ana.deleted_at)
        self.assertEqual(pending.status, Status.CANCELLED)
        self.assertEqual(pending.last_error, "Contato excluído manualmente (Ana).")
        self.assertEqual(locked.status, Status.LOCKED)
        self.assertEqual(
            self._messages(response)[-1],
            (
                "success",
                'Contato "Ana" arquivado. 1 envio(s) pendente(s) cancelado(s) nas campanhas.',
            ),
        )

    def test_delete_without_pending_sends(self):
        response = self.client.post(reverse("contacts:contact_delete", args=[self.bia.pk]))
        self.assertEqual(self._messages(response)[-1], ("success", 'Contato "Bia" arquivado.'))

    def test_archived_one_by_one_leaves_the_bulk_selection_once(self):
        self.client.post(reverse("contacts:contact_delete", args=[self.ana.pk]))
        self.client.post(reverse("contacts:contact_delete", args=[self.bia.pk]))
        self.client.post(reverse("contacts:contact_delete", args=[self.bia.pk]))  # already archived
        first = self.client.get(reverse("contacts:contact_list"))
        second = self.client.get(reverse("contacts:contact_list"))
        self.assertEqual(first.context["forget_bulk_ids"], [self.ana.pk, self.bia.pk])
        self.assertEqual(second.context["forget_bulk_ids"], [])

    def test_delete_already_archived_contact(self):
        self.ana.deleted_at = timezone.now()
        self.ana.save()
        response = self.client.post(reverse("contacts:contact_delete", args=[self.ana.pk]))
        self.assertEqual(self._messages(response)[-1][0], "info")
