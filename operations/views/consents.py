"""Согласия представителей."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import ConsentForm
from operations.models import Child, Consent

from ._common import is_admin_user


@login_required
@user_passes_test(is_admin_user)
def consent_list(request, child_id: int | None = None):
    qs = Consent.objects.select_related("child", "document").order_by("-signed_on")
    if child_id is None:
        child_id = request.GET.get("child_id")
    if child_id:
        qs = qs.filter(child_id=child_id)
    return render(request, "operations/consent_list.html", {"consents": qs[:80], "child_id": child_id})


@login_required
@user_passes_test(is_admin_user)
def consent_create(request, child_id: int | None = None):
    initial = {}
    if child_id:
        initial["child"] = get_object_or_404(Child, pk=child_id)
    if request.method == "POST":
        form = ConsentForm(request.POST)
        if form.is_valid():
            consent = form.save()
            messages.success(request, "Согласие зафиксировано.")
            return redirect("child_detail", pk=consent.child_id)
    else:
        form = ConsentForm(initial=initial)
    return render(
        request,
        "operations/consent_form.html",
        {
            "form": form,
            "title": "Зафиксировать согласие",
            "cancel_url": reverse("consent_list"),
        },
    )
