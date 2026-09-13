"""Build a synthetic pilot kit from an explicit committed Git tree.

No workspace walk, existing-directory replacement, or database copying is used.
Docker images are prepared separately on the demonstration computer before travel.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import date
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
ROOT_FILES = frozenset(
    {
        ".dockerignore",
        "Dockerfile",
        "README.md",
        "manage.py",
        "pyproject.toml",
        "requirements.txt",
        "START_PILOT.bat",
        "STOP_PILOT.bat",
    }
)
PILOT_FILES = frozenset(
    {
        "pilot/compose.yaml",
        "pilot/Start-Pilot.ps1",
        "pilot/Stop-Pilot.ps1",
        "pilot/README.md",
    }
)
REQUIRED_FILES = (
    ROOT_FILES
    | PILOT_FILES
    | {
        "operations/management/commands/seed_pilot.py",
        "rehab_center/settings.py",
        "rehab_center/settings_pilot.py",
        "rehab_center/wsgi.py",
    }
)
STATIC_SUFFIXES = frozenset(
    {
        ".css",
        ".js",
        ".mjs",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".map",
        ".txt",
        ".md",
        ".webp",
    }
)
WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


class PilotBuildError(ValueError):
    """The requested source or destination is unsuitable for a pilot kit."""


def _git(repository: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise PilotBuildError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def _safe_relative(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not path.is_absolute()
        and all(
            part not in {"", ".", ".."}
            and not part.endswith((" ", "."))
            and not any(char in part for char in '\\:<>"|?*\x00')
            and not any(ord(char) < 32 for char in part)
            and not WINDOWS_RESERVED.match(part)
            for part in name.split("/")
        )
    )


def _included(name: str) -> bool:
    if name in ROOT_FILES or name in PILOT_FILES:
        return True
    path = PurePosixPath(name)
    if any(part.startswith(".") for part in path.parts):
        return False
    if path.parts[0] in {"operations", "rehab_center"}:
        return (
            path.suffix == ".py"
            and "tests" not in path.parts
            and path.name != "tests.py"
            and name
            not in {
                "operations/management/commands/seed_demo.py",
                "rehab_center/settings_viewer.py",
            }
        )
    if path.parts[0] == "templates":
        return path.suffix in {".html", ".txt"}
    return path.parts[0] == "static" and path.suffix.lower() in STATIC_SUFFIXES


def _source_files(repository: Path, commit: str) -> dict[str, tuple[str, str]]:
    selected: dict[str, tuple[str, str]] = {}
    folded: set[str] = set()
    listing = _git(repository, "ls-tree", "-r", "-z", "--full-tree", commit)
    for entry in listing.split(b"\x00"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        name = raw_name.decode("utf-8")
        if not _included(name):
            continue
        if not _safe_relative(name):
            raise PilotBuildError(f"Unsafe package path: {name!r}")
        mode, kind, object_id = metadata.decode("ascii").split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise PilotBuildError(f"Only regular source files are allowed: {name}")
        if name.casefold() in folded:
            raise PilotBuildError(f"Case-insensitive path collision: {name}")
        folded.add(name.casefold())
        selected[name] = (mode, object_id)
    missing = REQUIRED_FILES - selected.keys()
    if missing:
        raise PilotBuildError("Required committed files are missing: " + ", ".join(sorted(missing)))
    return selected


def build_pilot(repository: Path, commit: str, output: Path, training_date: date) -> Path:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise PilotBuildError("Use a full 40-character lowercase Git commit SHA.")
    repository = repository.resolve(strict=True)
    resolved_commit = _git(repository, "rev-parse", "--verify", f"{commit}^{{commit}}")
    if resolved_commit.decode("ascii").strip() != commit:
        raise PilotBuildError("Source must identify a commit, not a tag object.")
    if output.exists() or output.is_symlink():
        raise PilotBuildError("Destination already exists; choose a new empty path.")
    output = output.resolve()
    selected = _source_files(repository, commit)

    # Git archive reads committed blobs. Extraction below copies regular members
    # individually, never tarfile.extractall or files from the working directory.
    with tempfile.TemporaryDirectory(prefix="rm-pilot-source-") as temporary:
        archive_path = Path(temporary) / "source.tar"
        _git(
            repository,
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            "archive",
            "--format=tar",
            f"--output={archive_path}",
            commit,
            "--",
            *sorted(selected),
        )
        with tarfile.open(archive_path, "r:") as archive:
            members = {member.name: member for member in archive if member.name in selected}
            if set(members) != set(selected):
                raise PilotBuildError("Git archive does not contain the selected source file set.")
            if any(not member.isfile() for member in members.values()):
                raise PilotBuildError("A selected archive member is not a regular file.")
            output.mkdir(parents=True, exist_ok=False)
            hashes: dict[str, str] = {}
            for name in sorted(selected):
                target = output.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(members[name])
                if source is None:
                    raise PilotBuildError(f"Cannot read source file: {name}")
                with source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
                content = target.read_bytes()
                mode, expected_blob = selected[name]
                blob_header = f"blob {len(content)}\0".encode("ascii")
                actual_blob = hashlib.sha1(blob_header + content, usedforsecurity=False).hexdigest()
                if actual_blob != expected_blob:
                    raise PilotBuildError(f"Archive content differs from committed blob: {name}")
                target.chmod(0o755 if mode == "100755" else 0o644)
                hashes[name] = hashlib.sha256(content).hexdigest()

    manifest = {
        "format_version": 1,
        "source_commit": commit,
        "project_name": f"rm-pilot-{commit[:12]}",
        "training_date": training_date.isoformat(),
        "application_url": "http://127.0.0.1:18000",
        "file_sha256": hashes,
    }
    manifest_path = output / "pilot-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a separate synthetic pilot kit from Git.")
    parser.add_argument(
        "--commit", required=True, help="Full source commit SHA (not HEAD or a branch)."
    )
    parser.add_argument(
        "--date", required=True, type=date.fromisoformat, help="Synthetic training day: YYYY-MM-DD."
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="New destination directory; never overwritten."
    )
    args = parser.parse_args()
    try:
        manifest_path = build_pilot(ROOT, args.commit, args.output, args.date)
    except (PilotBuildError, OSError, tarfile.TarError) as error:
        parser.exit(1, f"Pilot kit was not completed: {error}\n")
    print(f"Pilot kit created: {manifest_path.parent}")
    print(f"Source commit: {args.commit}")
    print(
        "Read pilot/README.md and prepare Docker images on the demonstration computer before travel."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
