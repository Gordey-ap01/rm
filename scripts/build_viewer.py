#!/usr/bin/env python
"""Build RMcodex Viewer portable EXE using PyInstaller.

Usage:
    python scripts/build_viewer.py              # Lite (SQLite, ~150 MB)
    python scripts/build_viewer.py --with-pg     # Full (PostgreSQL bundled, ~1 GB)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DIST_BASE = PROJECT / "dist"
DIST_DEFAULT = DIST_BASE / "RMcodex-viewer"
BUILD = PROJECT / "build" / "pyinstaller"

VENV_PY = PROJECT / ".venv-test" / "Scripts" / "python.exe"
if not VENV_PY.exists():
    VENV_PY = Path(sys.executable)

build_parser = argparse.ArgumentParser(description="Build RMcodex Viewer EXE")
build_parser.add_argument("--with-pg", action="store_true", help="Bundle portable PostgreSQL (~800 MB extra)")
build_args = build_parser.parse_args()


def _bundle_postgresql(dist: Path) -> None:
    """Download and bundle portable PostgreSQL, stripped to minimum."""
    pg_dir = dist / "RMcodex" / "_internal" / "pgsql"
    pg_url = "https://get.enterprisedb.com/postgresql/postgresql-16.4-1-windows-x64-binaries.zip"
    pg_zip = PROJECT / "postgresql-portable.zip"

    import urllib.request
    import zipfile

    print("  Downloading PostgreSQL ...")
    urllib.request.urlretrieve(pg_url, pg_zip)
    print("  Extracting (this may take a while) ...")

    with zipfile.ZipFile(pg_zip, "r") as zf:
        zf.extractall(str(pg_dir))

    pg_zip.unlink()

    # Move nested dir up
    items = list(pg_dir.iterdir())
    if len(items) == 1 and items[0].is_dir():
        nested = items[0]
        for child in sorted(nested.iterdir(), key=lambda p: p.name):
            shutil.move(str(child), str(pg_dir / child.name))
        nested.rmdir()

    # Strip unnecessary files — keep only runtime
    remove_dirs = ["pgAdmin 4", "doc", "include", "symbols", "StackBuilder"]
    for d in remove_dirs:
        target = pg_dir / d
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            print(f"  Removed {d}/")

    # Strip share/ — keep only essential (timezone, errors)
    share = pg_dir / "share"
    if share.exists():
        keep_extensions = {".dat", ".txt"}
        keep_names = {"timezones", "errcodes", "locales"}
        for f in share.rglob("*"):
            if f.is_file() and f.suffix not in keep_extensions:
                try:
                    f.unlink()
                except OSError:
                    pass
        print(f"  Stripped share/ to essential only")

    print(f"  PostgreSQL bundled: {pg_dir}")


def main() -> int:
    print("=== RMcodex Viewer Builder ===")

    # Step 1 — collectstatic
    print("[1/5] Collecting static files ...")
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "rehab_center.settings_viewer"
    env["VIEWER_DB"] = "sqlite"
    subprocess.run(
        [str(VENV_PY), "manage.py", "collectstatic", "--no-input", "--clear"],
        cwd=str(PROJECT), check=True, env=env,
    )

    # Step 2 — clean
    print("[2/5] Cleaning previous build ...")
    DIST = DIST_DEFAULT
    for p in [DIST_DEFAULT, BUILD]:
        if p.exists():
            try:
                shutil.rmtree(p)
            except PermissionError:
                print(f"  WARNING: {p} locked, building to alternative dir")
                alt = p.parent / (p.name + "-new")
                alt.mkdir(parents=True, exist_ok=True)
                if p == DIST_DEFAULT:
                    DIST = alt

    spec_file = PROJECT / "RMcodex-viewer.spec"
    if spec_file.exists():
        try:
            spec_file.unlink()
        except PermissionError:
            pass

    # Step 3 — PyInstaller
    print("[3/5] Building with PyInstaller ...")
    pyinstaller = str(VENV_PY).rsplit("\\", 1)[0] + "\\pyinstaller.exe"
    if not Path(pyinstaller).exists():
        pyinstaller = "pyinstaller"

    pyi_args = [
        str(VENV_PY), "-m", "PyInstaller",
        str(PROJECT / "scripts" / "launcher.py"),
        "--name=RMcodex",
        "--onedir",
        "--noconfirm",
        "--log-level=WARN",
        f"--distpath={DIST}",
        f"--workpath={BUILD}",
    ]

    # Data files
    data_dirs = ["templates", "staticfiles", "static", "rehab_center", "operations"]
    for d in data_dirs:
        pyi_args.append(f"--add-data={d};{d}")
    pyi_args.append("--add-data=manage.py;.")

    # Hidden imports
    hidden = [
        "ninja", "django_htmx", "whitenoise", "reportlab", "django_tasks",
        "psycopg", "psycopg.types", "asgiref", "sqlparse",
    ]
    for h in hidden:
        pyi_args.append(f"--hidden-import={h}")

    # Collect submodules
    collect = [
        "operations.migrations",
        "django.core.management",
        "django.contrib.admin.management",
        "django.contrib.auth.management",
    ]
    for c in collect:
        pyi_args.append(f"--collect-submodules={c}")
    pyi_args.append("--collect-data=operations")
    pyi_args.append("--collect-all=django_tasks")
    pyi_args.append("--collect-all=django_htmx")
    pyi_args.append("--collect-all=whitenoise")
    pyi_args.append("--collect-all=auditlog")
    pyi_args.append("--collect-all=reportlab")

    subprocess.run(pyi_args, cwd=str(PROJECT), check=True)

    # Step 4 — PostgreSQL (optional)
    if build_args.with_pg:
        print("[4/5] Bundling PostgreSQL ...")
        try:
            _bundle_postgresql(DIST)
        except Exception as e:
            print(f"  WARNING: PostgreSQL bundling failed: {e}")
    else:
        print("[4/5] Skipping PostgreSQL (use --with-pg to bundle)")

    # Step 5 — done
    print("[5/5] Build complete!")
    exe = DIST / "RMcodex" / "RMcodex.exe"
    launcher_bat = DIST / "START_RM.bat"
    launcher_bat.write_text(
        "\n".join(
            [
                "@echo off",
                "chcp 65001 >nul",
                'cd /d "%~dp0RMcodex"',
                'start "" "RMcodex.exe"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    if exe.exists():
        size_mb = sum(f.stat().st_size for f in (DIST / "RMcodex").rglob("*")) / 1_000_000
        count = len(list((DIST / "RMcodex").rglob("*")))
        print(f"  EXE: {exe}")
        print(f"  Launcher: {launcher_bat}")
        print(f"  Size: ~{size_mb:.0f} MB, {count} files")
    else:
        print("  ERROR: EXE not found!")

    return 0


if __name__ == "__main__":
    sys.exit(main())
