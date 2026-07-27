"""Director-owned payroll budgets and fixed grant compensation plans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import combinations
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from operations.models import (
    FundingPayrollBudget,
    FundingPayrollBudgetRevision,
    FundingSource,
    FundingStaffAllocation,
    GrantFixedCompensation,
    GrantFixedCompensationRevision,
    Service,
    StaffMember,
)
from operations.services.authority import is_director_user


@dataclass(frozen=True)
class GrantCompensationIntegrityFinding:
    code: str
    object_kind: str
    object_id: int
    detail: str


def _require_director(actor: Any) -> None:
    if not is_director_user(actor):
        raise PermissionDenied(
            "Бюджет и фиксированную оплату гранта может изменять только руководитель."
        )


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


def _periods_overlap(
    starts_on: date | None,
    ends_on: date | None,
    other_starts_on: date | None,
    other_ends_on: date | None,
) -> bool:
    if ends_on and other_starts_on and ends_on < other_starts_on:
        return False
    return not (other_ends_on and starts_on and other_ends_on < starts_on)


def _require_expected_revision(
    current_revision_id: int | None,
    expected_revision_id: int,
) -> None:
    if current_revision_id != expected_revision_id:
        raise ValidationError(
            {
                "expected_revision_id": (
                    "План уже изменен другим пользователем. "
                    "Обновите страницу и повторите действие."
                )
            }
        )


def _current_budget_revision(
    budget: FundingPayrollBudget,
) -> FundingPayrollBudgetRevision:
    if not budget.current_revision_id:
        raise ValidationError(
            "У бюджета отсутствует текущая редакция. Выполните integrity preflight."
        )
    revision = FundingPayrollBudgetRevision.objects.select_for_update().get(
        pk=budget.current_revision_id
    )
    if revision.payroll_budget_id != budget.pk:
        raise ValidationError("Текущая редакция не относится к выбранному бюджету.")
    return revision


def _current_fixed_revision(
    fixed: GrantFixedCompensation,
) -> GrantFixedCompensationRevision:
    if not fixed.current_revision_id:
        raise ValidationError(
            "У фиксированной позиции отсутствует текущая редакция. "
            "Выполните integrity preflight."
        )
    revision = GrantFixedCompensationRevision.objects.select_for_update().get(
        pk=fixed.current_revision_id
    )
    if revision.fixed_compensation_id != fixed.pk:
        raise ValidationError("Текущая редакция не относится к выбранной позиции.")
    return revision


def _locked_source_budgets(source_id: int) -> list[FundingPayrollBudget]:
    return list(
        FundingPayrollBudget.objects.select_for_update()
        .filter(funding_source_id=source_id)
        .order_by("pk")
    )


def _validate_budget_overlap(
    budgets: list[FundingPayrollBudget],
    *,
    starts_on: date,
    ends_on: date,
    exclude_id: int | None = None,
) -> None:
    for budget in budgets:
        if budget.pk == exclude_id:
            continue
        if _periods_overlap(starts_on, ends_on, budget.starts_on, budget.ends_on):
            raise ValidationError(
                {
                    "starts_on": (
                        "У источника уже есть бюджет оплаты труда с "
                        "пересекающимся периодом."
                    )
                }
            )


def _validate_budget_values(
    *,
    starts_on: date,
    ends_on: date,
    planned_amount: Decimal,
) -> None:
    errors: dict[str, str] = {}
    if starts_on is None:
        errors["starts_on"] = "Укажите дату начала."
    if ends_on is None:
        errors["ends_on"] = "Укажите дату окончания."
    if starts_on and ends_on and ends_on < starts_on:
        errors["ends_on"] = "Дата окончания не может быть раньше даты начала."
    if planned_amount is None or planned_amount <= 0:
        errors["planned_amount"] = "Бюджет должен быть положительным."
    if errors:
        raise ValidationError(errors)


def _validate_fixed_values(candidate: GrantFixedCompensation) -> None:
    candidate.full_clean(exclude=["current_revision"])


def _locked_budget_fixed_positions(
    budget: FundingPayrollBudget,
) -> list[GrantFixedCompensation]:
    return list(
        GrantFixedCompensation.objects.select_for_update()
        .filter(payroll_budget=budget)
        .order_by("pk")
    )


def _validate_budget_contains_positions(
    positions: list[GrantFixedCompensation],
    *,
    starts_on: date,
    ends_on: date,
) -> None:
    for position in positions:
        if (
            position.period_from < starts_on
            or position.period_to > ends_on
            or position.accrual_on < starts_on
            or position.accrual_on > ends_on
        ):
            raise ValidationError(
                {
                    "ends_on": (
                        "Новый период исключает фиксированную позицию "
                        f"№{position.pk}. Сначала измените или закройте позицию."
                    )
                }
            )


def _validate_fixed_overlap(
    *,
    budget: FundingPayrollBudget,
    staff_member_id: int,
    compensation_scope: str,
    service_id: int | None,
    assignment_key: str | None,
    period_from: date,
    period_to: date,
    exclude_id: int | None = None,
) -> None:
    candidates = (
        GrantFixedCompensation.objects.select_for_update()
        .filter(
            payroll_budget__funding_source_id=budget.funding_source_id,
            staff_member_id=staff_member_id,
            compensation_scope=compensation_scope,
        )
        .select_related("payroll_budget")
        .order_by("pk")
    )
    if compensation_scope == GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY:
        candidates = candidates.filter(service_id=service_id)
    else:
        candidates = candidates.filter(assignment_key=assignment_key)
    if exclude_id is not None:
        candidates = candidates.exclude(pk=exclude_id)
    for candidate in candidates:
        if _periods_overlap(
            period_from,
            period_to,
            candidate.period_from,
            candidate.period_to,
        ):
            field = (
                "service"
                if compensation_scope
                == GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
                else "assignment_label"
            )
            raise ValidationError(
                {
                    field: (
                        "У сотрудника уже есть пересекающаяся фиксированная "
                        "позиция этого вида."
                    )
                }
            )


def validate_no_fixed_service_delivery_overlap(
    *,
    funding_source_id: int,
    service_id: int,
    staff_member_id: int,
    starts_on: date | None,
    ends_on: date | None,
) -> None:
    """Reject per-session pay when fixed service delivery covers the same period."""

    candidates = (
        GrantFixedCompensation.objects.select_for_update()
        .filter(
            payroll_budget__funding_source_id=funding_source_id,
            staff_member_id=staff_member_id,
            compensation_scope=GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY,
            service_id=service_id,
        )
        .select_related("payroll_budget")
        .order_by("pk")
    )
    for candidate in candidates:
        if _periods_overlap(
            starts_on,
            ends_on,
            candidate.period_from,
            candidate.period_to,
        ):
            raise ValidationError(
                {
                    "starts_on": (
                        "На этот период уже назначена фиксированная оплата "
                        "оказания той же услуги. Выберите один способ оплаты."
                    )
                }
            )


def _validate_no_staff_allocation_overlap(candidate: GrantFixedCompensation) -> None:
    if (
        candidate.compensation_scope
        != GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
    ):
        return
    allocations = (
        FundingStaffAllocation.objects.select_for_update()
        .filter(
            funding_source_id=candidate.payroll_budget.funding_source_id,
            service_id=candidate.service_id,
            staff_member_id=candidate.staff_member_id,
        )
        .order_by("pk")
    )
    for allocation in allocations:
        if _periods_overlap(
            candidate.period_from,
            candidate.period_to,
            allocation.starts_on,
            allocation.ends_on,
        ):
            raise ValidationError(
                {
                    "service": (
                        "На этот период уже действует сдельная ставка по услуге. "
                        "Закройте ее либо выберите отдельную проектную роль."
                    )
                }
            )


@transaction.atomic
def create_payroll_budget(
    *,
    funding_source: FundingSource,
    starts_on: date,
    ends_on: date,
    planned_amount: Decimal,
    enforcement_mode: str,
    note: str,
    actor: Any,
    reason: str,
) -> FundingPayrollBudget:
    _require_director(actor)
    normalized_reason = _normalize_reason(reason)
    _validate_budget_values(
        starts_on=starts_on,
        ends_on=ends_on,
        planned_amount=planned_amount,
    )
    source = _locked_active_source(funding_source.pk)
    budgets = _locked_source_budgets(source.pk)
    _validate_budget_overlap(
        budgets,
        starts_on=starts_on,
        ends_on=ends_on,
    )
    budget = FundingPayrollBudget(
        funding_source=source,
        starts_on=starts_on,
        ends_on=ends_on,
        planned_amount=planned_amount,
        enforcement_mode=enforcement_mode,
        lifecycle_status=FundingPayrollBudget.LifecycleStatus.ACTIVE,
        note=(note or "").strip(),
    )
    budget.save()
    now = timezone.now()
    revision = FundingPayrollBudgetRevision.objects.create(
        payroll_budget=budget,
        revision_number=1,
        event_type=FundingPayrollBudgetRevision.EventType.CREATED,
        starts_on=budget.starts_on,
        ends_on=budget.ends_on,
        planned_amount=budget.planned_amount,
        enforcement_mode=budget.enforcement_mode,
        lifecycle_status=budget.lifecycle_status,
        note=budget.note,
        actor=actor,
        actor_role_snapshot=FundingPayrollBudgetRevision.ActorRole.DIRECTOR,
        reason=normalized_reason,
        decided_at=now,
    )
    FundingPayrollBudget.objects.filter(pk=budget.pk).update(
        current_revision=revision,
        updated_at=now,
    )
    budget.current_revision = revision
    return budget


def _lock_payroll_budget(
    budget: FundingPayrollBudget,
) -> tuple[FundingPayrollBudget, list[FundingPayrollBudget]]:
    reference = FundingPayrollBudget.objects.only("pk", "funding_source_id").get(
        pk=budget.pk
    )
    _locked_active_source(reference.funding_source_id)
    budgets = _locked_source_budgets(reference.funding_source_id)
    locked = next(item for item in budgets if item.pk == reference.pk)
    return locked, budgets


@transaction.atomic
def revise_payroll_budget(
    budget: FundingPayrollBudget,
    *,
    starts_on: date,
    ends_on: date,
    planned_amount: Decimal,
    enforcement_mode: str,
    note: str,
    actor: Any,
    reason: str,
    expected_revision_id: int,
) -> FundingPayrollBudget:
    _require_director(actor)
    normalized_reason = _normalize_reason(reason)
    _validate_budget_values(
        starts_on=starts_on,
        ends_on=ends_on,
        planned_amount=planned_amount,
    )
    locked, budgets = _lock_payroll_budget(budget)
    _require_expected_revision(locked.current_revision_id, expected_revision_id)
    if locked.lifecycle_status == FundingPayrollBudget.LifecycleStatus.CLOSED:
        raise ValidationError("Закрытый бюджет нельзя изменять.")
    positions = _locked_budget_fixed_positions(locked)
    _validate_budget_contains_positions(
        positions,
        starts_on=starts_on,
        ends_on=ends_on,
    )
    _validate_budget_overlap(
        budgets,
        starts_on=starts_on,
        ends_on=ends_on,
        exclude_id=locked.pk,
    )
    previous = _current_budget_revision(locked)
    now = timezone.now()
    revision = FundingPayrollBudgetRevision.objects.create(
        payroll_budget=locked,
        revision_number=previous.revision_number + 1,
        event_type=FundingPayrollBudgetRevision.EventType.REVISED,
        starts_on=starts_on,
        ends_on=ends_on,
        planned_amount=planned_amount,
        enforcement_mode=enforcement_mode,
        lifecycle_status=locked.lifecycle_status,
        note=(note or "").strip(),
        actor=actor,
        actor_role_snapshot=FundingPayrollBudgetRevision.ActorRole.DIRECTOR,
        reason=normalized_reason,
        supersedes=previous,
        decided_at=now,
    )
    FundingPayrollBudget.objects.filter(pk=locked.pk).update(
        starts_on=starts_on,
        ends_on=ends_on,
        planned_amount=planned_amount,
        enforcement_mode=enforcement_mode,
        note=(note or "").strip(),
        current_revision=revision,
        updated_at=now,
    )
    locked.refresh_from_db()
    return locked


@transaction.atomic
def close_payroll_budget(
    budget: FundingPayrollBudget,
    *,
    actor: Any,
    reason: str,
    expected_revision_id: int,
) -> FundingPayrollBudget:
    _require_director(actor)
    normalized_reason = _normalize_reason(reason)
    locked, _budgets = _lock_payroll_budget(budget)
    _require_expected_revision(locked.current_revision_id, expected_revision_id)
    if locked.lifecycle_status == FundingPayrollBudget.LifecycleStatus.CLOSED:
        raise ValidationError("Бюджет уже закрыт.")
    active_positions = GrantFixedCompensation.objects.select_for_update().filter(
        payroll_budget=locked,
        lifecycle_status=GrantFixedCompensation.LifecycleStatus.ACTIVE,
    )
    if active_positions.exists():
        raise ValidationError("Сначала закройте активные фиксированные позиции бюджета.")
    previous = _current_budget_revision(locked)
    now = timezone.now()
    revision = FundingPayrollBudgetRevision.objects.create(
        payroll_budget=locked,
        revision_number=previous.revision_number + 1,
        event_type=FundingPayrollBudgetRevision.EventType.CLOSED,
        starts_on=locked.starts_on,
        ends_on=locked.ends_on,
        planned_amount=locked.planned_amount,
        enforcement_mode=locked.enforcement_mode,
        lifecycle_status=FundingPayrollBudget.LifecycleStatus.CLOSED,
        note=locked.note,
        actor=actor,
        actor_role_snapshot=FundingPayrollBudgetRevision.ActorRole.DIRECTOR,
        reason=normalized_reason,
        supersedes=previous,
        decided_at=now,
    )
    FundingPayrollBudget.objects.filter(pk=locked.pk).update(
        lifecycle_status=FundingPayrollBudget.LifecycleStatus.CLOSED,
        current_revision=revision,
        updated_at=now,
    )
    locked.refresh_from_db()
    return locked


def _fixed_candidate(
    *,
    payroll_budget: FundingPayrollBudget,
    staff_member: StaffMember,
    compensation_scope: str,
    service: Service | None,
    assignment_label: str,
    period_from: date,
    period_to: date,
    accrual_on: date,
    amount: Decimal,
    note: str,
    lifecycle_status: str,
) -> GrantFixedCompensation:
    candidate = GrantFixedCompensation(
        payroll_budget=payroll_budget,
        staff_member=staff_member,
        compensation_scope=compensation_scope,
        service=service,
        assignment_label=assignment_label,
        period_from=period_from,
        period_to=period_to,
        accrual_on=accrual_on,
        amount=amount,
        lifecycle_status=lifecycle_status,
        note=(note or "").strip(),
    )
    candidate._normalize_scope_fields()
    _validate_fixed_values(candidate)
    return candidate


@transaction.atomic
def create_fixed_compensation(
    *,
    payroll_budget: FundingPayrollBudget,
    staff_member: StaffMember,
    compensation_scope: str,
    service: Service | None,
    assignment_label: str,
    period_from: date,
    period_to: date,
    accrual_on: date,
    amount: Decimal,
    note: str,
    actor: Any,
    reason: str,
) -> GrantFixedCompensation:
    _require_director(actor)
    normalized_reason = _normalize_reason(reason)
    source_id = FundingPayrollBudget.objects.values_list(
        "funding_source_id", flat=True
    ).get(pk=payroll_budget.pk)
    _locked_active_source(source_id)
    budget = FundingPayrollBudget.objects.select_for_update().get(pk=payroll_budget.pk)
    if budget.lifecycle_status != FundingPayrollBudget.LifecycleStatus.ACTIVE:
        raise ValidationError("Нельзя добавлять позицию в закрытый бюджет.")
    budget_revision = _current_budget_revision(budget)
    candidate = _fixed_candidate(
        payroll_budget=budget,
        staff_member=staff_member,
        compensation_scope=compensation_scope,
        service=service,
        assignment_label=assignment_label,
        period_from=period_from,
        period_to=period_to,
        accrual_on=accrual_on,
        amount=amount,
        note=note,
        lifecycle_status=GrantFixedCompensation.LifecycleStatus.ACTIVE,
    )
    _validate_fixed_overlap(
        budget=budget,
        staff_member_id=staff_member.pk,
        compensation_scope=candidate.compensation_scope,
        service_id=candidate.service_id,
        assignment_key=candidate.assignment_key,
        period_from=period_from,
        period_to=period_to,
    )
    _validate_no_staff_allocation_overlap(candidate)
    candidate.save()
    now = timezone.now()
    revision = GrantFixedCompensationRevision.objects.create(
        fixed_compensation=candidate,
        revision_number=1,
        event_type=GrantFixedCompensationRevision.EventType.CREATED,
        budget_revision_at_decision=budget_revision,
        compensation_scope=candidate.compensation_scope,
        service=candidate.service,
        assignment_label=candidate.assignment_label,
        assignment_key=candidate.assignment_key,
        period_from=candidate.period_from,
        period_to=candidate.period_to,
        accrual_on=candidate.accrual_on,
        amount=candidate.amount,
        lifecycle_status=candidate.lifecycle_status,
        note=candidate.note,
        actor=actor,
        actor_role_snapshot=GrantFixedCompensationRevision.ActorRole.DIRECTOR,
        reason=normalized_reason,
        decided_at=now,
    )
    GrantFixedCompensation.objects.filter(pk=candidate.pk).update(
        current_revision=revision,
        updated_at=now,
    )
    candidate.current_revision = revision
    return candidate


def _lock_fixed_compensation(
    fixed: GrantFixedCompensation,
) -> tuple[GrantFixedCompensation, FundingPayrollBudget]:
    reference = GrantFixedCompensation.objects.select_related("payroll_budget").get(
        pk=fixed.pk
    )
    _locked_active_source(reference.payroll_budget.funding_source_id)
    budget = FundingPayrollBudget.objects.select_for_update().get(
        pk=reference.payroll_budget_id
    )
    positions = _locked_budget_fixed_positions(budget)
    locked = next(item for item in positions if item.pk == reference.pk)
    return locked, budget


@transaction.atomic
def revise_fixed_compensation(
    fixed: GrantFixedCompensation,
    *,
    period_from: date,
    period_to: date,
    accrual_on: date,
    amount: Decimal,
    note: str,
    actor: Any,
    reason: str,
    expected_revision_id: int,
) -> GrantFixedCompensation:
    _require_director(actor)
    normalized_reason = _normalize_reason(reason)
    locked, budget = _lock_fixed_compensation(fixed)
    _require_expected_revision(locked.current_revision_id, expected_revision_id)
    if locked.lifecycle_status == GrantFixedCompensation.LifecycleStatus.CLOSED:
        raise ValidationError("Закрытую фиксированную позицию нельзя изменять.")
    budget_revision = _current_budget_revision(budget)
    candidate = _fixed_candidate(
        payroll_budget=budget,
        staff_member=locked.staff_member,
        compensation_scope=locked.compensation_scope,
        service=locked.service,
        assignment_label=locked.assignment_label,
        period_from=period_from,
        period_to=period_to,
        accrual_on=accrual_on,
        amount=amount,
        note=note,
        lifecycle_status=locked.lifecycle_status,
    )
    _validate_fixed_overlap(
        budget=budget,
        staff_member_id=locked.staff_member_id,
        compensation_scope=locked.compensation_scope,
        service_id=locked.service_id,
        assignment_key=locked.assignment_key,
        period_from=period_from,
        period_to=period_to,
        exclude_id=locked.pk,
    )
    _validate_no_staff_allocation_overlap(candidate)
    previous = _current_fixed_revision(locked)
    now = timezone.now()
    revision = GrantFixedCompensationRevision.objects.create(
        fixed_compensation=locked,
        revision_number=previous.revision_number + 1,
        event_type=GrantFixedCompensationRevision.EventType.REVISED,
        budget_revision_at_decision=budget_revision,
        compensation_scope=locked.compensation_scope,
        service=locked.service,
        assignment_label=locked.assignment_label,
        assignment_key=locked.assignment_key,
        period_from=period_from,
        period_to=period_to,
        accrual_on=accrual_on,
        amount=amount,
        lifecycle_status=locked.lifecycle_status,
        note=(note or "").strip(),
        actor=actor,
        actor_role_snapshot=GrantFixedCompensationRevision.ActorRole.DIRECTOR,
        reason=normalized_reason,
        supersedes=previous,
        decided_at=now,
    )
    GrantFixedCompensation.objects.filter(pk=locked.pk).update(
        period_from=period_from,
        period_to=period_to,
        accrual_on=accrual_on,
        amount=amount,
        note=(note or "").strip(),
        current_revision=revision,
        updated_at=now,
    )
    locked.refresh_from_db()
    return locked


@transaction.atomic
def close_fixed_compensation(
    fixed: GrantFixedCompensation,
    *,
    actor: Any,
    reason: str,
    expected_revision_id: int,
) -> GrantFixedCompensation:
    _require_director(actor)
    normalized_reason = _normalize_reason(reason)
    locked, budget = _lock_fixed_compensation(fixed)
    _require_expected_revision(locked.current_revision_id, expected_revision_id)
    if locked.lifecycle_status == GrantFixedCompensation.LifecycleStatus.CLOSED:
        raise ValidationError("Фиксированная позиция уже закрыта.")
    budget_revision = _current_budget_revision(budget)
    previous = _current_fixed_revision(locked)
    now = timezone.now()
    revision = GrantFixedCompensationRevision.objects.create(
        fixed_compensation=locked,
        revision_number=previous.revision_number + 1,
        event_type=GrantFixedCompensationRevision.EventType.CLOSED,
        budget_revision_at_decision=budget_revision,
        compensation_scope=locked.compensation_scope,
        service=locked.service,
        assignment_label=locked.assignment_label,
        assignment_key=locked.assignment_key,
        period_from=locked.period_from,
        period_to=locked.period_to,
        accrual_on=locked.accrual_on,
        amount=locked.amount,
        lifecycle_status=GrantFixedCompensation.LifecycleStatus.CLOSED,
        note=locked.note,
        actor=actor,
        actor_role_snapshot=GrantFixedCompensationRevision.ActorRole.DIRECTOR,
        reason=normalized_reason,
        supersedes=previous,
        decided_at=now,
    )
    GrantFixedCompensation.objects.filter(pk=locked.pk).update(
        lifecycle_status=GrantFixedCompensation.LifecycleStatus.CLOSED,
        current_revision=revision,
        updated_at=now,
    )
    locked.refresh_from_db()
    return locked


def grant_compensation_integrity_findings() -> list[GrantCompensationIntegrityFinding]:
    findings: list[GrantCompensationIntegrityFinding] = []
    budgets = list(
        FundingPayrollBudget.objects.select_related("current_revision").order_by("pk")
    )
    positions = list(
        GrantFixedCompensation.objects.select_related(
            "current_revision",
            "current_revision__budget_revision_at_decision",
            "payroll_budget",
        ).order_by("pk")
    )

    budget_fields = (
        "starts_on",
        "ends_on",
        "planned_amount",
        "enforcement_mode",
        "lifecycle_status",
        "note",
    )
    for budget in budgets:
        revision = budget.current_revision
        if revision is None:
            findings.append(
                GrantCompensationIntegrityFinding(
                    "payroll_budget_missing_current_revision",
                    "payroll_budget",
                    budget.pk,
                    "У корня отсутствует current_revision.",
                )
            )
            continue
        if revision.payroll_budget_id != budget.pk:
            findings.append(
                GrantCompensationIntegrityFinding(
                    "payroll_budget_current_revision_wrong_root",
                    "payroll_budget",
                    budget.pk,
                    f"Редакция {revision.pk} относится к другому корню.",
                )
            )
            continue
        if any(getattr(budget, field) != getattr(revision, field) for field in budget_fields):
            findings.append(
                GrantCompensationIntegrityFinding(
                    "payroll_budget_projection_mismatch",
                    "payroll_budget",
                    budget.pk,
                    f"Проекция не совпадает с редакцией {revision.pk}.",
                )
            )
        if revision.superseded_by.exists():
            findings.append(
                GrantCompensationIntegrityFinding(
                    "payroll_budget_current_revision_not_terminal",
                    "payroll_budget",
                    budget.pk,
                    f"Текущая редакция {revision.pk} уже имеет преемника.",
                )
            )

    fixed_fields = (
        "compensation_scope",
        "service_id",
        "assignment_label",
        "assignment_key",
        "period_from",
        "period_to",
        "accrual_on",
        "amount",
        "lifecycle_status",
        "note",
    )
    for position in positions:
        revision = position.current_revision
        if revision is None:
            findings.append(
                GrantCompensationIntegrityFinding(
                    "grant_fixed_missing_current_revision",
                    "fixed_compensation",
                    position.pk,
                    "У корня отсутствует current_revision.",
                )
            )
            continue
        if revision.fixed_compensation_id != position.pk:
            findings.append(
                GrantCompensationIntegrityFinding(
                    "grant_fixed_current_revision_wrong_root",
                    "fixed_compensation",
                    position.pk,
                    f"Редакция {revision.pk} относится к другому корню.",
                )
            )
            continue
        if any(getattr(position, field) != getattr(revision, field) for field in fixed_fields):
            findings.append(
                GrantCompensationIntegrityFinding(
                    "grant_fixed_projection_mismatch",
                    "fixed_compensation",
                    position.pk,
                    f"Проекция не совпадает с редакцией {revision.pk}.",
                )
            )
        if (
            revision.budget_revision_at_decision.payroll_budget_id
            != position.payroll_budget_id
        ):
            findings.append(
                GrantCompensationIntegrityFinding(
                    "grant_fixed_wrong_budget_revision",
                    "fixed_compensation",
                    position.pk,
                    (
                        f"Редакция бюджета {revision.budget_revision_at_decision_id} "
                        "относится к другому корню."
                    ),
                )
            )
        if revision.superseded_by.exists():
            findings.append(
                GrantCompensationIntegrityFinding(
                    "grant_fixed_current_revision_not_terminal",
                    "fixed_compensation",
                    position.pk,
                    f"Текущая редакция {revision.pk} уже имеет преемника.",
                )
            )
        if (
            position.period_from < position.payroll_budget.starts_on
            or position.period_to > position.payroll_budget.ends_on
            or position.accrual_on < position.payroll_budget.starts_on
            or position.accrual_on > position.payroll_budget.ends_on
        ):
            findings.append(
                GrantCompensationIntegrityFinding(
                    "grant_fixed_outside_budget",
                    "fixed_compensation",
                    position.pk,
                    "Позиция выходит за период бюджета.",
                )
            )

    for left, right in combinations(budgets, 2):
        if left.funding_source_id != right.funding_source_id:
            continue
        if _periods_overlap(left.starts_on, left.ends_on, right.starts_on, right.ends_on):
            findings.append(
                GrantCompensationIntegrityFinding(
                    "payroll_budget_period_overlap",
                    "payroll_budget",
                    left.pk,
                    f"Период пересекается с бюджетом {right.pk}.",
                )
            )

    for left, right in combinations(positions, 2):
        if (
            left.payroll_budget.funding_source_id
            != right.payroll_budget.funding_source_id
            or left.staff_member_id != right.staff_member_id
            or left.compensation_scope != right.compensation_scope
        ):
            continue
        same_subject = (
            left.service_id == right.service_id
            if left.compensation_scope
            == GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
            else left.assignment_key == right.assignment_key
        )
        if same_subject and _periods_overlap(
            left.period_from,
            left.period_to,
            right.period_from,
            right.period_to,
        ):
            findings.append(
                GrantCompensationIntegrityFinding(
                    "grant_fixed_period_overlap",
                    "fixed_compensation",
                    left.pk,
                    f"Период пересекается с позицией {right.pk}.",
                )
            )

    for position in positions:
        if (
            position.compensation_scope
            != GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
        ):
            continue
        allocations = FundingStaffAllocation.objects.filter(
            funding_source_id=position.payroll_budget.funding_source_id,
            service_id=position.service_id,
            staff_member_id=position.staff_member_id,
        )
        for allocation in allocations:
            if _periods_overlap(
                position.period_from,
                position.period_to,
                allocation.starts_on,
                allocation.ends_on,
            ):
                findings.append(
                    GrantCompensationIntegrityFinding(
                        "grant_fixed_staff_allocation_overlap",
                        "fixed_compensation",
                        position.pk,
                        f"Период пересекается со сдельной ставкой {allocation.pk}.",
                    )
                )

    return findings
