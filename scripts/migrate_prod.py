#!/usr/bin/env python3
"""Run production migrations while holding the shared host maintenance lock."""

from __future__ import annotations

import fcntl
import os
import pwd
import subprocess
import sys
from pathlib import Path

LOCK_PATH = Path("/app/.runtime/production-maintenance.lock")
MAINTENANCE_TRACES = (
    Path("/app/media/.restore-new"),
    Path("/app/media/.restore-old"),
    Path("/app/media/.restore-old-preparing"),
    Path("/app/media/.restore-discard"),
    Path("/app/private-artifacts/.restore-in-progress"),
    Path("/app/private-artifacts/.restore-in-progress.tmp"),
    Path("/app/private-artifacts/.restore-new"),
    Path("/app/private-artifacts/.restore-old"),
    Path("/app/private-artifacts/.restore-old-preparing"),
    Path("/app/private-artifacts/.restore-discard"),
    Path("/app/private-artifacts/.backup-in-progress"),
    Path("/app/private-artifacts/.backup-in-progress.tmp"),
)


def drop_to_application_user() -> None:
    if os.geteuid() != 0:
        return
    account = pwd.getpwnam("rehab")
    os.setgroups([])
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)


def main() -> None:
    LOCK_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o666)
    os.chmod(LOCK_PATH, 0o666)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit(
                "Production migration blocked by an active backup or restore."
            ) from exc
        if any(path.exists() for path in MAINTENANCE_TRACES):
            raise SystemExit(
                "Production migration blocked by incomplete backup or restore state."
            )
        drop_to_application_user()
        subprocess.run(
            [sys.executable, "manage.py", "migrate", "--noinput"],
            check=True,
        )
        subprocess.run(
            [sys.executable, "manage.py", "configure_runtime_database_role"],
            check=True,
        )
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    main()
