"""Contact list filters, pagination and the bulk selection (IDs) endpoint."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.contacts.models import Contact, ManualTag


def _phone(n: int) -> str:
    return f"{n:011d}"


class ContactListFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vip = ManualTag.objects.create(name="VIP")
        cls.news = ManualTag.objects.create(name="Newsletter")
        cls.ana = Contact.objects.create(
            name="Ana",
            mobile_phone=_phone(1),
            email="ana@example.com",
            city="Curitiba",
            state="PR",
            neighborhood="Centro",
            category="Cliente",
            subcategory="Ouro",
        )
        cls.bia = Contact.objects.create(
            name="Bia",
            mobile_phone=_phone(2),
            city="Sao Paulo",
            state="SP",
            neighborhood="Moema",
            category="Lead",
            subcategory="Bronze",
        )
        cls.caio = Contact.objects.create(
            name="Caio", mobile_phone=_phone(3), city="Curitiba", state="PR", category="Cliente"
        )
        cls.archived = Contact.objects.create(
            name="Arquivado", mobile_phone=_phone(4), city="Curitiba", deleted_at=timezone.now()
        )
        cls.ana.manual_tags.add(cls.vip, cls.news)
        cls.bia.manual_tags.add(cls.news)
        cls.archived.manual_tags.add(cls.vip)

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("u", password="senha-teste"))

    def _names(self, **params) -> list[str]:
        response = self.client.get(reverse("contacts:contact_list"), params)
        self.assertEqual(response.status_code, 200)
        return [c.name for c in response.context["page_obj"]]

    def test_without_filters_lists_only_active_contacts(self):
        self.assertEqual(self._names(), ["Ana", "Bia", "Caio"])

    def test_search_matches_name_phone_and_email(self):
        self.assertEqual(self._names(q="bia"), ["Bia"])
        self.assertEqual(self._names(q=_phone(3)), ["Caio"])
        self.assertEqual(self._names(q="ana@example"), ["Ana"])

    def test_each_text_filter(self):
        self.assertEqual(self._names(city="Curitiba"), ["Ana", "Caio"])
        self.assertEqual(self._names(state="SP"), ["Bia"])
        self.assertEqual(self._names(neighborhood="Moema"), ["Bia"])
        self.assertEqual(self._names(category="Cliente"), ["Ana", "Caio"])
        self.assertEqual(self._names(subcategory="Ouro"), ["Ana"])

    def test_text_filter_is_case_insensitive(self):
        self.assertEqual(self._names(city="curitiba"), ["Ana", "Caio"])

    def test_tags_use_any_semantics_without_duplicates(self):
        # Ana has both tags and must appear once.
        response = self.client.get(
            reverse("contacts:contact_list"), {"tags": [self.vip.pk, self.news.pk]}
        )
        self.assertEqual([c.name for c in response.context["page_obj"]], ["Ana", "Bia"])
        self.assertEqual(response.context["total_contacts"], 2)

    def test_single_tag(self):
        self.assertEqual(self._names(tags=[self.vip.pk]), ["Ana"])

    def test_exclude_tags(self):
        self.assertEqual(self._names(exclude_tags=[self.news.pk]), ["Caio"])

    def test_tags_and_exclude_tags_combined(self):
        self.assertEqual(self._names(tags=[self.news.pk], exclude_tags=[self.vip.pk]), ["Bia"])

    def test_combination_of_filters_is_an_and(self):
        self.assertEqual(self._names(city="Curitiba", tags=[self.news.pk]), ["Ana"])
        self.assertEqual(self._names(city="Curitiba", category="Lead"), [])

    def test_invalid_values_are_ignored(self):
        self.assertEqual(self._names(tags="abc", exclude_tags="999999"), ["Ana", "Bia", "Caio"])
        self.assertEqual(self._names(state="x" * 50, city="Curitiba"), ["Ana", "Caio"])

    def test_context_exposes_options_and_querystring(self):
        response = self.client.get(
            reverse("contacts:contact_list"), {"q": "a", "city": "Curitiba", "page": "1"}
        )
        context = response.context
        self.assertEqual(context["filters_querystring"], "q=a&city=Curitiba")
        self.assertTrue(context["has_active_filters"])
        self.assertEqual(context["filter_options"]["city"], ["Curitiba", "Sao Paulo"])
        self.assertEqual(context["filter_options"]["state"], ["PR", "SP"])
        self.assertEqual([t.name for t in context["filter_options"]["tags"]], ["Newsletter", "VIP"])
        self.assertEqual(context["contact_ids_url"], reverse("contacts:contact_ids"))
        self.assertEqual(context["bulk_archive_url"], reverse("contacts:contact_bulk_archive"))
        self.assertFalse(context["clear_bulk_selection"])

    def test_clear_bulk_selection_flag_is_read_once(self):
        session = self.client.session
        session["contacts_bulk_clear_selection"] = True
        session.save()
        first = self.client.get(reverse("contacts:contact_list"))
        second = self.client.get(reverse("contacts:contact_list"))
        self.assertTrue(first.context["clear_bulk_selection"])
        self.assertFalse(second.context["clear_bulk_selection"])

    def test_panel_filter_count_ignores_search_and_opens_the_panel(self):
        url = reverse("contacts:contact_list")
        only_search = self.client.get(url, {"q": "a"})
        self.assertEqual(only_search.context["active_filter_count"], 0)
        self.assertNotContains(only_search, 'class="contact-filters__panel" open')

        filtered = self.client.get(
            url, {"q": "a", "city": "Curitiba", "tags": [self.vip.pk], "exclude_tags": "999999"}
        )
        self.assertEqual(filtered.context["active_filter_count"], 2)
        self.assertContains(filtered, 'class="contact-filters__panel" open')

    def test_rows_post_through_the_bulk_form_without_javascript(self):
        response = self.client.get(reverse("contacts:contact_list"))
        self.assertContains(response, 'id="bulk-archive-form"')
        self.assertContains(
            response, f'form="bulk-archive-form" name="contact_ids_row" value="{self.ana.pk}"'
        )


class ContactListPaginationTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("u", password="senha-teste"))
        tag = ManualTag.objects.create(name="VIP")
        for n in range(45):
            contact = Contact.objects.create(
                name=f"Contato {n:02d}", mobile_phone=_phone(n), city="Curitiba"
            )
            contact.manual_tags.add(tag)
        Contact.objects.create(name="Outro", mobile_phone=_phone(99), city="Lima")
        self.tag = tag

    def test_pagination_links_preserve_every_filter(self):
        response = self.client.get(
            reverse("contacts:contact_list"),
            {"q": "Contato", "city": "Curitiba", "tags": [self.tag.pk]},
        )
        self.assertEqual(response.context["total_contacts"], 45)
        expected = f"q=Contato&amp;tags={self.tag.pk}&amp;city=Curitiba&amp;page=2"
        self.assertContains(response, f'href="?{expected}"')

    def test_second_page_keeps_filters(self):
        response = self.client.get(
            reverse("contacts:contact_list"), {"city": "Curitiba", "page": "3"}
        )
        self.assertEqual(len(response.context["page_obj"]), 5)
        self.assertNotIn("page=", response.context["filters_querystring"])


class ContactIdsEndpointTests(TestCase):
    def setUp(self):
        self.url = reverse("contacts:contact_ids")
        self.vip = ManualTag.objects.create(name="VIP")
        self.a = Contact.objects.create(name="A", mobile_phone=_phone(1), city="Curitiba")
        self.b = Contact.objects.create(name="B", mobile_phone=_phone(2), city="Lima")
        self.gone = Contact.objects.create(
            name="C", mobile_phone=_phone(3), city="Curitiba", deleted_at=timezone.now()
        )
        self.a.manual_tags.add(self.vip)

    def _login(self):
        self.client.force_login(get_user_model().objects.create_user("u", password="senha-teste"))

    def test_requires_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_returns_active_ids_matching_filters(self):
        self._login()
        response = self.client.get(self.url)
        self.assertEqual(response.json(), {"ids": [self.a.pk, self.b.pk], "count": 2})

        response = self.client.get(self.url, {"city": "Curitiba"})
        self.assertEqual(response.json(), {"ids": [self.a.pk], "count": 1})

        response = self.client.get(self.url, {"tags": [self.vip.pk]})
        self.assertEqual(response.json(), {"ids": [self.a.pk], "count": 1})

    def test_post_is_not_allowed(self):
        self._login()
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_over_the_limit_returns_400_with_count(self):
        self._login()
        with patch("apps.contacts.views.MAX_BULK_SELECTION", 1):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertIn("Refine o filtro", body["error"])
        self.assertNotIn("ids", body)

    def test_exactly_at_the_limit_is_allowed(self):
        self._login()
        with patch("apps.contacts.views.MAX_BULK_SELECTION", 2):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
