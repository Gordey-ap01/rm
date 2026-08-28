from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from operations.models import DonorReportSubmission
from operations.services import private_artifacts

TRANSIENT_GRACE_SECONDS = 5 * 60


class Command(BaseCommand):
    help = "Read-only integrity audit for private donor-report submission files."

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Return a non-zero exit code when any finding exists.",
        )
        parser.add_argument(
            "--quiescent",
            action="store_true",
            help="Treat every orphan or staging file as an error; use when writers are stopped.",
        )

    def handle(self, *args, **options):
        expected_keys = set()
        finding_counts: dict[str, int] = {}

        def finding(code: str) -> None:
            finding_counts[code] = finding_counts.get(code, 0) + 1

        for submission in DonorReportSubmission.objects.order_by("pk").iterator():
            expected_keys.add(submission.storage_key)
            try:
                private_artifacts.read_verified_artifact(
                    storage_key=submission.storage_key,
                    expected_size=submission.file_size,
                    expected_sha256=submission.file_sha256,
                    expected_content_type=submission.content_type,
                )
            except private_artifacts.ArtifactIntegrityError as exc:
                message = str(exc).lower()
                if "missing" in message:
                    finding("missing_object")
                elif "size" in message:
                    finding("size_mismatch")
                elif "sha-256" in message:
                    finding("hash_mismatch")
                elif "mime" in message or "content" in message:
                    finding("mime_mismatch")
                else:
                    finding("unsafe_storage_key")

        actual_keys = private_artifacts.iter_final_storage_keys()
        now = time.time()
        for storage_key in actual_keys - expected_keys:
            try:
                path = private_artifacts.resolve_storage_key(storage_key)
                old_enough = now - path.stat().st_mtime >= TRANSIENT_GRACE_SECONDS
            except (OSError, private_artifacts.ArtifactIntegrityError):
                old_enough = True
            if options["quiescent"] or old_enough:
                finding("orphan_object")

        for path in private_artifacts.iter_staging_paths():
            try:
                old_enough = now - path.stat().st_mtime >= TRANSIENT_GRACE_SECONDS
            except OSError:
                old_enough = True
            if options["quiescent"] or old_enough:
                finding("stale_staging_object")

        if finding_counts:
            summary = ", ".join(
                f"{code}={count}" for code, count in sorted(finding_counts.items())
            )
            self.stdout.write(f"Donor submission integrity findings: {summary}")
            if options["strict"]:
                raise CommandError("Donor submission integrity audit failed.")
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"Donor submission integrity passed: {len(expected_keys)} files."
            )
        )
