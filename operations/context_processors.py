"""Template flags derived from the central authority policy."""

from operations.services.authority import is_director_user


def authority_flags(request):
    return {
        "can_manage_compensation_rules": is_director_user(getattr(request, "user", None)),
    }
