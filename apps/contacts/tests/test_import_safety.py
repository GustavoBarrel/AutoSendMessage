"""Import wizard: failures are reported (not swallowed) and uploads are bounded."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from apps.contacts.models import Contact


class ContactImportSafetyTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="importer2", password="senha-teste")
        self.client.force_login(user)

    def _set_pending_import(self, rows):
        session = self.client.session
        session["import_contacts"] = {"to_import": rows, "conflicts": []}
        session.save()

    def test_get_never_runs_the_import(self):
        self._set_pending_import([{"mobile_phone": "00933333333", "name": "Cris"}])

        response = self.client.get(reverse("contacts:contacts_import_run"))

        self.assertEqual(response.status_code, 405)
        self.assertEqual(Contact.objects.count(), 0)
        self.assertIn("import_contacts", self.client.session)

    def test_post_without_csrf_token_is_rejected(self):
        user = get_user_model().objects.get(username="importer2")
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(user)
        session = strict_client.session
        session["import_contacts"] = {
            "to_import": [{"mobile_phone": "00933333333", "name": "Cris"}],
            "conflicts": [],
        }
        session.save()

        response = strict_client.post(reverse("contacts:contacts_import_run"))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Contact.objects.count(), 0)

    def test_post_imports_the_pending_rows(self):
        self._set_pending_import([{"mobile_phone": "00933333333", "name": "Cris"}])

        response = self.client.post(reverse("contacts:contacts_import_run"))

        self.assertRedirects(response, reverse("contacts:contact_list"))
        self.assertTrue(Contact.objects.filter(mobile_phone="00933333333").exists())

    def test_failing_row_is_logged_counted_and_does_not_stop_the_import(self):
        good = {"mobile_phone": "00911111111", "name": "Ana"}
        duplicate_in_same_file = {"mobile_phone": "00911111111", "name": "Ana Repetida"}
        other = {"mobile_phone": "00922222222", "name": "Bia"}
        self._set_pending_import([good, duplicate_in_same_file, other])

        with self.assertLogs("apps.contacts.views", level="WARNING") as logs:
            response = self.client.post(reverse("contacts:contacts_import_run"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            set(Contact.objects.values_list("mobile_phone", flat=True)),
            {"00911111111", "00922222222"},
        )
        self.assertEqual(Contact.objects.get(mobile_phone="00911111111").name, "Ana")
        self.assertTrue(any("row 1" in line for line in logs.output))
        texts = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("1 contato(s) não puderam ser salvos" in t for t in texts))
        self.assertTrue(any("2 importados" in t for t in texts))

    def test_row_without_mobile_phone_is_reported(self):
        self._set_pending_import([{"name": "Sem celular"}])

        with self.assertLogs("apps.contacts.views", level="WARNING"):
            response = self.client.post(reverse("contacts:contacts_import_run"))

        self.assertEqual(Contact.objects.count(), 0)
        texts = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("1 contato(s) não puderam ser salvos" in t for t in texts))

    def test_upload_over_the_size_limit_is_rejected(self):
        upload = SimpleUploadedFile("contatos.csv", b"Nome;Celular\nAna;00911111111\n", "text/csv")

        with patch("apps.contacts.forms.MAX_IMPORT_FILE_BYTES", 5):
            response = self.client.post(reverse("contacts:contacts_import"), {"file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Arquivo muito grande")
        self.assertNotIn("import_contacts_file", self.client.session)

    def test_preview_over_the_row_limit_goes_back_to_upload(self):
        csv_body = "Nome;Celular\nAna;00911111111\nBia;00922222222\nCris;00933333333\n"
        upload = SimpleUploadedFile("contatos.csv", csv_body.encode(), "text/csv")
        self.client.post(reverse("contacts:contacts_import"), {"file": upload})
        self.client.post(
            reverse("contacts:contacts_import_map"), {"name": "1", "mobile_phone": "2"}
        )

        with patch("apps.contacts.services.contacts_import.MAX_IMPORT_ROWS", 2):
            response = self.client.get(reverse("contacts:contacts_import_preview"))

        self.assertRedirects(response, reverse("contacts:contacts_import"))
        texts = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("mais de 2 linhas" in t for t in texts))
