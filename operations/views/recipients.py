"""Получатели и их представители."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Count, F, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.forms import RecipientForm, RecipientRepresentativeForm, RepresentativeForm
from operations.models import (
    Appointment,
    Child,
    ParentGuardian,
    RecipientRepresentative,
    TreatmentProgram,
)
from operations.services.pdf import contract_pdf

from ._common import is_admin_user


def _recipient_detail_summary_items(
    recipient: Child,
    *,
    representative_links: list[RecipientRepresentative],
    accounts: list,
    programs: list[TreatmentProgram],
    upcoming_appointments: list[Appointment],
) -> list[dict[str, str]]:
    low_accounts = [account for account in accounts if account.warning_level != "ok"]
    schedule_recipients = [link for link in representative_links if link.receives_schedule]
    return [
        {
            "label": "Статус",
            "value": recipient.get_status_display(),
            "hint": recipient.birth_date.strftime("%d.%m.%Y") if recipient.birth_date else "дата рождения не указана",
        },
        {
            "label": "Представители",
            "value": str(len(representative_links)),
            "hint": f"получают расписание: {len(schedule_recipients)}",
        },
        {
            "label": "Счета",
            "value": str(len(accounts)),
            "hint": f"с риском остатка: {len(low_accounts)}",
        },
        {
            "label": "Программы",
            "value": str(len(programs)),
            "hint": "активные и архивные каскады",
        },
        {
            "label": "Ближайшие занятия",
            "value": str(len(upcoming_appointments)),
            "hint": "показаны первые 20",
        },
    ]


def _recipient_detail_next_action(
    recipient: Child,
    *,
    representative_links: list[RecipientRepresentative],
    accounts: list,
    programs: list[TreatmentProgram],
) -> dict[str, str]:
    if not representative_links:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Назначить представителя",
            "detail": "Для договора, расписания и согласований нужен хотя бы один представитель.",
            "href": reverse("recipient_representative_create", args=[recipient.pk]),
        }

    if not accounts:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Создать счет баланса",
            "detail": "Без счета нельзя привязать оплату, грант или бронь занятий.",
            "href": f"{reverse('balance_account_create')}?recipient_id={recipient.pk}",
        }

    low_accounts = [account for account in accounts if account.warning_level != "ok"]
    if low_accounts:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Проверить низкие остатки",
            "detail": f"Счетов с предупреждениями: {len(low_accounts)}.",
            "href": "#recipient-balances",
        }

    if not programs:
        return {
            "tone": "info",
            "label": "Следующий шаг",
            "title": "Создать программу",
            "detail": "После консультации можно собрать каскад занятий и расписать блоки.",
            "href": reverse("program_create_for_child", args=[recipient.pk]),
        }

    return {
        "tone": "success",
        "label": "Следующий шаг",
        "title": "Запланировать занятие",
        "detail": "Ключевые данные получателя заполнены; можно работать с расписанием.",
        "href": f"{reverse('appointment_create')}?child_id={recipient.pk}",
    }


def _recipient_list_summary_items(
    recipients: list[Child],
    *,
    total_count: int,
    matching_count: int,
    waiting_count: int,
    without_primary_parent_count: int,
    query: str,
) -> list[dict[str, str]]:
    return [
        {
            "label": "Показано",
            "value": str(len(recipients)),
            "hint": f"найдено по фильтру: {matching_count}" if query else "лимит списка: 80 строк",
        },
        {
            "label": "Всего",
            "value": str(total_count),
            "hint": "получателей в базе",
        },
        {
            "label": "Ожидают",
            "value": str(waiting_count),
            "hint": "получатели в статусе ожидания",
        },
        {
            "label": "Без основного",
            "value": str(without_primary_parent_count),
            "hint": "нет представителя для договора",
        },
    ]


def _recipient_list_next_action(
    recipients: list[Child],
    *,
    query: str,
    waiting_count: int,
    without_primary_parent_count: int,
) -> dict[str, str]:
    if query and not recipients:
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Ничего не найдено",
            "detail": "Проверьте написание или добавьте нового получателя.",
            "href": reverse("recipient_create"),
        }
    if without_primary_parent_count:
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Проверить представителей",
            "detail": f"Получателей без основного представителя: {without_primary_parent_count}.",
            "href": "#recipient-list",
        }
    if waiting_count:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Разобрать ожидающих",
            "detail": f"В очереди ожидания: {waiting_count}.",
            "href": "#recipient-list",
        }
    return {
        "tone": "success",
        "label": "Следующее действие",
        "title": "Карточки готовы",
        "detail": "Можно открыть получателя, создать программу или записать занятие.",
        "href": reverse("recipient_create"),
    }


def _recipient_list_control_items(query: str) -> list[dict[str, str]]:
    items = [
        {
            "title": "Поиск до лимита",
            "detail": "Фильтр по ФИО и телефону применяется до ограничения в 80 строк.",
        },
        {
            "title": "Основной представитель",
            "detail": "Он подписывает договор; дополнительные представители задаются в карточке.",
        },
        {
            "title": "Расписание",
            "detail": "Рассылку получают только представители с включенным флажком расписания.",
        },
        {
            "title": "Карточка получателя",
            "detail": "В карточке видны счета, программы, занятия, документы и представители.",
        },
    ]
    if query:
        items.insert(
            0,
            {
                "title": "Активный фильтр",
                "detail": f"Список ограничен строкой поиска: {query}.",
            },
        )
    return items


def _recipient_form_control_items() -> list[dict[str, str]]:
    return [
        {
            "title": "Основной представитель",
            "detail": "Он используется как подписант договора и основной контакт получателя.",
        },
        {
            "title": "Дополнительные представители",
            "detail": "Их лучше привязывать отдельной связью в карточке получателя.",
        },
        {
            "title": "Контакты получателя",
            "detail": "Телефон и email получателя не заменяют контакты представителя.",
        },
        {
            "title": "Цветовая метка",
            "detail": "Цвет помогает быстро отличать получателя в расписании и карточках.",
        },
    ]


def _representative_profile_control_items() -> list[dict[str, str]]:
    return [
        {
            "title": "Контакт представителя",
            "detail": "Телефон и email используются для связи, согласований и расписания.",
        },
        {
            "title": "Связь с получателем",
            "detail": "Роль, флажок расписания и плательщика задаются в карточке получателя.",
        },
        {
            "title": "Email",
            "detail": "Без email представитель не сможет получить ссылку на публичное согласование.",
        },
    ]


def _recipient_representative_control_items(
    link: RecipientRepresentative | None = None,
) -> list[dict[str, str]]:
    items = [
        {
            "title": "Основной представитель",
            "detail": "Флажок делает представителя подписантом договора и основным контактом.",
        },
        {
            "title": "Расписание",
            "detail": "Расписание отправляется только представителям с включенным флажком.",
        },
        {
            "title": "Плательщик",
            "detail": "Флажок помогает администратору понимать, кто отвечает за личные оплаты.",
        },
        {
            "title": "Единственный основной",
            "detail": "При выборе нового основного прежний основной представитель будет снят автоматически.",
        },
    ]
    if link:
        items.insert(
            0,
            {
                "title": "Текущая связь",
                "detail": f"{link.representative.full_name} - {link.child.full_name}.",
            },
        )
    return items


@login_required
@user_passes_test(is_admin_user)
def recipient_list(request):
    query = request.GET.get("q", "").strip()
    base_qs = Child.objects.select_related("primary_parent")
    filtered_qs = base_qs
    if query:
        filtered_qs = filtered_qs.filter(
            Q(last_name__icontains=query)
            | Q(first_name__icontains=query)
            | Q(middle_name__icontains=query)
            | Q(primary_parent__last_name__icontains=query)
            | Q(primary_parent__first_name__icontains=query)
            | Q(primary_parent__phone__icontains=query)
            | Q(primary_parent__phone_alt__icontains=query)
        )
    matching_count = filtered_qs.count()
    recipients = list(
        filtered_qs
        .annotate(
            legacy_appointments_count=Count("appointments", distinct=True),
            participant_only_appointments_count=Count(
                "appointment_participations__appointment",
                filter=~Q(appointment_participations__appointment__child_id=F("pk")),
                distinct=True,
            ),
            balance_accounts_count=Count("balance_accounts", distinct=True),
        )
        .annotate(
            appointments_count=F("legacy_appointments_count")
            + F("participant_only_appointments_count")
        )
        .order_by("last_name", "first_name")[:80]
    )
    total_count = Child.objects.count()
    waiting_count = Child.objects.filter(status=Child.Status.WAITING).count()
    without_primary_parent_count = Child.objects.filter(primary_parent__isnull=True).count()
    return render(
        request,
        "operations/recipient_list.html",
        {
            "recipients": recipients,
            "query": query,
            "recipient_list_summary_items": _recipient_list_summary_items(
                recipients,
                total_count=total_count,
                matching_count=matching_count,
                waiting_count=waiting_count,
                without_primary_parent_count=without_primary_parent_count,
                query=query,
            ),
            "recipient_list_next_action": _recipient_list_next_action(
                recipients,
                query=query,
                waiting_count=waiting_count,
                without_primary_parent_count=without_primary_parent_count,
            ),
            "recipient_list_control_items": _recipient_list_control_items(query),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def recipient_detail(request, pk: int):
    recipient = get_object_or_404(
        Child.objects.select_related("primary_parent").prefetch_related(
            "representative_links__representative"
        ),
        pk=pk,
    )
    now = timezone.now()
    representative_links = list(recipient.representative_links.all())
    accounts = list(
        recipient.balance_accounts.select_related("funding_source", "service").order_by(
            "funding_source__name",
            "service__name",
        )
    )
    recipient_appointments = Appointment.objects.filter(
        Q(child=recipient) | Q(participants__child=recipient)
    ).distinct()
    upcoming_appointments = list(
        recipient_appointments.filter(starts_at__gte=now)
        .select_related("staff_member", "service", "room", "billing_account")
        .prefetch_related("participants__child", "staff_assignments__staff_member")
        .order_by("starts_at")[:20]
    )
    recent_appointments = list(
        recipient_appointments.filter(starts_at__lt=now)
        .select_related("staff_member", "service", "room", "billing_account")
        .prefetch_related("participants__child", "staff_assignments__staff_member")
        .order_by("-starts_at")[:20]
    )
    programs = list(
        TreatmentProgram.objects.filter(child=recipient)
        .prefetch_related(
            "blocks", "blocks__service", "blocks__staff_member", "blocks__balance_account"
        )
        .order_by("-starts_on", "title")
    )
    return render(
        request,
        "operations/recipient_detail.html",
        {
            "recipient": recipient,
            "representative": getattr(recipient, "primary_parent", None),
            "representative_links": representative_links,
            "accounts": accounts,
            "programs": programs,
            "upcoming_appointments": upcoming_appointments,
            "recent_appointments": recent_appointments,
            "recipient_detail_summary_items": _recipient_detail_summary_items(
                recipient,
                representative_links=representative_links,
                accounts=accounts,
                programs=programs,
                upcoming_appointments=upcoming_appointments,
            ),
            "recipient_detail_next_action": _recipient_detail_next_action(
                recipient,
                representative_links=representative_links,
                accounts=accounts,
                programs=programs,
            ),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def recipient_representative_create(request, child_id: int):
    recipient = get_object_or_404(Child.objects.select_related("primary_parent"), pk=child_id)
    if request.method == "POST":
        form = RecipientRepresentativeForm(request.POST, child=recipient)
        if form.is_valid():
            form.save()
            messages.success(request, "Представитель привязан к получателю.")
            return redirect("recipient_detail", pk=recipient.pk)
    else:
        form = RecipientRepresentativeForm(child=recipient)
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Привязать представителя",
            "subtitle": recipient.full_name,
            "form_panel_title": "Связь с представителем",
            "form_intro": (
                "Задайте роль представителя у конкретного получателя: подписант договора, "
                "получатель расписания и плательщик."
            ),
            "control_title": "Контроль представителя",
            "object_form_control_items": _recipient_representative_control_items(),
            "cancel_url": reverse("recipient_detail", args=[recipient.pk]),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def recipient_representative_edit(request, pk: int):
    link = get_object_or_404(
        RecipientRepresentative.objects.select_related("child", "representative"),
        pk=pk,
    )
    if request.method == "POST":
        form = RecipientRepresentativeForm(request.POST, instance=link, child=link.child)
        if form.is_valid():
            form.save()
            messages.success(request, "Связь с представителем обновлена.")
            return redirect("recipient_detail", pk=link.child_id)
    else:
        form = RecipientRepresentativeForm(instance=link, child=link.child)
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Редактировать связь с представителем",
            "subtitle": link.child.full_name,
            "form_panel_title": "Связь с представителем",
            "form_intro": (
                "Проверьте, кто подписывает договор, кто получает расписание и кто считается "
                "плательщиком по этому получателю."
            ),
            "control_title": "Контроль представителя",
            "object_form_control_items": _recipient_representative_control_items(link),
            "cancel_url": reverse("recipient_detail", args=[link.child_id]),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def representative_create(request):
    if request.method == "POST":
        form = RepresentativeForm(request.POST)
        if form.is_valid():
            representative = form.save()
            messages.success(request, "Представитель создан.")
            return redirect("representative_edit", pk=representative.pk)
    else:
        form = RepresentativeForm()
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Создать представителя",
            "subtitle": "После сохранения можно привязать к нему получателя.",
            "form_panel_title": "Контакты представителя",
            "form_intro": "Заполните контактные данные человека, который может быть связан с одним или несколькими получателями.",
            "control_title": "Контроль контакта",
            "object_form_control_items": _representative_profile_control_items(),
            "cancel_url": reverse("recipient_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def representative_edit(request, pk: int):
    representative = get_object_or_404(ParentGuardian, pk=pk)
    if request.method == "POST":
        form = RepresentativeForm(request.POST, instance=representative)
        if form.is_valid():
            form.save()
            messages.success(request, "Представитель обновлен.")
            return redirect("recipient_list")
    else:
        form = RepresentativeForm(instance=representative)
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Редактировать представителя",
            "subtitle": representative.full_name,
            "form_panel_title": "Контакты представителя",
            "form_intro": "Изменения контактов будут использоваться в карточках получателей и будущих согласованиях.",
            "control_title": "Контроль контакта",
            "object_form_control_items": _representative_profile_control_items(),
            "cancel_url": reverse("recipient_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def recipient_contract_pdf(request, pk: int):
    child = get_object_or_404(Child.objects.select_related("primary_parent"), pk=pk)
    pdf_buf = contract_pdf(child)
    filename = f"contract_{child.last_name}_{child.first_name}.pdf".replace(" ", "_")
    return HttpResponse(
        pdf_buf.read(),
        content_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@login_required
@user_passes_test(is_admin_user)
def recipient_create(request):
    initial = {}
    if request.GET.get("representative_id"):
        initial["primary_parent"] = request.GET["representative_id"]
    if request.method == "POST":
        form = RecipientForm(request.POST)
        if form.is_valid():
            recipient = form.save()
            messages.success(request, "Получатель создан.")
            return redirect("recipient_detail", pk=recipient.pk)
    else:
        form = RecipientForm(initial=initial)
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Создать получателя",
            "subtitle": "Заполните основные данные и выберите представителя.",
            "form_panel_title": "Данные получателя",
            "form_intro": "Эта карточка связывает расписание, программы занятий, счета баланса и документы.",
            "control_title": "Контроль получателя",
            "object_form_control_items": _recipient_form_control_items(),
            "cancel_url": reverse("recipient_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def recipient_edit(request, pk: int):
    recipient = get_object_or_404(Child, pk=pk)
    if request.method == "POST":
        form = RecipientForm(request.POST, instance=recipient)
        if form.is_valid():
            form.save()
            messages.success(request, "Получатель обновлен.")
            return redirect("recipient_detail", pk=recipient.pk)
    else:
        form = RecipientForm(instance=recipient)
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Редактировать получателя",
            "subtitle": recipient.full_name,
            "form_panel_title": "Данные получателя",
            "form_intro": "Проверьте основного представителя, статус, контакты и данные, которые видны в расписании.",
            "control_title": "Контроль получателя",
            "object_form_control_items": _recipient_form_control_items(),
            "cancel_url": reverse("recipient_detail", args=[recipient.pk]),
        },
    )
