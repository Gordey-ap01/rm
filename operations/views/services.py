"""Справочник услуг и направлений занятий."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import ServiceForm
from operations.models import Service

from ._common import is_admin_user


def service_summary_items(services: list[Service]) -> list[dict[str, str]]:
    active_count = sum(1 for service in services if service.is_active and not service.is_archived)
    archived_count = sum(1 for service in services if service.is_archived)
    free_price_count = sum(
        1
        for service in services
        if not service.is_archived and service.default_price == 0
    )
    categories_count = len({service.category for service in services if not service.is_archived})
    return [
        {
            "label": "Всего",
            "value": str(len(services)),
            "hint": "услуг и направлений",
        },
        {
            "label": "Активны",
            "value": str(active_count),
            "hint": "доступны для расписания",
        },
        {
            "label": "Категории",
            "value": str(categories_count),
            "hint": "направлений среди активных",
        },
        {
            "label": "Без цены",
            "value": str(free_price_count),
            "hint": "проверьте перед оплатой/грантами",
        },
        {
            "label": "Архив",
            "value": str(archived_count),
            "hint": "история занятий сохранена",
        },
    ]


def service_next_action(services: list[Service]) -> dict[str, str]:
    archived_count = sum(1 for service in services if service.is_archived)
    inactive_count = sum(
        1 for service in services if not service.is_active and not service.is_archived
    )
    free_price_count = sum(
        1
        for service in services
        if not service.is_archived and service.default_price == 0
    )
    if not services:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Создать первую услугу",
            "detail": "Без услуги нельзя составлять занятия, программы, балансы и ставки.",
            "href": reverse("service_create"),
        }
    if free_price_count:
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Проверить услуги без цены",
            "detail": f"Активных услуг с нулевой ценой: {free_price_count}.",
            "href": "#service-list",
        }
    if inactive_count:
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Проверить выключенные услуги",
            "detail": f"Выключено без архива: {inactive_count}.",
            "href": "#service-list",
        }
    if archived_count:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Проверить архив услуг",
            "detail": f"В архиве: {archived_count}. История занятий сохраняется.",
            "href": "#service-list",
        }
    return {
        "tone": "success",
        "label": "Следующее действие",
        "title": "Услуги готовы к работе",
        "detail": "Можно использовать их в расписании, программах, балансах и ставках.",
        "href": "#service-list",
    }


def service_form_control_items(service: Service | None = None) -> list[dict[str, str]]:
    duration_detail = (
        "Длительность по умолчанию используется как стартовое значение при создании занятий и каскадов."
    )
    price_detail = (
        "Цена по умолчанию нужна для платежей, рублевых счетов и оценки количества занятий при переносах."
    )
    status_detail = (
        "Активная услуга доступна в новых занятиях, программах, балансовых счетах, грантах и ставках."
    )
    if service:
        duration_detail = (
            f"Сейчас: {service.default_duration_minutes} мин. "
            "Изменение не переносит уже созданные занятия автоматически."
        )
        price_detail = (
            f"Сейчас: {service.default_price}. "
            "Проверьте рублевые счета и ставки, если меняется финансовая модель услуги."
        )
        status_detail = (
            "Услуга активна для новых записей."
            if service.is_active and not service.is_archived
            else "Услуга не должна попадать в новые записи, если выключена или архивирована."
        )
    return [
        {
            "title": "Код и категория",
            "detail": "Код помогает отличать услуги в отчетах, а категория группирует направления центра.",
        },
        {
            "title": "Длительность",
            "detail": duration_detail,
        },
        {
            "title": "Цена",
            "detail": price_detail,
        },
        {
            "title": "Активность и архив",
            "detail": status_detail,
        },
    ]


@login_required
@user_passes_test(is_admin_user)
def service_list(request):
    services = list(Service.all_objects.order_by("archived_at", "name"))
    return render(
        request,
        "operations/service_list.html",
        {
            "services": services,
            "service_summary_items": service_summary_items(services),
            "service_next_action": service_next_action(services),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def service_create(request):
    if request.method == "POST":
        form = ServiceForm(request.POST)
        if form.is_valid():
            service = form.save()
            messages.success(request, "Услуга создана.")
            return redirect("service_edit", pk=service.pk)
    else:
        form = ServiceForm()
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Создать услугу",
            "subtitle": "Направление занятий для расписания, программ, балансов, грантов и ставок.",
            "form": form,
            "form_panel_title": "Параметры услуги",
            "form_intro": (
                "Услуга связывает расписание, программы занятий, финансовые счета, грантовые квоты и ставки."
            ),
            "control_title": "Контроль услуги",
            "object_form_control_items": service_form_control_items(),
            "cancel_url": reverse("service_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def service_edit(request, pk: int):
    service = get_object_or_404(Service.all_objects, pk=pk)
    if request.method == "POST":
        form = ServiceForm(request.POST, instance=service)
        if form.is_valid():
            form.save()
            messages.success(request, "Услуга обновлена.")
            return redirect("service_list")
    else:
        form = ServiceForm(instance=service)
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Редактировать услугу",
            "subtitle": service.name,
            "form": form,
            "form_panel_title": "Параметры услуги",
            "form_intro": (
                "Изменения услуги влияют на новые записи и справочники, но не переписывают историю занятий."
            ),
            "control_title": "Контроль услуги",
            "object_form_control_items": service_form_control_items(service),
            "cancel_url": reverse("service_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def service_archive(request, pk: int):
    service = get_object_or_404(Service.all_objects, pk=pk)
    if request.method == "POST":
        service.archive()
        messages.success(request, "Услуга архивирована.")
    return redirect("service_list")


@login_required
@user_passes_test(is_admin_user)
def service_restore(request, pk: int):
    service = get_object_or_404(Service.all_objects, pk=pk)
    if request.method == "POST":
        service.restore()
        messages.success(request, "Услуга восстановлена.")
    return redirect("service_list")
