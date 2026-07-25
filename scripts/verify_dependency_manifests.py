"""Keep the production Docker dependency manifest aligned with pyproject.toml."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_NAME = re.compile(r"^[A-Za-z0-9_.-]+")


def normalized_dependencies(entries: list[str]) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    for entry in entries:
        requirement = entry.strip()
        if not requirement or requirement.startswith("#"):
            continue
        match = PACKAGE_NAME.match(requirement)
        if match is None:
            raise ValueError(f"Cannot parse dependency: {entry!r}")
        package = match.group(0).lower().replace("_", "-")
        if package in dependencies:
            raise ValueError(f"Dependency listed more than once: {package}")
        dependencies[package] = requirement.replace(" ", "")
    return dependencies


def main() -> int:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pyproject_dependencies = normalized_dependencies(project["project"]["dependencies"])
    requirements_dependencies = normalized_dependencies(
        (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    )

    missing = sorted(set(pyproject_dependencies) - set(requirements_dependencies))
    unexpected = sorted(set(requirements_dependencies) - set(pyproject_dependencies))
    changed = sorted(
        package
        for package in set(pyproject_dependencies) & set(requirements_dependencies)
        if pyproject_dependencies[package] != requirements_dependencies[package]
    )
    if missing or unexpected or changed:
        if missing:
            print(f"Missing from requirements.txt: {', '.join(missing)}", file=sys.stderr)
        if unexpected:
            print(f"Not declared in pyproject.toml: {', '.join(unexpected)}", file=sys.stderr)
        if changed:
            print(f"Version constraints differ: {', '.join(changed)}", file=sys.stderr)
        return 1

    print("Production dependency manifests are aligned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
