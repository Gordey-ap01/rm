#!/usr/bin/env python3
"""Durably publish backup directories mounted below /backup-host."""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

BACKUP_ROOT = Path("/backup-host")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def safe_path(relative: str) -> Path:
    candidate = (BACKUP_ROOT / relative).resolve()
    if candidate == BACKUP_ROOT or BACKUP_ROOT not in candidate.parents:
        raise RuntimeError("Unsafe backup fsync path.")
    return candidate


def fsync_tree(path: Path) -> None:
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError("Backup staging directory is missing or unsafe.")
    directories: list[Path] = []
    for current_root, directory_names, file_names in os.walk(path):
        current = Path(current_root)
        directories.append(current)
        for name in directory_names:
            if (current / name).is_symlink():
                raise RuntimeError("Backup staging tree contains a symlink.")
        for name in file_names:
            file_path = current / name
            if not stat.S_ISREG(file_path.lstat().st_mode):
                raise RuntimeError("Backup staging tree contains a special file.")
            with file_path.open("rb") as handle:
                os.fsync(handle.fileno())
    for directory in reversed(directories):
        fsync_directory(directory)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("tree", "root", "capacity"))
    parser.add_argument("relative", nargs="?", default="")
    args = parser.parse_args()
    if args.mode == "tree":
        fsync_tree(safe_path(args.relative))
    elif args.mode == "root":
        if args.relative:
            raise RuntimeError("Root fsync does not accept a relative path.")
        fsync_directory(BACKUP_ROOT)
    else:
        if args.relative:
            raise RuntimeError("Capacity check does not accept a relative path.")
        stats = os.statvfs(BACKUP_ROOT)
        print(f"{stats.f_bavail * stats.f_frsize}:{stats.f_favail}")


if __name__ == "__main__":
    main()
