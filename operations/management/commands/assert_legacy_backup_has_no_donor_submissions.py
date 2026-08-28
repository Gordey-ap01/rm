from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

SUBMISSION_TABLE = "operations_donorreportsubmission"


class Command(BaseCommand):
    help = "Fail when a legacy v1 backup references private donor files."

    def handle(self, *args, **options):
        tables = set(connection.introspection.table_names())
        if SUBMISSION_TABLE not in tables:
            self.stdout.write("Legacy backup predates donor submissions.")
            return
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT 1 FROM {SUBMISSION_TABLE} LIMIT 1")
            has_rows = cursor.fetchone() is not None
        if has_rows:
            raise CommandError(
                "v1 backup contains donor submissions without private files."
            )
        self.stdout.write("Legacy backup has no donor submissions.")
