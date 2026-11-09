from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.chips.evolution_debug import clear_log, get_log, log_evolution_call


class DebugEvolutionPageTests(TestCase):
    """The page renders the entries built by evolution_debug (keys must match the template)."""

    def setUp(self):
        # The log lives in the cache: isolate this test from whatever ran before it.
        clear_log()
        self.addCleanup(clear_log)
        self.staff = get_user_model().objects.create_user(
            username="debug-staff", password="senha-teste", is_staff=True
        )

    def test_entry_type_and_time_are_rendered(self):
        log_evolution_call(
            "instance_create",
            "http://evolution.test/instance/create",
            response_status=201,
            response_body="ok",
        )
        entry = get_log()[0]
        self.client.force_login(self.staff)

        response = self.client.get(reverse("chips:debug_evolution"))

        self.assertContains(response, "<strong>instance_create</strong>", html=True)
        self.assertContains(response, entry["time"])
        self.assertContains(response, "http://evolution.test/instance/create")

    def test_regular_user_is_redirected_away(self):
        regular = get_user_model().objects.create_user(username="regular", password="senha-teste")
        self.client.force_login(regular)

        response = self.client.get(reverse("chips:debug_evolution"))

        self.assertRedirects(response, reverse("dashboards:home"), fetch_redirect_response=False)

    def test_clear_button_empties_the_log(self):
        log_evolution_call("webhook_set", "http://evolution.test/webhook/set/x")
        self.client.force_login(self.staff)

        self.client.post(reverse("chips:debug_evolution"), {"clear": "1"})

        self.assertEqual(get_log(), [])
