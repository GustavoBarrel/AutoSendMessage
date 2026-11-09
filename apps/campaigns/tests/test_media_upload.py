"""Media upload in the messages step: the server decides type and size, not the client."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Workspace
from apps.campaigns.models import Campaign, CampaignMessageVariant
from apps.campaigns.services.campaign_audience import default_audience_config

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32


class MediaUploadTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.media_root = Path(tmp.name)
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)

        user = get_user_model().objects.create_user(username="uploader", password="senha-teste")
        self.workspace = Workspace.objects.create(name="Up", slug="up")
        self.campaign = Campaign.objects.create(
            workspace=self.workspace,
            name="Campanha Upload",
            audience=Campaign.Audience.ALL,
            audience_config=default_audience_config(),
            status=Campaign.Status.DRAFT,
        )
        self.client.force_login(user)
        session = self.client.session
        session["workspace_id"] = self.workspace.pk
        session.save()
        self.url = reverse(
            "campaigns:campaign_wizard", kwargs={"pk": self.campaign.pk, "step": "messages"}
        )

    def _post(self, **files):
        data = {
            "shared_message_type": "",
            "message_type_1": "image",
            "text_1": "Legenda principal da campanha de teste.",
            "media_1": "",
            "message_type_2": "text",
            "text_2": "",
            "message_type_3": "text",
            "text_3": "",
        }
        data.update(files)
        # URLField rejects the test client default host ("testserver"), which has no TLD.
        return self.client.post(self.url, data=data, SERVER_NAME="localhost")

    def _stored_files(self) -> list[Path]:
        folder = self.media_root / "campaign_media"
        return sorted(folder.iterdir()) if folder.exists() else []

    def _error_text(self, response) -> str:
        context = response.context
        return " ".join(str(m) for m in context["messages"]) if context else ""

    def test_valid_image_is_stored_and_referenced_by_the_variant(self):
        upload = SimpleUploadedFile("foto.png", PNG, content_type="image/png")

        response = self._post(media_file_1=upload)

        self.assertEqual(response.status_code, 302, self._error_text(response))
        self.assertEqual(len(self._stored_files()), 1)
        variant = CampaignMessageVariant.objects.get(campaign=self.campaign, position=1)
        self.assertIn("/media/campaign_media/", variant.media_url)
        self.assertTrue(variant.media_url.endswith(".png"))

    def test_upload_without_content_type_is_rejected(self):
        upload = SimpleUploadedFile("foto.png", PNG, content_type="")

        response = self._post(media_file_1=upload)

        self.assertEqual(response.status_code, 200)
        self.assertIn("imagem ou um vídeo", self._error_text(response))
        self.assertEqual(self._stored_files(), [])

    def test_non_media_content_type_is_rejected(self):
        upload = SimpleUploadedFile("foto.png", PNG, content_type="application/pdf")

        response = self._post(media_file_1=upload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._stored_files(), [])

    def test_size_limit_follows_the_extension_not_the_declared_type(self):
        # A video sent as image/png must still hit the (larger) video limit rules.
        upload = SimpleUploadedFile("clip.mp4", MP4, content_type="image/png")

        with patch("apps.campaigns.views_wizard._MAX_UPLOAD_VIDEO_BYTES", 10):
            response = self._post(media_file_1=upload)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Vídeo muito grande", self._error_text(response))
        self.assertEqual(self._stored_files(), [])

    def test_image_over_the_image_limit_is_rejected(self):
        upload = SimpleUploadedFile("foto.png", PNG, content_type="video/mp4")

        with patch("apps.campaigns.views_wizard._MAX_UPLOAD_IMAGE_BYTES", 10):
            response = self._post(media_file_1=upload)

        self.assertIn("Imagem muito grande", self._error_text(response))
        self.assertEqual(self._stored_files(), [])

    def test_content_that_does_not_match_the_extension_is_rejected(self):
        upload = SimpleUploadedFile("foto.png", b"<?php echo 1; ?>", content_type="image/png")

        response = self._post(media_file_1=upload)

        self.assertEqual(response.status_code, 200)
        self.assertIn("não corresponde", self._error_text(response))
        self.assertEqual(self._stored_files(), [])

    def test_unsupported_extension_is_rejected(self):
        upload = SimpleUploadedFile("script.html", b"<script>", content_type="image/png")

        response = self._post(media_file_1=upload)

        self.assertIn("Formato de arquivo não suportado", self._error_text(response))
        self.assertEqual(self._stored_files(), [])

    def test_files_saved_before_a_later_rejection_are_discarded(self):
        good = SimpleUploadedFile("foto.png", PNG, content_type="image/png")
        bad = SimpleUploadedFile("outra.png", b"not a png", content_type="image/png")

        response = self._post(
            media_file_1=good,
            message_type_2="image",
            text_2="Outra legenda para a variação dois.",
            media_file_2=bad,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._stored_files(), [])
        self.assertFalse(
            CampaignMessageVariant.objects.filter(campaign=self.campaign).exists(),
        )
