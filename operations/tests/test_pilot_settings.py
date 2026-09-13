from __future__ import annotations

import json
import os
import subprocess
import sys

from django.test import SimpleTestCase

PILOT_ENVIRONMENT = {
    "DJANGO_SETTINGS_MODULE": "rehab_center.settings_pilot",
    "RM_PILOT_MODE": "1",
    "DATABASE_HOST": "127.0.0.1",
    "DATABASE_PORT": "5448",
    "DATABASE_NAME": "rm_pilot_training",
    "DATABASE_USER": "pilot-test",
    "DATABASE_PASSWORD": "pilot-test-password",
    "DATABASE_URL": "",
    "RESTORE_DATABASE_NAME_OVERRIDE": "",
}


class PilotSettingsTests(SimpleTestCase):
    databases = set()

    def run_import(self, module: str, expression: str, **environment):
        process_environment = os.environ.copy()
        process_environment.update(PILOT_ENVIRONMENT)
        process_environment.update(environment)
        return subprocess.run(
            [
                sys.executable,
                "-c",
                f"import json; import {module} as settings; "
                f"print(json.dumps({expression}, sort_keys=True))",
            ],
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            env=process_environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_requires_explicit_pilot_mode(self):
        result = self.run_import(
            "rehab_center.settings_pilot",
            "{}",
            RM_PILOT_MODE="",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RM_PILOT_MODE=1", result.stderr)

    def test_requires_postgresql(self):
        result = self.run_import(
            "rehab_center.settings_pilot",
            "{}",
            DATABASE_HOST="",
            DATABASE_URL="",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("require PostgreSQL", result.stderr)

    def test_requires_exact_pilot_database_name(self):
        result = self.run_import(
            "rehab_center.settings_pilot",
            "{}",
            DATABASE_NAME="another_database",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rm_pilot_training", result.stderr)

    def test_forces_local_http_and_non_delivery_settings(self):
        expression = """{
            'debug': settings.DEBUG,
            'allowed_hosts': settings.ALLOWED_HOSTS,
            'csrf_origins': settings.CSRF_TRUSTED_ORIGINS,
            'email_backend': settings.EMAIL_BACKEND,
            'donor_submissions': settings.DONOR_REPORT_SUBMISSIONS_ENABLED,
            'session_secure': settings.SESSION_COOKIE_SECURE,
            'csrf_secure': settings.CSRF_COOKIE_SECURE,
            'ssl_redirect': settings.SECURE_SSL_REDIRECT,
            'hsts_seconds': settings.SECURE_HSTS_SECONDS,
            'hsts_subdomains': settings.SECURE_HSTS_INCLUDE_SUBDOMAINS,
            'hsts_preload': settings.SECURE_HSTS_PRELOAD,
            'database_engine': settings.DATABASES['default']['ENGINE'],
            'database_name': settings.DATABASES['default']['NAME'],
        }"""
        result = self.run_import(
            "rehab_center.settings_pilot",
            expression,
            DJANGO_DEBUG="1",
            DJANGO_ALLOWED_HOSTS="example.com",
            DJANGO_CSRF_TRUSTED_ORIGINS="https://example.com",
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            DONOR_REPORT_SUBMISSIONS_ENABLED="1",
            DJANGO_SECURE_SSL_REDIRECT="1",
            DJANGO_SECURE_HSTS_SECONDS="31536000",
            DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS="1",
            DJANGO_SECURE_HSTS_PRELOAD="1",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        self.assertEqual(
            values,
            {
                "allowed_hosts": ["localhost", "127.0.0.1"],
                "csrf_origins": [
                    "http://localhost:18000",
                    "http://127.0.0.1:18000",
                ],
                "csrf_secure": False,
                "database_engine": "django.db.backends.postgresql",
                "database_name": "rm_pilot_training",
                "debug": False,
                "donor_submissions": False,
                "email_backend": "django.core.mail.backends.locmem.EmailBackend",
                "hsts_preload": False,
                "hsts_seconds": 0,
                "hsts_subdomains": False,
                "session_secure": False,
                "ssl_redirect": False,
            },
        )

    def test_base_production_settings_keep_secure_cookies(self):
        result = self.run_import(
            "rehab_center.settings",
            "{'session': settings.SESSION_COOKIE_SECURE, "
            "'csrf': settings.CSRF_COOKIE_SECURE}",
            DJANGO_SETTINGS_MODULE="rehab_center.settings",
            DJANGO_DEBUG="0",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"csrf": True, "session": True})
