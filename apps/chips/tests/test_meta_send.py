"""Tests for sending through the Meta provider: success, errors and message types."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Workspace
from apps.campaigns.models import Campaign, CampaignChip, CampaignMessageVariant, CampaignSendJob
from apps.campaigns.services.campaign_provider_dispatch import dispatch_campaign_message
from apps.chips.models import Chip
from apps.contacts.models import Contact


class MetaProviderSendTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="meta-send-user", password="pass")
        self.workspace = Workspace.objects.create(name="MetaSend", slug="meta-send")
        self.chip = Chip.objects.create(
            workspace=self.workspace,
            name="MetaChip",
            provider=Chip.Provider.META,
            meta_phone_number_id="106540352242922",
            meta_access_token="EAAJB...",
            meta_api_version="v18.0",
            instance_name="",
        )
        self.contact = Contact.objects.create(
            name="João Meta",
            mobile_phone="11999999999",
        )
        self.campaign = Campaign.objects.create(
            workspace=self.workspace,
            name="Campanha Meta",
        )
        CampaignChip.objects.create(campaign=self.campaign, chip=self.chip)
        self.variant = CampaignMessageVariant.objects.create(
            campaign=self.campaign,
            position=1,
            message_type=CampaignMessageVariant.MessageType.TEXT,
            text="Olá {nome}!",
        )
        self.job = CampaignSendJob.objects.create(
            campaign=self.campaign,
            contact=self.contact,
            chip=self.chip,
            status=CampaignSendJob.Status.PENDING,
            attempts=0,
            max_attempts=3,
            idempotency_key=f"meta-{self.campaign.pk}-{self.contact.pk}",
        )
        self.job.refresh_from_db()

    def _login_with_workspace(self):
        session = self.client.session
        session["workspace_id"] = self.workspace.pk
        session.save()
        self.client.force_login(self.user)

    @patch("apps.chips.providers.meta.requests.post")
    def test_meta_send_text_success(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=201,
            json=lambda: {"messages": [{"id": "wamid.Meta123"}]},
            text='{"messages": [{"id": "wamid.Meta123"}]}',
        )
        ok, error, mid, payload, http_status, error_kind = dispatch_campaign_message(
            self.job, self.variant
        )
        self.assertTrue(ok)
        self.assertEqual(mid, "wamid.Meta123")
        self.assertEqual(http_status, 201)
        self.assertIsNone(error_kind)

    @patch("apps.chips.providers.meta.requests.post")
    def test_meta_send_retries_on_rate_limit(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=429,
            json=lambda: {"error": {"message": "Rate limit", "code": 4, "error_subcode": 131047}},
            text='{"error": {"message": "Rate limit", "code": 4}}',
        )
        ok, error, mid, payload, http_status, error_kind = dispatch_campaign_message(
            self.job, self.variant
        )
        self.assertFalse(ok)
        self.assertEqual(error_kind, "rate_limit")

    @patch("apps.chips.providers.meta.requests.post")
    def test_meta_send_image_success(self, mock_post):
        variant_img = CampaignMessageVariant.objects.create(
            campaign=self.campaign,
            position=2,
            message_type=CampaignMessageVariant.MessageType.IMAGE,
            text="Veja",
            media_url="https://example.com/img.jpg",
        )
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"messages": [{"id": "wamid.Img1"}]},
            text='{"messages": [{"id": "wamid.Img1"}]}',
        )
        ok, error, mid, payload, http_status, error_kind = dispatch_campaign_message(
            self.job, variant_img
        )
        self.assertTrue(ok)
        self.assertEqual(mid, "wamid.Img1")
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["type"], "image")
        self.assertEqual(sent_payload["image"]["link"], "https://example.com/img.jpg")
        self.assertEqual(sent_payload["image"]["caption"], "Veja")

    @patch("apps.chips.providers.meta.requests.post")
    def test_meta_send_video_success(self, mock_post):
        variant_vid = CampaignMessageVariant.objects.create(
            campaign=self.campaign,
            position=3,
            message_type=CampaignMessageVariant.MessageType.VIDEO,
            text="Assista",
            media_url="https://example.com/vid.mp4",
        )
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"messages": [{"id": "wamid.Vid1"}]},
            text='{"messages": [{"id": "wamid.Vid1"}]}',
        )
        ok, error, mid, payload, http_status, error_kind = dispatch_campaign_message(
            self.job, variant_vid
        )
        self.assertTrue(ok)
        self.assertEqual(mid, "wamid.Vid1")
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["type"], "video")
        self.assertEqual(sent_payload["video"]["link"], "https://example.com/vid.mp4")
        self.assertEqual(sent_payload["video"]["caption"], "Assista")

    @patch("apps.chips.providers.meta.requests.post")
    def test_meta_send_document_success(self, mock_post):
        variant_doc = CampaignMessageVariant.objects.create(
            campaign=self.campaign,
            position=4,
            message_type=CampaignMessageVariant.MessageType.DOCUMENT,
            text="Documento",
            media_url="https://example.com/doc.pdf",
        )
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"messages": [{"id": "wamid.Doc1"}]},
            text='{"messages": [{"id": "wamid.Doc1"}]}',
        )
        ok, error, mid, payload, http_status, error_kind = dispatch_campaign_message(
            self.job, variant_doc
        )
        self.assertTrue(ok)
        self.assertEqual(mid, "wamid.Doc1")
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["type"], "document")
        self.assertEqual(sent_payload["document"]["link"], "https://example.com/doc.pdf")
        self.assertEqual(sent_payload["document"]["caption"], "Documento")

    @patch("apps.chips.providers.meta.requests.post")
    def test_meta_payer_error_subcode_classification(self, mock_post):
        for subcode, expected_kind in (
            (131051, "permanent_contact"),
            (131047, "permanent_template"),
            (131042, "permanent_contact"),
            (131049, "permanent_config"),
            (131056, "permanent_template"),
            (131050, "permanent_config"),
            (131044, "permanent_contact"),
            (999999, "temporary"),
        ):
            mock_post.return_value = MagicMock(
                status_code=400,
                json=lambda sc=subcode: {
                    "error": {"message": "err", "code": 131051, "error_subcode": sc}
                },
                text='{"error": {"message": "err"}}',
            )
            ok, error, mid, payload, http_status, error_kind = dispatch_campaign_message(
                self.job, self.variant
            )
            self.assertFalse(ok)
            self.assertEqual(error_kind, expected_kind, msg=f"subcode={subcode}")

    @patch("apps.chips.providers.meta.requests.post")
    def test_meta_send_timeout_returns_temporary(self, mock_post):
        from requests.exceptions import Timeout

        mock_post.side_effect = Timeout("timeout")
        ok, error, mid, payload, http_status, error_kind = dispatch_campaign_message(
            self.job, self.variant
        )
        self.assertFalse(ok)
        self.assertEqual(error_kind, "timeout")

    @patch("apps.chips.providers.meta.requests.post")
    def test_meta_document_uses_id_when_no_url_prefix(self, mock_post):
        """Without http(s), send the 'id' field instead of 'link'."""
        variant_doc = CampaignMessageVariant.objects.create(
            campaign=self.campaign,
            position=5,
            message_type=CampaignMessageVariant.MessageType.DOCUMENT,
            text="Doc id",
            media_url="media_id_abc123",
        )
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"messages": [{"id": "wamid.DocId"}]},
            text='{"messages": [{"id": "wamid.DocId"}]}',
        )
        dispatch_campaign_message(self.job, variant_doc)
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["document"]["id"], "media_id_abc123")
        self.assertNotIn("link", sent_payload["document"])
