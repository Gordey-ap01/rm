"""Экран «Завтра» для администраторов."""

from __future__ import annotations

import contextlib

from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render
from django.utils import timezone

from operations.services import reports as reports_svc

from ._common import is_admin_user


@login_required
@user_passes_test(is_admin_user)
def tomorrow(request):
    target = timezone.localdate() + timezone.timedelta(days=1)
    if request.GET.get("date"):
        from datetime import datetime
        with contextlib.suppress(ValueError):
            target = datetime.fromisoformat(request.GET["date"]).date()
    overview = reports_svc.tomorrow_overview(target)
    return render(request, "operations/tomorrow.html", {"overview": overview})
