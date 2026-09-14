"""Template flags derived from the central authority policy."""

from operations.services.authority import AuthorityRole, authority_role
from operations.services.time_off_decisions import staff_request_summary


def authority_flags(request):
    role = authority_role(getattr(request, "user", None))
    staff = getattr(getattr(request, "user", None), "staff_profile", None)
    return {
        "can_manage_compensation_rules": role == AuthorityRole.DIRECTOR,
        "can_operate_center": role in {AuthorityRole.DIRECTOR, AuthorityRole.ADMINISTRATOR},
        "personal_time_off_summary": (
            staff_request_summary(staff.pk)
            if staff and staff.can_use_mobile
            else None
        ),
    }
