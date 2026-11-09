"""Hardening of the dashboards views: CSV injection and strict input validation."""

from __future__ import annotations

import csv
import io
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Workspace
from apps.campaigns.models import Campaign, CampaignSendJob
from apps.chips.models import Chip
from apps.contacts.models import Contact
from apps.dashboards.constants import MAX_WIDGET_PERIOD_DAYS
from apps.dashboards.models import StatsDashboard, StatsDashboardWidget
from apps.dashboards.views import _widget_jobs_queryset


class DashboardTestBase(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="dash", password="senha-teste")
        self.workspace = Workspace.objects.create(name="Dash", slug="dash")
        self.client.force_login(user)
        session = self.client.session
        session["workspace_id"] = self.workspace.pk
        session.save()
        self.dashboard = StatsDashboard.objects.create(workspace=self.workspace, name="Painel")


class StatsExportCsvInjectionTests(DashboardTestBase):
    def test_formula_like_values_are_neutralized_in_the_export(self):
        chip = Chip.objects.create(
            workspace=self.workspace, name="=CHIP()", instance_name="inj-chip"
        )
        campaign = Campaign.objects.create(workspace=self.workspace, name="+Campanha")
        contact = Contact.objects.create(name="@Contato", mobile_phone="00911112222")
        CampaignSendJob.objects.create(
            campaign=campaign,
            contact=contact,
            chip=chip,
            chip_name_snapshot=chip.name,
            last_error="-cmd|' /C calc'!A0",
            idempotency_key="inj-1",
        )

        response = self.client.get(reverse("dashboards:stats_export_csv"))

        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
        data_row = rows[1]
        self.assertEqual(data_row[1], "'+Campanha")
        self.assertEqual(data_row[2], "'=CHIP()")
        self.assertEqual(data_row[3], "'@Contato")
        self.assertEqual(data_row[7], "'-cmd|' /C calc'!A0")


class WidgetPreviewValidationTests(DashboardTestBase):
    def _preview(self, **params):
        url = reverse("dashboards:widget_preview", kwargs={"dashboard_pk": self.dashboard.pk})
        return self.client.get(url, params)

    def test_invalid_period_is_rejected_with_400(self):
        cases = [
            {"start_days_ago": "abc"},
            {"end_days_ago": "1.5"},
            {"start_days_ago": "-1"},
            {"end_days_ago": "-30"},
            {"start_days_ago": str(MAX_WIDGET_PERIOD_DAYS + 1)},
            {"start_days_ago": "99999999999999999999"},
        ]
        for params in cases:
            with self.subTest(params=params):
                response = self._preview(**params)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["ok"])

    def test_period_inside_the_window_is_accepted(self):
        response = self._preview(start_days_ago="0", end_days_ago=str(MAX_WIDGET_PERIOD_DAYS))
        self.assertEqual(response.status_code, 200)

    def test_stored_filters_out_of_range_are_clamped_instead_of_crashing(self):
        for filters in (
            {"start_days_ago": 10**12, "end_days_ago": -5},
            {"start_days_ago": "lixo", "end_days_ago": None},
        ):
            with self.subTest(filters=filters):
                self.assertEqual(_widget_jobs_queryset(self.workspace, filters).count(), 0)


class WidgetReorderValidationTests(DashboardTestBase):
    def setUp(self):
        super().setUp()
        self.first = StatsDashboardWidget.objects.create(dashboard=self.dashboard, position=0)
        self.second = StatsDashboardWidget.objects.create(dashboard=self.dashboard, position=1)
        self.url = reverse("dashboards:widget_reorder", kwargs={"dashboard_pk": self.dashboard.pk})

    def _post(self, payload):
        return self.client.post(self.url, data=json.dumps(payload), content_type="application/json")

    def test_valid_order_is_saved(self):
        response = self._post({"order": [str(self.second.pk), self.first.pk]})

        self.assertEqual(response.status_code, 200)
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual((self.second.position, self.first.position), (0, 1))

    def test_non_integer_ids_return_400_and_change_nothing(self):
        for order in (["abc", self.first.pk], [None], [[1]], "nope", 7):
            with self.subTest(order=order):
                response = self._post({"order": order})
                self.assertEqual(response.status_code, 400)
        self.first.refresh_from_db()
        self.assertEqual(self.first.position, 0)
