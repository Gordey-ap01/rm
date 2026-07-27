from __future__ import annotations

from collections import Counter

from django.core.management import BaseCommand, CommandError

from operations.services.grant_compensation import (
    grant_compensation_integrity_findings,
)
from operations.services.grant_plans import grant_plan_integrity_findings


class Command(BaseCommand):
    help = (
        "Read-only integrity check for grant plan, payroll-budget, fixed-compensation "
        "roots, revisions, periods, and payroll."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Return a non-zero exit status when findings exist.",
        )
        parser.add_argument(
            "--sample-limit",
            type=int,
            default=20,
            help="Maximum detailed findings shown.",
        )

    def handle(self, *args, **options) -> None:
        findings = [
            *grant_plan_integrity_findings(),
            *grant_compensation_integrity_findings(),
        ]
        counts = Counter(finding.code for finding in findings)
        self.stdout.write("Grant plan integrity check")
        self.stdout.write(f"Findings: {len(findings)}")
        for code, count in sorted(counts.items()):
            self.stdout.write(f"- {code}: {count}")
        for finding in findings[: max(options["sample_limit"], 0)]:
            self.stdout.write(
                f"  {finding.object_kind}#{finding.object_id}: "
                f"{finding.code}: {finding.detail}"
            )

        if findings:
            message = "Grant plan integrity check completed with findings."
            if options["strict"]:
                raise CommandError(message)
            self.stdout.write(self.style.WARNING(message))
        else:
            self.stdout.write(
                self.style.SUCCESS("Grant plan integrity check completed without findings.")
            )
