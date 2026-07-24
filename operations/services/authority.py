"""Role and authority policy for operational and management decisions."""

from __future__ import annotations

from enum import StrEnum


class AuthorityRole(StrEnum):
    DIRECTOR = "director"
    ADMINISTRATOR = "administrator"
    SPECIALIST = "specialist"
    AUTHENTICATED = "authenticated"
    ANONYMOUS = "anonymous"


DIRECTOR_GROUP_NAMES = {"Руководители", "Руководитель"}
ADMINISTRATOR_GROUP_NAMES = {"Администраторы", "Администратор"}


def _has_group(user, names: set[str]) -> bool:
    return bool(user and user.is_authenticated and user.groups.filter(name__in=names).exists())


def authority_role(user) -> AuthorityRole:
    if not user or not user.is_authenticated:
        return AuthorityRole.ANONYMOUS
    if user.is_superuser or _has_group(user, DIRECTOR_GROUP_NAMES):
        return AuthorityRole.DIRECTOR
    if user.is_staff or _has_group(user, ADMINISTRATOR_GROUP_NAMES):
        return AuthorityRole.ADMINISTRATOR
    if hasattr(user, "staff_profile"):
        return AuthorityRole.SPECIALIST
    return AuthorityRole.AUTHENTICATED


def is_director_user(user) -> bool:
    return authority_role(user) == AuthorityRole.DIRECTOR


def is_center_operator(user) -> bool:
    return authority_role(user) in {
        AuthorityRole.DIRECTOR,
        AuthorityRole.ADMINISTRATOR,
    }
