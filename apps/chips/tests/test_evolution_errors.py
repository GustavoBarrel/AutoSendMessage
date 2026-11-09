from django.test import TestCase


class EvolutionWhatsappErrorsTests(TestCase):
    def test_payload_contains_463_and_display(self):
        from apps.chips.services.evolution_whatsapp_errors import (
            ERROR_CODE_WHATSAPP_463,
            MSG_WHATSAPP_463,
            MSG_WHATSAPP_463_SHORT,
            get_send_error_display,
            payload_contains_whatsapp_error_463,
        )

        payload = {"update": {"status": 0, "messageStubParameters": ["463"]}}
        self.assertTrue(payload_contains_whatsapp_error_463(payload))
        display = get_send_error_display(MSG_WHATSAPP_463, payload)
        self.assertIsNotNone(display)
        self.assertEqual(display.code, ERROR_CODE_WHATSAPP_463)
        self.assertTrue(display.not_sent)
        self.assertEqual(display.title, MSG_WHATSAPP_463_SHORT)

    def test_payload_detects_message_update_error_without_463_stub(self):
        from apps.chips.services.evolution_whatsapp_errors import (
            payload_contains_whatsapp_error_463,
            record_indicates_whatsapp_rejection,
        )

        record = {
            "key": {"id": "MSG1", "remoteJid": "5500999999999@s.whatsapp.net"},
            "MessageUpdate": [{"status": "ERROR"}],
        }
        self.assertTrue(record_indicates_whatsapp_rejection(record, message_id="MSG1"))
        self.assertTrue(payload_contains_whatsapp_error_463({"messages": {"records": [record]}}))
