from __future__ import annotations

from .settings import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(BASE_DIR / "data" / "test_runtime.sqlite3"),  # noqa: F405
        "OPTIONS": {
            "init_command": "PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;",
            "transaction_mode": "EXCLUSIVE",
        },
    }
}

INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "django.contrib.postgres"]  # noqa: F405
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
