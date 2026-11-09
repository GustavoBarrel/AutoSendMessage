from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse

from apps.common.utils.safe import safe_action


def _request(**extra):
    request = RequestFactory().get("/", **extra)
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


@safe_action("fazer algo")
def _failing_view(request):
    raise RuntimeError("boom")


class SafeActionTests(SimpleTestCase):
    def test_default_fallback_is_a_resolvable_url_name(self):
        """Without a referer, redirect to home instead of raising NoReverseMatch."""
        with self.assertLogs("apps.common.utils.safe", level="ERROR"):
            response = _failing_view(_request())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dashboards:home"))

    def test_referer_wins_over_fallback(self):
        with self.assertLogs("apps.common.utils.safe", level="ERROR"):
            response = _failing_view(_request(HTTP_REFERER="/contacts/"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/contacts/")
