import base64
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.campaigns.services import message_utils
from apps.campaigns.services.message_utils import media_unreachable_message, resolve_media
from apps.contacts.models import Contact


class RenderTextTests(SimpleTestCase):
    def test_unknown_placeholder_is_kept(self):
        contact = Contact(name="Maria")

        self.assertEqual(
            message_utils.render_text("Oi {desconhecido}", contact), "Oi {desconhecido}"
        )

    def test_placeholders_are_case_insensitive(self):
        contact = Contact(name="Maria da Silva")

        self.assertEqual(
            message_utils.render_text("{NOME} / {Primeiro_Nome}", contact),
            "Maria da Silva / Maria",
        )

    def test_blank_name_falls_back_to_friendly_default(self):
        contact = Contact(name="   ")

        self.assertEqual(
            message_utils.render_text("Olá {primeiro_nome} ({ultimo_nome})", contact),
            "Olá amigo(a) (amigo(a))",
        )

    def test_single_name_is_both_first_and_last(self):
        contact = Contact(name="Maria")

        self.assertEqual(
            message_utils.render_text("{primeiro_nome}-{ultimo_nome}", contact), "Maria-Maria"
        )


class ResolveMediaTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.media_root = base / "media"
        (self.media_root / "campaign_media").mkdir(parents=True)
        self.image = self.media_root / "campaign_media" / "a.png"
        self.image.write_bytes(b"png-bytes")
        # A file outside MEDIA_ROOT that must never be read.
        self.secret = base / "secret.env"
        self.secret.write_text("SECRET_KEY=nope")
        override = override_settings(MEDIA_ROOT=self.media_root, MEDIA_URL="/media/")
        override.enable()
        self.addCleanup(override.disable)

    def test_local_upload_becomes_base64(self):
        resolved = resolve_media("http://localhost:8000/media/campaign_media/a.png")

        self.assertEqual(base64.b64decode(resolved), b"png-bytes")

    def test_external_url_is_returned_untouched(self):
        url = "https://example.com/imagem.jpg"

        self.assertEqual(resolve_media(url), url)

    def test_empty_url_is_returned_untouched(self):
        self.assertEqual(resolve_media(""), "")

    def test_missing_file_returns_the_url(self):
        url = "http://localhost/media/campaign_media/missing.png"

        self.assertEqual(resolve_media(url), url)

    def test_path_traversal_outside_media_root_is_not_read(self):
        url = "http://localhost/media/../secret.env"

        self.assertEqual(resolve_media(url), url)

    def test_deep_path_traversal_outside_media_root_is_not_read(self):
        url = "http://localhost/media/campaign_media/../../secret.env"

        self.assertEqual(resolve_media(url), url)

    def test_symlink_pointing_outside_media_root_is_not_read(self):
        link = self.media_root / "campaign_media" / "link.png"
        link.symlink_to(self.secret)
        url = "http://localhost/media/campaign_media/link.png"

        self.assertEqual(resolve_media(url), url)

    def test_prefix_must_match_a_whole_path_segment(self):
        sibling = self.media_root.parent / "mediafoo"
        sibling.mkdir()
        (sibling / "x.png").write_bytes(b"x")
        url = "http://localhost/mediafoo/x.png"

        self.assertEqual(resolve_media(url), url)

    def test_file_over_the_size_limit_returns_the_url(self):
        url = "http://localhost/media/campaign_media/a.png"

        with patch.object(message_utils, "_MAX_LOCAL_MEDIA_BASE64_BYTES", 3):
            self.assertEqual(resolve_media(url), url)

    def test_unreadable_file_returns_the_url_and_logs(self):
        url = "http://localhost/media/campaign_media/a.png"

        with (
            patch.object(Path, "read_bytes", side_effect=PermissionError("denied")),
            self.assertLogs(message_utils.logger, level="WARNING"),
        ):
            self.assertEqual(resolve_media(url), url)


class MediaUnreachableMessageTests(SimpleTestCase):
    def test_localhost_url_is_reported(self):
        self.assertIn("localhost", media_unreachable_message("http://localhost/x.png") or "")

    def test_public_url_and_base64_pass(self):
        self.assertIsNone(media_unreachable_message("https://example.com/x.png"))
        self.assertIsNone(media_unreachable_message("QUJD"))
