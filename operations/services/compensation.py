"""Shared staff compensation calculations for timesheets and payroll."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Q

from operations.models import (
    Appointment,
    FundingSource,
    FundingStaffAllocation,
    StaffCompensationRule,
    StaffMember,
)
from operations.services.financial_facts import AppointmentChargeFact


@dataclass(frozen=True)
class StaffCompensationCalculation:
    payable: bool
    has_rate: bool
    rule: StaffCompensationRule | None
    allocation: FundingStaffAllocation | None
    funding_source: FundingSource | None
    rate_type: str
    rate_amount: Decimal
    amount: Decimal
    note: str
    rate_label: str
    session_scope: str
    group_pay_policy: str
    charged_participants_count: int
    pay_units: int


def matching_compensation_rule(
    rules: list[StaffCompensationRule],
    *,
    service_id: int,
    funding_source_id: int | None,
    work_date: date,
    duration_minutes: int,
    session_type: str,
) -> StaffCompensationRule | None:
    desired_scope = (
        StaffCompensationRule.SessionScope.GROUP
        if session_type == Appointment.SessionType.GROUP
        else StaffCompensationRule.SessionScope.INDIVIDUAL
    )
    matches: list[StaffCompensationRule] = []
    for rule in rules:
        if rule.session_scope not in (StaffCompensationRule.SessionScope.ALL, desired_scope):
            continue
        if rule.service_id and rule.service_id != service_id:
            continue
        if rule.funding_source_id and rule.funding_source_id != funding_source_id:
            continue
        if rule.min_duration_minutes and duration_minutes < rule.min_duration_minutes:
            continue
        if rule.max_duration_minutes and duration_minutes > rule.max_duration_minutes:
            continue
        if rule.starts_on and rule.starts_on > work_date:
            continue
        if rule.ends_on and rule.ends_on < work_date:
            continue
        matches.append(rule)

    if not matches:
        return None

    return sorted(
        matches,
        key=lambda rule: (
            1 if rule.session_scope == desired_scope else 0,
            (1 if rule.service_id else 0) + (1 if rule.funding_source_id else 0),
            1 if rule.funding_source_id else 0,
            1 if rule.service_id else 0,
            (1 if rule.min_duration_minutes else 0) + (1 if rule.max_duration_minutes else 0),
            rule.starts_on or date.min,
            rule.pk or 0,
        ),
        reverse=True,
    )[0]


def matching_grant_allocation(
    *,
    staff: StaffMember,
    service_id: int,
    funding_source_id: int | None,
    work_date: date,
) -> FundingStaffAllocation | None:
    if not funding_source_id:
        return None
    return (
        FundingStaffAllocation.objects.filter(
            staff_member=staff,
            service_id=service_id,
            funding_source_id=funding_source_id,
            session_pay_amount__isnull=False,
        )
        .filter(Q(starts_on__isnull=True) | Q(starts_on__lte=work_date))
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=work_date))
        .select_related("service_quota", "funding_source", "service", "staff_member")
        .order_by("-starts_on", "-service_quota_id", "-pk")
        .first()
    )


def calculate_staff_compensation(
    *,
    staff: StaffMember,
    appointment: Appointment,
    charge_fact: AppointmentChargeFact,
    rules: list[StaffCompensationRule],
    work_date: date,
    duration_minutes: int,
) -> StaffCompensationCalculation:
    charged_count = _charged_participants_count(charge_fact)
    default = StaffCompensationCalculation(
        payable=False,
        has_rate=False,
        rule=None,
        allocation=None,
        funding_source=charge_fact.funding_source,
        rate_type=StaffCompensationRule.RateType.PER_SESSION,
        rate_amount=Decimal("0"),
        amount=Decimal("0"),
        note=charge_fact.note,
        rate_label="",
        session_scope=StaffCompensationRule.SessionScope.ALL,
        group_pay_policy=StaffCompensationRule.GroupPayPolicy.PER_SESSION,
        charged_participants_count=max(charged_count, 1),
        pay_units=1,
    )
    if not charge_fact.is_charged:
        return default

    allocation = matching_grant_allocation(
        staff=staff,
        service_id=appointment.service_id,
        funding_source_id=charge_fact.funding_source.pk if charge_fact.funding_source else None,
        work_date=work_date,
    )
    if allocation is not None:
        rate_amount = allocation.session_pay_amount or Decimal("0")
        return StaffCompensationCalculation(
            payable=True,
            has_rate=True,
            rule=None,
            allocation=allocation,
            funding_source=charge_fact.funding_source,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            rate_amount=rate_amount,
            amount=rate_amount,
            note=f"{charge_fact.note}; ставка из распределения грантовой квоты",
            rate_label=f"{rate_amount} / грантовая квота",
            session_scope=StaffCompensationRule.SessionScope.ALL,
            group_pay_policy=StaffCompensationRule.GroupPayPolicy.PER_SESSION,
            charged_participants_count=max(charged_count, 1),
            pay_units=1,
        )

    session_type = _compensation_session_type(appointment)
    rule = matching_compensation_rule(
        rules,
        service_id=appointment.service_id,
        funding_source_id=charge_fact.funding_source.pk if charge_fact.funding_source else None,
        work_date=work_date,
        duration_minutes=max(duration_minutes, 0),
        session_type=session_type,
    )
    if rule is None:
        return StaffCompensationCalculation(
            **{
                **default.__dict__,
                "payable": True,
                "note": f"{charge_fact.note}; ставка не задана",
            }
        )

    amount, rate_amount, pay_units, group_pay_policy, note_suffix = _rule_amount(
        rule=rule,
        minutes=max(duration_minutes, 0),
        is_group=session_type == Appointment.SessionType.GROUP,
        charged_participants_count=max(charged_count, 1),
    )
    note = charge_fact.note
    if note_suffix:
        note = f"{note}; {note_suffix}"
    rate_type = rule.rate_type
    if group_pay_policy == StaffCompensationRule.GroupPayPolicy.FIXED_GROUP_AMOUNT:
        rate_type = StaffCompensationRule.RateType.PER_SESSION
    return StaffCompensationCalculation(
        payable=True,
        has_rate=True,
        rule=rule,
        allocation=None,
        funding_source=charge_fact.funding_source,
        rate_type=rate_type,
        rate_amount=rate_amount,
        amount=amount,
        note=note,
        rate_label=_rate_label(
            rule=rule,
            rate_amount=rate_amount,
            group_pay_policy=group_pay_policy,
            pay_units=pay_units,
        ),
        session_scope=rule.session_scope,
        group_pay_policy=group_pay_policy,
        charged_participants_count=max(charged_count, 1),
        pay_units=pay_units,
    )


def _charged_participants_count(charge_fact: AppointmentChargeFact) -> int:
    if charge_fact.charged_participants:
        return len(charge_fact.charged_participants)
    return 1 if charge_fact.is_charged else 0


def _compensation_session_type(appointment: Appointment) -> str:
    if appointment.session_type == Appointment.SessionType.GROUP:
        return Appointment.SessionType.GROUP
    participants = tuple(appointment.participants.all())
    if len(participants) > 1:
        return Appointment.SessionType.GROUP
    return Appointment.SessionType.INDIVIDUAL


def _base_amount(rule: StaffCompensationRule, minutes: int) -> Decimal:
    if rule.rate_type == StaffCompensationRule.RateType.HOURLY:
        return (rule.amount * Decimal(minutes) / Decimal(60)).quantize(Decimal("0.01"))
    return rule.amount


def _rule_amount(
    *,
    rule: StaffCompensationRule,
    minutes: int,
    is_group: bool,
    charged_participants_count: int,
) -> tuple[Decimal, Decimal, int, str, str]:
    if not is_group:
        return (
            _base_amount(rule, minutes),
            rule.amount,
            1,
            StaffCompensationRule.GroupPayPolicy.PER_SESSION,
            "",
        )

    if rule.group_pay_policy == StaffCompensationRule.GroupPayPolicy.FIXED_GROUP_AMOUNT:
        amount = rule.group_fixed_amount or Decimal("0")
        return (
            amount,
            amount,
            1,
            StaffCompensationRule.GroupPayPolicy.FIXED_GROUP_AMOUNT,
            "фиксированная сумма за групповое занятие",
        )

    base = _base_amount(rule, minutes)
    if rule.group_pay_policy == StaffCompensationRule.GroupPayPolicy.PER_CHARGED_PARTICIPANT:
        units = max(charged_participants_count, 1)
        return (
            (base * Decimal(units)).quantize(Decimal("0.01")),
            rule.amount,
            units,
            StaffCompensationRule.GroupPayPolicy.PER_CHARGED_PARTICIPANT,
            f"начисление по списанным участникам: {units}",
        )

    return (
        base,
        rule.amount,
        1,
        StaffCompensationRule.GroupPayPolicy.PER_SESSION,
        "",
    )


def _rate_label(
    *,
    rule: StaffCompensationRule,
    rate_amount: Decimal,
    group_pay_policy: str,
    pay_units: int,
) -> str:
    if group_pay_policy == StaffCompensationRule.GroupPayPolicy.FIXED_GROUP_AMOUNT:
        return f"{rate_amount} / фикс. группа"

    label = f"{rule.amount} / {rule.get_rate_type_display()}"
    if group_pay_policy == StaffCompensationRule.GroupPayPolicy.PER_CHARGED_PARTICIPANT:
        label = f"{label} × {pay_units}"
    return label
