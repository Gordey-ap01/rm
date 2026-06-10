"""Расписание — FullCalendar (данные через API)."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render

from ._common import is_admin_user


@login_required
@user_passes_test(is_admin_user)
def schedule(request):
    return render(request, "operations/schedule.html")
