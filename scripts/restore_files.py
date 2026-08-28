#!/usr/bin/env python3
"""Durable, fail-closed filesystem transitions for production restore."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
from pathlib import Path

ALLOWED_ROOTS = {Path("/app/media"), Path("/app/private-artifacts")}
STATE_PATH = Path("/app/private-artifacts/.restore-in-progress")
BACKUP_STATE_PATH = Path("/app/private-artifacts/.backup-in-progress")
OLD_PREPARING_NAME = ".restore-old-preparing"
RESTORE_NAMES = {
    ".restore-in-progress",
    ".restore-in-progress.tmp",
    ".restore-new",
    ".restore-old",
    ".restore-discard",
    OLD_PREPARING_NAME,
}


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    if not root.is_dir():
        raise RuntimeError(f"Restore tree is missing: {root}")
    directories: list[Path] = []
    for current_root, directory_names, file_names in os.walk(root):
        current = Path(current_root)
        directories.append(current)
        for name in directory_names:
            path = current / name
            if path.is_symlink():
                raise RuntimeError(f"Restore tree contains a symlink: {path}")
        for name in file_names:
            path = current / name
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise RuntimeError(f"Restore tree contains a special file: {path}")
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    for directory in reversed(directories):
        _fsync_directory(directory)


def _durable_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _durable_rmtree(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"Refusing to remove unsafe restore path: {path}")
    shutil.rmtree(path)
    _fsync_directory(path.parent)


def _durable_touch(path: Path) -> None:
    with path.open("xb") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _durable_replace(source: Path, target: Path) -> None:
    os.replace(source, target)
    # Persist the new name before the old name disappears. A crash between
    # these fsync calls can leave a duplicate, which rollback handles safely.
    _fsync_directory(target.parent)
    if target.parent != source.parent:
        _fsync_directory(source.parent)


def _entries_match(left: Path, right: Path) -> bool:
    left_mode = left.lstat().st_mode
    right_mode = right.lstat().st_mode
    if stat.S_IFMT(left_mode) != stat.S_IFMT(right_mode):
        return False
    if stat.S_ISREG(left_mode):
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            while True:
                left_chunk = left_handle.read(1024 * 1024)
                right_chunk = right_handle.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    if stat.S_ISLNK(left_mode):
        return os.readlink(left) == os.readlink(right)
    if stat.S_ISDIR(left_mode):
        left_names = sorted(path.name for path in left.iterdir())
        right_names = sorted(path.name for path in right.iterdir())
        return left_names == right_names and all(
            _entries_match(left / name, right / name) for name in left_names
        )
    return False


def _durable_remove_entry(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
    _fsync_directory(path.parent)


def _root(value: str) -> Path:
    path = Path(value)
    if path not in ALLOWED_ROOTS or not path.is_dir() or path.is_symlink():
        raise RuntimeError(f"Unsafe production restore root: {value}")
    return path


def _live_entries(root: Path) -> list[Path]:
    entries: list[Path] = []
    for path in root.iterdir():
        if path.name in RESTORE_NAMES:
            continue
        if path.name.startswith(".restore-"):
            raise RuntimeError(f"Unknown restore control entry requires inspection: {path}")
        entries.append(path)
    return sorted(entries, key=lambda path: path.name)


def _copy_with_hardlinks(source: Path, target: Path) -> None:
    mode = source.lstat().st_mode
    if stat.S_ISREG(mode):
        os.link(source, target)
        return
    if stat.S_ISDIR(mode):
        target.mkdir(mode=stat.S_IMODE(mode))
        for child in sorted(source.iterdir(), key=lambda path: path.name):
            if child.name.startswith(".capture-"):
                continue
            _copy_with_hardlinks(child, target / child.name)
        _fsync_directory(target)
        return
    if stat.S_ISLNK(mode):
        os.symlink(os.readlink(source), target)
        return
    raise RuntimeError(f"Cannot preserve special file during rollback: {source}")


def switch_root(
    root_value: str,
    new_relative: str,
    inject_after_old_prepare: bool = False,
    inject_after_old_publish: bool = False,
) -> None:
    root = _root(root_value)
    new = root / new_relative
    old = root / ".restore-old"
    old_preparing = root / OLD_PREPARING_NAME
    discard = root / ".restore-discard"
    if old.exists() or old_preparing.exists() or discard.exists():
        raise RuntimeError(f"Unresolved restore history exists under {root}")
    if new.parent != root / ".restore-new":
        raise RuntimeError("Restore candidate must be directly below .restore-new")
    live_entries = _live_entries(root)
    _fsync_tree(new)
    old_preparing.mkdir(mode=0o700)
    started_preparing = old_preparing / ".capture-started"
    _durable_touch(started_preparing)
    if inject_after_old_prepare:
        raise RuntimeError("Injected failure after rollback directory preparation")
    _durable_replace(old_preparing, old)
    if inject_after_old_publish:
        raise RuntimeError("Injected failure after rollback directory publication")
    started = old / ".capture-started"
    complete = old / ".capture-complete"
    for entry in live_entries:
        _durable_replace(entry, old / entry.name)
    _durable_replace(started, complete)
    for entry in sorted(new.iterdir(), key=lambda path: path.name):
        _durable_replace(entry, root / entry.name)
    _fsync_directory(root)


def _discard_old(root: Path, old: Path, discard: Path) -> None:
    if old.exists() and discard.exists():
        raise RuntimeError(f"Conflicting restore history exists under {root}")
    if old.exists():
        _durable_replace(old, discard)
    _durable_rmtree(discard)


def rollback_root(root_value: str, inject_after_copy: bool) -> None:
    root = _root(root_value)
    new = root / ".restore-new"
    old = root / ".restore-old"
    old_preparing = root / OLD_PREPARING_NAME
    discard = root / ".restore-discard"
    if old_preparing.exists():
        if old.exists():
            raise RuntimeError(f"Conflicting restore preparation exists under {root}")
        entries = sorted(path.name for path in old_preparing.iterdir())
        if entries != [".capture-started"]:
            raise RuntimeError(
                f"Unknown rollback preparation under {old_preparing}; preserving it"
            )
        _durable_rmtree(old_preparing)
    if discard.exists() and not old.exists():
        _durable_rmtree(new)
        _durable_rmtree(discard)
        return
    if not old.exists():
        _durable_rmtree(new)
        return
    started = old / ".capture-started"
    complete = old / ".capture-complete"
    if started.exists():
        for entry in _live_entries(root):
            target = old / entry.name
            if target.exists():
                if not _entries_match(entry, target):
                    raise RuntimeError(
                        f"Rollback collision requires inspection: {target}"
                    )
                _durable_remove_entry(entry)
                continue
            _durable_replace(entry, target)
        _durable_replace(started, complete)
    if not complete.is_file():
        raise RuntimeError(f"Unknown restore state under {old}; preserving it")
    for entry in _live_entries(root):
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    _fsync_directory(root)
    for entry in sorted(old.iterdir(), key=lambda path: path.name):
        if entry.name.startswith(".capture-"):
            continue
        _copy_with_hardlinks(entry, root / entry.name)
    _fsync_directory(root)
    if inject_after_copy:
        raise RuntimeError("Injected failure during file rollback")
    _discard_old(root, old, discard)
    _durable_rmtree(new)


def cleanup_root(root_value: str) -> None:
    root = _root(root_value)
    old = root / ".restore-old"
    discard = root / ".restore-discard"
    _discard_old(root, old, discard)
    _durable_rmtree(root / OLD_PREPARING_NAME)
    _durable_rmtree(root / ".restore-new")


def _write_durable_state(
    temporary: Path,
    final: Path,
    payload: str,
    *,
    inject_before_publish: bool,
) -> None:
    with temporary.open("w", encoding="ascii", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.chmod(temporary, 0o600)
        os.fsync(handle.fileno())
    _fsync_directory(temporary.parent)
    if inject_before_publish:
        raise RuntimeError("Injected failure before durable state publication")
    _durable_replace(temporary, final)


def _read_state_payload(path: Path) -> str:
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or path.is_symlink() or path.stat().st_size > 1024:
        raise RuntimeError(f"Unsafe maintenance state file: {path}")
    try:
        return path.read_text(encoding="ascii")
    except UnicodeError as error:
        raise RuntimeError(f"Maintenance state is not ASCII: {path}") from error


def _validate_restore_state(path: Path) -> None:
    lines = _read_state_payload(path).splitlines()
    if len(lines) != 5:
        raise RuntimeError(f"Restore state has an invalid shape: {path}")
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator != "=" or key in values:
            raise RuntimeError(f"Restore state has invalid fields: {path}")
        values[key] = value
    if set(values) != {
        "STAGED_DB",
        "ROLLBACK_DB",
        "CADDY_WAS_RUNNING",
        "WEB_WAS_RUNNING",
        "STATUS",
    }:
        raise RuntimeError(f"Restore state has unknown fields: {path}")
    staged = re.fullmatch(r"rm_restore_stage_([0-9]{14}_[0-9]+)", values["STAGED_DB"])
    rollback = re.fullmatch(
        r"rm_restore_rollback_([0-9]{14}_[0-9]+)", values["ROLLBACK_DB"]
    )
    if staged is None or rollback is None or staged.group(1) != rollback.group(1):
        raise RuntimeError(f"Restore state has invalid database names: {path}")
    if values["CADDY_WAS_RUNNING"] not in {"true", "false"}:
        raise RuntimeError(f"Restore state has an invalid Caddy flag: {path}")
    if values["WEB_WAS_RUNNING"] not in {"true", "false"}:
        raise RuntimeError(f"Restore state has an invalid web flag: {path}")
    if values["STATUS"] not in {"preparing", "candidate", "validated"}:
        raise RuntimeError(f"Restore state has an invalid status: {path}")


def _validate_backup_state(path: Path) -> None:
    payload = _read_state_payload(path)
    if payload not in {"WEB_WAS_RUNNING=true\n", "WEB_WAS_RUNNING=false\n"}:
        raise RuntimeError(f"Backup state has invalid content: {path}")


def _adopt_temporary_state(final: Path, validator) -> None:
    temporary = final.with_suffix(".tmp")
    if final.exists():
        validator(final)
        if temporary.exists():
            validator(temporary)
        return
    if not temporary.exists():
        raise RuntimeError(f"No recoverable maintenance state exists: {final}")
    validator(temporary)
    _durable_replace(temporary, final)


def write_state(args: argparse.Namespace) -> None:
    STATE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    payload = (
        f"STAGED_DB={args.staged_db}\n"
        f"ROLLBACK_DB={args.rollback_db}\n"
        f"CADDY_WAS_RUNNING={args.caddy_was_running}\n"
        f"WEB_WAS_RUNNING={args.web_was_running}\n"
        f"STATUS={args.status}\n"
    )
    _write_durable_state(
        temporary,
        STATE_PATH,
        payload,
        inject_before_publish=args.inject_before_publish,
    )


def remove_state() -> None:
    _durable_unlink(STATE_PATH.with_suffix(".tmp"))
    _durable_unlink(STATE_PATH)


def write_backup_state(web_was_running: str, *, inject_before_publish: bool) -> None:
    temporary = BACKUP_STATE_PATH.with_suffix(".tmp")
    payload = f"WEB_WAS_RUNNING={web_was_running}\n"
    _write_durable_state(
        temporary,
        BACKUP_STATE_PATH,
        payload,
        inject_before_publish=inject_before_publish,
    )


def remove_backup_state() -> None:
    _durable_unlink(BACKUP_STATE_PATH.with_suffix(".tmp"))
    _durable_unlink(BACKUP_STATE_PATH)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    switch = subparsers.add_parser("switch-root")
    switch.add_argument("--root", required=True)
    switch.add_argument("--new-relative", required=True)
    switch.add_argument("--inject-after-old-prepare", action="store_true")
    switch.add_argument("--inject-after-old-publish", action="store_true")

    rollback = subparsers.add_parser("rollback-root")
    rollback.add_argument("--root", required=True)
    rollback.add_argument("--inject-after-copy", action="store_true")

    cleanup = subparsers.add_parser("cleanup-root")
    cleanup.add_argument("--root", required=True)

    state = subparsers.add_parser("write-state")
    state.add_argument("--staged-db", required=True)
    state.add_argument("--rollback-db", required=True)
    state.add_argument("--caddy-was-running", choices=("true", "false"), required=True)
    state.add_argument("--web-was-running", choices=("true", "false"), required=True)
    state.add_argument("--status", choices=("preparing", "candidate", "validated"), required=True)
    state.add_argument("--inject-before-publish", action="store_true")

    subparsers.add_parser("adopt-state")
    subparsers.add_parser("remove-state")
    backup_state = subparsers.add_parser("write-backup-state")
    backup_state.add_argument(
        "--web-was-running", choices=("true", "false"), required=True
    )
    backup_state.add_argument("--inject-before-publish", action="store_true")
    subparsers.add_parser("adopt-backup-state")
    subparsers.add_parser("remove-backup-state")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "switch-root":
        switch_root(
            args.root,
            args.new_relative,
            args.inject_after_old_prepare,
            args.inject_after_old_publish,
        )
    elif args.command == "rollback-root":
        rollback_root(args.root, args.inject_after_copy)
    elif args.command == "cleanup-root":
        cleanup_root(args.root)
    elif args.command == "write-state":
        write_state(args)
    elif args.command == "adopt-state":
        _adopt_temporary_state(STATE_PATH, _validate_restore_state)
    elif args.command == "remove-state":
        remove_state()
    elif args.command == "write-backup-state":
        write_backup_state(
            args.web_was_running,
            inject_before_publish=args.inject_before_publish,
        )
    elif args.command == "adopt-backup-state":
        _adopt_temporary_state(BACKUP_STATE_PATH, _validate_backup_state)
    elif args.command == "remove-backup-state":
        remove_backup_state()


if __name__ == "__main__":
    main()
