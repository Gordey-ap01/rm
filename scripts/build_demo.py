"""Сборка переносимого demo-каталога ``dist/RMcodex-demo/``.

Создаёт структуру::

    dist/RMcodex-demo/
        manage.py
        pyproject.toml
        rehab_center/
        operations/
        templates/
        static/
        scripts/
        START_DEMO.bat
        start_demo.sh
        README.txt
        .env.example

Исключает: ``data/``, ``*.sqlite3``, ``.env``, ``.venv*``, ``__pycache__/``,
``staticfiles/``, ``dist/``, ``htmlcov/``, ``.coverage``, ``.pytest_cache/``,
``.ruff_cache/``, ``.mypy_cache/``.

Запуск::

    python scripts/build_demo.py
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST_NAME = "RMcodex-demo"
DIST = ROOT / "dist" / DIST_NAME

EXCLUDE_DIR_NAMES = {
    ".git",
    ".venv",
    ".venv-test",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    "data",
    "staticfiles",
    "dist",
    "htmlcov",
    "media",
    "node_modules",
    "oldWorkMDprd",
    "docs",
}
EXCLUDE_FILE_SUFFIXES = {".sqlite3", ".pyc", ".pyo", ".m4a", ".pdf"}
EXCLUDE_FILE_NAMES = {
    ".env",
    ".DS_Store",
    ".coverage",
    ".dockerignore",
    "compose.yaml",
    "Dockerfile",
    "requirements.txt",
    "rehab-center.txt",
    "README.md",
}


def _should_skip(path: Path) -> bool:
    for part in path.relative_to(ROOT).parts:
        if part in EXCLUDE_DIR_NAMES:
            return True
    if path.name in EXCLUDE_FILE_NAMES:
        return True
    return path.suffix in EXCLUDE_FILE_SUFFIXES


def _copy(root: Path, dst: Path) -> int:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    count = 0
    for src in root.rglob("*"):
        if not src.is_file():
            continue
        if _should_skip(src):
            continue
        rel = src.relative_to(root)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        count += 1
    return count


def _write_readme(dst: Path) -> None:
    readme = """Реабилитационный центр — переносимая демо-сборка
=================================================

Запуск (Windows):
    START_DEMO.bat

Запуск (macOS/Linux):
    ./start_demo.sh

Скрипт автоматически:
  1. Создаёт виртуальное окружение ``.venv`` (если не создано).
  2. Устанавливает проект в editable-режиме: ``pip install -e ".[dev]"``.
  3. Применяет миграции: ``python manage.py migrate``.
  4. Наполняет базу демо-данными: ``python manage.py seed_demo``.
  5. Запускает сервер: ``python manage.py runserver 0.0.0.0:8000``.

После старта откройте http://127.0.0.1:8000/.

Демо-учётки:
  * admin / admin12345 (полный доступ)
  * specialist1..specialist4 / specialist123 (специалист)
"""
    (dst / "README.txt").write_text(readme, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build portable demo directory")
    parser.add_argument(
        "--out", default=str(DIST), help="Destination directory (default: dist/RMcodex-demo)"
    )
    args = parser.parse_args()
    out = Path(args.out).resolve()
    print(f"Сборка: {ROOT} -> {out}")
    copied = _copy(ROOT, out)
    _write_readme(out)
    print(f"Скопировано файлов: {copied}")
    print(f"Готово: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
