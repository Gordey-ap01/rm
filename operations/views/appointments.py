"""Занятия: создание, изменение, перенос, отмена, списание, детальная страница."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from auditlog.models import LogEntry
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.forms import (
    AppointmentCancelForm,
    AppointmentConfirmationSendForm,
    AppointmentForm,
    AppointmentMoveForm,
    AppointmentParticipantProgramForm,
    BillingDecisionForm,
)
from operations.models import Appointment, LedgerEntry

from ._common import is_admin_user, safe_next_url
from .scheduling_helpers import suggested_shift_candidates, suggested_transfer_slots


def appointment_participants_label(appointment, participants=None) -> str:
    if participants is None:
        participants = list(
            appointment.participants.select_related("child").order_by(
                "starts_at_snapshot", "child__last_name", "child__first_name"
            )
        )
    if participants:
        return ", ".join(participant.child.full_name for participant in participants)
    return appointment.child.full_name


def appointment_staff_label(appointment, staff_assignments=None) -> str:
    if staff_assignments is None:
        staff_assignments = list(
            appointment.staff_assignments.select_related("staff_member").order_by(
                "starts_at_snapshot", "staff_member__full_name"
            )
        )
    if staff_assignments:
        return ", ".join(assignment.staff_member.full_name for assignment in staff_assignments)
    return appointment.staff_member.full_name


def appointment_attendance_summary_label(appointment, participants=None) -> str:
    if participants is None:
        participants = list(appointment.participants.order_by("pk"))
    if not participants:
        return appointment.get_attendance_status_display()
    if len(participants) == 1:
        return participants[0].get_attendance_status_display()

    attendance_counts = {
        Appointment.AttendanceStatus.UNKNOWN: 0,
        Appointment.AttendanceStatus.ATTENDED: 0,
        Appointment.AttendanceStatus.MISSED: 0,
    }
    for participant in participants:
        attendance_counts[participant.attendance_status] = (
            attendance_counts.get(participant.attendance_status, 0) + 1
        )
    if attendance_counts[Appointment.AttendanceStatus.UNKNOWN]:
        return (
            "Есть неотмеченные участники: "
            f"{attendance_counts[Appointment.AttendanceStatus.UNKNOWN]} из {len(participants)}"
        )
    if attendance_counts[Appointment.AttendanceStatus.ATTENDED] == len(participants):
        return f"Посетили все участники: {len(participants)}"
    if attendance_counts[Appointment.AttendanceStatus.MISSED] == len(participants):
        return f"Не пришли все участники: {len(participants)}"
    return (
        "Отмечено по участникам: "
        f"пришли {attendance_counts[Appointment.AttendanceStatus.ATTENDED]}, "
        f"не пришли {attendance_counts[Appointment.AttendanceStatus.MISSED]}"
    )


def appointment_billing_summary_label(appointment, participants=None) -> str:
    if participants is None:
        participants = list(appointment.participants.select_related("billing_account").order_by("pk"))
    if not participants:
        return appointment.get_billing_decision_display()
    if len(participants) == 1:
        return participants[0].get_billing_decision_display()

    decision_counts = {
        Appointment.BillingDecision.UNDECIDED: 0,
        Appointment.BillingDecision.CHARGE: 0,
        Appointment.BillingDecision.DO_NOT_CHARGE: 0,
    }
    for participant in participants:
        decision_counts[participant.billing_decision] = (
            decision_counts.get(participant.billing_decision, 0) + 1
        )
    if decision_counts[Appointment.BillingDecision.UNDECIDED]:
        return (
            "Есть нерешенные участники: "
            f"{decision_counts[Appointment.BillingDecision.UNDECIDED]} из {len(participants)}"
        )
    if decision_counts[Appointment.BillingDecision.CHARGE] == len(participants):
        return f"Списать по всем участникам: {len(participants)}"
    if decision_counts[Appointment.BillingDecision.DO_NOT_CHARGE] == len(participants):
        return "Не списывать по всем участникам"
    return (
        "Решено по участникам: "
        f"списать {decision_counts[Appointment.BillingDecision.CHARGE]}, "
        f"не списывать {decision_counts[Appointment.BillingDecision.DO_NOT_CHARGE]}"
    )


def appointment_billing_account_label(appointment, participants=None) -> str:
    if participants is None:
        participants = list(appointment.participants.select_related("billing_account").order_by("pk"))
    if not participants:
        return str(appointment.billing_account) if appointment.billing_account else "Не выбран"
    if len(participants) == 1:
        participant = participants[0]
        return str(participant.billing_account) if participant.billing_account else "Не выбран"
    charged = [
        participant for participant in participants if participant.billing_decision == Appointment.BillingDecision.CHARGE
    ]
    if not charged:
        return "Счета не используются"
    missing = sum(1 for participant in charged if not participant.billing_account_id)
    if missing:
        return f"Не выбран счет у участников: {missing}"
    return f"Счета участников: {len(charged)}"


def appointment_payment_account(appointment, participants=None):
    if participants is None:
        participants = list(appointment.participants.select_related("billing_account").order_by("pk"))
    if not participants:
        return appointment.billing_account
    if len(participants) == 1:
        return participants[0].billing_account
    return None


def _appointment_people_hint(participants, staff_assignments) -> str:
    participant_count = len(participants) if participants else 1
    staff_count = len(staff_assignments) if staff_assignments else 1
    return f"Получателей: {participant_count} · специалистов: {staff_count}"


def appointment_overview_items(appointment, participants, staff_assignments) -> list[dict]:
    local_start = timezone.localtime(appointment.starts_at)
    local_end = timezone.localtime(appointment.ends_at)
    duration_minutes = int((appointment.ends_at - appointment.starts_at).total_seconds() // 60)
    return [
        {
            "label": "Тип",
            "value": appointment.get_session_type_display(),
            "hint": _appointment_people_hint(participants, staff_assignments),
            "tone": "accent" if appointment.session_type == Appointment.SessionType.GROUP else "",
        },
        {
            "label": "Время",
            "value": f"{local_start:%d.%m %H:%M}-{local_end:%H:%M}",
            "hint": f"{duration_minutes} мин",
            "tone": "",
        },
        {
            "label": "Кабинет",
            "value": str(appointment.room) if appointment.room_id else "Не указан",
            "hint": appointment.service.name,
            "tone": "",
        },
        {
            "label": "Статус",
            "value": appointment.get_status_display(),
            "hint": appointment_attendance_summary_label(appointment, participants),
            "tone": appointment.status,
        },
    ]


def appointment_attention_items(
    appointment,
    participants,
    staff_assignments,
    suggested_slots,
    shift_candidates,
) -> list[dict]:
    items = []
    now = timezone.now()
    inactive_statuses = {
        Appointment.Status.CANCELLED,
        Appointment.Status.RESCHEDULED,
    }
    is_actionable_time = appointment.starts_at <= now or appointment.status in {
        Appointment.Status.COMPLETED,
        Appointment.Status.NO_SHOW,
        Appointment.Status.CANCELLED,
    }
    if participants:
        unknown_attendance = sum(
            participant.attendance_status == Appointment.AttendanceStatus.UNKNOWN
            for participant in participants
        )
        unresolved_billing = sum(
            participant.billing_decision == Appointment.BillingDecision.UNDECIDED
            for participant in participants
        )
        charge_without_account = sum(
            participant.billing_decision == Appointment.BillingDecision.CHARGE
            and not participant.billing_account_id
            for participant in participants
        )
    else:
        unknown_attendance = int(
            appointment.attendance_status == Appointment.AttendanceStatus.UNKNOWN
        )
        unresolved_billing = int(
            appointment.billing_decision == Appointment.BillingDecision.UNDECIDED
        )
        charge_without_account = int(
            appointment.billing_decision == Appointment.BillingDecision.CHARGE
            and not appointment.billing_account_id
        )

    if (
        appointment.status not in inactive_statuses
        and is_actionable_time
        and unknown_attendance
    ):
        items.append(
            {
                "tone": "warning",
                "title": "Посещение не отмечено",
                "text": f"Не отмечено участников: {unknown_attendance}.",
                "href": None,
            }
        )
    if is_actionable_time and unresolved_billing:
        items.append(
            {
                "tone": "warning",
                "title": "Решение по списанию не принято",
                "text": f"Нерешенных участников: {unresolved_billing}.",
                "href": "#billing-decision",
            }
        )
    if charge_without_account:
        items.append(
            {
                "tone": "danger",
                "title": "Не выбран счет списания",
                "text": f"Списать нельзя без счета: {charge_without_account}.",
                "href": "#billing-decision",
            }
        )
    if appointment.staff_availability_override or any(
        assignment.override_availability for assignment in staff_assignments
    ):
        items.append(
            {
                "tone": "warning",
                "title": "Есть выход вне графика",
                "text": "Проверьте причину и согласование со специалистом.",
                "href": None,
            }
        )
    if not suggested_slots and shift_candidates:
        items.append(
            {
                "tone": "info",
                "title": "Свободных окон для переноса нет",
                "text": "Ниже показаны занятые окна-кандидаты для ручного сдвига.",
                "href": "#transfer-slots",
            }
        )
    return items


def _form_field_values(form, field_name: str) -> list:
    value = form[field_name].value()
    if value in (None, ""):
        return []
    if isinstance(value, list | tuple | set):
        return list(value)
    return [value]


def _form_model_labels(form, field_name: str) -> list[str]:
    values = _form_field_values(form, field_name)
    if not values:
        return []
    queryset = getattr(form.fields[field_name], "queryset", None)
    if queryset is None:
        return [str(value) for value in values]
    ids = [str(getattr(value, "pk", value)) for value in values if value not in (None, "")]
    objects = {str(obj.pk): str(obj) for obj in queryset.filter(pk__in=ids)}
    return [objects.get(item_id, item_id) for item_id in ids]


def _form_choice_label(form, field_name: str, fallback: str) -> str:
    values = _form_field_values(form, field_name)
    if not values:
        return fallback
    choices = {str(value): str(label) for value, label in form.fields[field_name].choices}
    return choices.get(str(values[0]), fallback)


def _form_scalar_label(form, field_name: str, fallback: str = "Не выбрано") -> str:
    values = _form_field_values(form, field_name)
    if not values:
        return fallback
    return str(values[0])


def appointment_form_summary_items(form, appointment=None) -> list[dict]:
    participants = _form_model_labels(form, "participants") or _form_model_labels(form, "child")
    staff_members = _form_model_labels(form, "staff_members") or _form_model_labels(
        form, "staff_member"
    )
    date_label = _form_scalar_label(form, "date", "Дата не выбрана")
    time_label = _form_scalar_label(form, "time", "время не выбрано")
    duration_label = _form_scalar_label(form, "duration_minutes", "длительность не выбрана")
    return [
        {
            "label": "Режим",
            "value": _form_choice_label(
                form, "session_type", Appointment.SessionType.INDIVIDUAL.label
            ),
            "hint": f"получателей: {len(participants)} · специалистов: {len(staff_members)}",
        },
        {
            "label": "Время",
            "value": f"{date_label} {time_label}",
            "hint": f"{duration_label} мин",
        },
        {
            "label": "Место и услуга",
            "value": (_form_model_labels(form, "room") or ["Кабинет не выбран"])[0],
            "hint": (_form_model_labels(form, "service") or ["Услуга не выбрана"])[0],
        },
        {
            "label": "Сохранение",
            "value": _form_choice_label(form, "status", Appointment.Status.CONFIRMED.label),
            "hint": "редактирование" if appointment else "создание",
        },
    ]


def appointment_form_attention_items(form) -> list[dict]:
    items = []
    if form.room_limit_warning:
        items.append(
            {
                "tone": "warning",
                "title": "Кабинет требует разового разрешения",
                "text": form.room_limit_warning,
            }
        )
    if form.availability_warning:
        items.append(
            {
                "tone": "warning",
                "title": "Специалист вне графика",
                "text": form.availability_warning,
            }
        )
    if form.non_field_errors():
        items.append(
            {
                "tone": "danger",
                "title": "Форма не сохранена",
                "text": "Проверьте конфликт расписания или обязательные поля.",
            }
        )
    return items


def appointment_form_context(form, title: str, appointment=None) -> dict:
    context = {
        "form": form,
        "title": title,
        "appointment": appointment,
        "appointment_form_summary_items": appointment_form_summary_items(form, appointment),
        "appointment_form_attention_items": appointment_form_attention_items(form),
    }
    if appointment:
        context["appointment_participants_label"] = appointment_participants_label(appointment)
    return context


def appointment_audit_entries(
    appointment,
    participants,
    staff_assignments,
    ledger_entries,
    confirmations,
    limit: int = 30,
):
    tracked_objects = [
        appointment,
        *participants,
        *staff_assignments,
        *ledger_entries,
        *confirmations,
    ]
    object_pks_by_model = {}
    for obj in tracked_objects:
        if obj.pk is None:
            continue
        object_pks_by_model.setdefault(obj.__class__, set()).add(str(obj.pk))

    if not object_pks_by_model:
        return []

    content_types = ContentType.objects.get_for_models(*object_pks_by_model.keys())
    query = Q(pk__isnull=True)
    for model, object_pks in object_pks_by_model.items():
        query |= Q(content_type=content_types[model], object_pk__in=object_pks)

    return list(
        LogEntry.objects.filter(query)
        .select_related("actor", "content_type")
        .order_by("-timestamp")[:limit]
    )


def appointment_cancel_summary_items(
    appointment,
    *,
    participants_label: str,
    staff_label: str,
) -> list[dict[str, str]]:
    local_start = timezone.localtime(appointment.starts_at)
    local_end = timezone.localtime(appointment.ends_at)
    return [
        {
            "label": "Получатель",
            "value": participants_label,
            "hint": appointment.get_session_type_display(),
        },
        {
            "label": "Специалист",
            "value": staff_label,
            "hint": appointment.service.name,
        },
        {
            "label": "Время",
            "value": f"{local_start:%d.%m.%Y %H:%M}-{local_end:%H:%M}",
            "hint": str(appointment.room) if appointment.room_id else "кабинет не указан",
        },
        {
            "label": "Текущий статус",
            "value": appointment.get_status_display(),
            "hint": appointment_billing_summary_label(appointment),
        },
    ]


def appointment_cancel_next_action(form: AppointmentCancelForm) -> dict[str, str]:
    if form.is_same_day_cancellation:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Подтвердить списание отдельно",
            "detail": (
                "Это отмена день-в-день. Сохранение изменит статус занятия, "
                "а финансовое решение останется за администратором."
            ),
            "href": "#appointment-cancel-form",
        }

    return {
        "tone": "info",
        "label": "Следующий шаг",
        "title": "Сохранить отмену",
        "detail": "После сохранения решение по списанию нужно принять отдельно в карточке занятия.",
        "href": "#appointment-cancel-form",
    }


def appointment_move_summary_items(
    appointment,
    *,
    participants_label: str,
    staff_label: str,
) -> list[dict[str, str]]:
    local_start = timezone.localtime(appointment.starts_at)
    local_end = timezone.localtime(appointment.ends_at)
    return [
        {
            "label": "Получатель",
            "value": participants_label,
            "hint": appointment.get_session_type_display(),
        },
        {
            "label": "Сейчас",
            "value": f"{local_start:%d.%m.%Y %H:%M}-{local_end:%H:%M}",
            "hint": str(appointment.room) if appointment.room_id else "кабинет не указан",
        },
        {
            "label": "Специалист",
            "value": staff_label,
            "hint": appointment.service.name,
        },
        {
            "label": "Статус",
            "value": appointment.get_status_display(),
            "hint": appointment_billing_summary_label(appointment),
        },
    ]


def appointment_move_next_action(
    form: AppointmentMoveForm,
    *,
    suggested_slots: list,
    shift_candidates: list,
) -> dict[str, str]:
    if form.availability_warning:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Подтвердить выход вне графика",
            "detail": "Выберите другое время или удерживайте кнопку разрешения перед переносом.",
            "href": "#appointment-move-form",
        }

    if suggested_slots:
        return {
            "tone": "info",
            "label": "Следующий шаг",
            "title": "Выбрать предложенное окно",
            "detail": f"Система нашла свободных вариантов: {len(suggested_slots)}.",
            "href": "#transfer-slots",
        }

    if shift_candidates:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Свободных окон нет",
            "detail": "Проверьте занятые окна-кандидаты и решите, что можно сдвинуть вручную.",
            "href": "#transfer-slots",
        }

    return {
        "tone": "info",
        "label": "Следующий шаг",
        "title": "Заполнить новое время",
        "detail": "Подходящих окон пока нет; можно выбрать дату, время, специалиста и кабинет вручную.",
        "href": "#appointment-move-form",
    }


def appointment_detail_context(
    appointment,
    billing_form=None,
    participant_billing_form=None,
    participant_program_form=None,
) -> dict:
    local_day = timezone.localtime(appointment.starts_at).date()
    participants = list(
        appointment.participants.select_related(
            "child",
            "billing_account",
            "program_block",
            "program_block__program",
        ).order_by("starts_at_snapshot", "child__last_name", "child__first_name")
    )
    related_child_ids = {participant.child_id for participant in participants}
    if not related_child_ids and appointment.child_id:
        related_child_ids.add(appointment.child_id)
    related_child_filter = Q(pk__isnull=True)
    if related_child_ids:
        related_child_filter = Q(child_id__in=related_child_ids) | Q(
            participants__child_id__in=related_child_ids
        )
    related_child_appointments = (
        Appointment.objects.filter(related_child_filter)
        .filter(
            starts_at__date__gte=local_day - timedelta(days=7),
            starts_at__date__lte=local_day + timedelta(days=14),
        )
        .exclude(pk=appointment.pk)
        .select_related("staff_member", "service", "room")
        .distinct()
        .order_by("starts_at")[:12]
    )
    ledger_entries = list(
        LedgerEntry.objects.filter(appointment=appointment)
        .select_related("account", "appointment_participant__child", "created_by")
        .order_by("-created_at")
    )
    confirmations = list(
        appointment.confirmations.select_related(
            "participant__child",
            "representative",
            "sent_by",
            "staff_assignment__staff_member",
        ).order_by("-created_at")
    )
    staff_assignments = appointment.staff_assignments.select_related("staff_member").order_by(
        "starts_at_snapshot", "staff_member__full_name"
    )
    staff_assignments = list(staff_assignments)
    participant_billing_rows = []
    participant_error_id = getattr(
        getattr(participant_billing_form, "participant", None), "pk", None
    )
    for participant in participants:
        row_form = (
            participant_billing_form
            if participant_error_id and participant.pk == participant_error_id
            else BillingDecisionForm(appointment=appointment, participant=participant)
        )
        participant_billing_rows.append({"participant": participant, "form": row_form})
    participant_program_rows = []
    participant_program_error_id = getattr(
        getattr(participant_program_form, "participant", None), "pk", None
    )
    for participant in participants:
        row_form = (
            participant_program_form
            if participant_program_error_id and participant.pk == participant_program_error_id
            else AppointmentParticipantProgramForm(appointment=appointment, participant=participant)
        )
        participant_program_rows.append({"participant": participant, "form": row_form})
    suggested_slots = suggested_transfer_slots(appointment)
    shift_candidates = [] if suggested_slots else suggested_shift_candidates(appointment)
    return {
        "appointment": appointment,
        "appointment_participants_label": appointment_participants_label(
            appointment, participants
        ),
        "appointment_staff_label": appointment_staff_label(appointment, staff_assignments),
        "appointment_attendance_summary_label": appointment_attendance_summary_label(
            appointment, participants
        ),
        "appointment_billing_summary_label": appointment_billing_summary_label(
            appointment, participants
        ),
        "appointment_billing_account_label": appointment_billing_account_label(
            appointment, participants
        ),
        "appointment_payment_account": appointment_payment_account(appointment, participants),
        "participants": participants,
        "staff_assignments": staff_assignments,
        "participant_billing_rows": participant_billing_rows,
        "participant_program_rows": participant_program_rows,
        "related_child_appointments": related_child_appointments,
        "suggested_slots": suggested_slots,
        "shift_candidates": shift_candidates,
        "appointment_overview_items": appointment_overview_items(
            appointment, participants, staff_assignments
        ),
        "appointment_attention_items": appointment_attention_items(
            appointment,
            participants,
            staff_assignments,
            suggested_slots,
            shift_candidates,
        ),
        "ledger_entries": ledger_entries,
        "confirmations": confirmations,
        "audit_entries": appointment_audit_entries(
            appointment, participants, staff_assignments, ledger_entries, confirmations
        ),
        "confirmation_form": AppointmentConfirmationSendForm(appointment=appointment),
        "billing_form": billing_form or BillingDecisionForm(appointment=appointment),
        "schedule_date": local_day,
    }


@login_required
@user_passes_test(is_admin_user)
def appointment_detail(request, pk: int):
    appointment = get_object_or_404(
        Appointment.objects.select_related(
            "child", "staff_member", "service", "room", "billing_account"
        ),
        pk=pk,
    )
    return render(
        request, "operations/appointment_detail.html", appointment_detail_context(appointment)
    )


@login_required
@user_passes_test(is_admin_user)
def appointment_create(request):
    initial = {
        "date": request.GET.get("date") or timezone.localdate(),
        "duration_minutes": 30,
    }
    if request.GET.get("child_id"):
        initial["child"] = request.GET["child_id"]
    if request.GET.get("service_id"):
        initial["service"] = request.GET["service_id"]
    if request.GET.get("staff_id"):
        initial["staff_member"] = request.GET["staff_id"]
    if request.GET.get("room_id"):
        initial["room"] = request.GET["room_id"]
    if request.GET.get("time"):
        initial["time"] = request.GET["time"]

    if request.method == "POST":
        form = AppointmentForm(request.POST, actor=request.user)
        if form.is_valid():
            try:
                appointment = form.save()
            except IntegrityError:
                form.add_error(None, "Не удалось сохранить: найден конфликт расписания.")
            else:
                messages.success(request, "Занятие создано.")
                day = timezone.localtime(appointment.starts_at).date()
                return redirect(f"{reverse('schedule')}?{urlencode({'date': day.isoformat()})}")
    else:
        form = AppointmentForm(initial=initial, actor=request.user)
    return render(
        request,
        "operations/appointment_form.html",
        appointment_form_context(form, "Создать занятие"),
    )


@login_required
@user_passes_test(is_admin_user)
def appointment_edit(request, pk: int):
    appointment = get_object_or_404(Appointment, pk=pk)
    if request.method == "POST":
        form = AppointmentForm(request.POST, instance=appointment, actor=request.user)
        if form.is_valid():
            try:
                appointment = form.save()
            except IntegrityError:
                form.add_error(None, "Не удалось сохранить: найден конфликт расписания.")
            else:
                messages.success(request, "Занятие обновлено.")
                return redirect("appointment_detail", pk=appointment.pk)
    else:
        form = AppointmentForm(instance=appointment, actor=request.user)
    return render(
        request,
        "operations/appointment_form.html",
        appointment_form_context(form, "Редактировать занятие", appointment),
    )


@login_required
@user_passes_test(is_admin_user)
def appointment_move(request, pk: int):
    appointment = get_object_or_404(
        Appointment.objects.select_related("child", "service", "staff_member", "room"), pk=pk
    )
    local_start = timezone.localtime(appointment.starts_at)
    initial = {
        "date": request.GET.get("date") or local_start.date(),
        "time": request.GET.get("time") or local_start.time().replace(second=0, microsecond=0),
        "duration_minutes": appointment.duration_minutes,
        "staff_member": request.GET.get("staff_id") or appointment.staff_member_id,
        "room": request.GET.get("room_id") or appointment.room_id,
    }
    if request.method == "POST":
        form = AppointmentMoveForm(request.POST, appointment=appointment, actor=request.user)
        if form.is_valid():
            try:
                new_appointment = form.save()
            except IntegrityError:
                form.add_error(None, "Не удалось перенести: найден конфликт расписания.")
            else:
                messages.success(
                    request,
                    "Занятие перенесено. Решение по списанию исходного занятия остается за администратором.",
                )
                return redirect("appointment_detail", pk=new_appointment.pk)
    else:
        form = AppointmentMoveForm(appointment=appointment, initial=initial, actor=request.user)
    suggested_slots = suggested_transfer_slots(appointment)
    shift_candidates = [] if suggested_slots else suggested_shift_candidates(appointment)
    participants_label = appointment_participants_label(appointment)
    staff_label = appointment_staff_label(appointment)
    return render(
        request,
        "operations/appointment_move.html",
        {
            "form": form,
            "appointment": appointment,
            "appointment_participants_label": participants_label,
            "appointment_staff_label": staff_label,
            "suggested_slots": suggested_slots,
            "shift_candidates": shift_candidates,
            "appointment_move_summary_items": appointment_move_summary_items(
                appointment,
                participants_label=participants_label,
                staff_label=staff_label,
            ),
            "appointment_move_next_action": appointment_move_next_action(
                form,
                suggested_slots=suggested_slots,
                shift_candidates=shift_candidates,
            ),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def appointment_cancel(request, pk: int):
    appointment = get_object_or_404(Appointment, pk=pk)
    if request.method == "POST":
        form = AppointmentCancelForm(request.POST, appointment=appointment)
        if form.is_valid():
            form.save()
            messages.success(
                request, "Статус занятия изменен. Решение по списанию примите отдельно."
            )
            return redirect("appointment_detail", pk=appointment.pk)
    else:
        form = AppointmentCancelForm(
            appointment=appointment, initial={"status": Appointment.Status.CANCELLED}
        )
    participants_label = appointment_participants_label(appointment)
    staff_label = appointment_staff_label(appointment)
    return render(
        request,
        "operations/appointment_cancel.html",
        {
            "form": form,
            "appointment": appointment,
            "appointment_participants_label": participants_label,
            "appointment_staff_label": staff_label,
            "appointment_cancel_summary_items": appointment_cancel_summary_items(
                appointment,
                participants_label=participants_label,
                staff_label=staff_label,
            ),
            "appointment_cancel_next_action": appointment_cancel_next_action(form),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def appointment_billing(request, pk: int):
    appointment = get_object_or_404(Appointment, pk=pk)
    if request.method == "POST":
        form = BillingDecisionForm(request.POST, appointment=appointment)
        if form.is_valid():
            form.save(request.user)
            messages.success(request, "Решение по списанию сохранено.")
            return redirect(
                safe_next_url(request, reverse("appointment_detail", args=[appointment.pk]))
            )
        else:
            messages.error(request, "Решение по списанию не сохранено. Проверьте поля формы.")
            context_kwargs = (
                {"participant_billing_form": form}
                if request.POST.get("participant_id")
                else {"billing_form": form}
            )
            return render(
                request,
                "operations/appointment_detail.html",
                appointment_detail_context(appointment, **context_kwargs),
                status=400,
            )
    return redirect("appointment_detail", pk=appointment.pk)


@login_required
@user_passes_test(is_admin_user)
def appointment_participant_program(request, pk: int):
    appointment = get_object_or_404(Appointment, pk=pk)
    if request.method == "POST":
        form = AppointmentParticipantProgramForm(request.POST, appointment=appointment)
        if form.is_valid():
            participant = form.save()
            messages.success(request, f"Каскад участника «{participant.child}» сохранен.")
            return redirect("appointment_detail", pk=appointment.pk)
        messages.error(request, "Каскад участника не сохранен. Проверьте поля формы.")
        return render(
            request,
            "operations/appointment_detail.html",
            appointment_detail_context(appointment, participant_program_form=form),
            status=400,
        )
    return redirect("appointment_detail", pk=appointment.pk)
