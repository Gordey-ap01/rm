"""Бизнес-логика расписания: проверка пересечений, поиск слотов, массовый перенос."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from operations.models import (
    ACTIVE_APPOINTMENT_STATUSES,
    Appointment,
    AppointmentConfirmation,
    AppointmentParticipant,
    AppointmentStaffAssignment,
    StaffAvailability,
    StaffMember,
    TimeOffRequest,
    room_usage_counts,
)
from operations.schedule_validation import (
    appointment_group_conflicts,
    build_local_datetime,
    conflict_messages,
)


@dataclass(frozen=True)
class ConflictReport:
    """Результат проверки пересечений."""

    child_conflict: Appointment | None
    staff_conflict: Appointment | None
    room_conflict: Appointment | None
    room_capacity: int = 1
    room_occupancy: int = 0
    room_staff_occupancy: int = 0
    room_recipient_occupancy: int = 0

    @property
    def has_conflict(self) -> bool:
        return any((self.child_conflict, self.staff_conflict, self.room_conflict))

    def human_messages(self) -> list[str]:
        out: list[str] = []
        if self.child_conflict:
            local_start = timezone.localtime(self.child_conflict.starts_at)
            out.append(
                f"у получателя уже есть занятие в это время "
                f"({local_start:%d.%m %H:%M})"
            )
        if self.staff_conflict:
            out.append("специалист уже занят в это время")
        if self.room_conflict:
            out.append("кабинет уже занят в это время")
        return out


def find_overlaps(
    starts_at: datetime,
    ends_at: datetime,
    *,
    child: Any = None,
    staff_member: Any = None,
    room: Any = None,
    exclude_pk: int | None = None,
) -> ConflictReport:
    """Ищет пересекающиеся активные занятия (по ребёнку, специалисту или кабинету).

    Новая модель хранит получателей и специалистов в отдельных таблицах.
    Старые поля ``Appointment.child`` и ``Appointment.staff_member`` остаются
    переходным fallback до полного перевода UI.
    """
    qs = (
        Appointment.objects.filter(
            status__in=ACTIVE_APPOINTMENT_STATUSES,
            starts_at__lt=ends_at,
            ends_at__gt=starts_at,
        )
        .select_related("child", "staff_member", "service", "room")
        .order_by("starts_at")
    )
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)

    child_conflict = None
    if child:
        participant_qs = AppointmentParticipant.objects.filter(
            appointment_status__in=ACTIVE_APPOINTMENT_STATUSES,
            child=child,
            starts_at_snapshot__lt=ends_at,
            ends_at_snapshot__gt=starts_at,
        ).select_related("appointment", "appointment__child", "appointment__staff_member", "appointment__service", "appointment__room")
        if exclude_pk:
            participant_qs = participant_qs.exclude(appointment_id=exclude_pk)
        participant = participant_qs.first()
        child_conflict = participant.appointment if participant else qs.filter(child=child).first()

    staff_conflict = None
    if staff_member:
        assignment_qs = AppointmentStaffAssignment.objects.filter(
            appointment_status__in=ACTIVE_APPOINTMENT_STATUSES,
            staff_member=staff_member,
            starts_at_snapshot__lt=ends_at,
            ends_at_snapshot__gt=starts_at,
        ).select_related("appointment", "appointment__child", "appointment__staff_member", "appointment__service", "appointment__room")
        if exclude_pk:
            assignment_qs = assignment_qs.exclude(appointment_id=exclude_pk)
        assignment = assignment_qs.first()
        staff_conflict = assignment.appointment if assignment else qs.filter(staff_member=staff_member).first()

    room_qs = qs.filter(room=room) if room else qs.none()
    room_staff_occupancy = 0
    room_recipient_occupancy = 0
    if room:
        room_staff_occupancy, room_recipient_occupancy = room_usage_counts(room_qs)
    room_occupancy = max(room_staff_occupancy, room_recipient_occupancy)
    room_capacity = (
        min(
            getattr(room, "effective_max_staff_count", max(getattr(room, "capacity", 1) or 1, 1)),
            getattr(room, "effective_max_recipient_count", max(getattr(room, "capacity", 1) or 1, 1)),
        )
        if room
        else 1
    )
    room_conflict = None
    if room:
        incoming_staff = 1 if staff_member else 0
        incoming_recipient = 1 if child else 0
        staff_over_limit = (
            room.limit_staff_count
            and room_staff_occupancy + incoming_staff > room.effective_max_staff_count
        )
        recipient_over_limit = (
            room.limit_recipient_count
            and room_recipient_occupancy + incoming_recipient > room.effective_max_recipient_count
        )
        if staff_over_limit or recipient_over_limit:
            room_conflict = room_qs.first()

    return ConflictReport(
        child_conflict=child_conflict,
        staff_conflict=staff_conflict,
        room_conflict=room_conflict,
        room_capacity=room_capacity,
        room_occupancy=room_occupancy,
        room_staff_occupancy=room_staff_occupancy,
        room_recipient_occupancy=room_recipient_occupancy,
    )


def is_within_availability(
    staff_member: StaffMember | None,
    starts_at: datetime | None,
    ends_at: datetime | None,
) -> str:
    """Возвращает ``""`` если время попадает в доступность, иначе — человекочитаемую причину.

    Учитывает:
    - активные ``TimeOffRequest`` (одобренные);
    - ``StaffAvailability`` (если есть) или базовое окно 09:00–18:00 (fallback).
    """
    if not staff_member or not starts_at or not ends_at:
        return ""

    local_start = timezone.localtime(starts_at)
    local_end = timezone.localtime(ends_at)
    day = local_start.date()
    if local_end.date() != day:
        return "занятие должно помещаться в один рабочий день"

    if TimeOffRequest.objects.filter(
        staff_member=staff_member,
        status=TimeOffRequest.Status.APPROVED,
        starts_on__lte=day,
        ends_on__gte=day,
    ).exists():
        return "у специалиста согласован отпуск/отгул на эту дату"

    windows = list(
        StaffAvailability.objects.filter(
            staff_member=staff_member,
            weekday=day.weekday(),
            is_active=True,
        ).order_by("starts_at")
    )
    start_time = local_start.time().replace(second=0, microsecond=0)
    end_time = local_end.time().replace(second=0, microsecond=0)

    if not windows:
        if time(9, 0) <= start_time and end_time <= time(18, 0):
            return ""
        return "время вне базового рабочего окна 09:00-18:00"

    if any(w.starts_at <= start_time and end_time <= w.ends_at for w in windows):
        return ""
    return "время вне рабочего графика специалиста"


def find_free_slots(
    day: date,
    duration_minutes: int,
    *,
    staff_member: StaffMember | None = None,
    child: Any = None,
    room: Any = None,
    slot_step_minutes: int = 30,
    start_hour: int = 9,
    end_hour: int = 18,
) -> list[datetime]:
    """Возвращает список свободных ``starts_at`` (datetime) на день.

    Алгоритм:
    - генерируем слоты с шагом ``slot_step_minutes`` от ``start_hour`` до ``end_hour - duration``;
    - исключаем слоты, которые пересекаются с любым активным занятием по (child, staff, room);
    - исключаем слоты вне окна доступности (если указан ``staff_member``).
    """
    if day < timezone.localdate():
        return []

    starts: list[datetime] = []
    step = timedelta(minutes=slot_step_minutes)
    window = timedelta(minutes=duration_minutes)
    cursor = build_local_datetime(day, time(start_hour, 0))
    window_end = build_local_datetime(day, time(end_hour, 0))

    while cursor + window <= window_end:
        slot_end = cursor + window
        report = find_overlaps(cursor, slot_end, child=child, staff_member=staff_member, room=room)
        if not report.has_conflict and not is_within_availability(staff_member, cursor, slot_end):
            starts.append(cursor)
        cursor += step
    return starts


@dataclass
class MassRescheduleResult:
    """Результат массового переноса при отсутствии специалиста."""

    staff: StaffMember
    date_from: date
    date_to: date
    cancelled: list[Appointment]
    confirmations: list[AppointmentConfirmation]
    suggested_slots_by_appointment: dict[int, list[datetime]]


@dataclass(frozen=True)
class RepresentativeConfirmationTarget:
    child: Any
    representative: Any
    participant: AppointmentParticipant | None


def representative_confirmation_targets(
    appointment: Appointment,
) -> list[RepresentativeConfirmationTarget]:
    participants = list(
        appointment.participants.select_related("child", "child__primary_parent").order_by(
            "starts_at_snapshot", "child__last_name", "child__first_name"
        )
    )
    if participants:
        child_rows = [(participant.child, participant) for participant in participants]
        if appointment.child_id and all(
            participant.child_id != appointment.child_id for participant in participants
        ):
            child_rows.insert(0, (appointment.child, None))
    else:
        child_rows = [(appointment.child, None)]

    targets: list[RepresentativeConfirmationTarget] = []
    for child, participant in child_rows:
        representative_links = list(
            child.representative_links.select_related("representative").order_by(
                "-is_primary", "representative__last_name", "representative__first_name"
            )
        )
        if representative_links:
            for link in representative_links:
                representative = link.representative
                if (
                    not link.receives_schedule
                    or not representative.email
                ):
                    continue
                targets.append(
                    RepresentativeConfirmationTarget(
                        child=child,
                        representative=representative,
                        participant=participant,
                    )
                )
            continue

        representative = child.primary_parent
        if (
            not representative
            or not representative.email
        ):
            continue
        targets.append(
            RepresentativeConfirmationTarget(
                child=child,
                representative=representative,
                participant=participant,
            )
        )
    return targets


def appointment_children_for_reschedule(appointment: Appointment) -> list[Any]:
    participants = list(appointment.participants.select_related("child").order_by("pk"))
    if not participants:
        return [appointment.child]
    children = [participant.child for participant in participants]
    if appointment.child_id and all(child.pk != appointment.child_id for child in children):
        children.insert(0, appointment.child)
    return children


@transaction.atomic
def mass_reschedule(
    staff: StaffMember,
    *,
    date_from: date,
    date_to: date,
    reason: str,
    actor: Any = None,
    same_category_only: bool = True,
    max_suggestions_per_appointment: int = 5,
) -> MassRescheduleResult:
    """Отменяет все активные занятия специалиста в диапазоне и предлагает слоты.

    Шаги:
    1. Все ``Appointment`` со статусом ``proposed/confirmed/reserved`` в диапазоне → ``cancelled``;
       (завершённые/уже отменённые не трогаем).
    2. Для каждого отменённого — создаём ``AppointmentConfirmation`` представителю
       с шаблоном "Ваше занятие отменено, предлагаем слоты: ..."
    3. Ищем свободные слоты у других активных специалистов той же ``Service.Category``
       (если ``same_category_only``) на ближайшие 14 дней.
    """
    if date_to < date_from:
        raise ValueError("Дата окончания не может быть раньше даты начала.")

    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(date_from, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(date_to, time.max), tz)

    active_statuses = [
        Appointment.Status.PROPOSED,
        Appointment.Status.CONFIRMED,
        Appointment.Status.RESERVED,
    ]
    appointments = list(
        Appointment.objects.filter(
            Q(staff_member=staff) | Q(staff_assignments__staff_member=staff),
            starts_at__gte=start_dt,
            starts_at__lte=end_dt,
            status__in=active_statuses,
        )
        .distinct()
        .select_related("child", "service", "child__primary_parent")
        .prefetch_related(
            "participants__child__primary_parent",
            "participants__child__representative_links__representative",
            "staff_assignments__staff_member",
        )
    )

    cancelled: list[Appointment] = []
    confirmations: list[AppointmentConfirmation] = []
    for appt in appointments:
        appt.status = Appointment.Status.CANCELLED
        appt.admin_note = "\n".join(
            part for part in [appt.admin_note, f"Массовая отмена ({reason})."] if part
        )
        appt.save(update_fields=["status", "admin_note", "updated_at"])
        cancelled.append(appt)

        for target in representative_confirmation_targets(appt):
            local_start = timezone.localtime(appt.starts_at)
            confirmation = AppointmentConfirmation.objects.create(
                appointment=appt,
                target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
                representative=target.representative,
                participant=target.participant,
                email=target.representative.email,
                subject=f"Занятие {local_start:%d.%m.%Y %H:%M} отменено",
                message=(
                    f"Занятие {target.child} у {staff.full_name} "
                    f"{local_start:%d.%m.%Y в %H:%M} отменено по причине: {reason}. "
                    "Администратор свяжется с вами для согласования нового времени."
                ),
                sent_by=actor,
            )
            confirmations.append(confirmation)

    suggestions: dict[int, list[datetime]] = {}
    if same_category_only and cancelled:
        peer_staff = list(
            StaffMember.objects.filter(
                status=StaffMember.Status.ACTIVE,
            )
            .exclude(pk=staff.pk)
            .order_by("full_name")
        )
        for appt in cancelled:
            slots: list[datetime] = []
            children = appointment_children_for_reschedule(appt)
            for peer in peer_staff:
                peer_slots = find_free_slots(
                    date_from,
                    appt.duration_minutes,
                    staff_member=peer,
                    child=None,
                    room=None,
                )
                for slot in peer_slots:
                    conflicts = appointment_group_conflicts(
                        slot,
                        slot + timedelta(minutes=appt.duration_minutes),
                        children,
                        [peer],
                        None,
                        exclude_pk=appt.pk,
                    )
                    if conflict_messages(conflicts):
                        continue
                    slots.append(slot)
                    if len(slots) >= max_suggestions_per_appointment:
                        break
                if len(slots) >= max_suggestions_per_appointment:
                    break
            suggestions[appt.pk] = slots

    return MassRescheduleResult(
        staff=staff,
        date_from=date_from,
        date_to=date_to,
        cancelled=cancelled,
        confirmations=confirmations,
        suggested_slots_by_appointment=suggestions,
    )


def filter_staff_by_category(
    category: str,
    *,
    exclude: StaffMember | None = None,
) -> QuerySet[StaffMember]:
    qs = StaffMember.objects.filter(status=StaffMember.Status.ACTIVE)
    if exclude:
        qs = qs.exclude(pk=exclude.pk)
    if category:
        from operations.models import Service

        # специалисты, которые вели хотя бы одно занятие этой категории за последние 90 дней
        recent = Appointment.objects.filter(
            service__category=category,
            starts_at__gte=timezone.now() - timedelta(days=90),
        ).values_list("staff_member_id", flat=True)
        qs = qs.filter(Q(pk__in=set(recent)) | Q(specializations__icontains=Service.Category.labels[0]))
    return qs.order_by("full_name")
