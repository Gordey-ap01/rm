"""Exercise pilot export against real, disposable Git repositories."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date

import pytest

from scripts.build_pilot import REQUIRED_FILES, PilotBuildError, build_pilot

TRAINING_DAY = date(2026, 9, 21)


def git(repository, *args, input_bytes=None):
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        input=input_bytes,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8").strip()


def commit(repository):
    git(repository, "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic pilot source")
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def source_repository(tmp_path):
    repository = tmp_path / "source"
    repository.mkdir()
    git(repository, "init", "-q")
    git(repository, "config", "user.name", "Pilot test")
    git(repository, "config", "user.email", "pilot-test@example.invalid")
    git(repository, "config", "core.autocrlf", "false")
    git(repository, "config", "core.hooksPath", str(repository / ".git" / "disabled-hooks"))
    for name in sorted(REQUIRED_FILES):
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"Synthetic committed file: {name}\n".encode())
    extras = {
        "static/app.js": b"const synthetic = true;\n",
        "templates/operations/pilot.html": b"<h1>Synthetic pilot</h1>\n",
        "operations/models.py": b"# Synthetic models\n",
        ".env": b"NOT_A_REAL_SECRET=must_not_be_exported\n",
        ".runtime/private.log": b"Do not export local diagnostics\n",
        "data/rehab.sqlite3": b"Not a real database\n",
        "private-artifacts/example.txt": b"Not a real private document\n",
        "operations/tests/test_example.py": b"# Do not ship tests\n",
        "operations/tests.py": b"# Do not ship tests\n",
        "operations/management/commands/seed_demo.py": b"# Old demo command\n",
        "rehab_center/settings_viewer.py": b"# Old viewer\n",
        "pilot/.pilot-local.env": b"NOT_A_REAL_SECRET=generated_elsewhere\n",
        "scripts/launcher.py": b"# Old viewer launcher\n",
        "interview.txt": b"Not part of the application\n",
        "static/database.dump": b"Not a public asset\n",
    }
    for name, content in extras.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    git(repository, "add", "--all", "--force")
    return repository, commit(repository)


@pytest.mark.parametrize("autocrlf", ["false", "true"])
def test_export_uses_committed_bytes_and_excludes_private_or_untracked_files(
    source_repository, tmp_path, autocrlf
):
    repository, source_commit = source_repository
    git(repository, "config", "core.autocrlf", autocrlf)
    (repository / "operations/models.py").write_text("UNCOMMITTED WORK", encoding="utf-8")
    (repository / "static/untracked.png").write_bytes(b"UNTRACKED")
    output = tmp_path / "kit"
    manifest_path = build_pilot(repository, source_commit, output, TRAINING_DAY)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["source_commit"] == source_commit
    assert manifest["training_date"] == "2026-09-21"
    assert manifest["project_name"] == f"rm-pilot-{source_commit[:12]}"
    assert manifest["application_url"] == "http://127.0.0.1:18000"
    expected = set(REQUIRED_FILES) | {
        "static/app.js",
        "templates/operations/pilot.html",
        "operations/models.py",
    }
    actual = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
    assert actual == expected | {"pilot-manifest.json"}
    assert set(manifest["file_sha256"]) == expected
    for name in expected:
        content = (output / name).read_bytes()
        assert manifest["file_sha256"][name] == hashlib.sha256(content).hexdigest()
        original = subprocess.run(
            ["git", "-C", str(repository), "show", f"{source_commit}:{name}"],
            capture_output=True,
            check=True,
        ).stdout
        assert content == original
    assert (repository / "operations/models.py").read_text(encoding="utf-8") == "UNCOMMITTED WORK"


def test_existing_destination_is_preserved(source_repository, tmp_path):
    repository, source_commit = source_repository
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "KEEP.txt"
    sentinel.write_text("Keep my training data", encoding="utf-8")
    with pytest.raises(PilotBuildError, match="already exists"):
        build_pilot(repository, source_commit, output, TRAINING_DAY)
    assert sentinel.read_text(encoding="utf-8") == "Keep my training data"
    assert list(output.iterdir()) == [sentinel]


def test_missing_committed_launcher_refuses_before_creating_output(source_repository, tmp_path):
    repository, _source_commit = source_repository
    git(repository, "rm", "pilot/Start-Pilot.ps1")
    incomplete_commit = commit(repository)
    output = tmp_path / "incomplete"
    with pytest.raises(PilotBuildError, match="Required committed files"):
        build_pilot(repository, incomplete_commit, output, TRAINING_DAY)
    assert not output.exists()


def test_tracked_symbolic_link_is_rejected_without_extracting_it(source_repository, tmp_path):
    repository, _source_commit = source_repository
    blob = git(repository, "hash-object", "-w", "--stdin", input_bytes=b"../../outside")
    git(repository, "update-index", "--add", "--cacheinfo", f"120000,{blob},static/link.js")
    linked_commit = commit(repository)
    output = tmp_path / "linked"
    with pytest.raises(PilotBuildError, match="Only regular source files"):
        build_pilot(repository, linked_commit, output, TRAINING_DAY)
    assert not output.exists()


def test_case_collisions_are_rejected_before_creating_output(source_repository, tmp_path):
    repository, _source_commit = source_repository
    blob = git(repository, "hash-object", "-w", "--stdin", input_bytes=b"another asset")
    git(
        repository,
        "-c",
        "core.ignoreCase=false",
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{blob},static/App.js",
    )
    collision_commit = commit(repository)
    output = tmp_path / "collision"
    with pytest.raises(PilotBuildError, match="path collision"):
        build_pilot(repository, collision_commit, output, TRAINING_DAY)
    assert not output.exists()


@pytest.mark.parametrize("source_ref", ["HEAD", "--help", "1234567", "a" * 39, "a" * 41])
def test_requires_an_explicit_full_commit(source_repository, tmp_path, source_ref):
    repository, _source_commit = source_repository
    output = tmp_path / "invalid-ref"
    with pytest.raises(PilotBuildError, match="full 40-character"):
        build_pilot(repository, source_ref, output, TRAINING_DAY)
    assert not output.exists()
