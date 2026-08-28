from __future__ import annotations

import os
import re
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "DJANGO_CSRF_TRUSTED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if origin.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "operations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "auditlog.middleware.AuditlogMiddleware",
]

ROOT_URLCONF = "rehab_center.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "operations.context_processors.authority_flags",
            ],
        },
    },
]

WSGI_APPLICATION = "rehab_center.wsgi.application"

DATABASE_HOST = os.environ.get("DATABASE_HOST")
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_HOST:
    required_database_values = {
        "NAME": os.environ.get("DATABASE_NAME", ""),
        "USER": os.environ.get("DATABASE_USER", ""),
        "PASSWORD": os.environ.get("DATABASE_PASSWORD", ""),
    }
    missing_database_values = [
        name for name, value in required_database_values.items() if not value
    ]
    if missing_database_values:
        raise ImproperlyConfigured(
            "Missing database settings: " + ", ".join(missing_database_values)
        )
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            **required_database_values,
            "HOST": DATABASE_HOST,
            "PORT": os.environ.get("DATABASE_PORT", "5432"),
            "CONN_MAX_AGE": 600,
        }
    }
elif DATABASE_URL:
    DATABASES = {"default": dj_database_url.config(default=DATABASE_URL, conn_max_age=600)}
else:
    _data_dir = BASE_DIR / "data"
    _data_dir.mkdir(parents=True, exist_ok=True)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(_data_dir / "rehab.sqlite3"),
            "OPTIONS": {
                "init_command": "PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;",
                "transaction_mode": "EXCLUSIVE",
            },
        }
    }

restore_database_name = os.environ.get("RESTORE_DATABASE_NAME_OVERRIDE")
if restore_database_name:
    if re.fullmatch(r"rm_restore_stage_\d{14}_\d+", restore_database_name) is None:
        raise ImproperlyConfigured("Invalid restore database name override.")
    if not DATABASES["default"]["ENGINE"].endswith("postgresql"):
        raise ImproperlyConfigured("Restore database override requires PostgreSQL.")
    DATABASES["default"]["NAME"] = restore_database_name

_IS_POSTGRES = DATABASES["default"].get("ENGINE", "").endswith("postgresql")
if _IS_POSTGRES:
    INSTALLED_APPS.append("django.contrib.postgres")

INSTALLED_APPS += ["django_tasks", "django_tasks.backends.database", "auditlog", "django_htmx"]

AUDITLOG_INCLUDE_TRACKING_MODELS: list = []
AUDITLOG_EXCLUDE_TRACKING_MODELS = ()

TASKS = {
    "default": {
        "BACKEND": "django_tasks.backends.database.DatabaseBackend",
    },
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Asia/Vladivostok"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
if not DEBUG:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
    }
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
PRIVATE_ARTIFACT_ROOT = Path(
    os.environ.get("PRIVATE_ARTIFACT_ROOT", BASE_DIR / "private-artifacts")
).resolve()
DONOR_REPORT_SUBMISSIONS_ENABLED = (
    os.environ.get("DONOR_REPORT_SUBMISSIONS_ENABLED", "0") == "1"
)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

CSRF_FAILURE_VIEW = "operations.views.csrf_failure"

EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "rehab-center@example.local")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1") == "1"
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")

if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "0") == "1"
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "0"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = (
        os.environ.get("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", "0") == "1"
    )
    SECURE_HSTS_PRELOAD = os.environ.get("DJANGO_SECURE_HSTS_PRELOAD", "0") == "1"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
