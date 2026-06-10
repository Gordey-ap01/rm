"""Общие хелперы для views."""

from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme


def is_admin_user(user) -> bool:
    return user.is_staff


def safe_next_url(request, fallback: str) -> str:
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return fallback


def csrf_failure(request, reason: str = ""):
    if request.path == reverse("login"):
        messages.warning(
            request,
            "Страница входа устарела. Откройте ее заново и повторите вход.",
        )
        login_url = reverse("login")
        next_url = request.GET.get("next")
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            login_url = f"{login_url}?{urlencode({'next': next_url})}"
        return redirect(login_url)
    return render(request, "registration/csrf_failure.html", {"reason": reason}, status=403)
