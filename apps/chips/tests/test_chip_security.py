"""Security hardening of the chips app: webhook auth, URL-safe values and token encryption."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Workspace
from apps.campaigns.models import Campaign, CampaignChip, CampaignMessageVariant, CampaignSendJob
from apps.campaigns.services.campaign_provider_dispatch import dispatch_campaign_message
from apps.chips.forms import ChipForm
from apps.chips.models import Chip
from apps.chips.services.evolution import instance_delete, webhook_set
from apps.chips.services.evolution_config import webhook_token
from apps.chips.validators import url_segment
from apps.common.fields import DecryptionError, decrypt_value, encrypt_value
from apps.contacts.models import Contact

PNG_B64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


class WebhookAuthTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        workspace = Workspace.objects.create(name="Hook", slug="hook")
        Chip.objects.create(workspace=workspace, name="Chip", instance_name="chip-hook")
        self.url = reverse("chips:evolution_webhook_qrcode", kwargs={"token": webhook_token()})

    def _post(self, payload, url=None):
        return self.client.post(
            url or self.url, data=json.dumps(payload), content_type="application/json"
        )

    def test_wrong_token_is_forbidden_and_writes_nothing(self):
        url = reverse("chips:evolution_webhook_qrcode", kwargs={"token": "not-the-token"})

        response = self._post({"instance": "chip-hook", "base64": PNG_B64}, url=url)

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(cache.get("evolution_qr:chip-hook"))

    def test_valid_token_stores_the_qr_for_a_known_instance(self):
        response = self._post({"instance": "chip-hook", "base64": PNG_B64, "pairingCode": "AB12"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            cache.get("evolution_qr:chip-hook"), {"base64": PNG_B64, "pairingCode": "AB12"}
        )

    def test_nested_data_payload_is_supported(self):
        response = self._post({"data": {"instance": "chip-hook", "qrcode": {"base64": PNG_B64}}})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(cache.get("evolution_qr:chip-hook"), {"base64": PNG_B64})

    def test_unknown_instance_is_rejected(self):
        response = self._post({"instance": "someone-else", "base64": PNG_B64})

        self.assertEqual(response.status_code, 404)
        self.assertIsNone(cache.get("evolution_qr:someone-else"))

    def test_qr_that_is_not_base64_is_not_stored(self):
        response = self._post({"instance": "chip-hook", "base64": "javascript:alert(1)"})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(cache.get("evolution_qr:chip-hook"))

    def test_oversized_qr_is_not_stored(self):
        with patch("apps.chips.views._MAX_QR_BASE64_CHARS", 10):
            self._post({"instance": "chip-hook", "base64": PNG_B64})

        self.assertIsNone(cache.get("evolution_qr:chip-hook"))

    def test_invalid_json_and_non_object_payload_are_bad_requests(self):
        invalid = self.client.post(self.url, data="{nope", content_type="application/json")
        non_object = self._post(["chip-hook"])

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(non_object.status_code, 400)

    def test_non_string_instance_is_a_bad_request(self):
        self.assertEqual(self._post({"instance": {"x": 1}, "base64": PNG_B64}).status_code, 400)

    def test_get_with_valid_token_is_ignored(self):
        self.assertEqual(self.client.get(self.url).json(), {"status": "ignored"})

    def test_old_tokenless_path_does_not_exist(self):
        response = self.client.post(
            "/webhook/evolution/qrcode/", data="{}", content_type="application/json"
        )

        self.assertEqual(response.status_code, 404)

    def test_token_is_not_written_to_the_debug_log(self):
        from apps.chips.evolution_debug import clear_log, get_log

        clear_log()
        self.addCleanup(clear_log)
        self._post({"instance": "chip-hook", "base64": PNG_B64})

        self.assertNotIn(webhook_token(), json.dumps(get_log()))


class WebhookSetLogMaskingTests(TestCase):
    """webhook_set sends the real URL to Evolution but must not log the secret token."""

    def setUp(self):
        from apps.chips.evolution_debug import clear_log

        clear_log()
        self.addCleanup(clear_log)
        self.token = webhook_token()
        self.webhook_url = f"https://app.test/webhook/evolution/qrcode/{self.token}/"

    def _response(self, status=201):
        response = MagicMock(status_code=status)
        response.text = json.dumps({"webhook": {"url": self.webhook_url}})
        response.json.return_value = {"webhook": {"url": self.webhook_url}}
        return response

    def test_token_is_masked_in_the_debug_log_but_sent_to_evolution(self):
        from apps.chips.evolution_debug import get_log

        with patch("requests.post", return_value=self._response()) as post:
            ok, error = webhook_set("http://evo.test", "key", "chip-1", self.webhook_url)

        self.assertEqual((ok, error), (True, None))
        self.assertEqual(post.call_args.kwargs["json"]["webhook"]["url"], self.webhook_url)
        logged = json.dumps(get_log())
        self.assertNotIn(self.token, logged)
        self.assertIn("/webhook/evolution/qrcode/<token>/", logged)

    def test_token_is_masked_in_the_debug_log_when_the_call_fails(self):
        import requests

        from apps.chips.evolution_debug import get_log

        with patch("requests.post", side_effect=requests.ConnectionError("down")):
            ok, _error = webhook_set("http://evo.test", "key", "chip-1", self.webhook_url)

        self.assertFalse(ok)
        entry = get_log()[-1]
        self.assertEqual(entry["type"], "webhook_set")
        self.assertNotIn(self.token, json.dumps(entry))


class ConnectUsesTokenWebhookTests(TestCase):
    def test_chip_connect_registers_the_tokenized_webhook_url(self):
        user = get_user_model().objects.create_user(username="conn", password="senha-teste")
        workspace = Workspace.objects.create(name="Conn", slug="conn")
        chip = Chip.objects.create(workspace=workspace, name="C", instance_name="c-1")
        self.client.force_login(user)
        session = self.client.session
        session["workspace_id"] = workspace.pk
        session.save()

        with (
            override_settings(EVOLUTION_URL_BASE="http://evo.test", EVOLUTION_API_KEY="k"),
            patch("apps.chips.views.webhook_set") as webhook_set,
            patch("apps.chips.views.instance_connect", return_value={"base64": PNG_B64}),
        ):
            response = self.client.get(reverse("chips:chip_connect", args=[chip.pk]))

        self.assertEqual(response.status_code, 200)
        registered_url = webhook_set.call_args.args[3]
        self.assertTrue(registered_url.endswith(f"/webhook/evolution/qrcode/{webhook_token()}/"))


class ChipFormUrlSafetyTests(TestCase):
    def _data(self, **overrides):
        data = {
            "name": "Chip",
            "provider": Chip.Provider.EVOLUTION,
            "instance_name": "chip-ok",
            "account_type": "normal",
            "send_window_start": "08:00",
            "send_window_end": "18:00",
            "meta_api_version": "v18.0",
        }
        data.update(overrides)
        return data

    def test_valid_values_pass(self):
        self.assertTrue(ChipForm(self._data()).is_valid())

    def test_evolution_chip_still_requires_an_instance_name(self):
        form = ChipForm(self._data(instance_name=""))

        self.assertFalse(form.is_valid())
        self.assertIn("instance_name", form.errors)

    def test_meta_chip_does_not_need_an_instance_name(self):
        form = ChipForm(
            self._data(
                provider=Chip.Provider.META,
                instance_name="",
                meta_phone_number_id="106540352242922",
                meta_access_token="tok",
            )
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_instance_name_with_path_traversal_is_rejected(self):
        for bad in ("../../instance/logout/x", "a/b", "a b", "a?x=1", "x" * 61, ".."):
            form = ChipForm(self._data(instance_name=bad))
            self.assertFalse(form.is_valid(), bad)
            self.assertIn("instance_name", form.errors, bad)

    def test_meta_phone_number_id_must_be_digits(self):
        for bad in ("http://127.0.0.1:8080/admin#", "//evil.test", "12ab", "1234"):
            form = ChipForm(
                self._data(
                    provider=Chip.Provider.META,
                    meta_phone_number_id=bad,
                    meta_access_token="tok",
                )
            )
            self.assertFalse(form.is_valid(), bad)
            self.assertIn("meta_phone_number_id", form.errors, bad)

    def test_meta_api_version_must_look_like_a_version(self):
        for bad in ("v18", "../x", "18.0", "v100.0", "@evil"):
            form = ChipForm(
                self._data(
                    provider=Chip.Provider.META,
                    meta_phone_number_id="106540352242922",
                    meta_access_token="tok",
                    meta_api_version=bad,
                )
            )
            self.assertFalse(form.is_valid(), bad)
            self.assertIn("meta_api_version", form.errors, bad)


class UrlSegmentTests(SimpleTestCase):
    def test_plain_name_is_unchanged(self):
        self.assertEqual(url_segment("chip_1-a"), "chip_1-a")

    def test_slashes_and_dot_segments_are_neutralized(self):
        self.assertEqual(url_segment("../../x"), "..%2F..%2Fx")
        self.assertEqual(url_segment(".."), "%2E%2E")
        self.assertEqual(url_segment("."), "%2E")

    def test_instance_delete_keeps_the_name_inside_its_path_segment(self):
        response = MagicMock(status_code=200)
        with patch("apps.chips.services.evolution.requests.delete", return_value=response) as m:
            instance_delete("http://evo.test:8080", "key", "../../instance/fetchInstances")

        self.assertEqual(
            m.call_args.args[0],
            "http://evo.test:8080/instance/delete/..%2F..%2Finstance%2FfetchInstances",
        )


class MetaProviderUrlTests(TestCase):
    def setUp(self):
        workspace = Workspace.objects.create(name="MetaUrl", slug="meta-url")
        self.contact = Contact.objects.create(name="Ana", mobile_phone="00911111111")
        self.campaign = Campaign.objects.create(workspace=workspace, name="Camp")
        self.variant = CampaignMessageVariant.objects.create(
            campaign=self.campaign,
            position=1,
            message_type=CampaignMessageVariant.MessageType.TEXT,
            text="Oi",
        )
        self.workspace = workspace

    def _job_for(self, **chip_values):
        chip = Chip.objects.create(
            workspace=self.workspace,
            name="Meta",
            provider=Chip.Provider.META,
            meta_access_token="EAAtoken",
            instance_name="",
            **chip_values,
        )
        CampaignChip.objects.create(campaign=self.campaign, chip=chip)
        return CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=self.contact,
            chip=chip,
            idempotency_key=f"k-{chip.pk}",
        )

    def test_request_goes_to_graph_facebook_com(self):
        job = self._job_for(meta_phone_number_id="106540352242922", meta_api_version="v19.0")
        response = MagicMock(status_code=200, json=lambda: {"messages": [{"id": "wamid.1"}]})

        with patch("apps.chips.providers.meta.requests.post", return_value=response) as post:
            ok, *_ = dispatch_campaign_message(job, self.variant)

        self.assertTrue(ok)
        self.assertEqual(
            post.call_args.args[0], "https://graph.facebook.com/v19.0/106540352242922/messages"
        )

    def test_unsafe_values_saved_before_validation_never_reach_the_network(self):
        for phone_id, version in (
            ("http://127.0.0.1:8080/admin#", "v18.0"),
            ("//internal.host/x", "v18.0"),
            ("106540352242922", "../../v1.0"),
        ):
            with self.subTest(phone_id=phone_id, version=version):
                job = self._job_for(meta_phone_number_id=phone_id, meta_api_version=version)
                with patch("apps.chips.providers.meta.requests.post") as post:
                    ok, error, _mid, _payload, _status, error_kind = dispatch_campaign_message(
                        job, self.variant
                    )
                post.assert_not_called()
                self.assertFalse(ok)
                self.assertIn("inválidos", error)
                self.assertEqual(error_kind, "permanent_config")
                job.delete()
                job.chip.delete()


class MetaTokenEncryptionTests(TestCase):
    TOKEN = "EAAJBxyzSECRETtokenvalue"

    def setUp(self):
        self.workspace = Workspace.objects.create(name="Enc", slug="enc")

    def _chip(self, **kwargs):
        return Chip.objects.create(
            workspace=self.workspace,
            name="Meta",
            provider=Chip.Provider.META,
            meta_phone_number_id="106540352242922",
            instance_name="",
            **kwargs,
        )

    def _raw_token(self, chip):
        with connection.cursor() as cursor:
            cursor.execute("SELECT meta_access_token FROM chips_chip WHERE id = %s", [chip.pk])
            return cursor.fetchone()[0]

    def test_token_is_encrypted_at_rest_and_decrypted_on_read(self):
        chip = self._chip(meta_access_token=self.TOKEN)

        raw = self._raw_token(chip)

        self.assertNotIn(self.TOKEN, raw)
        self.assertTrue(raw.startswith("gAAAA"))
        self.assertEqual(Chip.objects.get(pk=chip.pk).meta_access_token, self.TOKEN)

    def test_empty_token_stays_empty(self):
        chip = self._chip(meta_access_token="")

        self.assertEqual(self._raw_token(chip), "")
        self.assertEqual(Chip.objects.get(pk=chip.pk).meta_access_token, "")

    def test_wrong_key_fails_loudly_instead_of_returning_ciphertext(self):
        ciphertext = encrypt_value(self.TOKEN)

        with override_settings(SECRET_KEY="another-secret-key-for-this-test-only-0123456789"):
            with self.assertRaises(DecryptionError):
                decrypt_value(ciphertext)

    def test_dedicated_key_overrides_the_secret_key_derivation(self):
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        with override_settings(FIELD_ENCRYPTION_KEY=key):
            ciphertext = encrypt_value(self.TOKEN)
            self.assertEqual(decrypt_value(ciphertext), self.TOKEN)
        with self.assertRaises(DecryptionError):
            decrypt_value(ciphertext)

    def test_edit_form_never_renders_the_stored_token(self):
        chip = self._chip(meta_access_token=self.TOKEN)

        html = str(ChipForm(instance=chip)["meta_access_token"])

        self.assertNotIn(self.TOKEN, html)

    def test_blank_token_on_edit_keeps_the_stored_one_and_new_value_replaces_it(self):
        chip = self._chip(meta_access_token=self.TOKEN)
        data = {
            "name": "Meta",
            "provider": Chip.Provider.META,
            "instance_name": "",
            "account_type": "normal",
            "send_window_start": "08:00",
            "send_window_end": "18:00",
            "meta_phone_number_id": "106540352242922",
            "meta_api_version": "v18.0",
            "meta_access_token": "",
        }

        form = ChipForm(data, instance=chip)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(Chip.objects.get(pk=chip.pk).meta_access_token, self.TOKEN)

        form = ChipForm({**data, "meta_access_token": "EAAnew"}, instance=chip)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(Chip.objects.get(pk=chip.pk).meta_access_token, "EAAnew")

    def test_blank_token_on_a_new_meta_chip_is_still_an_error(self):
        data = {
            "name": "Meta",
            "provider": Chip.Provider.META,
            "instance_name": "",
            "account_type": "normal",
            "send_window_start": "08:00",
            "send_window_end": "18:00",
            "meta_phone_number_id": "106540352242922",
            "meta_api_version": "v18.0",
            "meta_access_token": "",
        }

        form = ChipForm(data)

        self.assertFalse(form.is_valid())
        self.assertIn("meta_access_token", form.errors)


class StoredTokenFormHintTests(TestCase):
    def test_edit_form_marks_that_a_token_is_already_stored(self):
        workspace = Workspace.objects.create(name="Hint", slug="hint")
        chip = Chip.objects.create(
            workspace=workspace,
            name="Meta",
            provider=Chip.Provider.META,
            meta_phone_number_id="106540352242922",
            meta_access_token="EAAtoken",
            instance_name="",
        )

        html = str(ChipForm(instance=chip)["meta_access_token"])

        self.assertIn('data-has-stored="1"', html)
        self.assertNotIn("EAAtoken", html)
        self.assertNotIn('data-has-stored="1"', str(ChipForm()["meta_access_token"]))
