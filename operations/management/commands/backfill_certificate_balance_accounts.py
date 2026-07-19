from __future__ import annotations

from django.core.management import BaseCommand, CommandError

from operations.services.certificates import backfill_certificate_balance_accounts


class Command(BaseCommand):
    help = "Safely backfill balance accounts for certificate records."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Create linked accounts. Without this flag the command is dry-run only.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required together with --apply to write changes.",
        )
        parser.add_argument(
            "--allow-existing-issues",
            action="store_true",
            help="Allow apply when preflight reports unrelated existing data issues.",
        )
        parser.add_argument(
            "--certificate-id",
            action="append",
            type=int,
            dest="certificate_ids",
            help="Limit the command to one certificate ID. Can be passed multiple times.",
        )

    def handle(self, *args, **options) -> None:
        is_apply = options["apply"]
        certificate_ids = options["certificate_ids"]

        try:
            result = backfill_certificate_balance_accounts(
                apply=is_apply,
                confirm=options["confirm"],
                allow_existing_issues=options["allow_existing_issues"],
                certificate_ids=certificate_ids,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write("Certificate balance account backfill")
        self.stdout.write(f"Mode: {'APPLY' if result.applied else 'DRY-RUN'}")
        if certificate_ids:
            self.stdout.write("Scoped certificate IDs: " + ", ".join(map(str, certificate_ids)))
        self.stdout.write(f"Total certificates checked: {result.report.total_certificates}")
        self.stdout.write(f"Candidates: {result.candidate_count}")
        if result.candidate_certificate_ids:
            self.stdout.write(
                "Candidate certificate IDs: "
                + ", ".join(map(str, result.candidate_certificate_ids))
            )
        if result.report.has_issues:
            self.stdout.write(
                self.style.WARNING(
                    "Preflight found issues. Apply requires --allow-existing-issues."
                )
            )
        else:
            self.stdout.write("Preflight issues: none")

        if result.applied:
            self.stdout.write(f"Linked accounts: {result.linked_count}")
            if result.linked_account_ids:
                self.stdout.write(
                    "Linked account IDs: " + ", ".join(map(str, result.linked_account_ids))
                )
            self.stdout.write(self.style.SUCCESS("Backfill completed."))
        else:
            self.stdout.write(self.style.WARNING("Dry-run only; no records were changed."))
