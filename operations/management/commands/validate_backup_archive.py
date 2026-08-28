from __future__ import annotations

import tarfile
from pathlib import PurePosixPath

from django.core.management.base import BaseCommand, CommandError

MAX_ARCHIVE_MEMBERS = 100_000
DEFAULT_MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024 * 1024


class Command(BaseCommand):
    help = "Validate a backup tar archive without extracting it."

    def add_arguments(self, parser):
        parser.add_argument("archive")
        parser.add_argument("--root", required=True)
        parser.add_argument(
            "--max-uncompressed-bytes",
            type=int,
            default=DEFAULT_MAX_UNCOMPRESSED_BYTES,
        )
        parser.add_argument("--print-total-bytes", action="store_true")
        parser.add_argument("--print-capacity", action="store_true")

    def handle(self, *args, **options):
        expected_root = options["root"]
        max_uncompressed_bytes = options["max_uncompressed_bytes"]
        if options["print_total_bytes"] and options["print_capacity"]:
            raise CommandError("Choose only one archive output mode.")
        if max_uncompressed_bytes < 1:
            raise CommandError("Archive size limit must be positive.")
        seen = set()
        seen_casefold = set()
        total_bytes = 0
        try:
            with tarfile.open(options["archive"], mode="r:gz") as archive:
                members = archive.getmembers()
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    raise CommandError("Backup archive has too many members.")
                for member in members:
                    path = PurePosixPath(member.name)
                    if (
                        path.is_absolute()
                        or not path.parts
                        or path.parts[0] != expected_root
                        or ".." in path.parts
                    ):
                        raise CommandError("Backup archive contains an unsafe path.")
                    normalized = path.as_posix()
                    casefolded = normalized.casefold()
                    if normalized in seen or casefolded in seen_casefold:
                        raise CommandError("Backup archive contains duplicate paths.")
                    seen.add(normalized)
                    seen_casefold.add(casefolded)
                    if not (member.isfile() or member.isdir()):
                        raise CommandError(
                            "Backup archive contains a link or special file."
                        )
                    if member.isfile():
                        total_bytes += member.size
                        if total_bytes > max_uncompressed_bytes:
                            raise CommandError(
                                "Backup archive exceeds the uncompressed size limit."
                            )
        except (OSError, tarfile.TarError) as exc:
            raise CommandError("Backup archive cannot be read safely.") from exc
        if options["print_capacity"]:
            self.stdout.write(f"{total_bytes}:{len(seen)}")
        elif options["print_total_bytes"]:
            self.stdout.write(str(total_bytes))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "Backup archive validated: "
                    f"root={expected_root}, members={len(seen)}, bytes={total_bytes}."
                )
            )
