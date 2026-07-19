from __future__ import annotations

from django.core.management import BaseCommand

from operations.services.certificates import (
    CERTIFICATE_BALANCE_PREFLIGHT_LABELS,
    certificate_balance_preflight_report,
)


class Command(BaseCommand):
    help = "Read-only preflight audit before certificate balance-account backfill."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--sample-limit",
            type=int,
            default=20,
            help="Maximum certificate IDs or duplicate groups shown per issue.",
        )
        parser.add_argument(
            "--details",
            action="store_true",
            help="Show sample certificate IDs and duplicate number groups.",
        )

    def handle(self, *args, **options) -> None:
        report = certificate_balance_preflight_report(sample_limit=options["sample_limit"])

        self.stdout.write("Certificate balance backfill preflight")
        self.stdout.write(f"Total certificates: {report.total_certificates}")
        self.stdout.write(f"Linked certificates: {report.linked_certificates}")
        self.stdout.write(f"Unlinked certificates: {report.unlinked_certificates}")
        self.stdout.write(f"Backfill candidates with positive balance: {report.backfill_candidates}")
        self.stdout.write(
            f"Zero-balance certificates without account: {report.zero_balance_without_account}"
        )
        self.stdout.write("")

        issue_rows = report.issue_rows()
        if issue_rows:
            self.stdout.write("Issues:")
            for code, label, count, samples in issue_rows:
                self.stdout.write(f"- {code}: {count} ({label})")
                if options["details"] and samples:
                    self.stdout.write(f"  sample certificate IDs: {', '.join(map(str, samples))}")
        else:
            self.stdout.write("Issues: none")

        if report.duplicate_number_groups:
            self.stdout.write(
                "Duplicate certificate numbers: "
                f"{report.duplicate_number_groups} groups, "
                f"{report.duplicate_number_certificate_count} certificates"
            )
            if options["details"]:
                for group in report.duplicate_number_samples:
                    self.stdout.write(
                        "  child_id={child_id}; number={number}; count={count}".format(
                            child_id=group["child_id"],
                            number=group["number"],
                            count=group["count"],
                        )
                    )
        else:
            self.stdout.write("Duplicate certificate numbers: none")

        self.stdout.write("")
        if report.has_issues:
            self.stdout.write(
                self.style.WARNING(
                    "Read-only preflight completed with issues; no records were changed."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS("Read-only preflight completed; no records were changed.")
            )

        if options.get("verbosity", 1) > 1:
            self.stdout.write(
                "Tracked issue codes: "
                + ", ".join(CERTIFICATE_BALANCE_PREFLIGHT_LABELS.keys())
            )
