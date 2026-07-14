from __future__ import annotations

from django.core.management import BaseCommand

from operations.models import FinancialIntegrityCheckRun
from operations.services.financial_integrity_checks import run_financial_integrity_check


class Command(BaseCommand):
    help = "Run financial integrity audit and persist check findings."

    def add_arguments(self, parser):
        parser.add_argument(
            "--run-type",
            choices=FinancialIntegrityCheckRun.RunType.values,
            default=FinancialIntegrityCheckRun.RunType.MANAGEMENT_COMMAND,
            help="Check run type saved with the audit run.",
        )

    def handle(self, *args, **options):
        run = run_financial_integrity_check(run_type=options["run_type"])
        self.stdout.write(
            self.style.SUCCESS(
                "Financial integrity check "
                f"#{run.pk} completed: "
                f"{run.issue_count} issues "
                f"({run.error_count} errors, "
                f"{run.warning_count} warnings, "
                f"{run.info_count} info), "
                f"{run.candidate_count} appointments checked."
            )
        )
