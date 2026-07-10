"""Справочник специалистов."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import StaffMemberForm
from operations.models import StaffMember

from ._common import is_admin_user


def staff_member_summary_items(staff_members: list[StaffMember]) -> list[dict[str, str]]:
    active_count = sum(
        1
        for staff in staff_members
        if staff.status == StaffMember.Status.ACTIVE and not staff.is_archived
    )
    mobile_count = sum(
        1 for staff in staff_members if staff.can_use_mobile and not staff.is_archived
    )
    unbound_count = sum(1 for staff in staff_members if not staff.user_id and not staff.is_archived)
    away_count = sum(
        1
        for staff in staff_members
        if staff.status in [StaffMember.Status.VACATION, StaffMember.Status.SICK]
        and not staff.is_archived
    )
    archived_count = sum(1 for staff in staff_members if staff.is_archived)
    return [
        {
            "label": "Всего",
            "value": str(len(staff_members)),
            "hint": "профилей специалистов",
        },
        {
            "label": "Активны",
            "value": str(active_count),
            "hint": "доступны для расписания",
        },
        {
            "label": "Мобильный",
            "value": str(mobile_count),
            "hint": "имеют доступ к кабинету",
        },
        {
            "label": "Без пользователя",
            "value": str(unbound_count),
            "hint": "нужно привязать учетную запись",
        },
        {
            "label": "Отсутствуют",
            "value": str(away_count),
            "hint": "отпуск или больничный",
        },
        {
            "label": "Архив",
            "value": str(archived_count),
            "hint": "история занятий сохранена",
        },
    ]


def staff_member_next_action(staff_members: list[StaffMember]) -> dict[str, str]:
    unbound_count = sum(1 for staff in staff_members if not staff.user_id and not staff.is_archived)
    no_mobile_count = sum(
        1
        for staff in staff_members
        if not staff.can_use_mobile
        and staff.status == StaffMember.Status.ACTIVE
        and not staff.is_archived
    )
    inactive_count = sum(
        1
        for staff in staff_members
        if staff.status != StaffMember.Status.ACTIVE and not staff.is_archived
    )
    archived_count = sum(1 for staff in staff_members if staff.is_archived)
    if not staff_members:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Создать первого специалиста",
            "detail": "Без специалиста нельзя вести расписание, табель и кабинет.",
            "href": reverse("staff_member_create"),
        }
    if unbound_count:
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Привязать пользователей",
            "detail": f"Профилей без учетной записи: {unbound_count}.",
            "href": "#staff-member-list",
        }
    if no_mobile_count:
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Проверить мобильный доступ",
            "detail": f"Активных специалистов без мобильного кабинета: {no_mobile_count}.",
            "href": "#staff-member-list",
        }
    if inactive_count:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Проверить отсутствующих специалистов",
            "detail": f"Неактивны, в отпуске или на больничном: {inactive_count}.",
            "href": "#staff-member-list",
        }
    if archived_count:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Проверить архив специалистов",
            "detail": f"В архиве: {archived_count}. История занятий сохраняется.",
            "href": "#staff-member-list",
        }
    return {
        "tone": "success",
        "label": "Следующее действие",
        "title": "Специалисты готовы к работе",
        "detail": "Можно вести расписание, табель, ставки и мобильный кабинет.",
        "href": "#staff-member-list",
    }


def staff_member_form_control_items(staff: StaffMember | None = None) -> list[dict[str, str]]:
    user_detail = (
        "Пользователь нужен для входа в мобильный кабинет; один пользователь не может быть привязан к двум специалистам."
    )
    status_detail = (
        "Статус управляет доступностью специалиста для расписания и отчетов по отсутствиям."
    )
    mobile_detail = (
        "Флажок мобильного кабинета разрешает специалисту видеть занятия и отмечать факт посещения."
    )
    archive_detail = (
        "Архивирование выполняется из списка и сохраняет историю занятий, табеля и начислений."
    )
    if staff:
        user_detail = (
            f"Привязанный пользователь: {staff.user.username}."
            if staff.user_id
            else "Пользователь еще не привязан; мобильный вход невозможен без учетной записи."
        )
        status_detail = f"Текущий статус: {staff.get_status_display()}."
        mobile_detail = (
            "Мобильный кабинет включен."
            if staff.can_use_mobile
            else "Мобильный кабинет выключен; прямые отметки специалиста будут недоступны."
        )
        archive_detail = (
            "Специалист в архиве; новые занятия должны назначаться другим активным специалистам."
            if staff.is_archived
            else "Специалист не архивирован; историю можно смотреть через табель и карточки занятий."
        )
    return [
        {
            "title": "Учетная запись",
            "detail": user_detail,
        },
        {
            "title": "Статус и расписание",
            "detail": status_detail,
        },
        {
            "title": "Мобильный кабинет",
            "detail": mobile_detail,
        },
        {
            "title": "Архив и история",
            "detail": archive_detail,
        },
    ]


@login_required
@user_passes_test(is_admin_user)
def staff_member_list(request):
    staff_members = list(
        StaffMember.all_objects.select_related("user").order_by("archived_at", "full_name")
    )
    return render(
        request,
        "operations/staff_member_list.html",
        {
            "staff_members": staff_members,
            "staff_summary_items": staff_member_summary_items(staff_members),
            "staff_next_action": staff_member_next_action(staff_members),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def staff_member_create(request):
    if request.method == "POST":
        form = StaffMemberForm(request.POST)
        if form.is_valid():
            staff = form.save()
            messages.success(request, "Специалист создан.")
            return redirect("staff_member_edit", pk=staff.pk)
    else:
        form = StaffMemberForm()
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Создать специалиста",
            "subtitle": "Профиль для расписания, табеля и мобильного кабинета.",
            "form": form,
            "form_panel_title": "Профиль специалиста",
            "form_intro": (
                "Профиль специалиста связывает расписание, табель, начисления и мобильный кабинет."
            ),
            "control_title": "Контроль специалиста",
            "object_form_control_items": staff_member_form_control_items(),
            "cancel_url": reverse("staff_member_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def staff_member_edit(request, pk: int):
    staff = get_object_or_404(StaffMember.all_objects.select_related("user"), pk=pk)
    if request.method == "POST":
        form = StaffMemberForm(request.POST, instance=staff)
        if form.is_valid():
            form.save()
            messages.success(request, "Специалист обновлен.")
            return redirect("staff_member_list")
    else:
        form = StaffMemberForm(instance=staff)
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Редактировать специалиста",
            "subtitle": staff.full_name,
            "form": form,
            "form_panel_title": "Профиль специалиста",
            "form_intro": (
                "Изменения статуса и мобильного доступа влияют на будущую работу расписания и кабинета."
            ),
            "control_title": "Контроль специалиста",
            "object_form_control_items": staff_member_form_control_items(staff),
            "cancel_url": reverse("staff_member_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def staff_member_archive(request, pk: int):
    staff = get_object_or_404(StaffMember.all_objects, pk=pk)
    if request.method == "POST":
        staff.archive()
        messages.success(request, "Специалист архивирован.")
    return redirect("staff_member_list")


@login_required
@user_passes_test(is_admin_user)
def staff_member_restore(request, pk: int):
    staff = get_object_or_404(StaffMember.all_objects, pk=pk)
    if request.method == "POST":
        staff.restore()
        messages.success(request, "Специалист восстановлен.")
    return redirect("staff_member_list")
