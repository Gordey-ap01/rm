from __future__ import annotations

import os

from django.core.exceptions import ImproperlyConfigured

from .settings import *  # noqa: F403

PILOT_DATABASE_NAME = "rm_pilot_training"

if os.environ.get("RM_PILOT_MODE") != "1":
    raise ImproperlyConfigured("Pilot settings require RM_PILOT_MODE=1.")

_pilot_database = DATABASES["default"]  # noqa: F405
if not _pilot_database.get("ENGINE", "").endswith("postgresql"):
    raise ImproperlyConfigured("Pilot settings require PostgreSQL.")
if str(_pilot_database.get("NAME", "")) != PILOT_DATABASE_NAME:
    raise ImproperlyConfigured(
        f"Pilot database name must be exactly {PILOT_DATABASE_NAME}."
    )

DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
CSRF_TRUSTED_ORIGINS = [
    "http://localhost:18000",
    "http://127.0.0.1:18000",
]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
DONOR_REPORT_SUBMISSIONS_ENABLED = False

SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
