from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Workspace
from apps.chips.models import Chip


class ChipMetaProviderTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="meta-tester",
            password="senha-teste",
        )
        self.workspace = Workspace.objects.create(name="Perfil Meta", slug="perfil-meta")

    def _login_with_workspace(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["workspace_id"] = self.workspace.pk
        session.save()

    def test_meta_chip_is_open_in_home_view(self):
        Chip.objects.create(
            workspace=self.workspace,
            name="Meta Home",
            provider=Chip.Provider.META,
            meta_phone_number_id="123456789",
            meta_access_token="token-abc",
            instance_name="",
        )
        Chip.objects.create(
            workspace=self.workspace,
            name="Evolution Off",
            provider=Chip.Provider.EVOLUTION,
            instance_name="evo-off",
        )
        self._login_with_workspace()
        response = self.client.get(reverse("dashboards:home"))
        self.assertEqual(response.status_code, 200)
        context = response.context or {}
        chips = context.get("chips_with_state", [])
        states = {item["chip"].name: item["connection_state"] for item in chips}
        self.assertEqual(states["Meta Home"], "open")
        self.assertIsNone(states["Evolution Off"])

    def test_meta_chip_skips_evolution_api_call_in_home(self):
        Chip.objects.create(
            workspace=self.workspace,
            name="Meta Skip",
            provider=Chip.Provider.META,
            meta_phone_number_id="123456789",
            meta_access_token="token-abc",
            instance_name="meta-skip-instance",
        )
        self._login_with_workspace()
        with patch("apps.chips.services.evolution.requests.request") as mock_request:
            response = self.client.get(reverse("dashboards:home"))
        self.assertEqual(response.status_code, 200)
        mock_request.assert_not_called()

    def test_meta_chip_redirects_from_connect_view(self):
        chip = Chip.objects.create(
            workspace=self.workspace,
            name="Meta Connect",
            provider=Chip.Provider.META,
            meta_phone_number_id="123456789",
            meta_access_token="token-abc",
            instance_name="",
        )
        self._login_with_workspace()
        response = self.client.post(reverse("chips:chip_connect", args=[chip.pk]))
        self.assertEqual(response.status_code, 302)
