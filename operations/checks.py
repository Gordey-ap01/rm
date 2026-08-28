from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.checks import Error, Tags, register


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


@register(Tags.security, deploy=True)
def private_artifact_root_is_not_public(app_configs, **kwargs):
    del app_configs, kwargs
    private_root = Path(settings.PRIVATE_ARTIFACT_ROOT).resolve()
    public_roots = {
        "MEDIA_ROOT": Path(settings.MEDIA_ROOT).resolve(),
        "STATIC_ROOT": Path(settings.STATIC_ROOT).resolve(),
    }
    errors = []
    for setting_name, public_root in public_roots.items():
        if _is_within(private_root, public_root) or _is_within(public_root, private_root):
            errors.append(
                Error(
                    f"PRIVATE_ARTIFACT_ROOT must not overlap {setting_name}.",
                    hint="Use a separate directory or Docker volume not mounted in Caddy.",
                    id="operations.E002",
                )
            )
    return errors
