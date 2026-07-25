"""Общие хелперы для views."""

from __future__ import annotations

from functools import wraps
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from operations.services.authority import is_center_operator, is_director_user


def is_admin_user(user) -> bool:
    return is_center_operator(user)


def is_director(user) -> bool:
    return is_director_user(user)


def director_required(view_func):
    """Require a director without redirecting authenticated operators through login."""

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_director(request.user):
            raise PermissionDenied("This action is available only to a director.")
        return view_func(request, *args, **kwargs)

    return wrapped


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
