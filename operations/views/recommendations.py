"""Рекомендации специалистов."""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.forms import RecommendationForm
from operations.models import Child, Recommendation

from ._common import is_admin_user


def _recommendation_create_url(child_id: int | str | None) -> str:
    if child_id:
        return reverse("recommendation_create_for_child", args=[child_id])
    return reverse("recommendation_create")


def _recommendation_summary_items(recommendations: list[Recommendation]) -> list[dict[str, str]]:
    today = timezone.localdate()
    open_items = [item for item in recommendations if not item.is_acknowledged]
    overdue = [item for item in open_items if item.due_on and item.due_on < today]
    due_soon = [
        item
        for item in open_items
        if item.due_on and today <= item.due_on <= today + timedelta(days=7)
    ]
    return [
        {
            "label": "Всего в списке",
            "value": str(len(recommendations)),
            "hint": "показаны последние 80",
        },
        {
            "label": "Не приняты",
            "value": str(len(open_items)),
            "hint": "ждут отметки администратора",
        },
        {
            "label": "Просрочены",
            "value": str(len(overdue)),
            "hint": "срок прошел, отметки нет",
        },
        {
            "label": "На 7 дней",
            "value": str(len(due_soon)),
            "hint": "ближайшие сроки",
        },
    ]


def _recommendation_next_action(
    recommendations: list[Recommendation],
    child_id: int | str | None,
) -> dict[str, str]:
    open_count = sum(1 for item in recommendations if not item.is_acknowledged)
    if open_count:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Разобрать непринятые",
            "detail": f"Рекомендаций без отметки: {open_count}.",
            "href": "#recommendation-list",
        }
    return {
        "tone": "success",
        "label": "Следующий шаг",
        "title": "Создать рекомендацию",
        "detail": "Новых непринятых рекомендаций в текущем списке нет.",
        "href": _recommendation_create_url(child_id),
    }


def _recommendation_control_items(
    recommendations: list[Recommendation],
    child_id: int | str | None,
) -> list[dict[str, str]]:
    today = timezone.localdate()
    open_items = [item for item in recommendations if not item.is_acknowledged]
    overdue_count = sum(1 for item in open_items if item.due_on and item.due_on < today)
    items = []
    if child_id:
        items.append(
            {
                "tone": "info",
                "title": "Фильтр по получателю",
                "text": "Список показывает рекомендации только выбранного получателя.",
            }
        )
    if overdue_count:
        items.append(
            {
                "tone": "warning",
                "title": "Есть просроченные рекомендации",
                "text": f"Просроченных без отметки: {overdue_count}.",
            }
        )
    if open_items:
        items.append(
            {
                "tone": "info",
                "title": "Отметка не удаляет рекомендацию",
                "text": "Кнопка 'Принято' фиксирует, что администратор взял рекомендацию в работу.",
            }
        )
    if not recommendations:
        items.append(
            {
                "tone": "info",
                "title": "Рекомендаций пока нет",
                "text": "Создайте рекомендацию после занятия или консультации специалиста.",
            }
        )
    return items


@login_required
@user_passes_test(is_admin_user)
def recommendation_list(request):
    child_id = request.GET.get("child_id")
    qs = Recommendation.objects.select_related("child", "staff_member", "appointment").order_by(
        "-created_at"
    )
    if child_id:
        qs = qs.filter(child_id=child_id)
    recommendations = list(qs[:80])
    return render(
        request,
        "operations/recommendation_list.html",
        {
            "recommendations": recommendations,
            "child_id": child_id,
            "recommendation_create_url": _recommendation_create_url(child_id),
            "recommendation_summary_items": _recommendation_summary_items(recommendations),
            "recommendation_next_action": _recommendation_next_action(
                recommendations,
                child_id,
            ),
            "recommendation_control_items": _recommendation_control_items(
                recommendations,
                child_id,
            ),
        },
    )


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
