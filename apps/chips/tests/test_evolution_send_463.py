from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.chips.services.evolution_send import send_text
from apps.chips.services.evolution_whatsapp_errors import MSG_WHATSAPP_463


class EvolutionSend463Tests(TestCase):
    @patch("apps.chips.services.evolution_send._probe_whatsapp_error_463", return_value=True)
    @patch("apps.chips.services.evolution_send.requests.post")
    def test_send_text_fails_when_probe_detects_463(self, mocked_post, _probe):
        send_response = SimpleNamespace(status_code=201, text="{}")
        send_response.json = lambda: {
            "key": {"id": "ABC123", "remoteJid": "5500999999999@s.whatsapp.net"},
            "status": "PENDING",
        }
        mocked_post.return_value = send_response

        result = send_text("https://evolution.test", "key", "inst", "5500999999999", "oi")

        self.assertFalse(result.ok)
        self.assertEqual(result.error_message, MSG_WHATSAPP_463)

    @patch("apps.chips.services.evolution_send._probe_whatsapp_error_463", return_value=False)
    @patch("apps.chips.services.evolution_send.requests.post")
    def test_send_text_succeeds_on_http_201_when_no_463(self, mocked_post, _probe):
        send_response = SimpleNamespace(status_code=201, text="{}")
        send_response.json = lambda: {
            "key": {"id": "ABC123", "remoteJid": "5500999999999@s.whatsapp.net"},
            "status": "PENDING",
        }
        mocked_post.return_value = send_response

        result = send_text("https://evolution.test", "key", "inst", "5500999999999", "oi")

        self.assertTrue(result.ok)
        self.assertEqual(result.message_id, "ABC123")
