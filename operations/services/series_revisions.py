"""Version and execution journal helpers for appointment series."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentSeries,
    AppointmentSeriesMaterializationResult,
    AppointmentSeriesMaterializationRun,
    AppointmentSeriesMaterializationRunEvent,
    AppointmentSeriesOccurrence,
    AppointmentSeriesParticipant,
    AppointmentSeriesRetryTarget,
    AppointmentSeriesRevision,
    AppointmentSeriesRevisionParticipant,
    AppointmentSeriesRevisionStaffAssignment,
    AppointmentSeriesStaffAssignment,
    BalanceAccount,
    Child,
    ProgramBlock,
    Room,
    StaffMember,
    TreatmentProgram,
    normalize_immutable_reason,
)
from operations.services.authority import AuthorityRole, authority_role


class SeriesRevisionMismatch(ValidationError):
    """The mutable root projection no longer matches its immutable revision."""
class SeriesRetryMismatch(ValidationError):
    """A frozen retry target no longer matches the append-only result chain."""




@dataclass(frozen=True)
class SeriesParticipantInput:
    child_id: int
    program_block_id: int
    billing_account_id: int | None
    position: int


@dataclass(frozen=True)
class SeriesStaffInput:
    staff_member_id: int
    role: str
    override_availability: bool = False
    override_reason: str = ""
@dataclass(frozen=True)
class _RetryCandidate:
    scheduled_starts_at: datetime
    scheduled_date: date
    chain_head: AppointmentSeriesMaterializationResult
    effective_skipped: AppointmentSeriesMaterializationResult




def canonical_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_operator_role(actor: Any) -> str:
    role = authority_role(actor)
    if role not in {AuthorityRole.DIRECTOR, AuthorityRole.ADMINISTRATOR}:
        raise PermissionDenied("Сериями занятий управляет администратор или руководитель.")
    return role.value


def _series_composition(
    series: AppointmentSeries,
) -> tuple[list[AppointmentSeriesParticipant], list[AppointmentSeriesStaffAssignment]]:
    participants = list(
        series.default_participants.select_related(
            "child",
            "program_block__program",
            "billing_account",
        ).order_by("position", "pk")
    )
    assignments = list(
        series.default_staff_assignments.select_related("staff_member").order_by("pk")
    )
    return participants, assignments


def _validate_composition(
    series: AppointmentSeries,
    participants: list[AppointmentSeriesParticipant],
    assignments: list[AppointmentSeriesStaffAssignment],
    *,
    allow_single_legacy_group: bool = False,
) -> None:
    if not participants:
        raise ValidationError("У серии должен быть хотя бы один получатель.")
    positions = [participant.position for participant in participants]
    if len(positions) != len(set(positions)):
        raise ValidationError("Позиции получателей серии не должны повторяться.")

    if series.materialization_mode == AppointmentSeries.MaterializationMode.JOIN_EXISTING:
        if series.session_type != "group" or len(participants) != 1 or assignments:
            raise ValidationError(
                "Операция присоединения хранит одного получателя без собственного состава "
                "специалистов."
            )
        return

    primary_count = sum(
        assignment.role == AppointmentSeriesStaffAssignment.Role.PRIMARY
        for assignment in assignments
    )
    if primary_count != 1:
        raise ValidationError("В серии должен быть ровно один основной специалист.")
    if series.session_type == "individual" and (
        len(participants) != 1 or len(assignments) != 1
    ):
        raise ValidationError(
            "Индивидуальная серия содержит одного получателя и одного специалиста."
        )
    if (
        series.session_type == "group"
        and len(participants) < 2
        and not allow_single_legacy_group
    ):
        raise ValidationError("Групповая серия должна содержать минимум двух получателей.")


def _revision_payload(
    series: AppointmentSeries,
    participants: Iterable[AppointmentSeriesParticipant],
    assignments: Iterable[AppointmentSeriesStaffAssignment],
    *,
    revision_number: int,
) -> dict[str, Any]:
    return {
        "series_id": series.pk,
        "revision_number": revision_number,
        "title": series.title,
        "service_id": series.service_id,
        "room_id": series.room_id,
        "start_date": series.start_date,
        "end_date": series.end_date,
        "days_of_week": series.days_of_week,
        "time": series.time,
        "duration_minutes": series.duration_minutes,
        "session_type": series.session_type,
        "materialization_mode": series.materialization_mode,
        "default_appointment_status": series.default_appointment_status,
        "allow_unpaid_reserve": series.allow_unpaid_reserve,
        "allow_outside_availability": series.allow_outside_availability,
        "override_reason": series.override_reason,
        "participants": [
            (
                item.child_id,
                item.program_block_id,
                item.billing_account_id,
                item.position,
            )
            for item in participants
        ],
        "staff": [
            (
                item.staff_member_id,
                item.role,
                item.override_availability,
                item.override_reason,
            )
            for item in assignments
        ],
    }


def assert_current_projection(
    series: AppointmentSeries,
    revision: AppointmentSeriesRevision,
) -> None:
    if series.current_revision_id != revision.pk or revision.series_id != series.pk:
        raise SeriesRevisionMismatch(
            "Текущая редакция серии изменилась во время материализации."
        )
    participants, assignments = _series_composition(series)
    _validate_composition(
        series,
        participants,
        assignments,
        allow_single_legacy_group=(
            revision.provenance_kind
            != AppointmentSeriesRevision.ProvenanceKind.NATIVE
        ),
    )
    payload = _revision_payload(
        series,
        participants,
        assignments,
        revision_number=revision.revision_number,
    )
    if canonical_fingerprint(payload) != revision.fingerprint:
        raise SeriesRevisionMismatch(
            "Текущая проекция серии не совпадает с immutable-редакцией."
        )


@transaction.atomic
def ensure_initial_revision(
    series: AppointmentSeries,
    *,
    actor: Any,
) -> tuple[AppointmentSeriesRevision, bool]:
    role = require_operator_role(actor)
    locked = AppointmentSeries.objects.select_for_update().get(pk=series.pk)
    if locked.current_revision_id:
        revision = AppointmentSeriesRevision.objects.get(pk=locked.current_revision_id)
        assert_current_projection(locked, revision)
        return revision, False
    if AppointmentSeriesOccurrence.objects.filter(series=locked).exists():
        raise ValidationError(
            "Существующая история серии должна пройти legacy backfill до нового запуска."
        )

    participants, assignments = _series_composition(locked)
    _validate_composition(locked, participants, assignments)
    payload = _revision_payload(
        locked,
        participants,
        assignments,
        revision_number=1,
    )
    now = timezone.now()
    revision = AppointmentSeriesRevision.objects.create(
        series=locked,
        revision_number=1,
        event_type=AppointmentSeriesRevision.EventType.CREATED,
        provenance_kind=AppointmentSeriesRevision.ProvenanceKind.NATIVE,
        effective_from=locked.start_date,
        title=locked.title,
        service=locked.service,
        room=locked.room,
        start_date=locked.start_date,
        end_date=locked.end_date,
        days_of_week=locked.days_of_week,
        time=locked.time,
        duration_minutes=locked.duration_minutes,
        session_type=locked.session_type,
        materialization_mode=locked.materialization_mode,
        default_appointment_status=locked.default_appointment_status,
        allow_unpaid_reserve=locked.allow_unpaid_reserve,
        allow_outside_availability=locked.allow_outside_availability,
        override_reason=locked.override_reason,
        fingerprint=canonical_fingerprint(payload),
        actor=actor,
        actor_role_snapshot=role,
        reason="Первичная редакция при создании серии.",
        decided_at=now,
    )
    for participant in participants:
        AppointmentSeriesRevisionParticipant.objects.create(
            revision=revision,
            child=participant.child,
            program_block=participant.program_block,
            billing_account=participant.billing_account,
            position=participant.position,
        )
    for assignment in assignments:
        AppointmentSeriesRevisionStaffAssignment.objects.create(
            revision=revision,
            staff_member=assignment.staff_member,
            role=assignment.role,
            override_availability=assignment.override_availability,
            override_reason=assignment.override_reason,
        )
    AppointmentSeries.objects.filter(pk=locked.pk).update(
        current_revision=revision,
        updated_at=now,
    )
    return revision, True


@transaction.atomic
def revise_future_composition(
    series: AppointmentSeries,
    *,
    expected_revision_id: int,
    effective_from: date,
    participants: Iterable[SeriesParticipantInput],
    staff_assignments: Iterable[SeriesStaffInput],
    actor: Any,
    reason: str,
) -> AppointmentSeriesRevision:
    role = require_operator_role(actor)
    participant_inputs = tuple(sorted(participants, key=lambda item: item.position))
    staff_inputs = tuple(staff_assignments)
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValidationError("Укажите основание изменения будущего состава.")
    if not participant_inputs:
        raise ValidationError("В будущей редакции нужен хотя бы один получатель.")
    if len({item.child_id for item in participant_inputs}) != len(participant_inputs):
        raise ValidationError("Получатель не должен повторяться в составе редакции.")
    positions = [item.position for item in participant_inputs]
    if any(position < 1 for position in positions) or len(set(positions)) != len(
        positions
    ):
        raise ValidationError("Позиции получателей должны быть положительными и уникальными.")
    if len({item.staff_member_id for item in staff_inputs}) != len(staff_inputs):
        raise ValidationError("Специалист не должен повторяться в составе редакции.")
    allowed_roles = set(AppointmentSeriesStaffAssignment.Role.values)
    if any(item.role not in allowed_roles for item in staff_inputs):
        raise ValidationError("В составе редакции указана неизвестная роль специалиста.")
    primary_count = sum(
        item.role == AppointmentSeriesStaffAssignment.Role.PRIMARY
        for item in staff_inputs
    )
    if primary_count != 1:
        raise ValidationError("В будущей редакции нужен ровно один основной специалист.")
    if any(
        item.override_availability and len(item.override_reason.strip()) < 5
        for item in staff_inputs
    ):
        raise ValidationError("Для выхода специалиста вне графика укажите основание.")

    locked = AppointmentSeries.objects.select_for_update().get(pk=series.pk)
    if locked.current_revision_id != expected_revision_id:
        raise SeriesRevisionMismatch("Состав серии уже изменен другим пользователем.")
    if locked.status != AppointmentSeries.Status.ACTIVE:
        raise ValidationError("Будущий состав можно изменить только у активной серии.")
    if (
        locked.materialization_mode
        != AppointmentSeries.MaterializationMode.CREATE_APPOINTMENTS
    ):
        raise ValidationError("Состав join-серии является историей выбранной операции.")
    previous = AppointmentSeriesRevision.objects.get(pk=expected_revision_id)
    assert_current_projection(locked, previous)
    today = timezone.localdate()
    if effective_from <= today:
        raise ValidationError("Новая редакция должна начинаться с будущей даты.")
    if effective_from <= previous.effective_from:
        raise ValidationError("Дата новой редакции должна быть позже текущей редакции.")
    if effective_from > previous.end_date:
        raise ValidationError("Дата новой редакции находится за пределами серии.")

    if locked.session_type == "individual" and (
        len(participant_inputs) != 1 or len(staff_inputs) != 1
    ):
        raise ValidationError(
            "Индивидуальная редакция содержит одного получателя и специалиста."
        )
    if locked.session_type == "group" and len(participant_inputs) < 2:
        raise ValidationError("Групповая редакция содержит минимум двух получателей.")
    if locked.room_id is None:
        raise ValidationError("Для серии должен быть выбран кабинет.")
    locked.room = Room.objects.select_for_update().get(pk=locked.room_id)
    if not locked.room.is_active:
        raise ValidationError("Будущий состав нельзя назначить в неактивный кабинет.")
    if locked.session_type == "group" and not locked.room.allow_group_sessions:
        raise ValidationError("Кабинет не разрешает групповые занятия.")
    if (
        locked.room.limit_recipient_count
        and len(participant_inputs) > locked.room.max_recipient_count
    ):
        raise ValidationError("Будущий состав превышает лимит получателей кабинета.")
    if locked.room.limit_staff_count and len(staff_inputs) > locked.room.max_staff_count:
        raise ValidationError("Будущий состав превышает лимит специалистов кабинета.")

    child_ids = {item.child_id for item in participant_inputs}
    children = Child.objects.in_bulk(child_ids)
    if len(children) != len(child_ids):
        raise ValidationError("Один из получателей будущего состава не найден.")
    block_ids = sorted({item.program_block_id for item in participant_inputs})
    blocks = {
        block.pk: block
        for block in ProgramBlock.objects.select_for_update(of=("self",))
        .select_related("program", "service", "balance_account")
        .filter(pk__in=block_ids)
        .order_by("pk")
    }
    if len(blocks) != len(block_ids):
        raise ValidationError("Один из каскадов будущего состава не найден.")
    program_ids = sorted({block.program_id for block in blocks.values()})
    programs = {
        program.pk: program
        for program in TreatmentProgram.objects.select_for_update()
        .filter(pk__in=program_ids)
        .order_by("pk")
    }
    account_ids = sorted(
        {
            item.billing_account_id
            for item in participant_inputs
            if item.billing_account_id is not None
        }
    )
    accounts = {
        account.pk: account
        for account in BalanceAccount.all_objects.select_for_update()
        .filter(pk__in=account_ids)
        .order_by("pk")
    }
    if len(accounts) != len(account_ids):
        raise ValidationError("Один из счетов будущего состава не найден.")
    for item in participant_inputs:
        block = blocks[item.program_block_id]
        program = programs[block.program_id]
        account = accounts.get(item.billing_account_id)
        if program.child_id != item.child_id or block.service_id != locked.service_id:
            raise ValidationError("Каскад не соответствует получателю или услуге серии.")
        if block.status in {ProgramBlock.Status.COMPLETED, ProgramBlock.Status.CANCELLED}:
            raise ValidationError("Завершенный или отмененный каскад нельзя назначить серии.")
        if program.status != TreatmentProgram.Status.ACTIVE:
            raise ValidationError("Программа получателя должна быть активна.")
        if program.starts_on and effective_from < program.starts_on:
            raise ValidationError("Редакция начинается раньше программы получателя.")
        if program.ends_on and previous.end_date > program.ends_on:
            raise ValidationError(
                "Программа получателя заканчивается раньше будущего периода серии."
            )
        if block.balance_account_id != item.billing_account_id:
            raise ValidationError("Счет будущего состава должен совпадать со счетом каскада.")
        if account and (
            account.child_id != item.child_id or not account.can_pay_for(locked.service)
        ):
            raise ValidationError("Счет не принадлежит получателю или не подходит услуге.")
        if account is None and not locked.allow_unpaid_reserve:
            raise ValidationError("Для оплачиваемой серии укажите счет каждого получателя.")

    staff_ids = sorted({item.staff_member_id for item in staff_inputs})
    staff = {
        member.pk: member
        for member in StaffMember.all_objects.select_for_update()
        .filter(pk__in=staff_ids)
        .order_by("pk")
    }
    if len(staff) != len(staff_ids):
        raise ValidationError("Один из специалистов будущего состава не найден.")
    if any(
        member.is_archived or member.status == StaffMember.Status.INACTIVE
        for member in staff.values()
    ):
        raise ValidationError("В будущий состав нельзя включить неактивного специалиста.")
    inputs_by_staff = {item.staff_member_id: item for item in staff_inputs}
    if any(
        member.status != StaffMember.Status.ACTIVE
        and not inputs_by_staff[member.pk].override_availability
        for member in staff.values()
    ):
        raise ValidationError(
            "Отпуск или больничный специалиста требует явного разрешения выхода вне графика."
        )

    AppointmentSeriesParticipant.objects.filter(series=locked).delete()
    AppointmentSeriesStaffAssignment.objects.filter(series=locked).delete()
    current_participants = [
        AppointmentSeriesParticipant.objects.create(
            series=locked,
            child_id=item.child_id,
            program_block_id=item.program_block_id,
            billing_account_id=item.billing_account_id,
            position=item.position,
        )
        for item in participant_inputs
    ]
    current_staff = [
        AppointmentSeriesStaffAssignment.objects.create(
            series=locked,
            staff_member_id=item.staff_member_id,
            role=item.role,
            override_availability=item.override_availability,
            override_reason=item.override_reason.strip(),
        )
        for item in staff_inputs
    ]
    primary_participant = current_participants[0]
    primary_staff = next(
        item
        for item in current_staff
        if item.role == AppointmentSeriesStaffAssignment.Role.PRIMARY
    )
    locked.child_id = primary_participant.child_id
    locked.program_block_id = primary_participant.program_block_id
    locked.staff_member_id = primary_staff.staff_member_id
    next_number = previous.revision_number + 1
    payload = _revision_payload(
        locked,
        current_participants,
        current_staff,
        revision_number=next_number,
    )
    now = timezone.now()
    revision = AppointmentSeriesRevision.objects.create(
        series=locked,
        revision_number=next_number,
        event_type=AppointmentSeriesRevision.EventType.FUTURE_COMPOSITION,
        provenance_kind=AppointmentSeriesRevision.ProvenanceKind.NATIVE,
        effective_from=effective_from,
        title=previous.title,
        service=previous.service,
        room=previous.room,
        start_date=previous.start_date,
        end_date=previous.end_date,
        days_of_week=previous.days_of_week,
        time=previous.time,
        duration_minutes=previous.duration_minutes,
        session_type=previous.session_type,
        materialization_mode=previous.materialization_mode,
        default_appointment_status=previous.default_appointment_status,
        allow_unpaid_reserve=previous.allow_unpaid_reserve,
        allow_outside_availability=previous.allow_outside_availability,
        override_reason=previous.override_reason,
        fingerprint=canonical_fingerprint(payload),
        actor=actor,
        actor_role_snapshot=role,
        reason=reason,
        supersedes=previous,
        decided_at=now,
    )
    for item in current_participants:
        AppointmentSeriesRevisionParticipant.objects.create(
            revision=revision,
            child_id=item.child_id,
            program_block_id=item.program_block_id,
            billing_account_id=item.billing_account_id,
            position=item.position,
        )
    for item in current_staff:
        AppointmentSeriesRevisionStaffAssignment.objects.create(
            revision=revision,
            staff_member_id=item.staff_member_id,
            role=item.role,
            override_availability=item.override_availability,
            override_reason=item.override_reason,
        )
    AppointmentSeries.objects.filter(pk=locked.pk).update(
        child_id=locked.child_id,
        program_block_id=locked.program_block_id,
        staff_member_id=locked.staff_member_id,
        current_revision=revision,
        updated_at=now,
    )
    locked.current_revision_id = revision.pk
    return revision


def _run_payload(
    series: AppointmentSeries,
    revision: AppointmentSeriesRevision,
    *,
    actor: Any,
    reason: str,
    expected_result_count: int,
    target_appointment_ids: Iterable[int] = (),
) -> dict[str, Any]:
    return {
        "series_id": series.pk,
        "revision_id": revision.pk,
        "revision_fingerprint": revision.fingerprint,
        "mode": AppointmentSeriesMaterializationRun.Mode.INITIAL,
        "date_from": revision.start_date,
        "date_to": revision.end_date,
        "expected_result_count": expected_result_count,
        "actor_id": actor.pk,
        "reason": reason,
        "target_appointment_ids": sorted(set(target_appointment_ids)),
    }


@transaction.atomic
def get_or_create_initial_run(
    series: AppointmentSeries,
    revision: AppointmentSeriesRevision,
    *,
    operation_key: UUID,
    actor: Any,
    expected_result_count: int,
    target_appointment_ids: Iterable[int] = (),
) -> tuple[AppointmentSeriesMaterializationRun, bool]:
    role = require_operator_role(actor)
    reason = "Первичная материализация серии."
    payload = _run_payload(
        series,
        revision,
        actor=actor,
        reason=reason,
        expected_result_count=expected_result_count,
        target_appointment_ids=target_appointment_ids,
    )
    fingerprint = canonical_fingerprint(payload)
    locked = AppointmentSeries.objects.select_for_update().get(pk=series.pk)
    if locked.status != AppointmentSeries.Status.ACTIVE:
        raise ValidationError("Создавать занятия можно только для активной серии.")
    if locked.current_revision_id != revision.pk or revision.series_id != locked.pk:
        raise ValidationError("Первый запуск должен использовать текущую редакцию серии.")

    existing = AppointmentSeriesMaterializationRun.objects.filter(
        operation_key=operation_key
    ).first()
    if existing:
        if (
            existing.series_id == locked.pk
            and existing.revision_id == revision.pk
            and existing.mode == AppointmentSeriesMaterializationRun.Mode.LEGACY_IMPORT
        ):
            if existing.expected_result_count != expected_result_count:
                raise ValidationError(
                    "Legacy-запуск не покрывает все даты серии; нужен отдельный "
                    "запуск недостающих дат."
                )
            if not existing.events.filter(
                event_type=AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED
            ).exists():
                raise ValidationError("Legacy-запуск серии не имеет завершенного журнала.")
            return existing, False
        if (
            existing.series_id != locked.pk
            or existing.revision_id != revision.pk
            or existing.fingerprint != fingerprint
        ):
            raise ValidationError("Ключ запуска уже использован для другой операции.")
        return existing, False
    if AppointmentSeriesMaterializationRun.objects.filter(
        revision=revision,
        mode__in=[
            AppointmentSeriesMaterializationRun.Mode.INITIAL,
            AppointmentSeriesMaterializationRun.Mode.LEGACY_IMPORT,
        ],
    ).exists():
        raise ValidationError("Первая материализация этой редакции уже зарегистрирована.")

    try:
        with transaction.atomic():
            run = AppointmentSeriesMaterializationRun.objects.create(
                series=locked,
                revision=revision,
                operation_key=operation_key,
                fingerprint=fingerprint,
                mode=AppointmentSeriesMaterializationRun.Mode.INITIAL,
                date_from=revision.start_date,
                date_to=revision.end_date,
                expected_result_count=expected_result_count,
                actor=actor,
                actor_role_snapshot=role,
                reason=reason,
            )
    except IntegrityError as exc:
        existing = AppointmentSeriesMaterializationRun.objects.filter(
            operation_key=operation_key
        ).first()
        if existing and existing.fingerprint == fingerprint:
            return existing, False
        raise ValidationError("Первый запуск серии уже создан конкурентным запросом.") from exc
    return run, True


def _missing_run_payload(
    series: AppointmentSeries,
    revision: AppointmentSeriesRevision,
    *,
    actor: Any,
    date_from: date,
    date_to: date,
    expected_result_count: int,
    reason: str,
) -> dict[str, Any]:
    return {
        "series_id": series.pk,
        "revision_id": revision.pk,
        "revision_fingerprint": revision.fingerprint,
        "mode": AppointmentSeriesMaterializationRun.Mode.MISSING_ONLY,
        "date_from": date_from,
        "date_to": date_to,
        "expected_result_count": expected_result_count,
        "actor_id": actor.pk,
        "reason": reason,
        "target_appointment_ids": [],
    }


def _missing_run_reason(reason: str) -> str:
    return (reason or "").strip() or "Материализация дат серии без истории."


def get_existing_missing_run(
    series: AppointmentSeries,
    *,
    operation_key: UUID,
    actor: Any,
    date_from: date | None = None,
    date_to: date | None = None,
    reason: str = "",
) -> AppointmentSeriesMaterializationRun | None:
    require_operator_role(actor)
    existing = (
        AppointmentSeriesMaterializationRun.objects.select_related(
            "series",
            "revision",
        )
        .filter(operation_key=operation_key)
        .first()
    )
    if existing is None:
        return None

    normalized_reason = _missing_run_reason(reason)
    revision = existing.revision
    lower_bound = max(revision.start_date, revision.effective_from)
    requested_from = (
        max(date_from, lower_bound) if date_from is not None else existing.date_from
    )
    requested_to = (
        min(date_to, revision.end_date) if date_to is not None else existing.date_to
    )
    payload = _missing_run_payload(
        existing.series,
        revision,
        actor=actor,
        date_from=existing.date_from,
        date_to=existing.date_to,
        expected_result_count=existing.expected_result_count,
        reason=normalized_reason,
    )
    if (
        existing.series_id != series.pk
        or existing.revision.series_id != series.pk
        or existing.mode != AppointmentSeriesMaterializationRun.Mode.MISSING_ONLY
        or existing.actor_id != actor.pk
        or existing.reason != normalized_reason
        or requested_from != existing.date_from
        or requested_to != existing.date_to
        or existing.fingerprint != canonical_fingerprint(payload)
    ):
        raise ValidationError(
            "Ключ запуска уже использован для другой операции материализации."
        )
    return existing


@transaction.atomic
def get_or_create_missing_run(
    series: AppointmentSeries,
    revision: AppointmentSeriesRevision,
    *,
    operation_key: UUID,
    actor: Any,
    date_from: date,
    date_to: date,
    expected_result_count: int,
    reason: str = "",
) -> tuple[AppointmentSeriesMaterializationRun, bool]:
    role = require_operator_role(actor)
    reason = _missing_run_reason(reason)
    if expected_result_count < 1:
        raise ValidationError("Запуск должен содержать хотя бы одну дату расписания.")

    locked = AppointmentSeries.objects.select_for_update().get(pk=series.pk)
    lower_bound = max(revision.start_date, revision.effective_from)
    if date_from < lower_bound or date_to > revision.end_date or date_to < date_from:
        raise ValidationError("Диапазон запуска выходит за период текущей редакции.")

    payload = _missing_run_payload(
        locked,
        revision,
        actor=actor,
        date_from=date_from,
        date_to=date_to,
        expected_result_count=expected_result_count,
        reason=reason,
    )
    fingerprint = canonical_fingerprint(payload)
    existing = AppointmentSeriesMaterializationRun.objects.filter(
        operation_key=operation_key
    ).first()
    if existing:
        if (
            existing.series_id != locked.pk
            or existing.revision_id != revision.pk
            or existing.mode != AppointmentSeriesMaterializationRun.Mode.MISSING_ONLY
            or existing.fingerprint != fingerprint
        ):
            raise ValidationError(
                "Ключ запуска уже использован для другой операции материализации."
            )
        return existing, False
    if locked.status != AppointmentSeries.Status.ACTIVE:
        raise ValidationError("Дополнять расписание можно только для активной серии.")
    if locked.current_revision_id != revision.pk or revision.series_id != locked.pk:
        raise SeriesRevisionMismatch(
            "Запуск дат без истории должен использовать текущую редакцию серии."
        )
    assert_current_projection(locked, revision)
    unfinished_older_run = (
        AppointmentSeriesMaterializationRun.objects.filter(
            series=locked,
            revision__revision_number__lt=revision.revision_number,
            date_from__lte=date_to,
            date_to__gte=date_from,
        )
        .exclude(events__event_type=(AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED))
        .order_by("revision__revision_number", "started_at", "pk")
        .first()
    )
    if unfinished_older_run:
        raise ValidationError(
            "Сначала завершите ранее принятый пересекающийся запуск редакции "
            f"№{unfinished_older_run.revision.revision_number}."
        )
    unfinished_retry_target = (
        AppointmentSeriesRetryTarget.objects.filter(
            run__series=locked,
            scheduled_date__gte=date_from,
            scheduled_date__lte=date_to,
        )
        .exclude(
            run__events__event_type=(AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED)
        )
        .select_related("run")
        .order_by("scheduled_starts_at", "pk")
        .first()
    )
    if unfinished_retry_target:
        raise ValidationError(
            "Сначала завершите принятый повтор пропущенной даты "
            f"{unfinished_retry_target.run.operation_key}."
        )

    try:
        with transaction.atomic():
            run = AppointmentSeriesMaterializationRun.objects.create(
                series=locked,
                revision=revision,
                operation_key=operation_key,
                fingerprint=fingerprint,
                mode=AppointmentSeriesMaterializationRun.Mode.MISSING_ONLY,
                date_from=date_from,
                date_to=date_to,
                expected_result_count=expected_result_count,
                actor=actor,
                actor_role_snapshot=role,
                reason=reason,
            )
    except IntegrityError as exc:
        existing = AppointmentSeriesMaterializationRun.objects.filter(
            operation_key=operation_key
        ).first()
        if (
            existing
            and existing.series_id == locked.pk
            and existing.revision_id == revision.pk
            and existing.mode == AppointmentSeriesMaterializationRun.Mode.MISSING_ONLY
            and existing.fingerprint == fingerprint
        ):
            return existing, False
        raise ValidationError(
            "Запуск дат без истории уже создан конкурентным запросом с другим составом."
        ) from exc
    return run, True


def _retry_run_reason(reason: str) -> str:
    return normalize_immutable_reason(reason)


def _retry_targets_payload(
    targets: Iterable[AppointmentSeriesRetryTarget | _RetryCandidate],
) -> list[dict[str, Any]]:
    payload = []
    for target in targets:
        if isinstance(target, AppointmentSeriesRetryTarget):
            chain_head_id = target.chain_head_result_id
            effective_skipped_id = target.effective_skipped_result_id
        else:
            chain_head_id = target.chain_head.pk
            effective_skipped_id = target.effective_skipped.pk
        payload.append(
            {
                "scheduled_starts_at": target.scheduled_starts_at.isoformat(),
                "chain_head_result_id": chain_head_id,
                "effective_skipped_result_id": effective_skipped_id,
            }
        )
    return sorted(payload, key=lambda item: item["scheduled_starts_at"])


def _retry_run_payload(
    series: AppointmentSeries,
    revision: AppointmentSeriesRevision,
    *,
    actor: Any,
    date_from: date,
    date_to: date,
    reason: str,
    targets: Iterable[AppointmentSeriesRetryTarget | _RetryCandidate],
) -> dict[str, Any]:
    target_payload = _retry_targets_payload(targets)
    return {
        "series_id": series.pk,
        "revision_id": revision.pk,
        "revision_fingerprint": revision.fingerprint,
        "mode": AppointmentSeriesMaterializationRun.Mode.RETRY_SKIPPED,
        "date_from": date_from,
        "date_to": date_to,
        "expected_result_count": len(target_payload),
        "actor_id": actor.pk,
        "reason": reason,
        "targets": target_payload,
    }


def _validate_existing_retry_run(
    existing: AppointmentSeriesMaterializationRun,
    series: AppointmentSeries,
    *,
    actor: Any,
    date_from: date | None,
    date_to: date | None,
    reason: str,
) -> AppointmentSeriesMaterializationRun:
    revision = existing.revision
    lower_bound = max(revision.start_date, revision.effective_from)
    requested_from = (
        max(date_from, lower_bound) if date_from is not None else existing.date_from
    )
    requested_to = (
        min(date_to, revision.end_date) if date_to is not None else existing.date_to
    )
    targets = list(
        existing.retry_targets.select_related(
            "chain_head_result",
            "effective_skipped_result",
        ).order_by("scheduled_starts_at", "pk")
    )
    payload = _retry_run_payload(
        existing.series,
        revision,
        actor=actor,
        date_from=existing.date_from,
        date_to=existing.date_to,
        reason=reason,
        targets=targets,
    )
    if (
        existing.series_id != series.pk
        or revision.series_id != series.pk
        or existing.mode != AppointmentSeriesMaterializationRun.Mode.RETRY_SKIPPED
        or existing.actor_id != actor.pk
        or existing.reason != reason
        or requested_from != existing.date_from
        or requested_to != existing.date_to
        or len(targets) != existing.expected_result_count
        or existing.fingerprint != canonical_fingerprint(payload)
    ):
        raise ValidationError(
            "Ключ запуска уже использован для другой операции повторения пропущенных дат."
        )
    return existing


def get_existing_retry_run(
    series: AppointmentSeries,
    *,
    operation_key: UUID,
    actor: Any,
    date_from: date | None = None,
    date_to: date | None = None,
    reason: str,
) -> AppointmentSeriesMaterializationRun | None:
    require_operator_role(actor)
    normalized_reason = _retry_run_reason(reason)
    existing = (
        AppointmentSeriesMaterializationRun.objects.select_related(
            "series",
            "revision",
        )
        .filter(operation_key=operation_key)
        .first()
    )
    if existing is None:
        return None
    return _validate_existing_retry_run(
        existing,
        series,
        actor=actor,
        date_from=date_from,
        date_to=date_to,
        reason=normalized_reason,
    )


def resolve_retry_revision_range(
    series: AppointmentSeries,
    *,
    date_from: date | None,
    date_to: date | None,
) -> tuple[AppointmentSeriesRevision, date, date]:
    revisions = list(
        series.revisions.order_by("effective_from", "revision_number", "pk")
    )
    if not revisions or not series.current_revision_id:
        raise ValidationError(
            "До повторного запуска серия должна иметь каноническую редакцию."
        )

    if date_from is None and date_to is None:
        revision = next(
            (item for item in revisions if item.pk == series.current_revision_id),
            None,
        )
    else:
        selector = date_from or date_to
        revision = next(
            (
                item
                for item in reversed(revisions)
                if item.effective_from <= selector <= item.end_date
                and item.start_date <= selector
            ),
            None,
        )
    if revision is None:
        raise ValidationError(
            "Для выбранной даты не найдена применимая редакция серии."
        )

    next_effective_from = next(
        (
            item.effective_from
            for item in revisions
            if item.effective_from > revision.effective_from
        ),
        None,
    )
    lower_bound = max(revision.start_date, revision.effective_from)
    upper_bound = revision.end_date
    if next_effective_from is not None:
        upper_bound = min(upper_bound, next_effective_from - timedelta(days=1))
    resolved_from = date_from or lower_bound
    resolved_to = date_to or upper_bound
    if (
        resolved_from < lower_bound
        or resolved_to > upper_bound
        or resolved_to < resolved_from
    ):
        raise ValidationError(
            "Один повторный запуск не может выходить за период применимой редакции."
        )
    return revision, resolved_from, resolved_to


def _retry_candidates(
    series: AppointmentSeries,
    revision: AppointmentSeriesRevision,
    *,
    date_from: date,
    date_to: date,
) -> list[_RetryCandidate]:
    results = list(
        AppointmentSeriesMaterializationResult.objects.select_for_update(of=("self",))
        .select_related("revision")
        .filter(
            series=series,
            scheduled_date__gte=date_from,
            scheduled_date__lte=date_to,
        )
        .order_by("scheduled_starts_at", "attempt_number", "pk")
    )
    by_id = {result.pk: result for result in results}
    superseded_ids = {
        result.supersedes_id for result in results if result.supersedes_id is not None
    }
    heads = [result for result in results if result.pk not in superseded_ids]
    candidates = []
    for head in heads:
        if head.revision.revision_number > revision.revision_number:
            raise SeriesRevisionMismatch(
                "Повтор не может продолжать результат более новой редакции серии."
            )
        effective = head
        visited: set[int] = set()
        while effective.outcome == AppointmentSeriesOccurrence.Outcome.UNCHANGED:
            if effective.pk in visited or effective.supersedes_id not in by_id:
                raise SeriesRetryMismatch(
                    "Цепочка попыток даты повреждена и не может быть безопасно повторена."
                )
            visited.add(effective.pk)
            effective = by_id[effective.supersedes_id]
        if effective.outcome != AppointmentSeriesOccurrence.Outcome.SKIPPED:
            continue
        candidates.append(
            _RetryCandidate(
                scheduled_starts_at=head.scheduled_starts_at,
                scheduled_date=head.scheduled_date,
                chain_head=head,
                effective_skipped=effective,
            )
        )
    return sorted(candidates, key=lambda item: item.scheduled_starts_at)


@transaction.atomic
def get_or_create_retry_run(
    series: AppointmentSeries,
    revision: AppointmentSeriesRevision,
    *,
    operation_key: UUID,
    actor: Any,
    date_from: date,
    date_to: date,
    reason: str,
) -> tuple[AppointmentSeriesMaterializationRun, bool]:
    role = require_operator_role(actor)
    reason = _retry_run_reason(reason)
    locked = AppointmentSeries.objects.select_for_update().get(pk=series.pk)

    existing = (
        AppointmentSeriesMaterializationRun.objects.select_related(
            "series",
            "revision",
        )
        .filter(operation_key=operation_key)
        .first()
    )
    if existing:
        return (
            _validate_existing_retry_run(
                existing,
                locked,
                actor=actor,
                date_from=date_from,
                date_to=date_to,
                reason=reason,
            ),
            False,
        )

    if locked.status != AppointmentSeries.Status.ACTIVE:
        raise ValidationError("Повторять пропущенные даты можно только для активной серии.")
    applicable_revision, resolved_from, resolved_to = resolve_retry_revision_range(
        locked,
        date_from=date_from,
        date_to=date_to,
    )
    if (
        applicable_revision.pk != revision.pk
        or revision.series_id != locked.pk
        or resolved_from != date_from
        or resolved_to != date_to
    ):
        raise SeriesRevisionMismatch(
            "Новый повторный запуск должен использовать редакцию, применимую ко всему диапазону."
        )
    if revision.materialization_mode != (
        AppointmentSeries.MaterializationMode.CREATE_APPOINTMENTS
    ):
        raise ValidationError(
            "Повтор пропущенных дат поддерживается только для создания занятий."
        )
    current_revision = AppointmentSeriesRevision.objects.get(pk=locked.current_revision_id)
    assert_current_projection(locked, current_revision)

    unfinished_older_run = (
        AppointmentSeriesMaterializationRun.objects.filter(
            series=locked,
            revision__revision_number__lt=revision.revision_number,
            date_from__lte=date_to,
            date_to__gte=date_from,
        )
        .exclude(
            events__event_type=(
                AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED
            )
        )
        .order_by("revision__revision_number", "started_at", "pk")
        .first()
    )
    if unfinished_older_run:
        raise ValidationError(
            "Сначала завершите ранее принятый пересекающийся запуск редакции "
            f"№{unfinished_older_run.revision.revision_number}."
        )

    candidates = _retry_candidates(
        locked,
        revision,
        date_from=date_from,
        date_to=date_to,
    )
    if not candidates:
        raise ValidationError(
            "В выбранном диапазоне нет дат с последним эффективным исходом skipped."
        )
    reserved_head = (
        AppointmentSeriesRetryTarget.objects.filter(
            chain_head_result_id__in=[item.chain_head.pk for item in candidates]
        )
        .select_related("run")
        .first()
    )
    if reserved_head:
        raise ValidationError(
            "Одна из пропущенных дат уже закреплена за принятым повторным запуском "
            f"{reserved_head.run.operation_key}."
        )

    payload = _retry_run_payload(
        locked,
        revision,
        actor=actor,
        date_from=date_from,
        date_to=date_to,
        reason=reason,
        targets=candidates,
    )
    fingerprint = canonical_fingerprint(payload)
    try:
        with transaction.atomic():
            run = AppointmentSeriesMaterializationRun.objects.create(
                series=locked,
                revision=revision,
                operation_key=operation_key,
                fingerprint=fingerprint,
                mode=AppointmentSeriesMaterializationRun.Mode.RETRY_SKIPPED,
                date_from=date_from,
                date_to=date_to,
                expected_result_count=len(candidates),
                actor=actor,
                actor_role_snapshot=role,
                reason=reason,
            )
            for candidate in candidates:
                AppointmentSeriesRetryTarget.objects.create(
                    run=run,
                    scheduled_starts_at=candidate.scheduled_starts_at,
                    scheduled_date=candidate.scheduled_date,
                    chain_head_result=candidate.chain_head,
                    effective_skipped_result=candidate.effective_skipped,
                )
            if run.retry_targets.count() != run.expected_result_count:
                raise ValidationError("Набор целей повторного запуска сохранен не полностью.")
    except IntegrityError as exc:
        existing = (
            AppointmentSeriesMaterializationRun.objects.select_related(
                "series",
                "revision",
            )
            .filter(operation_key=operation_key)
            .first()
        )
        if existing:
            return (
                _validate_existing_retry_run(
                    existing,
                    locked,
                    actor=actor,
                    date_from=date_from,
                    date_to=date_to,
                    reason=reason,
                ),
                False,
            )
        raise SeriesRetryMismatch(
            "Цепочка пропущенной даты изменилась во время принятия повторного запуска."
        ) from exc
    return run, True


@transaction.atomic
def record_retry_result(
    run: AppointmentSeriesMaterializationRun,
    target: AppointmentSeriesRetryTarget,
    *,
    appointment: Appointment | None,
    outcome: str,
    reason_code: str = "",
    reason: str = "",
) -> AppointmentSeriesMaterializationResult:
    AppointmentSeries.objects.select_for_update().get(pk=run.series_id)
    locked_run = AppointmentSeriesMaterializationRun.objects.select_for_update().get(
        pk=run.pk
    )
    locked_target = (
        AppointmentSeriesRetryTarget.objects.select_for_update()
        .select_related("chain_head_result", "effective_skipped_result")
        .get(pk=target.pk)
    )
    existing = locked_run.results.filter(
        scheduled_starts_at=locked_target.scheduled_starts_at
    ).first()
    if existing:
        if (
            existing.supersedes_id != locked_target.chain_head_result_id
            or existing.appointment_id != getattr(appointment, "pk", None)
            or existing.outcome != outcome
            or existing.reason_code != reason_code
            or existing.reason != reason
            or existing.outcome
            not in {
                AppointmentSeriesOccurrence.Outcome.CREATED,
                AppointmentSeriesOccurrence.Outcome.SKIPPED,
            }
        ):
            raise SeriesRetryMismatch(
                "Сохраненный результат не совпадает с зафиксированной целью повторного запуска."
            )
        return existing
    _assert_run_accepts_results(locked_run)
    if (
        locked_run.mode != AppointmentSeriesMaterializationRun.Mode.RETRY_SKIPPED
        or locked_target.run_id != locked_run.pk
        or outcome
        not in {
            AppointmentSeriesOccurrence.Outcome.CREATED,
            AppointmentSeriesOccurrence.Outcome.SKIPPED,
        }
        or (outcome == AppointmentSeriesOccurrence.Outcome.CREATED) != bool(appointment)
    ):
        raise SeriesRetryMismatch(
            "Результат не соответствует контракту зафиксированной retry-цели."
        )
    if appointment and (
        appointment.series_id != locked_run.series_id
        or appointment.starts_at != locked_target.scheduled_starts_at
        or appointment.service_id != locked_run.revision.service_id
        or appointment.session_type != locked_run.revision.session_type
    ):
        raise SeriesRetryMismatch(
            "Созданное занятие не относится к серии, времени, услуге и типу retry-цели."
        )

    chain_head = AppointmentSeriesMaterializationResult.objects.select_for_update().get(
        pk=locked_target.chain_head_result_id
    )
    if chain_head.superseded_by.exists():
        raise SeriesRetryMismatch("Зафиксированная вершина цепочки уже продолжена другой попыткой.")
    try:
        with transaction.atomic():
            return AppointmentSeriesMaterializationResult.objects.create(
                series_id=locked_run.series_id,
                revision_id=locked_run.revision_id,
                run=locked_run,
                scheduled_starts_at=locked_target.scheduled_starts_at,
                scheduled_date=locked_target.scheduled_date,
                attempt_number=chain_head.attempt_number + 1,
                provenance_kind=(
                    AppointmentSeriesMaterializationResult.ProvenanceKind.NATIVE
                ),
                appointment=appointment,
                outcome=outcome,
                reason_code=reason_code,
                reason=reason,
                supersedes=chain_head,
            )
    except (IntegrityError, ValidationError) as exc:
        raise SeriesRetryMismatch(
            "Не удалось линейно продолжить зафиксированную цепочку повторного запуска."
        ) from exc


def _run_outcomes(
    run: AppointmentSeriesMaterializationRun,
) -> Counter[str]:
    return Counter(run.results.values_list("outcome", flat=True))


def _run_event_counts(
    run: AppointmentSeriesMaterializationRun,
) -> dict[str, int]:
    outcomes = _run_outcomes(run)
    return {
        "result_count": sum(outcomes.values()),
        "created_count": outcomes[AppointmentSeriesOccurrence.Outcome.CREATED],
        "joined_count": outcomes[AppointmentSeriesOccurrence.Outcome.JOINED],
        "skipped_count": outcomes[AppointmentSeriesOccurrence.Outcome.SKIPPED],
        "unchanged_count": outcomes[AppointmentSeriesOccurrence.Outcome.UNCHANGED],
    }


def _assert_run_accepts_results(
    run: AppointmentSeriesMaterializationRun,
) -> None:
    latest = run.events.order_by("-event_number").first()
    if latest is None or latest.event_type == (
        AppointmentSeriesMaterializationRunEvent.EventType.RESUMED
    ):
        return
    if latest.event_type == (
        AppointmentSeriesMaterializationRunEvent.EventType.INTERRUPTED
    ):
        raise ValidationError(
            "Прерванный запуск нужно явно возобновить до записи результатов."
        )
    raise ValidationError("Завершенный запуск не принимает новые результаты.")


@transaction.atomic
def resume_run(
    run: AppointmentSeriesMaterializationRun,
) -> AppointmentSeriesMaterializationRunEvent | None:
    locked_run = AppointmentSeriesMaterializationRun.objects.select_for_update().get(
        pk=run.pk
    )
    latest = locked_run.events.order_by("-event_number").first()
    if latest is None or latest.event_type in {
        AppointmentSeriesMaterializationRunEvent.EventType.RESUMED,
        AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED,
    }:
        return latest
    return AppointmentSeriesMaterializationRunEvent.objects.create(
        run=locked_run,
        event_number=latest.event_number + 1,
        event_type=AppointmentSeriesMaterializationRunEvent.EventType.RESUMED,
        **_run_event_counts(locked_run),
    )


@transaction.atomic
def interrupt_run(
    run: AppointmentSeriesMaterializationRun,
    *,
    reason: str,
) -> AppointmentSeriesMaterializationRunEvent:
    locked_run = AppointmentSeriesMaterializationRun.objects.select_for_update().get(
        pk=run.pk
    )
    latest = locked_run.events.order_by("-event_number").first()
    if latest and latest.event_type in {
        AppointmentSeriesMaterializationRunEvent.EventType.INTERRUPTED,
        AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED,
    }:
        return latest
    normalized_reason = (reason or "").strip()
    if len(normalized_reason) < 5:
        normalized_reason = "Выполнение запуска прервано ошибкой."
    return AppointmentSeriesMaterializationRunEvent.objects.create(
        run=locked_run,
        event_number=(latest.event_number if latest else 0) + 1,
        event_type=AppointmentSeriesMaterializationRunEvent.EventType.INTERRUPTED,
        reason=normalized_reason,
        **_run_event_counts(locked_run),
    )


@transaction.atomic
def record_unchanged_result(
    run: AppointmentSeriesMaterializationRun,
    *,
    scheduled_starts_at: datetime,
) -> AppointmentSeriesMaterializationResult:
    AppointmentSeries.objects.select_for_update().get(pk=run.series_id)
    locked_run = AppointmentSeriesMaterializationRun.objects.select_for_update().get(
        pk=run.pk
    )
    existing = AppointmentSeriesMaterializationResult.objects.filter(
        run=locked_run,
        scheduled_starts_at=scheduled_starts_at,
    ).first()
    if existing:
        return existing
    _assert_run_accepts_results(locked_run)

    previous = (
        AppointmentSeriesMaterializationResult.objects.select_related("revision")
        .filter(
            series_id=locked_run.series_id,
            scheduled_starts_at=scheduled_starts_at,
        )
        .order_by("-attempt_number", "-pk")
        .first()
    )
    if previous is None:
        raise ValidationError("Для даты без истории нельзя записать исход «без изменений».")
    if previous.revision.revision_number > locked_run.revision.revision_number:
        raise SeriesRevisionMismatch(
            "Новая попытка не может продолжать результат более новой редакции."
        )
    return AppointmentSeriesMaterializationResult.objects.create(
        series_id=locked_run.series_id,
        revision_id=locked_run.revision_id,
        run=locked_run,
        scheduled_starts_at=scheduled_starts_at,
        scheduled_date=timezone.localtime(scheduled_starts_at).date(),
        attempt_number=previous.attempt_number + 1,
        provenance_kind=AppointmentSeriesMaterializationResult.ProvenanceKind.NATIVE,
        outcome=AppointmentSeriesOccurrence.Outcome.UNCHANGED,
        reason_code="existing_history",
        reason=(
            "Дата уже имеет неизменяемый результат; занятие и предыдущий исход "
            "оставлены без изменений."
        ),
        supersedes=previous,
    )


@transaction.atomic
def record_compatibility_result(
    run: AppointmentSeriesMaterializationRun,
    occurrence: AppointmentSeriesOccurrence,
) -> AppointmentSeriesMaterializationResult:
    AppointmentSeries.objects.select_for_update().get(pk=run.series_id)
    locked_run = AppointmentSeriesMaterializationRun.objects.select_for_update().get(
        pk=run.pk
    )
    existing = AppointmentSeriesMaterializationResult.objects.filter(
        run=locked_run,
        scheduled_starts_at=occurrence.scheduled_starts_at,
    ).first()
    if existing:
        if (
            existing.compatibility_occurrence_id != occurrence.pk
            or existing.series_id != occurrence.series_id
            or existing.scheduled_starts_at != occurrence.scheduled_starts_at
            or existing.appointment_id != occurrence.appointment_id
            or existing.appointment_participant_id
            != occurrence.appointment_participant_id
            or existing.outcome != occurrence.outcome
            or existing.reason_code != occurrence.reason_code
            or existing.reason != occurrence.reason
        ):
            raise ValidationError(
                "Существующий результат запуска не совпадает с immutable occurrence."
            )
        return existing
    _assert_run_accepts_results(locked_run)
    return AppointmentSeriesMaterializationResult.objects.create(
        series_id=locked_run.series_id,
        revision_id=locked_run.revision_id,
        run=locked_run,
        scheduled_starts_at=occurrence.scheduled_starts_at,
        scheduled_date=timezone.localtime(occurrence.scheduled_starts_at).date(),
        attempt_number=1,
        provenance_kind=AppointmentSeriesMaterializationResult.ProvenanceKind.NATIVE,
        appointment=occurrence.appointment,
        appointment_participant=occurrence.appointment_participant,
        outcome=occurrence.outcome,
        reason_code=occurrence.reason_code,
        reason=occurrence.reason,
        compatibility_occurrence=occurrence,
    )


@transaction.atomic
def complete_run(
    run: AppointmentSeriesMaterializationRun,
) -> AppointmentSeriesMaterializationRunEvent:
    locked_run = AppointmentSeriesMaterializationRun.objects.select_for_update().get(
        pk=run.pk
    )
    completed = locked_run.events.filter(
        event_type=AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED
    ).first()
    if completed:
        return completed
    outcomes = _run_outcomes(locked_run)
    result_count = sum(outcomes.values())
    if result_count != locked_run.expected_result_count:
        raise ValidationError(
            "Запуск нельзя завершить: сохранено "
            f"{result_count} из {locked_run.expected_result_count} результатов."
        )
    return AppointmentSeriesMaterializationRunEvent.objects.create(
        run=locked_run,
        event_number=(
            locked_run.events.order_by("-event_number")
            .values_list("event_number", flat=True)
            .first()
            or 0
        )
        + 1,
        event_type=AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED,
        result_count=result_count,
        created_count=outcomes[AppointmentSeriesOccurrence.Outcome.CREATED],
        joined_count=outcomes[AppointmentSeriesOccurrence.Outcome.JOINED],
        skipped_count=outcomes[AppointmentSeriesOccurrence.Outcome.SKIPPED],
        unchanged_count=outcomes[AppointmentSeriesOccurrence.Outcome.UNCHANGED],
    )
