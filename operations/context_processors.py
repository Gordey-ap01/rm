"""Template flags derived from the central authority policy."""

from operations.services.authority import AuthorityRole, authority_role


def authority_flags(request):
    role = authority_role(getattr(request, "user", None))
    return {
        "can_manage_compensation_rules": role == AuthorityRole.DIRECTOR,
        "can_operate_center": role in {AuthorityRole.DIRECTOR, AuthorityRole.ADMINISTRATOR},
    }
