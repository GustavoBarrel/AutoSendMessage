"""End-to-end test of the contact import wizard (upload to execution)."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.contacts.models import Contact


class ContactImportFlowTests(TestCase):
    """Walk through the import wizard end to end (upload to execution)."""

    CSV_CONTENT = (
        "Nome;Celular;Cidade\nMaria Silva;00999999999;Curitiba\nJoao Souza;00988888888;São Paulo\n"
    )

    def setUp(self):
        user = get_user_model().objects.create_user(username="importer", password="senha-teste")
        self.client.force_login(user)
        self.existing = Contact.objects.create(
            name="Maria Silva", mobile_phone="00999999999", city="São Paulo"
        )

    def test_full_import_creates_new_contacts_and_updates_conflicts(self):
        upload = SimpleUploadedFile("contatos.csv", self.CSV_CONTENT.encode(), "text/csv")
        response = self.client.post(reverse("contacts:contacts_import"), {"file": upload})
        self.assertRedirects(response, reverse("contacts:contacts_import_map"))

        response = self.client.get(reverse("contacts:contacts_import_map"))
        self.assertContains(response, ">celular<")
        response = self.client.post(
            reverse("contacts:contacts_import_map"),
            {"name": "1", "mobile_phone": "2", "city": "3"},
        )
        self.assertRedirects(response, reverse("contacts:contacts_import_preview"))

        self.assertEqual(
            self.client.get(reverse("contacts:contacts_import_preview")).status_code, 200
        )
        response = self.client.post(reverse("contacts:contacts_import_preview"))
        self.assertRedirects(response, reverse("contacts:contacts_import_summary"))

        response = self.client.get(reverse("contacts:contacts_import_summary"))
        self.assertContains(response, "<td>cidade</td>")  # label of the conflicting field
        self.assertContains(response, f'action="{reverse("contacts:contacts_import_run")}"')
        response = self.client.post(
            reverse("contacts:contacts_import_run"), {"conflict_0": "update"}
        )
        self.assertRedirects(response, reverse("contacts:contact_list"))

        self.assertTrue(
            Contact.objects.filter(mobile_phone="00988888888", name="Joao Souza").exists()
        )
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.city, "Curitiba")

    def test_missing_conflict_decision_re_renders_the_summary_without_importing(self):
        self.client.post(
            reverse("contacts:contacts_import"),
            {"file": SimpleUploadedFile("contatos.csv", self.CSV_CONTENT.encode(), "text/csv")},
        )
        self.client.post(
            reverse("contacts:contacts_import_map"),
            {"name": "1", "mobile_phone": "2", "city": "3"},
        )
        self.client.get(reverse("contacts:contacts_import_preview"))
        self.client.post(reverse("contacts:contacts_import_preview"))

        response = self.client.post(reverse("contacts:contacts_import_run"), {})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "contacts/import_summary.html")
        self.assertFalse(Contact.objects.filter(mobile_phone="00988888888").exists())
