"""Расписание — FullCalendar (данные через API)."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render
from django.utils import dateparse, timezone

from ._common import is_admin_user


@login_required
@user_passes_test(is_admin_user)
def schedule(request):
    requested_day = dateparse.parse_date(request.GET.get("date", ""))
    return render(
        request,
        "operations/schedule.html",
        {"day": requested_day or timezone.localdate()},
    )
