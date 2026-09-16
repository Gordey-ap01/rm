"""Explicit worker for durable confirmation delivery; never starts from a web request."""

import json
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connection
from django.db.models import Count

from operations.models import ConfirmationEmailDelivery
from operations.services.confirmation_email_outbox import LEASE_SECONDS, process_due


class Command(BaseCommand):
    help = "Send due confirmation emails, or inspect counts with --status (no email contents)."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--once", action="store_true", help="Process one bounded batch")
        mode.add_argument("--loop", action="store_true", help="Run until stopped by the supervisor")
        mode.add_argument("--status", action="store_true", help="Read counts only; never send")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--interval", type=int, default=10)

    def handle(self, *args, **options):
        if options["status"]:
            counts = dict(
                ConfirmationEmailDelivery.objects.values_list("status").annotate(total=Count("pk"))
            )
            self.stdout.write(json.dumps(counts, sort_keys=True))
            return
        if not 1 <= options["limit"] <= 1000 or not 1 <= options["interval"] <= 60:
            raise CommandError("Use limit 1–1000 and interval 1–60 seconds.")
        if connection.vendor != "postgresql":
            raise CommandError("The delivery worker requires PostgreSQL row locks.")
        if not 1 <= (settings.EMAIL_TIMEOUT or 0) < LEASE_SECONDS:
            raise CommandError("EMAIL_TIMEOUT must be 1–299 seconds.")
        if settings.EMAIL_BACKEND not in {
            "django.core.mail.backends.smtp.EmailBackend",
            "django.core.mail.backends.locmem.EmailBackend",
        }:
            raise CommandError(
                "Configure SMTP before starting the worker; console/file/dummy delivery is disabled."
            )
        marker = Path(settings.PRIVATE_ARTIFACT_ROOT) / ".restore-in-progress"
        try:
            while True:
                # A restored queue must not start delivering while restore validation runs.
                if marker.exists():
                    raise CommandError("Restore marker is present; delivery is blocked.")
                result = process_due(limit=options["limit"])
                if options["once"] or result["processed"]:
                    self.stdout.write(json.dumps(result, sort_keys=True))
                if options["once"]:
                    return
                close_old_connections()
                time.sleep(options["interval"])
        except KeyboardInterrupt:
            return
