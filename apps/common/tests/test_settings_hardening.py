"""Production-safety rules of config/settings.py, checked in a clean interpreter.

Settings are evaluated at import time, so each case runs `python -c` with its own environment
(variables set explicitly, which also keeps a developer's local .env from leaking in).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

GOOD_KEY = "a-long-random-looking-key-for-tests-only-0123456789abcdef"
PROBE = (
    "import json; from django.conf import settings as s; "
    "print(json.dumps({'hosts': s.ALLOWED_HOSTS, 'csrf': s.CSRF_TRUSTED_ORIGINS, "
    "'session_secure': s.SESSION_COOKIE_SECURE, 'csrf_secure': s.CSRF_COOKIE_SECURE, "
    "'ssl_redirect': s.SECURE_SSL_REDIRECT, 'hsts': s.SECURE_HSTS_SECONDS, "
    "'proxy': s.SECURE_PROXY_SSL_HEADER, 'key': s.SECRET_KEY, 'log': "
    "s.LOGGING['loggers']['apps']['level']}))"
)


def _load_settings(**env: str) -> subprocess.CompletedProcess[str]:
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DEBUG": "",
        "SECRET_KEY": "",
        "USE_HTTPS": "",
        "ALLOWED_HOSTS": "",
        "CSRF_TRUSTED_ORIGINS": "",
        "TRUST_X_FORWARDED_PROTO": "",
        "LOG_LEVEL": "",
    }
    base_env.update(env)
    code = f"import django; django.setup(); {PROBE}"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(settings.BASE_DIR),
        env=base_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class SettingsHardeningTests(SimpleTestCase):
    def _loaded(self, **env: str) -> dict:
        result = _load_settings(**env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_missing_secret_key_without_debug_refuses_to_start(self):
        result = _load_settings(DEBUG="False", SECRET_KEY="")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)

    def test_placeholder_secret_keys_are_refused_without_debug(self):
        for placeholder in ("django-insecure-dev-only-change-me", "troque-por-uma-chave-segura"):
            result = _load_settings(DEBUG="False", SECRET_KEY=placeholder)

            self.assertNotEqual(result.returncode, 0, placeholder)
            self.assertIn("ImproperlyConfigured", result.stderr)

    def test_debug_may_run_without_a_secret_key(self):
        loaded = self._loaded(DEBUG="True", SECRET_KEY="")

        self.assertTrue(loaded["key"].startswith("django-insecure-"))
        self.assertFalse(loaded["session_secure"])
        self.assertEqual(loaded["log"], "DEBUG")

    def test_production_defaults_turn_on_https_hardening(self):
        loaded = self._loaded(DEBUG="False", SECRET_KEY=GOOD_KEY)

        self.assertTrue(loaded["session_secure"])
        self.assertTrue(loaded["csrf_secure"])
        self.assertTrue(loaded["ssl_redirect"])
        self.assertEqual(loaded["hsts"], 3600)
        self.assertEqual(loaded["log"], "INFO")
        self.assertIsNone(loaded["proxy"])

    def test_plain_http_can_be_chosen_explicitly(self):
        loaded = self._loaded(DEBUG="False", SECRET_KEY=GOOD_KEY, USE_HTTPS="False")

        self.assertFalse(loaded["session_secure"])
        self.assertFalse(loaded["ssl_redirect"])

    def test_hosts_origins_and_proxy_header_come_from_the_environment(self):
        loaded = self._loaded(
            DEBUG="False",
            SECRET_KEY=GOOD_KEY,
            ALLOWED_HOSTS="app.example.com, www.example.com",
            CSRF_TRUSTED_ORIGINS="https://app.example.com",
            TRUST_X_FORWARDED_PROTO="True",
        )

        self.assertEqual(loaded["hosts"], ["app.example.com", "www.example.com"])
        self.assertEqual(loaded["csrf"], ["https://app.example.com"])
        self.assertEqual(loaded["proxy"], ["HTTP_X_FORWARDED_PROTO", "https"])

    def test_real_environment_wins_over_the_dotenv_file(self):
        # override=False: a variable already set in the environment is never replaced by .env.
        loaded = self._loaded(DEBUG="False", SECRET_KEY=GOOD_KEY, USE_HTTPS="False")

        self.assertEqual(loaded["key"], GOOD_KEY)
