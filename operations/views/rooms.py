"""Кабинеты и правила вместимости."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import RoomForm
from operations.models import Room

from ._common import is_admin_user


def room_summary_items(rooms: list[Room]) -> list[dict[str, str]]:
    active_count = sum(1 for room in rooms if room.is_active)
    group_ready_count = sum(
        1
        for room in rooms
        if room.allow_group_sessions
        and (not room.limit_recipient_count or room.effective_max_recipient_count > 1)
    )
    multi_staff_count = sum(
        1
        for room in rooms
        if not room.limit_staff_count or room.effective_max_staff_count > 1
    )
    multi_recipient_count = sum(
        1
        for room in rooms
        if not room.limit_recipient_count or room.effective_max_recipient_count > 1
    )
    return [
        {
            "label": "Всего",
            "value": str(len(rooms)),
            "hint": "кабинетов в справочнике",
        },
        {
            "label": "Активны",
            "value": str(active_count),
            "hint": "доступны для расписания",
        },
        {
            "label": "Группы",
            "value": str(group_ready_count),
            "hint": "можно ставить несколько получателей",
        },
        {
            "label": "Несколько специалистов",
            "value": str(multi_staff_count),
            "hint": "кабинеты без одиночного ограничения",
        },
        {
            "label": "Несколько получателей",
            "value": str(multi_recipient_count),
            "hint": "кабинеты для групповых занятий",
        },
    ]


def room_next_action(rooms: list[Room]) -> dict[str, str]:
    inactive_count = sum(1 for room in rooms if not room.is_active)
    group_ready_count = sum(
        1
        for room in rooms
        if room.allow_group_sessions
        and (not room.limit_recipient_count or room.effective_max_recipient_count > 1)
    )
    multi_staff_count = sum(
        1
        for room in rooms
        if not room.limit_staff_count or room.effective_max_staff_count > 1
    )
    if not rooms:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Создать первый кабинет",
            "detail": "Без кабинетов расписание не сможет проверять вместимость.",
            "href": reverse("room_create"),
        }
    if inactive_count:
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Проверить выключенные кабинеты",
            "detail": f"Выключено: {inactive_count}. Они не должны попадать в новое расписание.",
            "href": "#room-list",
        }
    if not group_ready_count:
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Настроить групповой кабинет",
            "detail": "Для групп нужно разрешить группы и лимит получателей больше одного.",
            "href": "#room-list",
        }
    if not multi_staff_count:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Проверить кабинеты для двух специалистов",
            "detail": "Если занятия ведут несколько специалистов, настройте лимит специалистов.",
            "href": "#room-list",
        }
    return {
        "tone": "success",
        "label": "Следующее действие",
        "title": "Кабинеты готовы к расписанию",
        "detail": "Проверьте исключения вручную при создании занятий вне правил.",
        "href": "#room-list",
    }


def room_form_control_items(room: Room | None = None) -> list[dict[str, str]]:
    staff_detail = (
        "Если ограничение специалистов включено, расписание не даст поставить сверх заданного максимума."
    )
    recipient_detail = (
        "Если ограничение получателей включено, групповые занятия проверяются по отдельному максимуму."
    )
    group_detail = (
        "Для группового занятия кабинет должен разрешать группы и иметь лимит получателей больше одного "
        "или отключенное ограничение получателей."
    )
    override_detail = (
        "При ручном создании занятия администратор может осознанно удержать одноразовое разрешение, "
        "но настройки кабинета остаются базовым правилом."
    )
    if room:
        staff_detail = (
            f"Сейчас: максимум специалистов одновременно - {room.effective_max_staff_count}. "
            f"Ограничение {'включено' if room.limit_staff_count else 'выключено'}."
        )
        recipient_detail = (
            f"Сейчас: максимум получателей одновременно - {room.effective_max_recipient_count}. "
            f"Ограничение {'включено' if room.limit_recipient_count else 'выключено'}."
        )
        group_detail = (
            "Групповые занятия разрешены."
            if room.allow_group_sessions
            else "Групповые занятия сейчас запрещены для этого кабинета."
        )
    return [
        {
            "title": "Вместимость специалистов",
            "detail": staff_detail,
        },
        {
            "title": "Вместимость получателей",
            "detail": recipient_detail,
        },
        {
            "title": "Групповые занятия",
            "detail": group_detail,
        },
        {
            "title": "Разовое исключение",
            "detail": override_detail,
        },
    ]


@login_required
@user_passes_test(is_admin_user)
def room_list(request):
    rooms = list(Room.objects.order_by("name"))
    return render(
        request,
        "operations/room_list.html",
        {
            "rooms": rooms,
            "room_summary_items": room_summary_items(rooms),
            "room_next_action": room_next_action(rooms),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def room_create(request):
    if request.method == "POST":
        form = RoomForm(request.POST)
        if form.is_valid():
            room = form.save()
            messages.success(request, "Кабинет создан.")
            return redirect("room_edit", pk=room.pk)
    else:
        form = RoomForm()
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Создать кабинет",
            "subtitle": "Настройте вместимость и правила групповых занятий.",
            "form_panel_title": "Правила кабинета",
            "form_intro": (
                "Кабинет ограничивает одновременное число специалистов и получателей при создании расписания."
            ),
            "control_title": "Контроль кабинета",
            "object_form_control_items": room_form_control_items(),
            "cancel_url": reverse("room_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def room_edit(request, pk: int):
    room = get_object_or_404(Room, pk=pk)
    if request.method == "POST":
        form = RoomForm(request.POST, instance=room)
        if form.is_valid():
            form.save()
            messages.success(request, "Настройки кабинета обновлены.")
            return redirect("room_list")
    else:
        form = RoomForm(instance=room)
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Редактировать кабинет",
            "subtitle": room.name,
            "form_panel_title": "Правила кабинета",
            "form_intro": (
                "Изменения применяются к будущим проверкам расписания; существующие занятия не переносятся автоматически."
            ),
            "control_title": "Контроль кабинета",
            "object_form_control_items": room_form_control_items(room),
            "cancel_url": reverse("room_list"),
        },
    )
