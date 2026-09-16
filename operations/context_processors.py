"""Template flags derived from the central authority policy."""

from django.db.models import Count, Q

from operations.services.authority import AuthorityRole, authority_role
from operations.services.time_off_decisions import staff_request_summary


def authority_flags(request):
    role = authority_role(getattr(request, "user", None))
    staff = getattr(getattr(request, "user", None), "staff_profile", None)
    schedule_summary = None
    if staff and staff.can_use_mobile:
        schedule_summary = staff.schedule_change_requests.aggregate(
            total=Count("pk", distinct=True),
            pending=Count("pk", filter=Q(status="pending"), distinct=True),
            approved=Count("pk", filter=Q(status="approved"), distinct=True),
            rejected=Count("pk", filter=Q(status="rejected"), distinct=True),
            review=Count("pk", filter=Q(decisions__is_current=True, decisions__requires_director_review=True), distinct=True),
        )
    return {
        "can_manage_compensation_rules": role == AuthorityRole.DIRECTOR,
        "can_operate_center": role in {AuthorityRole.DIRECTOR, AuthorityRole.ADMINISTRATOR},
        "personal_schedule_summary": schedule_summary,
        "personal_time_off_summary": (
            staff_request_summary(staff.pk)
            if staff and staff.can_use_mobile
            else None
        ),
    }
