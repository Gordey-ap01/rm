"""Director-owned, append-only grant plan revisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import combinations
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from operations.models import (
    AppointmentParticipant,
    AppointmentStaffAssignment,
    FundingServiceQuota,
    FundingServiceQuotaRevision,
    FundingSource,
    FundingStaffAllocation,
    FundingStaffAllocationRevision,
    LedgerEntry,
    PayrollSheet,
    PayrollSheetLine,
    Service,
    StaffMember,
)
from operations.services.authority import is_director_user
from operations.services.financial_facts import appointment_charge_fact


@dataclass(frozen=True)
class GrantPlanIntegrityFinding:
    code: str
    object_kind: str
    object_id: int
    detail: str


def _require_director(actor: Any) -> None:
    if not is_director_user(actor):
        raise PermissionDenied("Грантовый план может изменять только руководитель.")


def _normalize_reason(reason: str) -> str:
    normalized = (reason or "").strip()
    if len(normalized) < 5:
        raise ValidationError(
            {"reason": "Укажите содержательное основание (минимум 5 символов)."}
        )
    return normalized


def _locked_active_source(source_id: int) -> FundingSource:
    source = FundingSource.all_objects.select_for_update().get(pk=source_id)
    if source.archived_at is not None:
        raise ValidationError("Архивный источник финансирования доступен только для чтения.")
    return source


def _current_quota_revision(
    quota: FundingServiceQuota,
) -> FundingServiceQuotaRevision:
    if not quota.current_revision_id:
        raise ValidationError(
            "У квоты отсутствует текущая редакция. Сначала выполните integrity preflight."
        )
    revision = FundingServiceQuotaRevision.objects.select_for_update().get(
        pk=quota.current_revision_id
    )
    if revision.service_quota_id != quota.pk:
        raise ValidationError("Текущая редакция не относится к выбранной квоте.")
    return revision


def _current_staff_revision(
    allocation: FundingStaffAllocation,
) -> FundingStaffAllocationRevision:
    if not allocation.current_revision_id:
        raise ValidationError(
            "У распределения отсутствует текущая редакция. Сначала выполните integrity preflight."
        )
    revision = FundingStaffAllocationRevision.objects.select_for_update().get(
        pk=allocation.current_revision_id
    )
    if revision.staff_allocation_id != allocation.pk:
        raise ValidationError("Текущая редакция не относится к выбранному распределению.")
    return revision


def current_staff_revision(
    allocation: FundingStaffAllocation,
) -> FundingStaffAllocationRevision | None:
    if not allocation.current_revision_id:
        return None
    return FundingStaffAllocationRevision.objects.filter(
        pk=allocation.current_revision_id,
        staff_allocation=allocation,
    ).first()


def _require_expected_revision(current_revision_id: int | None, expected_revision_id: int) -> None:
    if current_revision_id != expected_revision_id:
        raise ValidationError(
            {
                "expected_revision_id": (
                    "План уже изменен другим пользователем. Обновите страницу и повторите действие."
                )
            }
        )


def _period_contains(
    work_date: date,
    starts_on: date | None,
    ends_on: date | None,
) -> bool:
    if starts_on and work_date < starts_on:
        return False
    return not (ends_on and work_date > ends_on)


def _periods_overlap(
    starts_on: date | None,
    ends_on: date | None,
    other_starts_on: date | None,
    other_ends_on: date | None,
) -> bool:
    if ends_on and other_starts_on and ends_on < other_starts_on:
        return False
    return not (other_ends_on and starts_on and other_ends_on < starts_on)


def _charged_work_dates(
    *,
    funding_source_id: int,
    service_id: int,
    staff_member_id: int | None = None,
) -> dict[int, date]:
    debited_ids = LedgerEntry.objects.filter(
        account__funding_source_id=funding_source_id,
        appointment__service_id=service_id,
        entry_type=LedgerEntry.EntryType.DEBIT,
    ).values_list("appointment_id", flat=True)
    assignments = AppointmentStaffAssignment.objects.filter(
        appointment_id__in=debited_ids,
        appointment__service_id=service_id,
    )
    if staff_member_id is not None:
        assignments = assignments.filter(staff_member_id=staff_member_id)
    participant_qs = AppointmentParticipant.objects.select_related(
        "billing_account__funding_source"
    )
    assignments = (
        assignments.select_related("appointment")
        .prefetch_related(
            Prefetch("appointment__participants", queryset=participant_qs)
        )
        .order_by("appointment_id", "pk")
    )
    work_dates: dict[int, date] = {}
    for assignment in assignments:
        if (
            funding_source_id
            not in appointment_charge_fact(
                assignment.appointment,
                include_ledger=False,
            ).funding_source_ids
        ):
            continue
        work_dates[assignment.appointment_id] = timezone.localtime(
            assignment.starts_at_snapshot
        ).date()
    return work_dates


def _validate_fact_retention(
    *,
    work_dates: dict[int, date],
    old_starts_on: date | None,
    old_ends_on: date | None,
    new_starts_on: date | None,
    new_ends_on: date | None,
    new_limit: int,
    limit_field: str,
) -> None:
    previously_attributed = {
        appointment_id
        for appointment_id, work_date in work_dates.items()
        if _period_contains(work_date, old_starts_on, old_ends_on)
    }
    newly_attributed = {
        appointment_id
        for appointment_id, work_date in work_dates.items()
        if _period_contains(work_date, new_starts_on, new_ends_on)
    }
    if not previously_attributed.issubset(newly_attributed):
        raise ValidationError(
            {
                "ends_on": (
                    "Новый период исключает уже списанные занятия. "
                    "Исторический факт нельзя освободить редакцией плана."
                )
            }
        )
    if new_limit < len(newly_attributed):
        raise ValidationError(
            {
                limit_field: (
                    "Количество нельзя уменьшить ниже уже списанного факта: "
                    f"{len(newly_attributed)}."
                )
            }
        )


def _validate_no_allocation_overlap(
    *,
    funding_source_id: int,
    service_id: int,
    staff_member_id: int,
    starts_on: date | None,
    ends_on: date | None,
    exclude_id: int | None = None,
) -> None:
    candidates = FundingStaffAllocation.objects.select_for_update().filter(
        funding_source_id=funding_source_id,
        service_id=service_id,
        staff_member_id=staff_member_id,
    )
    if exclude_id is not None:
        candidates = candidates.exclude(pk=exclude_id)
    for candidate in candidates.order_by("pk"):
        if _periods_overlap(
            starts_on,
            ends_on,
            candidate.starts_on,
            candidate.ends_on,
        ):
            raise ValidationError(
                {
                    "starts_on": (
                        "У специалиста уже есть пересекающееся распределение "
                        "по этой услуге и источнику."
                    )
                }
            )


def _locked_quota_allocations(
    quota: FundingServiceQuota,
) -> list[FundingStaffAllocation]:
    return list(
        FundingStaffAllocation.objects.select_for_update()
        .filter(service_quota=quota)
        .order_by("pk")
    )


def _allocated_total(
    allocations: list[FundingStaffAllocation],
    *,
    replacing_id: int | None = None,
    replacing_sessions: int | None = None,
) -> int:
    total = 0
    replaced = False
    for allocation in allocations:
        if allocation.pk == replacing_id:
            total += replacing_sessions or 0
            replaced = True
        else:
            total += allocation.allocated_sessions
    if replacing_id is not None and not replaced:
        raise ValidationError("Распределение не относится к выбранной квоте.")
    return total


def _validate_quota_projection(quota: FundingServiceQuota) -> None:
    quota.full_clean()


def _validate_staff_projection(allocation: FundingStaffAllocation) -> None:
    allocation.full_clean()


@transaction.atomic
def create_service_quota(
    *,
    funding_source: FundingSource,
    service: Service,
    planned_sessions: int,
    starts_on: date | None,
    ends_on: date | None,
    note: str,
    actor: Any,
    reason: str,
) -> FundingServiceQuota:
    _require_director(actor)
    normalized_reason = _normalize_reason(reason)
    source = _locked_active_source(funding_source.pk)

    quota = FundingServiceQuota(
        funding_source=source,
        service=service,
        planned_sessions=planned_sessions,
        starts_on=starts_on,
        ends_on=ends_on,
        note=note,
        lifecycle_status=FundingServiceQuota.LifecycleStatus.ACTIVE,
    )
    _validate_quota_projection(quota)
    work_dates = _charged_work_dates(
        funding_source_id=source.pk,
        service_id=service.pk,
    )
    _validate_fact_retention(
        work_dates=work_dates,
        old_starts_on=starts_on,
        old_ends_on=ends_on,
        new_starts_on=starts_on,
        new_ends_on=ends_on,
        new_limit=planned_sessions,
        limit_field="planned_sessions",
    )
    quota.save()
    now = timezone.now()
    revision = FundingServiceQuotaRevision.objects.create(
        service_quota=quota,
        revision_number=1,
        event_type=FundingServiceQuotaRevision.EventType.CREATED,
        planned_sessions=quota.planned_sessions,
        starts_on=quota.starts_on,
        ends_on=quota.ends_on,
        note=quota.note,
        lifecycle_status=quota.lifecycle_status,
        actor=actor,
        actor_role_snapshot=FundingServiceQuotaRevision.ActorRole.DIRECTOR,
        reason=normalized_reason,
        decided_at=now,
    )
    FundingServiceQuota.objects.filter(pk=quota.pk).update(
        current_revision=revision,
        updated_at=now,
    )
    quota.current_revision = revision
    return quota


@transaction.atomic
def revise_service_quota(
    quota: FundingServiceQuota,
    *,
    planned_sessions: int,
    starts_on: date | None,
    ends_on: date | None,
    note: str,
    actor: Any,
    reason: str,
    expected_revision_id: int,
) -> FundingServiceQuota:
    _require_director(actor)
    normalized_reason = _normalize_reason(reason)
    source_id = FundingServiceQuota.objects.values_list(
        "funding_source_id", flat=True
    ).get(pk=quota.pk)
    _locked_active_source(source_id)
    locked = FundingServiceQuota.objects.select_for_update().get(pk=quota.pk)
    _require_expected_revision(locked.current_revision_id, expected_revision_id)
    allocations = _locked_quota_allocations(locked)
    allocated_total = _allocated_total(allocations)
    if planned_sessions < allocated_total:
        raise ValidationError(
            {
                "planned_sessions": (
                    "План нельзя уменьшить ниже уже распределенного количества: "
                    f"{allocated_total}."
                )
            }
        )

    candidate = FundingServiceQuota(
        funding_source_id=locked.funding_source_id,
        service_id=locked.service_id,
        planned_sessions=planned_sessions,
        starts_on=starts_on,
        ends_on=ends_on,
        note=note,
        lifecycle_status=locked.lifecycle_status,
    )
    _validate_quota_projection(candidate)
    work_dates = _charged_work_dates(
        funding_source_id=locked.funding_source_id,
        service_id=locked.service_id,
    )
    _validate_fact_retention(
        work_dates=work_dates,
        old_starts_on=locked.starts_on,
        old_ends_on=locked.ends_on,
        new_starts_on=starts_on,
        new_ends_on=ends_on,
        new_limit=planned_sessions,
        limit_field="planned_sessions",
    )
    previous = _current_quota_revision(locked)
    now = timezone.now()
    revision = FundingServiceQuotaRevision.objects.create(
        service_quota=locked,
        revision_number=previous.revision_number + 1,
        event_type=FundingServiceQuotaRevision.EventType.REVISED,
        planned_sessions=planned_sessions,
        starts_on=starts_on,
        ends_on=ends_on,
        note=note,
        lifecycle_status=locked.lifecycle_status,
        actor=actor,
        actor_role_snapshot=FundingServiceQuotaRevision.ActorRole.DIRECTOR,
        reason=normalized_reason,
        supersedes=previous,
        decided_at=now,
    )
    FundingServiceQuota.objects.filter(pk=locked.pk).update(
        planned_sessions=planned_sessions,
        starts_on=starts_on,
        ends_on=ends_on,
        note=note,
        current_revision=revision,
        updated_at=now,
    )
    locked.refresh_from_db()
    return locked


@transaction.atomic
def close_service_quota(
    quota: FundingServiceQuota,
    *,
    close_on: date,
    actor: Any,
    reason: str,
    expected_revision_id: int,
) -> FundingServiceQuota:
    _require_director(actor)
    normalized_reason = _normalize_reason(reason)
    source_id = FundingServiceQuota.objects.values_list(
        "funding_source_id", flat=True
    ).get(pk=quota.pk)
    _locked_active_source(source_id)
    locked = FundingServiceQuota.objects.select_for_update().get(pk=quota.pk)
    _require_expected_revision(locked.current_revision_id, expected_revision_id)
    if locked.lifecycle_status == FundingServiceQuota.LifecycleStatus.CLOSED:
        raise ValidationError("Квота уже закрыта.")
    allocations = _locked_quota_allocations(locked)
    active_count = sum(
        allocation.lifecycle_status == FundingStaffAllocation.LifecycleStatus.ACTIVE
        for allocation in allocations
    )
    if active_count:
        raise ValidationError(
            f"Сначала закройте активные распределения специалистам: {active_count}."
        )
    if locked.starts_on and close_on < locked.starts_on:
        raise ValidationError({"close_on": "Дата закрытия не может быть раньше даты начала."})

    effective_end = min(locked.ends_on, close_on) if locked.ends_on else close_on
    work_dates = _charged_work_dates(
        funding_source_id=locked.funding_source_id,
        service_id=locked.service_id,
    )
    _validate_fact_retention(
        work_dates=work_dates,
        old_starts_on=locked.starts_on,
        old_ends_on=locked.ends_on,
        new_starts_on=locked.starts_on,
        new_ends_on=effective_end,
        new_limit=locked.planned_sessions,
        limit_field="planned_sessions",
    )
    previous = _current_quota_revision(locked)
    now = timezone.now()
    revision = FundingServiceQuotaRevision.objects.create(
        service_quota=locked,
        revision_number=previous.revision_number + 1,
        event_type=FundingServiceQuotaRevision.EventType.CLOSED,
        planned_sessions=locked.planned_sessions,
        starts_on=locked.starts_on,
        ends_on=effective_end,
        note=locked.note,
        lifecycle_status=FundingServiceQuota.LifecycleStatus.CLOSED,
        actor=actor,
        actor_role_snapshot=FundingServiceQuotaRevision.ActorRole.DIRECTOR,
        reason=normalized_reason,
        supersedes=previous,
        decided_at=now,
    )
    FundingServiceQuota.objects.filter(pk=locked.pk).update(
        ends_on=effective_end,
        lifecycle_status=FundingServiceQuota.LifecycleStatus.CLOSED,
        current_revision=revision,
        updated_at=now,
    )
    locked.refresh_from_db()
    return locked


@transaction.atomic
def create_staff_allocation(
    *,
    service_quota: FundingServiceQuota | None,
    funding_source: FundingSource | None,
    service: Service | None,
    staff_member: StaffMember,
    allocated_sessions: int,
    session_pay_amount: Decimal | None,
    starts_on: date | None,
    ends_on: date | None,
    note: str,
    actor: Any,
    reason: str,
) -> FundingStaffAllocation:
    _require_director(actor)
    normalized_reason = _normalize_reason(reason)
    locked_allocations: list[FundingStaffAllocation] = []
    if service_quota is not None:
        source_id = FundingServiceQuota.objects.values_list(
            "funding_source_id", flat=True
        ).get(pk=service_quota.pk)
        _locked_active_source(source_id)
        quota = FundingServiceQuota.objects.select_for_update().get(pk=service_quota.pk)
        if quota.lifecycle_status != FundingServiceQuota.LifecycleStatus.ACTIVE:
            raise ValidationError("Нельзя распределять закрытую квоту.")
        locked_allocations = _locked_quota_allocations(quota)
        funding_source = quota.funding_source
        service = quota.service
    else:
        quota = None
        if funding_source is None or service is None:
            raise ValidationError("Укажите источник и услугу для прямого распределения.")
        funding_source = _locked_active_source(funding_source.pk)

    if quota and _allocated_total(locked_allocations) + allocated_sessions > quota.planned_sessions:
        raise ValidationError(
            {
                "allocated_sessions": (
                    "Количество превышает план квоты. "
                    f"Распределено: {_allocated_total(locked_allocations)}, "
                    f"план: {quota.planned_sessions}."
                )
            }
        )

    _validate_no_allocation_overlap(
        funding_source_id=funding_source.pk,
        service_id=service.pk,
        staff_member_id=staff_member.pk,
        starts_on=starts_on,
        ends_on=ends_on,
    )
    work_dates = _charged_work_dates(
        funding_source_id=funding_source.pk,
        service_id=service.pk,
        staff_member_id=staff_member.pk,
    )
    _validate_fact_retention(
        work_dates=work_dates,
        old_starts_on=starts_on,
        old_ends_on=ends_on,
        new_starts_on=starts_on,
        new_ends_on=ends_on,
        new_limit=allocated_sessions,
        limit_field="allocated_sessions",
    )
    allocation = FundingStaffAllocation(
        service_quota=quota,
        funding_source=funding_source,
        service=service,
        staff_member=staff_member,
        allocated_sessions=allocated_sessions,
        session_pay_amount=session_pay_amount,
        starts_on=starts_on,
        ends_on=ends_on,
        note=note,
        lifecycle_status=FundingStaffAllocation.LifecycleStatus.ACTIVE,
    )
    _validate_staff_projection(allocation)
    allocation.save()
    now = timezone.now()
    revision = FundingStaffAllocationRevision.objects.create(
        staff_allocation=allocation,
        revision_number=1,
        event_type=FundingStaffAllocationRevision.EventType.CREATED,
        allocated_sessions=allocation.allocated_sessions,
        session_pay_amount=allocation.session_pay_amount,
        starts_on=allocation.starts_on,
        ends_on=allocation.ends_on,
        note=allocation.note,
        lifecycle_status=allocation.lifecycle_status,
        actor=actor,
        actor_role_snapshot=FundingStaffAllocationRevision.ActorRole.DIRECTOR,
        reason=normalized_reason,
        decided_at=now,
    )
    FundingStaffAllocation.objects.filter(pk=allocation.pk).update(
        current_revision=revision,
        updated_at=now,
    )
    allocation.current_revision = revision
    return allocation


def _lock_staff_allocation(
    allocation: FundingStaffAllocation,
) -> tuple[FundingStaffAllocation, FundingServiceQuota | None, list[FundingStaffAllocation]]:
    reference = FundingStaffAllocation.objects.only("pk", "service_quota_id").get(
        pk=allocation.pk
    )
    source_id = FundingStaffAllocation.objects.values_list(
        "funding_source_id", flat=True
    ).get(pk=reference.pk)
    _locked_active_source(source_id)
    if reference.service_quota_id:
        quota = FundingServiceQuota.objects.select_for_update().get(
            pk=reference.service_quota_id
        )
        allocations = _locked_quota_allocations(quota)
        locked = next(item for item in allocations if item.pk == reference.pk)
        return locked, quota, allocations

    locked = FundingStaffAllocation.objects.select_for_update().get(pk=reference.pk)
    return locked, None, [locked]


@transaction.atomic
def revise_staff_allocation(
    allocation: FundingStaffAllocation,
    *,
    allocated_sessions: int,
    session_pay_amount: Decimal | None,
    starts_on: date | None,
    ends_on: date | None,
    note: str,
    actor: Any,
    reason: str,
    expected_revision_id: int,
) -> FundingStaffAllocation:
    _require_director(actor)
    normalized_reason = _normalize_reason(reason)
    locked, quota, allocations = _lock_staff_allocation(allocation)
    _require_expected_revision(locked.current_revision_id, expected_revision_id)
    if quota:
        total = _allocated_total(
            allocations,
            replacing_id=locked.pk,
            replacing_sessions=allocated_sessions,
        )
        if total > quota.planned_sessions:
            raise ValidationError(
                {
                    "allocated_sessions": (
                        "Количество превышает план квоты. "
                        f"После изменения будет распределено: {total}, "
                        f"план: {quota.planned_sessions}."
                    )
                }
            )

    _validate_no_allocation_overlap(
        funding_source_id=locked.funding_source_id,
        service_id=locked.service_id,
        staff_member_id=locked.staff_member_id,
        starts_on=starts_on,
        ends_on=ends_on,
        exclude_id=locked.pk,
    )
    work_dates = _charged_work_dates(
        funding_source_id=locked.funding_source_id,
        service_id=locked.service_id,
        staff_member_id=locked.staff_member_id,
    )
    _validate_fact_retention(
        work_dates=work_dates,
        old_starts_on=locked.starts_on,
        old_ends_on=locked.ends_on,
        new_starts_on=starts_on,
        new_ends_on=ends_on,
        new_limit=allocated_sessions,
        limit_field="allocated_sessions",
    )
    candidate = FundingStaffAllocation(
        service_quota_id=locked.service_quota_id,
        funding_source_id=locked.funding_source_id,
        service_id=locked.service_id,
        staff_member_id=locked.staff_member_id,
        allocated_sessions=allocated_sessions,
        session_pay_amount=session_pay_amount,
        starts_on=starts_on,
        ends_on=ends_on,
        note=note,
        lifecycle_status=locked.lifecycle_status,
    )
    _validate_staff_projection(candidate)
    previous = _current_staff_revision(locked)
    now = timezone.now()
    revision = FundingStaffAllocationRevision.objects.create(
        staff_allocation=locked,
        revision_number=previous.revision_number + 1,
        event_type=FundingStaffAllocationRevision.EventType.REVISED,
        allocated_sessions=allocated_sessions,
        session_pay_amount=session_pay_amount,
        starts_on=starts_on,
        ends_on=ends_on,
        note=note,
        lifecycle_status=locked.lifecycle_status,
        actor=actor,
        actor_role_snapshot=FundingStaffAllocationRevision.ActorRole.DIRECTOR,
        reason=normalized_reason,
        supersedes=previous,
        decided_at=now,
    )
    FundingStaffAllocation.objects.filter(pk=locked.pk).update(
        allocated_sessions=allocated_sessions,
        session_pay_amount=session_pay_amount,
        starts_on=starts_on,
        ends_on=ends_on,
        note=note,
        current_revision=revision,
        updated_at=now,
    )
    locked.refresh_from_db()
    return locked


@transaction.atomic
def close_staff_allocation(
    allocation: FundingStaffAllocation,
    *,
    close_on: date,
    actor: Any,
    reason: str,
    expected_revision_id: int,
) -> FundingStaffAllocation:
    _require_director(actor)
    normalized_reason = _normalize_reason(reason)
    locked, _quota, _allocations = _lock_staff_allocation(allocation)
    _require_expected_revision(locked.current_revision_id, expected_revision_id)
    if locked.lifecycle_status == FundingStaffAllocation.LifecycleStatus.CLOSED:
        raise ValidationError("Распределение уже закрыто.")
    if locked.starts_on and close_on < locked.starts_on:
        raise ValidationError({"close_on": "Дата закрытия не может быть раньше даты начала."})

    effective_end = min(locked.ends_on, close_on) if locked.ends_on else close_on
    _validate_no_allocation_overlap(
        funding_source_id=locked.funding_source_id,
        service_id=locked.service_id,
        staff_member_id=locked.staff_member_id,
        starts_on=locked.starts_on,
        ends_on=effective_end,
        exclude_id=locked.pk,
    )
    work_dates = _charged_work_dates(
        funding_source_id=locked.funding_source_id,
        service_id=locked.service_id,
        staff_member_id=locked.staff_member_id,
    )
    _validate_fact_retention(
        work_dates=work_dates,
        old_starts_on=locked.starts_on,
        old_ends_on=locked.ends_on,
        new_starts_on=locked.starts_on,
        new_ends_on=effective_end,
        new_limit=locked.allocated_sessions,
        limit_field="allocated_sessions",
    )
    previous = _current_staff_revision(locked)
    now = timezone.now()
    revision = FundingStaffAllocationRevision.objects.create(
        staff_allocation=locked,
        revision_number=previous.revision_number + 1,
        event_type=FundingStaffAllocationRevision.EventType.CLOSED,
        allocated_sessions=locked.allocated_sessions,
        session_pay_amount=locked.session_pay_amount,
        starts_on=locked.starts_on,
        ends_on=effective_end,
        note=locked.note,
        lifecycle_status=FundingStaffAllocation.LifecycleStatus.CLOSED,
        actor=actor,
        actor_role_snapshot=FundingStaffAllocationRevision.ActorRole.DIRECTOR,
        reason=normalized_reason,
        supersedes=previous,
        decided_at=now,
    )
    FundingStaffAllocation.objects.filter(pk=locked.pk).update(
        ends_on=effective_end,
        lifecycle_status=FundingStaffAllocation.LifecycleStatus.CLOSED,
        current_revision=revision,
        updated_at=now,
    )
    locked.refresh_from_db()
    return locked


def grant_plan_integrity_findings() -> list[GrantPlanIntegrityFinding]:
    findings: list[GrantPlanIntegrityFinding] = []
    quotas = list(
        FundingServiceQuota.objects.select_related("current_revision").order_by("pk")
    )
    allocations = list(
        FundingStaffAllocation.objects.select_related("current_revision").order_by("pk")
    )

    quota_fields = (
        "planned_sessions",
        "starts_on",
        "ends_on",
        "note",
        "lifecycle_status",
    )
    for quota in quotas:
        revision = quota.current_revision
        if revision is None:
            findings.append(
                GrantPlanIntegrityFinding(
                    "quota_missing_current_revision",
                    "service_quota",
                    quota.pk,
                    "У корня отсутствует current_revision.",
                )
            )
            continue
        if revision.service_quota_id != quota.pk:
            findings.append(
                GrantPlanIntegrityFinding(
                    "quota_current_revision_wrong_root",
                    "service_quota",
                    quota.pk,
                    f"Редакция {revision.pk} относится к другому корню.",
                )
            )
            continue
        if any(getattr(quota, field) != getattr(revision, field) for field in quota_fields):
            findings.append(
                GrantPlanIntegrityFinding(
                    "quota_projection_mismatch",
                    "service_quota",
                    quota.pk,
                    f"Проекция не совпадает с редакцией {revision.pk}.",
                )
            )
        if revision.superseded_by.exists():
            findings.append(
                GrantPlanIntegrityFinding(
                    "quota_current_revision_not_terminal",
                    "service_quota",
                    quota.pk,
                    f"Текущая редакция {revision.pk} уже имеет преемника.",
                )
            )

    allocation_fields = (
        "allocated_sessions",
        "session_pay_amount",
        "starts_on",
        "ends_on",
        "note",
        "lifecycle_status",
    )
    for allocation in allocations:
        revision = allocation.current_revision
        if revision is None:
            findings.append(
                GrantPlanIntegrityFinding(
                    "staff_missing_current_revision",
                    "staff_allocation",
                    allocation.pk,
                    "У корня отсутствует current_revision.",
                )
            )
            continue
        if revision.staff_allocation_id != allocation.pk:
            findings.append(
                GrantPlanIntegrityFinding(
                    "staff_current_revision_wrong_root",
                    "staff_allocation",
                    allocation.pk,
                    f"Редакция {revision.pk} относится к другому корню.",
                )
            )
            continue
        if any(
            getattr(allocation, field) != getattr(revision, field)
            for field in allocation_fields
        ):
            findings.append(
                GrantPlanIntegrityFinding(
                    "staff_projection_mismatch",
                    "staff_allocation",
                    allocation.pk,
                    f"Проекция не совпадает с редакцией {revision.pk}.",
                )
            )
        if revision.superseded_by.exists():
            findings.append(
                GrantPlanIntegrityFinding(
                    "staff_current_revision_not_terminal",
                    "staff_allocation",
                    allocation.pk,
                    f"Текущая редакция {revision.pk} уже имеет преемника.",
                )
            )

    allocations_by_quota: dict[int, list[FundingStaffAllocation]] = {}
    for allocation in allocations:
        if allocation.service_quota_id:
            allocations_by_quota.setdefault(allocation.service_quota_id, []).append(
                allocation
            )
    for quota in quotas:
        allocated = sum(
            allocation.allocated_sessions
            for allocation in allocations_by_quota.get(quota.pk, ())
        )
        if allocated > quota.planned_sessions:
            findings.append(
                GrantPlanIntegrityFinding(
                    "quota_overallocated",
                    "service_quota",
                    quota.pk,
                    f"Распределено {allocated}, план {quota.planned_sessions}.",
                )
            )

    allocation_groups: dict[tuple[int, int, int], list[FundingStaffAllocation]] = {}
    for allocation in allocations:
        key = (
            allocation.funding_source_id,
            allocation.service_id,
            allocation.staff_member_id,
        )
        allocation_groups.setdefault(key, []).append(allocation)
    for group in allocation_groups.values():
        for left, right in combinations(group, 2):
            if _periods_overlap(
                left.starts_on,
                left.ends_on,
                right.starts_on,
                right.ends_on,
            ):
                findings.append(
                    GrantPlanIntegrityFinding(
                        "staff_allocation_period_overlap",
                        "staff_allocation",
                        left.pk,
                        f"Период пересекается с распределением {right.pk}.",
                    )
                )

    active_sheet_statuses = {
        PayrollSheet.Status.DRAFT,
        PayrollSheet.Status.APPROVED,
        PayrollSheet.Status.SENT,
        PayrollSheet.Status.PAID,
    }
    lines = PayrollSheetLine.objects.filter(
        payroll_sheet__status__in=active_sheet_statuses
    ).select_related("payroll_accrual")
    for line in lines:
        if line.amount != line.payroll_accrual.amount:
            findings.append(
                GrantPlanIntegrityFinding(
                    "payroll_sheet_line_amount_mismatch",
                    "payroll_sheet_line",
                    line.pk,
                    (
                        f"Строка {line.amount} не совпадает с начислением "
                        f"{line.payroll_accrual.amount}."
                    ),
                )
            )

    return findings
