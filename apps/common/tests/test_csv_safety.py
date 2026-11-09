"""CSV formula-injection helper and safe ``next`` redirects."""

from __future__ import annotations

from django.test import RequestFactory, SimpleTestCase

from apps.common.utils.csv_safety import neutralize_csv_cell
from apps.common.utils.redirects import safe_next_url


class NeutralizeCsvCellTests(SimpleTestCase):
    def test_formula_prefixes_get_an_apostrophe(self):
        for dangerous in ("=1+1", "+55119999", "-2+3", "@SUM(A1)", "\tcmd", "\rcmd"):
            with self.subTest(value=dangerous):
                self.assertEqual(neutralize_csv_cell(dangerous), "'" + dangerous)

    def test_harmless_values_are_unchanged(self):
        for harmless in ("Maria", "", "a=b", "—", " =x", 42, None):
            with self.subTest(value=harmless):
                self.assertEqual(neutralize_csv_cell(harmless), harmless)


class SafeNextUrlTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get("/", HTTP_HOST="testserver")

    def test_local_paths_are_accepted(self):
        self.assertEqual(safe_next_url(self.request, "/painel/?a=1"), "/painel/?a=1")
        self.assertEqual(
            safe_next_url(self.request, "http://testserver/x/"), "http://testserver/x/"
        )

    def test_external_and_malformed_targets_are_rejected(self):
        for bad in (
            "//evil.com",
            "/\\evil.com",
            "http://evil.com",
            "https://evil.com/x",
            "javascript:alert(1)",
            "",
            None,
        ):
            with self.subTest(value=bad):
                self.assertIsNone(safe_next_url(self.request, bad))
