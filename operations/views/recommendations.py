"""Рекомендации специалистов."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import RecommendationForm
from operations.models import Child, Recommendation

from ._common import is_admin_user


@login_required
@user_passes_test(is_admin_user)
def recommendation_list(request):
    qs = Recommendation.objects.select_related("child", "staff_member", "appointment").order_by("-created_at")[:80]
    child_id = request.GET.get("child_id")
    if child_id:
        qs = qs.filter(child_id=child_id)
    return render(request, "operations/recommendation_list.html", {"recommendations": qs, "child_id": child_id})


@login_required
@user_passes_test(is_admin_user)
def recommendation_create(request, child_id: int | None = None):
    initial = {}
    if child_id:
        initial["child"] = get_object_or_404(Child, pk=child_id)
    if request.method == "POST":
        form = RecommendationForm(request.POST)
        if form.is_valid():
            rec = form.save()
            messages.success(request, "Рекомендация создана.")
            return redirect("child_detail", pk=rec.child_id)
    else:
        form = RecommendationForm(initial=initial)
    return render(
        request,
        "operations/recommendation_form.html",
        {
            "form": form,
            "title": "Создать рекомендацию",
            "cancel_url": reverse("recommendation_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def recommendation_acknowledge(request, pk: int):
    rec = get_object_or_404(Recommendation, pk=pk)
    rec.acknowledge(actor=request.user)
    messages.success(request, "Рекомендация отмечена как принятая.")
    return redirect(request.META.get("HTTP_REFERER") or "recommendation_list")
