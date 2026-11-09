"""Smoke test: GET every HTML page with one object of each main type.

It is a safety net for refactors (e.g. renames), since the templates have almost
no tests of their own.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Workspace
from apps.campaigns.models import (
    Campaign,
    CampaignChip,
    CampaignMessageVariant,
    CampaignSendAttempt,
    CampaignSendJob,
)
from apps.chips.models import Chip
from apps.contacts.models import Contact, ManualTag
from apps.dashboards.models import StatsDashboard, StatsDashboardWidget

OK = 200
REDIRECT = 302

_MISSING_VARIABLE_RE = re.compile(
    r"Exception while resolving variable '([^']+)' in template '([^']+)'"
)


class _MissingVariableHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.missing: set[tuple[str, str]] = set()

    def emit(self, record: logging.LogRecord) -> None:
        match = _MISSING_VARIABLE_RE.search(record.getMessage())
        if match:
            self.missing.add((match.group(2), match.group(1)))


@contextmanager
def collect_missing_template_variables() -> Iterator[set[tuple[str, str]]]:
    """Collect (template, variable) pairs Django failed to resolve during the block.

    A template variable renamed on only one side disappears silently; Django just logs
    a DEBUG message on the ``django.template`` logger.
    """
    logger = logging.getLogger("django.template")
    handler = _MissingVariableHandler()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.missing
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


class PageSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="smoke-admin", password="senha-teste", is_staff=True, is_superuser=True
        )
        cls.workspace = Workspace.objects.create(name="Perfil Fumaça", slug="perfil-fumaca")
        cls.chip = Chip.objects.create(
            workspace=cls.workspace,
            name="Chip Fumaça",
            provider=Chip.Provider.EVOLUTION,
            instance_name="smoke-instance",
        )
        cls.meta_chip = Chip.objects.create(
            workspace=cls.workspace,
            name="Chip Meta Fumaça",
            provider=Chip.Provider.META,
            meta_phone_number_id="123456789",
            meta_access_token="token-abc",
        )
        cls.tag = ManualTag.objects.create(name="Tag Fumaça")
        cls.contact = Contact.objects.create(
            name="Maria Silva", mobile_phone="00999999999", city="São Paulo", neighborhood="Centro"
        )
        cls.campaign = Campaign.objects.create(workspace=cls.workspace, name="Campanha Fumaça")
        cls.dashboard = StatsDashboard.objects.create(workspace=cls.workspace, name="Painel Fumaça")
        cls.widget = StatsDashboardWidget.objects.create(
            dashboard=cls.dashboard, title="Widget Fumaça"
        )

    def setUp(self):
        # The Evolution debug log lives in the cache and leaks between tests; start empty.
        cache.clear()
        self.client.force_login(self.user)
        session = self.client.session
        session["workspace_id"] = self.workspace.pk
        session.save()

    def _pages(self) -> list[tuple[str, dict[str, object], int]]:
        pk = {"pk": self.workspace.pk}
        return [
            ("dashboards:home", {}, OK),
            ("accounts:workspace_select", {}, OK),
            ("accounts:workspace_list", {}, OK),
            ("accounts:workspace_create", {}, OK),
            ("accounts:workspace_edit", pk, OK),
            ("accounts:workspace_confirm_delete", pk, OK),
            # chip_list and campaign_edit are shortcuts that redirect (home and wizard).
            ("chips:chip_list", {}, REDIRECT),
            ("chips:chip_create", {}, OK),
            ("chips:chip_edit", {"pk": self.chip.pk}, OK),
            ("chips:chip_confirm_delete", {"pk": self.chip.pk}, OK),
            # Meta chips do not use a QR Code: the view redirects without calling Evolution.
            ("chips:chip_connect", {"pk": self.meta_chip.pk}, REDIRECT),
            ("chips:debug_evolution", {}, OK),
            ("contacts:contact_list", {}, OK),
            ("contacts:contact_tags", {"pk": self.contact.pk}, OK),
            ("contacts:contact_confirm_delete", {"pk": self.contact.pk}, OK),
            ("contacts:manual_tag_list", {}, OK),
            ("contacts:manual_tag_create", {}, OK),
            ("contacts:manual_tag_edit", {"pk": self.tag.pk}, OK),
            ("contacts:manual_tag_confirm_delete", {"pk": self.tag.pk}, OK),
            ("contacts:contacts_import", {}, OK),
            ("contacts:contacts_import_template", {}, OK),
            # Import wizard steps without session data go back to the upload.
            ("contacts:contacts_import_map", {}, REDIRECT),
            ("contacts:contacts_import_preview", {}, REDIRECT),
            ("contacts:contacts_import_duplicates", {}, REDIRECT),
            ("contacts:contacts_import_summary", {}, REDIRECT),
            ("campaigns:campaign_list", {}, OK),
            ("campaigns:campaign_create", {}, OK),
            ("campaigns:campaign_edit", {"pk": self.campaign.pk}, REDIRECT),
            ("campaigns:campaign_detail", {"pk": self.campaign.pk}, OK),
            ("campaigns:campaign_confirm_delete", {"pk": self.campaign.pk}, OK),
            ("campaigns:campaign_wizard", {"pk": self.campaign.pk, "step": "audience"}, OK),
            ("campaigns:campaign_wizard", {"pk": self.campaign.pk, "step": "chips"}, OK),
            ("campaigns:campaign_wizard", {"pk": self.campaign.pk, "step": "messages"}, OK),
            ("dashboards:stats_dashboard", {}, OK),
            ("dashboards:stats_export_csv", {}, OK),
            ("dashboards:custom_dashboard_list", {}, OK),
            ("dashboards:custom_dashboard_create", {}, OK),
            ("dashboards:custom_dashboard_view", {"pk": self.dashboard.pk}, OK),
            ("dashboards:custom_dashboard_edit", {"pk": self.dashboard.pk}, OK),
            ("dashboards:custom_dashboard_delete", {"pk": self.dashboard.pk}, OK),
            ("dashboards:widget_create", {"dashboard_pk": self.dashboard.pk}, OK),
            (
                "dashboards:widget_edit",
                {"dashboard_pk": self.dashboard.pk, "widget_pk": self.widget.pk},
                OK,
            ),
        ]

    def test_authenticated_pages_respond(self):
        for url_name, kwargs, expected in self._pages():
            with self.subTest(url=url_name, kwargs=kwargs):
                response = self.client.get(reverse(url_name, kwargs=kwargs))
                self.assertEqual(response.status_code, expected)

    def test_pages_do_not_reference_missing_template_variables(self):
        with collect_missing_template_variables() as missing:
            for url_name, kwargs, _ in self._pages():
                self.client.get(reverse(url_name, kwargs=kwargs))
        # Create forms have no instance: these missing variables are legitimate.
        expected_missing = {
            ("campaigns/create.html", "name"),
            ("chips/form.html", "chip"),
            ("chips/form.html", "is_active"),
        }
        self.assertEqual(missing - expected_missing, set())

    def test_login_page_is_public(self):
        self.client.logout()
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, OK)

    def test_pages_without_workspace_redirect_to_picker(self):
        session = self.client.session
        del session["workspace_id"]
        session.save()
        response = self.client.get(reverse("dashboards:home"))
        self.assertRedirects(response, reverse("accounts:workspace_select"))

    def test_pages_render_object_data(self):
        """Ensure the template context variables are still bound to the objects."""
        expected_content = [
            ("accounts:workspace_select", {}, "Perfil Fumaça"),
            ("accounts:workspace_list", {}, "Perfil Fumaça"),
            ("accounts:workspace_edit", {"pk": self.workspace.pk}, "Perfil Fumaça"),
            ("dashboards:home", {}, "Chip Fumaça"),
            ("chips:chip_edit", {"pk": self.chip.pk}, "smoke-instance"),
            ("chips:chip_confirm_delete", {"pk": self.chip.pk}, "Chip Fumaça"),
            ("contacts:contact_list", {}, "Maria Silva"),
            ("contacts:contact_tags", {"pk": self.contact.pk}, "Tag Fumaça"),
            ("contacts:contact_confirm_delete", {"pk": self.contact.pk}, "Maria Silva"),
            ("contacts:manual_tag_list", {}, "Tag Fumaça"),
            ("contacts:manual_tag_edit", {"pk": self.tag.pk}, "Tag Fumaça"),
            ("campaigns:campaign_list", {}, "Campanha Fumaça"),
            ("campaigns:campaign_detail", {"pk": self.campaign.pk}, "Campanha Fumaça"),
            ("campaigns:campaign_confirm_delete", {"pk": self.campaign.pk}, "Campanha Fumaça"),
            ("dashboards:custom_dashboard_list", {}, "Painel Fumaça"),
            ("dashboards:custom_dashboard_view", {"pk": self.dashboard.pk}, "Painel Fumaça"),
            ("dashboards:custom_dashboard_edit", {"pk": self.dashboard.pk}, "Painel Fumaça"),
            (
                "dashboards:widget_edit",
                {"dashboard_pk": self.dashboard.pk, "widget_pk": self.widget.pk},
                "Widget Fumaça",
            ),
        ]
        for url_name, kwargs, text in expected_content:
            with self.subTest(url=url_name):
                response = self.client.get(reverse(url_name, kwargs=kwargs))
                self.assertContains(response, text)


class CampaignWithDataPageTests(TestCase):
    """Render pages that list data (queue, attempts, statistics) to catch broken context
    variables, which templates ignore silently.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="data-user", password="x")
        cls.workspace = Workspace.objects.create(name="Perfil Dados", slug="perfil-dados")
        cls.chip = Chip.objects.create(
            workspace=cls.workspace, name="Chip Dados", instance_name="chip-dados"
        )
        cls.contact = Contact.objects.create(
            name="Joana Dados",
            mobile_phone="00977777777",
            city="São Paulo",
            state="SP",
            neighborhood="Centro",
            category="Cliente",
            gender="F",
        )
        cls.campaign = Campaign.objects.create(
            workspace=cls.workspace, name="Campanha Dados", status=Campaign.Status.DRAFT
        )
        CampaignChip.objects.create(campaign=cls.campaign, chip=cls.chip)
        cls.variants = [
            CampaignMessageVariant.objects.create(
                campaign=cls.campaign,
                position=n,
                message_type=CampaignMessageVariant.MessageType.TEXT,
                text=f"Olá {{primeiro_nome}}, variação {n}.",
            )
            for n in (1, 2, 3)
        ]
        cls.job = CampaignSendJob.objects.create(
            campaign=cls.campaign,
            contact=cls.contact,
            chip=cls.chip,
            chip_name_snapshot=cls.chip.name,
            variant=cls.variants[0],
            status=CampaignSendJob.Status.SENT,
        )
        CampaignSendAttempt.objects.create(
            job=cls.job, attempt_no=1, success=True, provider_payload={"ok": True}
        )
        cls.dashboard = StatsDashboard.objects.create(workspace=cls.workspace, name="Painel Dados")
        for dataset in StatsDashboardWidget.Dataset.values:
            StatsDashboardWidget.objects.create(dashboard=cls.dashboard, dataset=dataset)

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["workspace_id"] = self.workspace.pk
        session.save()

    def test_pages_show_related_data(self):
        expected = [
            ("campaigns:campaign_detail", {"pk": self.campaign.pk}, "Joana Dados"),
            ("campaigns:campaign_detail", {"pk": self.campaign.pk}, "Chip Dados"),
            (
                "campaigns:campaign_wizard",
                {"pk": self.campaign.pk, "step": "audience"},
                "Joana Dados",
            ),
            ("campaigns:campaign_wizard", {"pk": self.campaign.pk, "step": "chips"}, "Chip Dados"),
            (
                "campaigns:campaign_wizard",
                {"pk": self.campaign.pk, "step": "messages"},
                "variação 2",
            ),
            ("dashboards:stats_dashboard", {}, "Chip Dados"),
            ("dashboards:stats_dashboard", {}, "Campanha Dados"),
            ("dashboards:custom_dashboard_view", {"pk": self.dashboard.pk}, "Painel Dados"),
            ("dashboards:home", {}, "Chip Dados"),
            ("contacts:contact_list", {}, "Joana Dados"),
        ]
        for url_name, kwargs, text in expected:
            with self.subTest(url=url_name, step=kwargs.get("step"), text=text):
                response = self.client.get(reverse(url_name, kwargs=kwargs))
                self.assertContains(response, text)

    def test_pages_with_data_do_not_reference_missing_template_variables(self):
        pages = [
            ("campaigns:campaign_list", {}),
            ("campaigns:campaign_detail", {"pk": self.campaign.pk}),
            ("campaigns:campaign_wizard", {"pk": self.campaign.pk, "step": "audience"}),
            ("campaigns:campaign_wizard", {"pk": self.campaign.pk, "step": "chips"}),
            ("campaigns:campaign_wizard", {"pk": self.campaign.pk, "step": "messages"}),
            ("chips:chip_edit", {"pk": self.chip.pk}),
            ("dashboards:stats_dashboard", {}),
            ("dashboards:custom_dashboard_view", {"pk": self.dashboard.pk}),
            ("dashboards:home", {}),
            ("contacts:contact_list", {}),
        ]
        with collect_missing_template_variables() as missing:
            for url_name, kwargs in pages:
                self.client.get(reverse(url_name, kwargs=kwargs))
        self.assertEqual(missing, set())

    def test_every_dataset_builds_chart_payload(self):
        """Every dataset (including the contact ones) answers the widget preview without errors."""
        for dataset in StatsDashboardWidget.Dataset.values:
            with self.subTest(dataset=dataset):
                response = self.client.get(
                    reverse(
                        "dashboards:widget_preview", kwargs={"dashboard_pk": self.dashboard.pk}
                    ),
                    {"dataset": dataset, "chart": "bar"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json())

    def test_stats_export_has_rows(self):
        response = self.client.get(reverse("dashboards:stats_export_csv"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Joana Dados", response.content.decode("utf-8-sig"))


class RenameRegressionTests(TestCase):
    """Ensure renamed names still match between view, template and JS."""

    def setUp(self):
        user = get_user_model().objects.create_user(username="regression", password="x")
        self.client.force_login(user)
        self.workspace = Workspace.objects.create(name="Perfil Regressão", slug="perfil-regressao")
        session = self.client.session
        session["workspace_id"] = self.workspace.pk
        session.save()

    def test_campaign_list_tabs_use_the_query_keys_the_view_reads(self):
        Campaign.objects.create(
            workspace=self.workspace, name="Campanha arquivada", deleted_at=timezone.now()
        )
        Campaign.objects.create(workspace=self.workspace, name="Campanha ativa")
        Campaign.objects.create(
            workspace=self.workspace, name="Campanha finalizada", status=Campaign.Status.FINISHED
        )
        url = reverse("campaigns:campaign_list")
        html = self.client.get(url).content.decode()
        self.assertIn("?archived=1", html)
        self.assertIn("?finished=1", html)

        archived = self.client.get(url + "?archived=1")
        self.assertContains(archived, "Campanha arquivada")
        self.assertNotContains(archived, "Campanha ativa")

        finished = self.client.get(url + "?finished=1")
        self.assertContains(finished, "Campanha finalizada")
        self.assertNotContains(finished, "Campanha ativa")

    def test_campaign_detail_links_back_to_archived_tab(self):
        campaign = Campaign.objects.create(
            workspace=self.workspace, name="Campanha arquivada", deleted_at=timezone.now()
        )
        response = self.client.get(reverse("campaigns:campaign_detail", kwargs={"pk": campaign.pk}))
        self.assertContains(response, "?archived=1")

    def test_chip_edit_label_points_to_the_active_checkbox(self):
        chip = Chip.objects.create(workspace=self.workspace, name="Chip", instance_name="chip-x")
        response = self.client.get(reverse("chips:chip_edit", kwargs={"pk": chip.pk}))
        self.assertContains(response, 'id="id_is_active"')
        self.assertContains(response, 'for="id_is_active"')

    def test_import_preview_shows_whatsapp_of_repeated_rows(self):
        csv_content = "Nome;Celular;Whatsapp\nAna;00911111111;sim\nBia;00911111111;sim\n"
        self.client.post(
            reverse("contacts:contacts_import"),
            {"file": SimpleUploadedFile("c.csv", csv_content.encode(), "text/csv")},
        )
        self.client.post(
            reverse("contacts:contacts_import_map"),
            {"name": "1", "mobile_phone": "2", "has_whatsapp": "3"},
        )
        with collect_missing_template_variables() as missing:
            response = self.client.get(reverse("contacts:contacts_import_preview"))
        self.assertContains(response, "WhatsApp Sim")
        self.assertEqual(missing, set())
