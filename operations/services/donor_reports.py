"""Immutable, privacy-safe donor report snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentStaffAssignment,
    BalanceAccount,
    CenterExpense,
    CenterExpenseCategory,
    Counterparty,
    DonorReport,
    DonorReportSnapshot,
    ExpenseFundingSplit,
    FundingPayrollBudget,
    FundingServiceQuota,
    FundingSource,
    FundingStaffAllocation,
    GrantFixedCompensation,
    GrantRecipientAllocation,
    LedgerEntry,
    PayrollAccrual,
    PayrollSheet,
    PayrollSheetLine,
    Service,
)
from operations.services import (
    financial_integrity as financial_integrity_svc,
    grant_compensation as grant_compensation_svc,
    grant_plans as grant_plans_svc,
    reports as reports_svc,
)
from operations.services.authority import is_director_user

SNAPSHOT_SCHEMA_VERSION = "internal_grant_reconciliation_v1"
CANONICALIZER_VERSION = "canonical-json-v1"
MAX_TRANSACTION_ATTEMPTS = 3
RETRYABLE_SQLSTATES = {"40001", "40P01"}
RETRYABLE_UNIQUE_CONSTRAINTS = {
    "unique_donor_report_identity",
    "unique_donor_report_snapshot_number",
    "unique_donor_report_snapshot_successor",
}
FORBIDDEN_PAYLOAD_KEYS = {
    "account_id",
    "account_number",
    "certificate_number",
    "child_id",
    "contact",
    "email",
    "full_name",
    "medical",
    "note",
    "notes",
    "phone",
    "recipient_id",
    "staff_id",
}
ALIAS_PREFIXES = {
    "funding_source": "SRC",
    "counterparty": "DON",
    "service": "SVC",
    "service_quota": "QTA",
    "direct_service_quota": "QTA",
    "staff_allocation": "SAL",
    "staff": "SPC",
    "recipient": "RCP",
    "recipient_allocation": "RAL",
    "payroll_budget": "BUD",
    "fixed_position": "FIX",
}
EVIDENCE_SCHEMA_VERSION = "internal_grant_reconciliation_evidence_v1"
SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)\.[0-9]{2}$")
REF_RE = re.compile(
    r"^(?:SRC|DON|SVC|QTA|SAL|SPC|RCP|RAL|BUD|FIX)-[0-9]{3,}$"
)
EVIDENCE_SOURCE_KINDS = {
    "appointment",
    "appointment_participant",
    "appointment_staff_assignment",
    "balance_account",
    "center_expense",
    "counterparty",
    "expense_category",
    "expense_split",
    "fixed_position",
    "funding_source",
    "ledger_entry",
    "payroll_accrual",
    "payroll_budget",
    "payroll_sheet",
    "payroll_sheet_line",
    "recipient_allocation",
    "service",
    "service_quota",
    "staff_allocation",
}


@dataclass(frozen=True)
class DonorReportReview:
    funding_source_id: int
    counterparty_id: int | None
    date_from: date
    date_to: date
    expected_snapshot_id: int | None
    payload: dict[str, Any]
    evidence_manifest: dict[str, Any]
    payload_sha256: str
    evidence_manifest_sha256: str
    review_token: str
    data_as_of: datetime


def _alias_registry(
    previous_manifest: dict[str, Any] | None,
    current_ids: dict[str, set[int]],
) -> tuple[dict[str, dict[int, str]], list[dict[str, Any]]]:
    previous_rows = (
        previous_manifest.get("aliases", [])
        if isinstance(previous_manifest, dict)
        else []
    )
    registry: dict[tuple[str, int], str] = {}
    used_numbers: dict[str, set[int]] = {}
    for row in previous_rows:
        if not isinstance(row, dict):
            continue
        kind = row.get("kind")
        source_pk = row.get("source_pk")
        ref = row.get("ref")
        prefix = ALIAS_PREFIXES.get(kind)
        if (
            prefix is None
            or not isinstance(source_pk, int)
            or not isinstance(ref, str)
            or not ref.startswith(f"{prefix}-")
        ):
            continue
        suffix = ref.removeprefix(f"{prefix}-")
        if not suffix.isdigit():
            continue
        registry[(kind, source_pk)] = ref
        used_numbers.setdefault(prefix, set()).add(int(suffix))

    for kind in sorted(current_ids):
        prefix = ALIAS_PREFIXES[kind]
        used = used_numbers.setdefault(prefix, set())
        next_number = max(used, default=0) + 1
        for source_pk in sorted(current_ids[kind]):
            key = (kind, source_pk)
            if key in registry:
                continue
            while next_number in used:
                next_number += 1
            registry[key] = f"{prefix}-{next_number:03d}"
            used.add(next_number)
            next_number += 1

    mappings = {
        kind: {
            source_pk: registry[(kind, source_pk)]
            for source_pk in sorted(ids)
        }
        for kind, ids in current_ids.items()
    }
    active_keys = {
        (kind, source_pk)
        for kind, ids in current_ids.items()
        for source_pk in ids
    }
    aliases = [
        {
            "kind": kind,
            "ref": ref,
            "source_pk": source_pk,
            "active": (kind, source_pk) in active_keys,
        }
        for (kind, source_pk), ref in sorted(
            registry.items(),
            key=lambda item: (item[0][0], item[1]),
        )
    ]
    return mappings, aliases


def _source_record(
    kind: str,
    source_pk: int,
    *,
    revision_pk: int | None = None,
    projection: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "source_pk": source_pk,
        "revision_pk": revision_pk,
        "projection_sha256": canonical_sha256(projection),
    }


def _normalize_reason(reason: str) -> str:
    normalized = (reason or "").strip()
    if len(normalized) < 5:
        raise ValidationError(
            {"reason": "Укажите содержательное основание (минимум 5 символов)."}
        )
    return normalized


def _require_director(actor: Any) -> None:
    if not is_director_user(actor):
        raise PermissionDenied("Закрыть или исправить отчет может только руководитель.")


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError("Non-finite Decimal is not supported in canonical JSON.")
        return format(value.quantize(Decimal("0.01")), "f")
    if isinstance(value, datetime):
        aware = value if timezone.is_aware(value) else timezone.make_aware(value)
        return (
            aware.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("Canonical JSON object keys must be strings.")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise TypeError(
                    "Canonical JSON object contains colliding Unicode-normalized keys."
                )
            normalized[normalized_key] = _json_value(child)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if value is None or isinstance(value, int | bool):
        return value
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    normalized = _json_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_document(value: Any) -> tuple[bytes, str]:
    document = canonical_json_bytes(value)
    return document, hashlib.sha256(document).hexdigest()


def canonical_sha256(value: Any) -> str:
    return canonical_json_document(value)[1]


def _payload_forbidden_key_paths(value: Any, path: str = "payload") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).lower()
            child_path = f"{path}.{key}"
            if normalized_key in FORBIDDEN_PAYLOAD_KEYS:
                paths.append(child_path)
            paths.extend(_payload_forbidden_key_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_payload_forbidden_key_paths(child, f"{path}[{index}]"))
    return paths


def _expect_payload_keys(
    value: Any,
    expected: set[str],
    *,
    path: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError({"payload": f"{path} должен быть JSON-объектом."})
    actual = set(value)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        details = []
        if unknown:
            details.append("лишние: " + ", ".join(unknown))
        if missing:
            details.append("отсутствуют: " + ", ".join(missing))
        raise ValidationError(
            {"payload": f"{path}: нарушен allowlist ({'; '.join(details)})."}
        )
    return value


def _validate_payload_allowlist(payload: dict[str, Any]) -> None:
    _expect_payload_keys(
        payload,
        {
            "schema_version",
            "report",
            "balances",
            "quotas",
            "recipient_allocations",
            "payroll",
            "expenses",
            "integrity",
        },
        path="payload",
    )
    report = _expect_payload_keys(
        payload["report"],
        {"report_kind", "period", "funding_source", "donor"},
        path="payload.report",
    )
    _expect_payload_keys(
        report["period"],
        {"from", "to"},
        path="payload.report.period",
    )
    _expect_payload_keys(
        report["funding_source"],
        {"ref", "type"},
        path="payload.report.funding_source",
    )
    if report["donor"] is not None:
        _expect_payload_keys(
            report["donor"],
            {"ref", "type"},
            path="payload.report.donor",
        )
    for index, row in enumerate(
        _require_list(
            payload["balances"],
            field="payload",
            path="payload.balances",
        )
    ):
        _expect_payload_keys(
            row,
            {
                "unit",
                "opening",
                "inflows",
                "outflows",
                "closing",
                "appointment_count",
                "planned_count",
                "completed_count",
            },
            path=f"payload.balances[{index}]",
        )
    for index, row in enumerate(
        _require_list(
            payload["quotas"],
            field="payload",
            path="payload.quotas",
        )
    ):
        _expect_payload_keys(
            row,
            {
                "ref",
                "service",
                "period",
                "planned_sessions",
                "allocated_sessions",
                "charged_sessions",
                "remaining_sessions",
                "staff_allocations",
            },
            path=f"payload.quotas[{index}]",
        )
        _expect_payload_keys(
            row["service"],
            {"ref", "category"},
            path=f"payload.quotas[{index}].service",
        )
        _expect_payload_keys(
            row["period"],
            {"from", "to"},
            path=f"payload.quotas[{index}].period",
        )
        for allocation_index, allocation in enumerate(
            _require_list(
                row["staff_allocations"],
                field="payload",
                path=f"payload.quotas[{index}].staff_allocations",
            )
        ):
            _expect_payload_keys(
                allocation,
                {
                    "ref",
                    "staff_ref",
                    "allocated_sessions",
                    "charged_sessions",
                    "remaining_sessions",
                    "session_pay_amount",
                },
                path=(
                    f"payload.quotas[{index}].staff_allocations"
                    f"[{allocation_index}]"
                ),
            )
    recipient_rows = _require_list(
        payload["recipient_allocations"],
        field="payload",
        path="payload.recipient_allocations",
    )
    for index, row in enumerate(recipient_rows):
        _expect_payload_keys(
            row,
            {
                "ref",
                "recipient_ref",
                "service",
                "period",
                "allocated_sessions",
                "charged_sessions",
                "remaining_sessions",
            },
            path=f"payload.recipient_allocations[{index}]",
        )
        _expect_payload_keys(
            row["service"],
            {"ref", "category"},
            path=f"payload.recipient_allocations[{index}].service",
        )
        _expect_payload_keys(
            row["period"],
            {"from", "to"},
            path=f"payload.recipient_allocations[{index}].period",
        )
    payroll = _expect_payload_keys(
        payload["payroll"],
        {"budgets", "fixed_positions", "accrual_totals"},
        path="payload.payroll",
    )
    for index, row in enumerate(
        _require_list(
            payroll["budgets"],
            field="payload",
            path="payload.payroll.budgets",
        )
    ):
        _expect_payload_keys(
            row,
            {
                "budget_ref",
                "starts_on",
                "ends_on",
                "planned_amount",
                "enforcement_mode",
                "lifecycle_status",
                "consumed_amount",
                "draft_commitment_amount",
                "available_amount",
                "forecast_available_amount",
            },
            path=f"payload.payroll.budgets[{index}]",
        )
    for index, row in enumerate(
        _require_list(
            payroll["fixed_positions"],
            field="payload",
            path="payload.payroll.fixed_positions",
        )
    ):
        _expect_payload_keys(
            row,
            {
                "fixed_ref",
                "budget_ref",
                "staff_ref",
                "compensation_scope",
                "service",
                "period",
                "accrual_on",
                "amount",
                "lifecycle_status",
            },
            path=f"payload.payroll.fixed_positions[{index}]",
        )
        if row["service"] is not None:
            _expect_payload_keys(
                row["service"],
                {"ref", "category"},
                path=f"payload.payroll.fixed_positions[{index}].service",
            )
        _expect_payload_keys(
            row["period"],
            {"from", "to"},
            path=f"payload.payroll.fixed_positions[{index}].period",
        )
    for index, row in enumerate(
        _require_list(
            payroll["accrual_totals"],
            field="payload",
            path="payload.payroll.accrual_totals",
        )
    ):
        _expect_payload_keys(
            row,
            {"staff_ref", "accrual_kind", "status", "count", "amount"},
            path=f"payload.payroll.accrual_totals[{index}]",
        )
    for index, row in enumerate(
        _require_list(
            payload["expenses"],
            field="payload",
            path="payload.expenses",
        )
    ):
        _expect_payload_keys(
            row,
            {"expense_type", "status", "count", "amount"},
            path=f"payload.expenses[{index}]",
        )
    integrity = _expect_payload_keys(
        payload["integrity"],
        {"status", "warnings"},
        path="payload.integrity",
    )
    for index, row in enumerate(
        _require_list(
            integrity["warnings"],
            field="payload",
            path="payload.integrity.warnings",
        )
    ):
        if not isinstance(row, dict):
            raise ValidationError(
                {"payload": f"payload.integrity.warnings[{index}] должен быть объектом."}
            )
        allowed = {"code", "severity", "count", "object_ref", "unit"}
        if not {"code", "severity", "count"}.issubset(row) or set(row) - allowed:
            raise ValidationError(
                {
                    "payload": (
                        f"payload.integrity.warnings[{index}]: "
                        "нарушен allowlist предупреждения."
                    )
                }
            )


def _fail_json(field: str, path: str, message: str) -> None:
    raise ValidationError({field: f"{path}: {message}"})


def _require_list(value: Any, *, field: str, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail_json(field, path, "ожидается JSON-массив.")
    return value


def _require_stable_order(
    rows: list[Any],
    *,
    field: str,
    path: str,
    key: Callable[[Any], Any],
) -> None:
    if rows != sorted(rows, key=key):
        _fail_json(field, path, "строки должны быть в каноническом порядке.")


def _require_int(
    value: Any,
    *,
    field: str,
    path: str,
    non_negative: bool = False,
    positive: bool = False,
) -> int:
    if type(value) is not int:
        _fail_json(field, path, "ожидается целое число.")
    if non_negative and value < 0:
        _fail_json(field, path, "число не может быть отрицательным.")
    if positive and value <= 0:
        _fail_json(field, path, "число должно быть положительным.")
    return value


def _require_choice(
    value: Any,
    choices: set[str],
    *,
    field: str,
    path: str,
) -> str:
    if not isinstance(value, str) or value not in choices:
        _fail_json(field, path, "значение не входит в разрешенный справочник.")
    return value


def _require_date(
    value: Any,
    *,
    field: str,
    path: str,
    optional: bool = False,
) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        _fail_json(field, path, "ожидается ISO-дата.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _fail_json(field, path, "ожидается ISO-дата.")
    if parsed.isoformat() != value:
        _fail_json(field, path, "дата должна быть в каноническом ISO-формате.")
    return value


def _require_date_order(
    start_value: Any,
    end_value: Any,
    *,
    field: str,
    path: str,
    optional: bool = False,
    start_key: str = "from",
    end_key: str = "to",
) -> None:
    start = _require_date(
        start_value,
        field=field,
        path=f"{path}.{start_key}",
        optional=optional,
    )
    end = _require_date(
        end_value,
        field=field,
        path=f"{path}.{end_key}",
        optional=optional,
    )
    if start is not None and end is not None and end < start:
        _fail_json(field, path, "дата окончания не может быть раньше даты начала.")


def _require_decimal(value: Any, *, field: str, path: str) -> str:
    if not isinstance(value, str) or DECIMAL_RE.fullmatch(value) is None:
        _fail_json(field, path, "ожидается Decimal-строка с двумя знаками.")
    return value


def _require_ref(
    value: Any,
    prefix: str,
    *,
    field: str,
    path: str,
) -> str:
    if (
        not isinstance(value, str)
        or REF_RE.fullmatch(value) is None
        or not value.startswith(f"{prefix}-")
    ):
        _fail_json(field, path, f"ожидается локальный псевдоним {prefix}-NNN.")
    return value


def _validate_payload_values(payload: dict[str, Any]) -> None:
    _validate_payload_allowlist(payload)
    if payload["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
        _fail_json("payload", "payload.schema_version", "неподдерживаемая версия.")

    report = payload["report"]
    if report["report_kind"] != SNAPSHOT_SCHEMA_VERSION:
        _fail_json("payload", "payload.report.report_kind", "неподдерживаемый вид.")
    _require_date_order(
        report["period"]["from"],
        report["period"]["to"],
        field="payload",
        path="payload.report.period",
    )
    _require_ref(
        report["funding_source"]["ref"],
        "SRC",
        field="payload",
        path="payload.report.funding_source.ref",
    )
    _require_choice(
        report["funding_source"]["type"],
        set(FundingSource.SourceType.values),
        field="payload",
        path="payload.report.funding_source.type",
    )
    if report["donor"] is not None:
        _require_ref(
            report["donor"]["ref"],
            "DON",
            field="payload",
            path="payload.report.donor.ref",
        )
        _require_choice(
            report["donor"]["type"],
            set(Counterparty.CounterpartyType.values),
            field="payload",
            path="payload.report.donor.type",
        )

    balance_amount_fields = {"opening", "inflows", "outflows", "closing"}
    balance_count_fields = {"appointment_count", "planned_count", "completed_count"}
    balance_rows = _require_list(
        payload["balances"],
        field="payload",
        path="payload.balances",
    )
    for index, row in enumerate(balance_rows):
        base = f"payload.balances[{index}]"
        _require_choice(
            row["unit"],
            set(BalanceAccount.Unit.values),
            field="payload",
            path=f"{base}.unit",
        )
        for key in balance_amount_fields:
            _require_decimal(row[key], field="payload", path=f"{base}.{key}")
        for key in balance_count_fields:
            _require_int(
                row[key],
                field="payload",
                path=f"{base}.{key}",
                non_negative=True,
            )
    _require_stable_order(
        balance_rows,
        field="payload",
        path="payload.balances",
        key=lambda row: list(BalanceAccount.Unit.values).index(row["unit"]),
    )

    for index, row in enumerate(
        _require_list(payload["quotas"], field="payload", path="payload.quotas")
    ):
        base = f"payload.quotas[{index}]"
        _require_ref(row["ref"], "QTA", field="payload", path=f"{base}.ref")
        _require_ref(
            row["service"]["ref"],
            "SVC",
            field="payload",
            path=f"{base}.service.ref",
        )
        _require_choice(
            row["service"]["category"],
            set(Service.Category.values),
            field="payload",
            path=f"{base}.service.category",
        )
        _require_date_order(
            row["period"]["from"],
            row["period"]["to"],
            field="payload",
            path=f"{base}.period",
            optional=True,
        )
        for key in ("planned_sessions", "allocated_sessions", "charged_sessions"):
            _require_int(
                row[key],
                field="payload",
                path=f"{base}.{key}",
                non_negative=True,
            )
        _require_int(
            row["remaining_sessions"],
            field="payload",
            path=f"{base}.remaining_sessions",
        )
        staff_allocation_rows = _require_list(
            row["staff_allocations"],
            field="payload",
            path=f"{base}.staff_allocations",
        )
        for allocation_index, allocation in enumerate(staff_allocation_rows):
            allocation_base = f"{base}.staff_allocations[{allocation_index}]"
            _require_ref(
                allocation["ref"],
                "SAL",
                field="payload",
                path=f"{allocation_base}.ref",
            )
            _require_ref(
                allocation["staff_ref"],
                "SPC",
                field="payload",
                path=f"{allocation_base}.staff_ref",
            )
            for key in ("allocated_sessions", "charged_sessions"):
                _require_int(
                    allocation[key],
                    field="payload",
                    path=f"{allocation_base}.{key}",
                    non_negative=True,
                )
            _require_int(
                allocation["remaining_sessions"],
                field="payload",
                path=f"{allocation_base}.remaining_sessions",
            )
            if allocation["session_pay_amount"] is not None:
                _require_decimal(
                    allocation["session_pay_amount"],
                    field="payload",
                    path=f"{allocation_base}.session_pay_amount",
                )
        _require_stable_order(
            staff_allocation_rows,
            field="payload",
            path=f"{base}.staff_allocations",
            key=lambda allocation: allocation["ref"],
        )

    recipient_rows = _require_list(
        payload["recipient_allocations"],
        field="payload",
        path="payload.recipient_allocations",
    )
    for index, row in enumerate(recipient_rows):
        base = f"payload.recipient_allocations[{index}]"
        _require_ref(row["ref"], "RAL", field="payload", path=f"{base}.ref")
        _require_ref(
            row["recipient_ref"],
            "RCP",
            field="payload",
            path=f"{base}.recipient_ref",
        )
        _require_ref(
            row["service"]["ref"],
            "SVC",
            field="payload",
            path=f"{base}.service.ref",
        )
        _require_choice(
            row["service"]["category"],
            set(Service.Category.values),
            field="payload",
            path=f"{base}.service.category",
        )
        _require_date_order(
            row["period"]["from"],
            row["period"]["to"],
            field="payload",
            path=f"{base}.period",
            optional=True,
        )
        _require_int(
            row["allocated_sessions"],
            field="payload",
            path=f"{base}.allocated_sessions",
            non_negative=True,
        )
        _require_decimal(
            row["charged_sessions"],
            field="payload",
            path=f"{base}.charged_sessions",
        )
        _require_decimal(
            row["remaining_sessions"],
            field="payload",
            path=f"{base}.remaining_sessions",
        )
    _require_stable_order(
        recipient_rows,
        field="payload",
        path="payload.recipient_allocations",
        key=lambda row: row["ref"],
    )

    payroll = payload["payroll"]
    budget_rows = _require_list(
        payroll["budgets"],
        field="payload",
        path="payload.payroll.budgets",
    )
    for index, row in enumerate(budget_rows):
        base = f"payload.payroll.budgets[{index}]"
        _require_ref(row["budget_ref"], "BUD", field="payload", path=f"{base}.budget_ref")
        _require_date_order(
            row["starts_on"],
            row["ends_on"],
            field="payload",
            path=base,
            start_key="starts_on",
            end_key="ends_on",
        )
        for key in (
            "planned_amount",
            "consumed_amount",
            "draft_commitment_amount",
            "available_amount",
            "forecast_available_amount",
        ):
            _require_decimal(row[key], field="payload", path=f"{base}.{key}")
        _require_choice(
            row["enforcement_mode"],
            set(FundingPayrollBudget.EnforcementMode.values),
            field="payload",
            path=f"{base}.enforcement_mode",
        )
        _require_choice(
            row["lifecycle_status"],
            set(FundingPayrollBudget.LifecycleStatus.values),
            field="payload",
            path=f"{base}.lifecycle_status",
        )
    _require_stable_order(
        budget_rows,
        field="payload",
        path="payload.payroll.budgets",
        key=lambda row: row["budget_ref"],
    )
    fixed_rows = _require_list(
        payroll["fixed_positions"],
        field="payload",
        path="payload.payroll.fixed_positions",
    )
    for index, row in enumerate(fixed_rows):
        base = f"payload.payroll.fixed_positions[{index}]"
        _require_ref(row["fixed_ref"], "FIX", field="payload", path=f"{base}.fixed_ref")
        _require_ref(row["budget_ref"], "BUD", field="payload", path=f"{base}.budget_ref")
        _require_ref(row["staff_ref"], "SPC", field="payload", path=f"{base}.staff_ref")
        compensation_scope = _require_choice(
            row["compensation_scope"],
            set(GrantFixedCompensation.CompensationScope.values),
            field="payload",
            path=f"{base}.compensation_scope",
        )
        if (
            compensation_scope
            == GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
            and row["service"] is None
        ):
            _fail_json(
                "payload",
                f"{base}.service",
                "для service_delivery услуга обязательна.",
            )
        if (
            compensation_scope
            == GrantFixedCompensation.CompensationScope.PROJECT_ROLE
            and row["service"] is not None
        ):
            _fail_json(
                "payload",
                f"{base}.service",
                "для project_role услуга должна отсутствовать.",
            )
        if row["service"] is not None:
            _require_ref(
                row["service"]["ref"],
                "SVC",
                field="payload",
                path=f"{base}.service.ref",
            )
            _require_choice(
                row["service"]["category"],
                set(Service.Category.values),
                field="payload",
                path=f"{base}.service.category",
            )
        _require_date_order(
            row["period"]["from"],
            row["period"]["to"],
            field="payload",
            path=f"{base}.period",
        )
        _require_date(row["accrual_on"], field="payload", path=f"{base}.accrual_on")
        _require_decimal(row["amount"], field="payload", path=f"{base}.amount")
        _require_choice(
            row["lifecycle_status"],
            set(GrantFixedCompensation.LifecycleStatus.values),
            field="payload",
            path=f"{base}.lifecycle_status",
        )
    _require_stable_order(
        fixed_rows,
        field="payload",
        path="payload.payroll.fixed_positions",
        key=lambda row: row["fixed_ref"],
    )
    accrual_rows = _require_list(
        payroll["accrual_totals"],
        field="payload",
        path="payload.payroll.accrual_totals",
    )
    for index, row in enumerate(accrual_rows):
        base = f"payload.payroll.accrual_totals[{index}]"
        _require_ref(row["staff_ref"], "SPC", field="payload", path=f"{base}.staff_ref")
        _require_choice(
            row["accrual_kind"],
            set(PayrollAccrual.AccrualKind.values),
            field="payload",
            path=f"{base}.accrual_kind",
        )
        _require_choice(
            row["status"],
            set(PayrollAccrual.Status.values),
            field="payload",
            path=f"{base}.status",
        )
        _require_int(
            row["count"],
            field="payload",
            path=f"{base}.count",
            non_negative=True,
        )
        _require_decimal(row["amount"], field="payload", path=f"{base}.amount")
    _require_stable_order(
        accrual_rows,
        field="payload",
        path="payload.payroll.accrual_totals",
        key=lambda row: (
            row["staff_ref"],
            row["accrual_kind"],
            row["status"],
        ),
    )

    expense_rows = _require_list(
        payload["expenses"],
        field="payload",
        path="payload.expenses",
    )
    for index, row in enumerate(expense_rows):
        base = f"payload.expenses[{index}]"
        _require_choice(
            row["expense_type"],
            set(CenterExpenseCategory.ExpenseType.values),
            field="payload",
            path=f"{base}.expense_type",
        )
        _require_choice(
            row["status"],
            set(CenterExpense.Status.values),
            field="payload",
            path=f"{base}.status",
        )
        _require_int(
            row["count"],
            field="payload",
            path=f"{base}.count",
            non_negative=True,
        )
        _require_decimal(row["amount"], field="payload", path=f"{base}.amount")
    _require_stable_order(
        expense_rows,
        field="payload",
        path="payload.expenses",
        key=lambda row: (row["expense_type"], row["status"]),
    )

    integrity = payload["integrity"]
    if integrity["status"] != "passed":
        _fail_json("payload", "payload.integrity.status", "допустимо только passed.")
    warning_rows = _require_list(
        integrity["warnings"],
        field="payload",
        path="payload.integrity.warnings",
    )
    for index, row in enumerate(warning_rows):
        base = f"payload.integrity.warnings[{index}]"
        if not isinstance(row["code"], str) or SAFE_CODE_RE.fullmatch(row["code"]) is None:
            _fail_json("payload", f"{base}.code", "ожидается безопасный код.")
        _require_choice(
            row["severity"],
            {"warning", "info"},
            field="payload",
            path=f"{base}.severity",
        )
        _require_int(
            row["count"],
            field="payload",
            path=f"{base}.count",
            non_negative=True,
        )
        if "object_ref" in row and (
            not isinstance(row["object_ref"], str)
            or REF_RE.fullmatch(
                row["object_ref"]
            )
            is None
        ):
            _fail_json(
                "payload",
                f"{base}.object_ref",
                "ожидается локальный псевдоним.",
            )
        if "unit" in row:
            _require_choice(
                row["unit"],
                set(BalanceAccount.Unit.values),
                field="payload",
                path=f"{base}.unit",
            )
    _require_stable_order(
        warning_rows,
        field="payload",
        path="payload.integrity.warnings",
        key=lambda row: (
            row["code"],
            row.get("object_ref", ""),
            row.get("unit", ""),
        ),
    )


def _expect_evidence_keys(
    value: Any,
    expected: set[str],
    *,
    path: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail_json("evidence_manifest", path, "ожидается JSON-объект.")
    actual = set(value)
    if actual != expected:
        _fail_json("evidence_manifest", path, "нарушен exact allowlist.")
    return value


def _validate_evidence_manifest(evidence: dict[str, Any]) -> None:
    _expect_evidence_keys(
        evidence,
        {
            "schema_version",
            "canonicalizer_version",
            "payload_sha256",
            "aliases",
            "sources",
            "preflight",
            "source_sets",
        },
        path="evidence_manifest",
    )
    if evidence["schema_version"] != EVIDENCE_SCHEMA_VERSION:
        _fail_json(
            "evidence_manifest",
            "evidence_manifest.schema_version",
            "неподдерживаемая версия.",
        )
    if evidence["canonicalizer_version"] != CANONICALIZER_VERSION:
        _fail_json(
            "evidence_manifest",
            "evidence_manifest.canonicalizer_version",
            "неподдерживаемая версия.",
        )
    if not isinstance(evidence["payload_sha256"], str) or SHA256_RE.fullmatch(
        evidence["payload_sha256"]
    ) is None:
        _fail_json(
            "evidence_manifest",
            "evidence_manifest.payload_sha256",
            "ожидается SHA-256.",
        )

    alias_keys: set[tuple[str, int]] = set()
    alias_refs: set[str] = set()
    alias_rows = _require_list(
        evidence["aliases"],
        field="evidence_manifest",
        path="evidence_manifest.aliases",
    )
    for index, row in enumerate(alias_rows):
        base = f"evidence_manifest.aliases[{index}]"
        _expect_evidence_keys(
            row,
            {"kind", "ref", "source_pk", "active"},
            path=base,
        )
        kind = _require_choice(
            row["kind"],
            set(ALIAS_PREFIXES),
            field="evidence_manifest",
            path=f"{base}.kind",
        )
        _require_ref(
            row["ref"],
            ALIAS_PREFIXES[kind],
            field="evidence_manifest",
            path=f"{base}.ref",
        )
        source_pk = _require_int(
            row["source_pk"],
            field="evidence_manifest",
            path=f"{base}.source_pk",
            positive=True,
        )
        if type(row["active"]) is not bool:
            _fail_json("evidence_manifest", f"{base}.active", "ожидается boolean.")
        if (kind, source_pk) in alias_keys or row["ref"] in alias_refs:
            _fail_json("evidence_manifest", base, "псевдоним должен быть уникальным.")
        alias_keys.add((kind, source_pk))
        alias_refs.add(row["ref"])
    _require_stable_order(
        alias_rows,
        field="evidence_manifest",
        path="evidence_manifest.aliases",
        key=lambda row: (row["kind"], row["ref"]),
    )

    source_keys: set[tuple[str, int]] = set()
    source_rows = _require_list(
        evidence["sources"],
        field="evidence_manifest",
        path="evidence_manifest.sources",
    )
    for index, row in enumerate(source_rows):
        base = f"evidence_manifest.sources[{index}]"
        _expect_evidence_keys(
            row,
            {"kind", "source_pk", "revision_pk", "projection_sha256"},
            path=base,
        )
        kind = _require_choice(
            row["kind"],
            EVIDENCE_SOURCE_KINDS,
            field="evidence_manifest",
            path=f"{base}.kind",
        )
        source_pk = _require_int(
            row["source_pk"],
            field="evidence_manifest",
            path=f"{base}.source_pk",
            positive=True,
        )
        if row["revision_pk"] is not None:
            _require_int(
                row["revision_pk"],
                field="evidence_manifest",
                path=f"{base}.revision_pk",
                positive=True,
            )
        if not isinstance(row["projection_sha256"], str) or SHA256_RE.fullmatch(
            row["projection_sha256"]
        ) is None:
            _fail_json(
                "evidence_manifest",
                f"{base}.projection_sha256",
                "ожидается SHA-256.",
            )
        if (kind, source_pk) in source_keys:
            _fail_json("evidence_manifest", base, "источник не должен повторяться.")
        source_keys.add((kind, source_pk))
    _require_stable_order(
        source_rows,
        field="evidence_manifest",
        path="evidence_manifest.sources",
        key=lambda row: (row["kind"], row["source_pk"]),
    )

    preflight_rows = _require_list(
        evidence["preflight"],
        field="evidence_manifest",
        path="evidence_manifest.preflight",
    )
    for index, row in enumerate(preflight_rows):
        base = f"evidence_manifest.preflight[{index}]"
        _expect_evidence_keys(
            row,
            {"code", "severity", "result", "count"},
            path=base,
        )
        if not isinstance(row["code"], str) or SAFE_CODE_RE.fullmatch(row["code"]) is None:
            _fail_json("evidence_manifest", f"{base}.code", "ожидается безопасный код.")
        _require_choice(
            row["severity"],
            {"error", "warning", "info"},
            field="evidence_manifest",
            path=f"{base}.severity",
        )
        _require_choice(
            row["result"],
            {"passed", "warning", "blocked"},
            field="evidence_manifest",
            path=f"{base}.result",
        )
        _require_int(
            row["count"],
            field="evidence_manifest",
            path=f"{base}.count",
            non_negative=True,
        )
    _require_stable_order(
        preflight_rows,
        field="evidence_manifest",
        path="evidence_manifest.preflight",
        key=lambda row: (row["code"], row["severity"]),
    )

    source_sets = _expect_evidence_keys(
        evidence["source_sets"],
        {
            "payroll_period_accrual_ids",
            "payroll_line_ids",
            "payroll_sheet_ids",
            "expense_category_ids",
        },
        path="evidence_manifest.source_sets",
    )
    for key, values in source_sets.items():
        ids = _require_list(
            values,
            field="evidence_manifest",
            path=f"evidence_manifest.source_sets.{key}",
        )
        for index, source_pk in enumerate(ids):
            _require_int(
                source_pk,
                field="evidence_manifest",
                path=f"evidence_manifest.source_sets.{key}[{index}]",
                positive=True,
            )
        if ids != sorted(set(ids)):
            _fail_json(
                "evidence_manifest",
                f"evidence_manifest.source_sets.{key}",
                "ID должны быть уникальны и отсортированы.",
            )


def _scoped_plan_integrity_codes(funding_source: FundingSource) -> list[str]:
    quota_ids = set(
        FundingServiceQuota.objects.filter(funding_source=funding_source).values_list(
            "pk", flat=True
        )
    )
    allocation_ids = set(
        FundingStaffAllocation.objects.filter(funding_source=funding_source).values_list(
            "pk", flat=True
        )
    )
    codes = []
    for finding in grant_plans_svc.grant_plan_integrity_findings():
        if (
            finding.object_kind == "service_quota"
            and finding.object_id in quota_ids
        ) or (
            finding.object_kind == "staff_allocation"
            and finding.object_id in allocation_ids
        ):
            codes.append(finding.code)
    return codes


def _scoped_compensation_integrity_codes(
    funding_source: FundingSource,
) -> list[str]:
    budget_ids = set(
        FundingPayrollBudget.objects.filter(
            funding_source=funding_source
        ).values_list("pk", flat=True)
    )
    position_ids = set(
        GrantFixedCompensation.objects.filter(
            payroll_budget__funding_source=funding_source
        ).values_list("pk", flat=True)
    )
    codes = []
    for finding in grant_compensation_svc.grant_compensation_integrity_findings():
        if (
            finding.object_kind == "payroll_budget"
            and finding.object_id in budget_ids
        ) or (
            finding.object_kind == "fixed_compensation"
            and finding.object_id in position_ids
        ):
            codes.append(finding.code)
    return codes


def _unbalanced_expense_ids(
    funding_source: FundingSource,
    *,
    date_from: date,
    date_to: date,
) -> list[int]:
    expense_ids = (
        ExpenseFundingSplit.objects.filter(
            funding_source=funding_source,
            expense__expense_date__range=(date_from, date_to),
            expense__status__in=[
                CenterExpense.Status.APPROVED,
                CenterExpense.Status.PAID,
            ],
        )
        .order_by()
        .values_list("expense_id", flat=True)
        .distinct()
    )
    expenses = (
        CenterExpense.objects.filter(
            pk__in=expense_ids,
            expense_date__range=(date_from, date_to),
            status__in=[CenterExpense.Status.APPROVED, CenterExpense.Status.PAID],
        )
        .annotate(split_total=Sum("funding_splits__amount", distinct=False))
        .distinct()
    )
    return [
        expense.pk
        for expense in expenses
        if (expense.split_total or Decimal("0")) != expense.total_amount
    ]


def _payroll_line_mismatch_count(
    funding_source: FundingSource,
    *,
    date_from: date,
    date_to: date,
) -> int:
    return (
        PayrollSheetLine.objects.filter(
            funding_source=funding_source,
            work_date__range=(date_from, date_to),
        )
        .filter(
            ~Q(amount=F("payroll_accrual__amount"))
            | ~Q(accrual_kind_snapshot=F("payroll_accrual__accrual_kind"))
            | Q(
                payroll_budget_revision__isnull=True,
                payroll_accrual__payroll_budget_revision__isnull=False,
            )
            | Q(
                payroll_budget_revision__isnull=False,
                payroll_accrual__payroll_budget_revision__isnull=True,
            )
            | (
                Q(payroll_budget_revision__isnull=False)
                & Q(payroll_accrual__payroll_budget_revision__isnull=False)
                & ~Q(
                    payroll_budget_revision_id=F(
                        "payroll_accrual__payroll_budget_revision_id"
                    )
                )
            )
        )
        .count()
    )


def _scoped_financial_integrity(
    funding_source: FundingSource,
    *,
    date_from: date,
    date_to: date,
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    allocations = list(
        GrantRecipientAllocation.objects.filter(funding_source=funding_source)
        .filter(Q(valid_from__isnull=True) | Q(valid_from__lte=date_to))
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=date_from))
        .only("child_id", "service_id", "valid_from", "valid_until")
        .order_by("pk")
    )
    allocation_periods: dict[
        tuple[int, int],
        list[tuple[date | None, date | None]],
    ] = {}
    for allocation in allocations:
        allocation_periods.setdefault(
            (allocation.child_id, allocation.service_id),
            [],
        ).append((allocation.valid_from, allocation.valid_until))

    def has_allocation(child_id: int, service_id: int, work_date: date) -> bool:
        return any(
            (valid_from is None or valid_from <= work_date)
            and (valid_until is None or valid_until >= work_date)
            for valid_from, valid_until in allocation_periods.get(
                (child_id, service_id),
                [],
            )
        )

    candidate_scope = (
        Q(billing_account__funding_source=funding_source)
        | Q(participants__billing_account__funding_source=funding_source)
        | Q(ledger_entries__account__funding_source=funding_source)
    )
    allocation_child_ids = {
        child_id for child_id, _service_id in allocation_periods
    }
    allocation_service_ids = {
        service_id for _child_id, service_id in allocation_periods
    }
    if allocation_child_ids and allocation_service_ids:
        candidate_scope |= (
            Q(
                child_id__in=allocation_child_ids,
                service_id__in=allocation_service_ids,
            )
            | Q(
                participants__child_id__in=allocation_child_ids,
                service_id__in=allocation_service_ids,
            )
        )

    appointments = list(
        Appointment.objects.filter(starts_at__date__range=(date_from, date_to))
        .filter(candidate_scope)
        .select_related("billing_account", "billing_account__funding_source")
        .distinct()
        .order_by("pk")
    )
    relevant_appointment_ids: set[int] = set()
    relevant_legacy_ids: set[int] = set()
    relevant_participant_ids: set[int] = set()
    for appointment in appointments:
        participants = list(
            appointment.participants.select_related(
                "billing_account",
                "billing_account__funding_source",
            ).order_by("pk")
        )
        appointment_date = timezone.localtime(appointment.starts_at).date()
        legacy_matches = (
            not participants
            and (
                (
                    appointment.billing_account_id
                    and appointment.billing_account.funding_source_id
                    == funding_source.pk
                )
                or has_allocation(
                    appointment.child_id,
                    appointment.service_id,
                    appointment_date,
                )
            )
        )
        if legacy_matches:
            relevant_legacy_ids.add(appointment.pk)
            relevant_appointment_ids.add(appointment.pk)
        for participant in participants:
            participant_date = timezone.localtime(
                participant.starts_at_snapshot
            ).date()
            participant_matches = (
                (
                    participant.billing_account_id
                    and participant.billing_account.funding_source_id
                    == funding_source.pk
                )
                or has_allocation(
                    participant.child_id,
                    appointment.service_id,
                    participant_date,
                )
            )
            if participant_matches:
                relevant_participant_ids.add(participant.pk)
                relevant_appointment_ids.add(appointment.pk)
        if appointment.ledger_entries.filter(
            account__funding_source=funding_source
        ).exists():
            relevant_appointment_ids.add(appointment.pk)

    audited_issues = financial_integrity_svc.audit_appointments(appointments)

    def issue_matches_source(
        issue: financial_integrity_svc.FinancialIntegrityIssue,
    ) -> bool:
        if issue.funding_source is not None:
            return issue.funding_source.pk == funding_source.pk
        if issue.account is not None:
            return issue.account.funding_source_id == funding_source.pk
        if issue.ledger_entry is not None:
            return issue.ledger_entry.account.funding_source_id == funding_source.pk
        if (
            issue.code
            == financial_integrity_svc.FinancialIssueCode.MISSING_DEBIT_LEDGER
            and issue.appointment is not None
        ):
            charged_participants = issue.appointment.participants.filter(
                billing_decision=Appointment.BillingDecision.CHARGE,
                billing_account__funding_source=funding_source,
            )
            missing_current_source_debit = charged_participants.exclude(
                pk__in=LedgerEntry.objects.filter(
                    appointment=issue.appointment,
                    account__funding_source=funding_source,
                    entry_type=LedgerEntry.EntryType.DEBIT,
                ).values("appointment_participant_id")
            ).exists()
            if charged_participants.exists():
                return missing_current_source_debit
            return (
                issue.appointment.billing_account_id is not None
                and issue.appointment.billing_account.funding_source_id
                == funding_source.pk
            )
        if issue.participant is not None:
            return issue.participant.pk in relevant_participant_ids
        if (
            issue.code
            == financial_integrity_svc.FinancialIssueCode.APPOINTMENT_CHARGE_WITHOUT_ACCOUNT
            and issue.appointment is not None
        ):
            return issue.appointment.pk in relevant_legacy_ids
        return (
            issue.appointment is not None
            and issue.appointment.pk in relevant_appointment_ids
        )

    issues = [issue for issue in audited_issues if issue_matches_source(issue)]
    blocking_codes: list[str] = []
    warning_rows: list[dict[str, Any]] = []
    preflight_rows: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], int] = {}
    for issue in issues:
        key = (issue.code, issue.severity)
        grouped[key] = grouped.get(key, 0) + 1
    for (code, severity), count in sorted(grouped.items()):
        is_blocking = (
            severity == financial_integrity_svc.FinancialIssueSeverity.ERROR
            or code
            == financial_integrity_svc.FinancialIssueCode.STALE_DEBIT_LEDGER_WITHOUT_CHARGE_FACT
        )
        preflight_rows.append(
            {
                "code": code,
                "severity": severity,
                "result": "blocked" if is_blocking else "warning",
                "count": count,
            }
        )
        if is_blocking:
            blocking_codes.append(code)
        else:
            warning_rows.append(
                {
                    "code": code,
                    "severity": severity,
                    "count": count,
                }
            )
    preflight_rows.append(
        {
            "code": "financial_integrity",
            "severity": "error",
            "result": "blocked" if blocking_codes else "passed",
            "count": sum(
                row["count"]
                for row in preflight_rows
                if row["result"] == "blocked"
            ),
        }
    )
    return blocking_codes, warning_rows, preflight_rows


def _blocking_preflight_codes(
    *,
    report: reports_svc.GrantReport,
    funding_source: FundingSource,
    date_from: date,
    date_to: date,
    budgets: list[FundingPayrollBudget],
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    codes: list[str] = []
    financial_codes, warning_rows, preflight_rows = _scoped_financial_integrity(
        funding_source,
        date_from=date_from,
        date_to=date_to,
    )
    codes.extend(financial_codes)
    plan_codes = _scoped_plan_integrity_codes(funding_source)
    compensation_codes = _scoped_compensation_integrity_codes(funding_source)
    usage_by_budget = grant_compensation_svc.payroll_budget_usages(budgets)
    hard_budget_overage = any(
        budget.enforcement_mode == FundingPayrollBudget.EnforcementMode.HARD
        and usage_by_budget[budget.pk].available < 0
        for budget in budgets
    )
    payroll_line_mismatch_count = _payroll_line_mismatch_count(
        funding_source,
        date_from=date_from,
        date_to=date_to,
    )
    unbalanced_expense_count = len(
        _unbalanced_expense_ids(
            funding_source,
            date_from=date_from,
            date_to=date_to,
        )
    )
    allowed_units = {"money", "sessions"}
    report_units = [row.unit for row in report.unit_totals]
    balance_unit_integrity_failed = (
        any(unit not in allowed_units for unit in report_units)
        or len(report_units) != len(set(report_units))
    )

    if report.quota_missing_debit_count:
        codes.append("grant_charge_missing_debit")
    codes.extend(plan_codes)
    codes.extend(compensation_codes)
    if hard_budget_overage:
        codes.append("grant_hard_payroll_budget_exceeded")
    if payroll_line_mismatch_count:
        codes.append("grant_payroll_line_accrual_mismatch")
    if unbalanced_expense_count:
        codes.append("grant_expense_funding_unbalanced")
    if balance_unit_integrity_failed:
        codes.append("grant_balance_unit_integrity")

    named_checks = {
        "grant_charge_missing_debit": report.quota_missing_debit_count,
        "grant_plan_integrity": len(plan_codes),
        "grant_compensation_integrity": len(compensation_codes),
        "grant_hard_payroll_budget_exceeded": int(hard_budget_overage),
        "grant_payroll_line_accrual_mismatch": payroll_line_mismatch_count,
        "grant_expense_funding_unbalanced": unbalanced_expense_count,
        "grant_balance_unit_integrity": int(balance_unit_integrity_failed),
    }
    for code, count in named_checks.items():
        preflight_rows.append(
            {
                "code": code,
                "severity": "error",
                "result": "blocked" if count else "passed",
                "count": count,
            }
        )
    for code in sorted(set(codes)):
        if not any(row["code"] == code for row in preflight_rows):
            preflight_rows.append(
                {
                    "code": code,
                    "severity": "error",
                    "result": "blocked",
                    "count": 1,
                }
            )
    return sorted(set(codes)), warning_rows, sorted(
        preflight_rows,
        key=lambda row: (row["code"], row["severity"]),
    )


def _payroll_payload(
    funding_source: FundingSource,
    *,
    date_from: date,
    date_to: date,
    budgets: list[FundingPayrollBudget],
    fixed_positions: list[GrantFixedCompensation],
    aliases: dict[str, dict[int, str]],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    usage_by_budget = grant_compensation_svc.payroll_budget_usages(budgets)
    budget_rows = []
    warning_rows: list[dict[str, Any]] = []
    for budget in sorted(budgets, key=lambda item: item.pk):
        usage = usage_by_budget[budget.pk]
        budget_rows.append(
            {
                "budget_ref": aliases["payroll_budget"][budget.pk],
                "starts_on": budget.starts_on,
                "ends_on": budget.ends_on,
                "planned_amount": budget.planned_amount,
                "enforcement_mode": budget.enforcement_mode,
                "lifecycle_status": budget.lifecycle_status,
                "consumed_amount": usage.consumed,
                "draft_commitment_amount": usage.draft_commitment,
                "available_amount": usage.available,
                "forecast_available_amount": usage.forecast_available,
            }
        )
        if usage.forecast_available < 0:
            warning_rows.append(
                {
                    "code": "grant_payroll_budget_forecast_overage",
                    "object_ref": aliases["payroll_budget"][budget.pk],
                    "severity": "warning",
                    "count": 1,
                }
            )

    accrual_rows = list(
        PayrollAccrual.objects.filter(
            funding_source=funding_source,
            work_date__range=(date_from, date_to),
        )
        .values("staff_member_id", "accrual_kind", "status")
        .annotate(count=Count("pk"), amount=Sum("amount"))
        .order_by("staff_member_id", "accrual_kind", "status")
    )
    payload = {
        "budgets": budget_rows,
        "fixed_positions": [
            {
                "fixed_ref": aliases["fixed_position"][position.pk],
                "budget_ref": aliases["payroll_budget"][position.payroll_budget_id],
                "staff_ref": aliases["staff"][position.staff_member_id],
                "compensation_scope": position.compensation_scope,
                "service": (
                    {
                        "ref": aliases["service"][position.service_id],
                        "category": position.service.category,
                    }
                    if position.service_id
                    else None
                ),
                "period": {
                    "from": position.period_from,
                    "to": position.period_to,
                },
                "accrual_on": position.accrual_on,
                "amount": position.amount,
                "lifecycle_status": position.lifecycle_status,
            }
            for position in sorted(fixed_positions, key=lambda item: item.pk)
        ],
        "accrual_totals": [
            {
                "staff_ref": aliases["staff"][row["staff_member_id"]],
                "accrual_kind": row["accrual_kind"],
                "status": row["status"],
                "count": row["count"],
                "amount": row["amount"] or Decimal("0"),
            }
            for row in accrual_rows
        ],
    }
    budget_ids = [budget.pk for budget in sorted(budgets, key=lambda item: item.pk)]
    period_accrual_ids = list(
        PayrollAccrual.objects.filter(
            funding_source=funding_source,
            work_date__range=(date_from, date_to),
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    line_ids = list(
        PayrollSheetLine.objects.filter(
            Q(
                funding_source=funding_source,
                work_date__range=(date_from, date_to),
            )
            | Q(payroll_budget_revision__payroll_budget_id__in=budget_ids)
        )
        .order_by("pk")
        .values_list("pk", flat=True)
        .distinct()
    )
    sheet_ids = list(
        PayrollSheetLine.objects.filter(pk__in=line_ids)
        .order_by("payroll_sheet_id")
        .values_list("payroll_sheet_id", flat=True)
        .distinct()
    )
    evidence = {
        "budget_ids": budget_ids,
        "fixed_position_ids": [
            position.pk for position in sorted(fixed_positions, key=lambda item: item.pk)
        ],
        "period_accrual_ids": period_accrual_ids,
        "payroll_line_ids": line_ids,
        "payroll_sheet_ids": sheet_ids,
    }
    return payload, evidence, warning_rows


def _expense_payload(
    funding_source: FundingSource,
    *,
    date_from: date,
    date_to: date,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    splits = list(
        ExpenseFundingSplit.objects.filter(
            funding_source=funding_source,
            expense__expense_date__range=(date_from, date_to),
        )
        .select_related("expense", "expense__category")
        .order_by(
            "expense__category__expense_type",
            "expense__status",
            "expense_id",
            "pk",
        )
    )
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for split in splits:
        key = (
            split.expense.category.expense_type,
            split.expense.status,
        )
        row = grouped.setdefault(
            key,
            {
                "expense_type": key[0],
                "status": key[1],
                "count": 0,
                "amount": Decimal("0"),
            },
        )
        row["count"] += 1
        row["amount"] += split.amount
    scoped_expense_ids = sorted({split.expense_id for split in splits})
    return [grouped[key] for key in sorted(grouped)], {
        "expense_ids": scoped_expense_ids,
        "expense_split_ids": list(
            ExpenseFundingSplit.objects.filter(expense_id__in=scoped_expense_ids)
            .order_by("pk")
            .values_list("pk", flat=True)
        ),
        "expense_category_ids": sorted(
            {split.expense.category_id for split in splits}
        ),
    }


def build_internal_grant_reconciliation(
    funding_source: FundingSource,
    *,
    date_from: date,
    date_to: date,
    counterparty: Counterparty | None,
    previous_snapshot: DonorReportSnapshot | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if date_to < date_from:
        raise ValidationError({"date_to": "Дата окончания не может быть раньше даты начала."})

    live_report = reports_svc.grant_report(funding_source, date_from, date_to)
    budgets = list(
        FundingPayrollBudget.objects.filter(
            funding_source=funding_source,
            starts_on__lte=date_to,
            ends_on__gte=date_from,
        )
        .select_related("current_revision")
        .order_by("pk")
    )
    fixed_positions = list(
        GrantFixedCompensation.objects.filter(
            payroll_budget__funding_source=funding_source,
            period_from__lte=date_to,
            period_to__gte=date_from,
        )
        .select_related(
            "current_revision",
            "payroll_budget",
            "service",
            "staff_member",
        )
        .order_by("pk")
    )
    accruals = list(
        PayrollAccrual.objects.filter(
            funding_source=funding_source,
            work_date__range=(date_from, date_to),
        )
        .select_related("payroll_budget_revision")
        .order_by("pk")
    )
    blocking_codes, financial_warnings, preflight_rows = _blocking_preflight_codes(
        report=live_report,
        funding_source=funding_source,
        date_from=date_from,
        date_to=date_to,
        budgets=budgets,
    )
    if blocking_codes:
        raise ValidationError(
            {
                "report": [
                    "Закрытие заблокировано проверками целостности: "
                    + ", ".join(blocking_codes)
                ]
            }
        )

    current_ids: dict[str, set[int]] = {
        "funding_source": {funding_source.pk},
        "counterparty": {counterparty.pk} if counterparty else set(),
        "service": {
            quota_row.service.pk for quota_row in live_report.quota_rows
        }
        | {row.service.pk for row in live_report.recipient_allocation_rows}
        | {position.service_id for position in fixed_positions if position.service_id},
        "service_quota": {
            quota_row.quota.pk
            for quota_row in live_report.quota_rows
            if quota_row.quota
        },
        "direct_service_quota": {
            quota_row.service.pk
            for quota_row in live_report.quota_rows
            if quota_row.quota is None
        },
        "staff_allocation": {
            staff_row.allocation.pk
            for quota_row in live_report.quota_rows
            for staff_row in quota_row.staff_rows
        },
        "staff": {
            staff_row.staff_member.pk
            for quota_row in live_report.quota_rows
            for staff_row in quota_row.staff_rows
        }
        | {position.staff_member_id for position in fixed_positions}
        | {accrual.staff_member_id for accrual in accruals},
        "recipient": {
            row.child.pk for row in live_report.recipient_allocation_rows
        },
        "recipient_allocation": {
            row.allocation.pk for row in live_report.recipient_allocation_rows
        },
        "payroll_budget": {budget.pk for budget in budgets},
        "fixed_position": {position.pk for position in fixed_positions},
    }
    aliases, alias_rows = _alias_registry(
        previous_snapshot.evidence_manifest if previous_snapshot else None,
        current_ids,
    )

    quota_payload = []
    for quota_row in sorted(
        live_report.quota_rows,
        key=lambda item: (
            item.quota.pk if item.quota else 9_223_372_036_854_775_807,
            item.service.pk,
        ),
    ):
        quota_kind = (
            "service_quota" if quota_row.quota else "direct_service_quota"
        )
        quota_source_pk = (
            quota_row.quota.pk if quota_row.quota else quota_row.service.pk
        )
        staff_rows = sorted(quota_row.staff_rows, key=lambda item: item.allocation.pk)
        quota_payload.append(
            {
                "ref": aliases[quota_kind][quota_source_pk],
                "service": {
                    "ref": aliases["service"][quota_row.service.pk],
                    "category": quota_row.service.category,
                },
                "period": {
                    "from": quota_row.quota.starts_on if quota_row.quota else None,
                    "to": quota_row.quota.ends_on if quota_row.quota else None,
                },
                "planned_sessions": quota_row.planned_sessions,
                "allocated_sessions": quota_row.allocated_sessions,
                "charged_sessions": quota_row.charged_sessions,
                "remaining_sessions": quota_row.remaining_sessions,
                "staff_allocations": [
                    {
                        "ref": aliases["staff_allocation"][
                            staff_row.allocation.pk
                        ],
                        "staff_ref": aliases["staff"][
                            staff_row.staff_member.pk
                        ],
                        "allocated_sessions": staff_row.allocated_sessions,
                        "charged_sessions": staff_row.charged_sessions,
                        "remaining_sessions": staff_row.remaining_sessions,
                        "session_pay_amount": staff_row.session_pay_amount,
                    }
                    for staff_row in staff_rows
                ],
            }
        )

    recipient_payload = [
        {
            "ref": aliases["recipient_allocation"][row.allocation.pk],
            "recipient_ref": aliases["recipient"][row.child.pk],
            "service": {
                "ref": aliases["service"][row.service.pk],
                "category": row.service.category,
            },
            "period": {
                "from": row.allocation.valid_from,
                "to": row.allocation.valid_until,
            },
            "allocated_sessions": row.allocated_sessions,
            "charged_sessions": row.charged_sessions,
            "remaining_sessions": row.remaining_sessions,
        }
        for row in sorted(
            live_report.recipient_allocation_rows,
            key=lambda item: item.allocation.pk,
        )
    ]

    payroll_payload, payroll_evidence, payroll_warnings = _payroll_payload(
        funding_source,
        date_from=date_from,
        date_to=date_to,
        budgets=budgets,
        fixed_positions=fixed_positions,
        aliases=aliases,
    )
    expenses_payload, expense_evidence = _expense_payload(
        funding_source,
        date_from=date_from,
        date_to=date_to,
    )
    warnings = list(financial_warnings) + list(payroll_warnings)
    for unit_total in live_report.unit_totals:
        if unit_total.closing_balance < 0:
            warnings.append(
                {
                    "code": "grant_negative_period_closing_balance",
                    "severity": "warning",
                    "count": 1,
                    "unit": unit_total.unit,
                }
            )

    payload = _json_value(
        {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "report": {
                "report_kind": SNAPSHOT_SCHEMA_VERSION,
                "period": {
                    "from": date_from,
                    "to": date_to,
                },
                "funding_source": {
                    "ref": aliases["funding_source"][funding_source.pk],
                    "type": funding_source.source_type,
                },
                "donor": (
                    {
                        "ref": aliases["counterparty"][counterparty.pk],
                        "type": counterparty.counterparty_type,
                    }
                    if counterparty
                    else None
                ),
            },
            "balances": [
                {
                    "unit": total.unit,
                    "opening": total.opening_balance,
                    "inflows": total.inflows,
                    "outflows": total.outflows,
                    "closing": total.closing_balance,
                    "appointment_count": total.appointments_count,
                    "planned_count": total.planned_count,
                    "completed_count": total.completed_count,
                }
                for total in live_report.unit_totals
            ],
            "quotas": quota_payload,
            "recipient_allocations": recipient_payload,
            "payroll": payroll_payload,
            "expenses": expenses_payload,
            "integrity": {
                "status": "passed",
                "warnings": sorted(
                    warnings,
                    key=lambda item: (
                        item["code"],
                        item.get("object_ref", ""),
                        item.get("unit", ""),
                    ),
                ),
            },
        }
    )
    forbidden_paths = _payload_forbidden_key_paths(payload)
    if forbidden_paths:
        raise ValidationError(
            {"payload": "Payload содержит запрещенные поля: " + ", ".join(forbidden_paths)}
        )
    _validate_payload_values(payload)

    sources: list[dict[str, Any]] = [
        _source_record(
            "funding_source",
            funding_source.pk,
            projection={
                "source_type": funding_source.source_type,
                "starts_on": funding_source.starts_on,
                "ends_on": funding_source.ends_on,
                "archived": funding_source.archived_at is not None,
            },
        )
    ]
    if counterparty:
        sources.append(
            _source_record(
                "counterparty",
                counterparty.pk,
                projection={
                    "counterparty_type": counterparty.counterparty_type,
                    "archived": counterparty.archived_at is not None,
                },
            )
        )
    sources.extend(
        _source_record(
            "service",
            service.pk,
            projection={"category": service.category},
        )
        for service in Service.all_objects.filter(
            pk__in=current_ids["service"]
        ).order_by("pk")
    )
    for row in live_report.rows:
        account = row.account
        sources.append(
            _source_record(
                "balance_account",
                account.pk,
                projection={
                    "funding_source_id": account.funding_source_id,
                    "unit": account.unit,
                    "service_scope": account.service_scope,
                    "service_id": account.service_id,
                    "initial_amount": account.initial_amount,
                    "valid_from": account.valid_from,
                    "valid_until": account.valid_until,
                    "status": account.status,
                },
            )
        )
    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(date_from, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(date_to, time.max), tz)
    ledgers = list(
        LedgerEntry.objects.filter(account__funding_source=funding_source)
        .annotate(
            donor_effective_at=reports_svc._ledger_effective_at_expression()
        )
        .filter(donor_effective_at__lte=end_dt)
        .order_by("pk")
    )
    sources.extend(
        _source_record(
            "ledger_entry",
            entry.pk,
            projection={
                "account_id": entry.account_id,
                "appointment_id": entry.appointment_id,
                "appointment_participant_id": entry.appointment_participant_id,
                "entry_type": entry.entry_type,
                "amount": entry.amount,
                "effective_at": entry.donor_effective_at,
            },
        )
        for entry in ledgers
    )
    account_ids = {row.account.pk for row in live_report.rows}
    report_appointment_ids = set(
        Appointment.objects.filter(
            billing_account_id__in=account_ids,
            starts_at__gte=start_dt,
            starts_at__lte=end_dt,
        ).values_list("pk", flat=True)
    )
    report_appointment_ids.update(
        AppointmentParticipant.objects.filter(
            billing_account_id__in=account_ids,
            starts_at_snapshot__gte=start_dt,
            starts_at_snapshot__lte=end_dt,
        ).values_list("appointment_id", flat=True)
    )
    quota_service_ids = {
        row.service.pk for row in live_report.quota_rows
    }
    quota_assignments = list(
        AppointmentStaffAssignment.objects.filter(
            appointment__service_id__in=quota_service_ids,
            starts_at_snapshot__gte=start_dt,
            starts_at_snapshot__lte=end_dt,
        ).order_by("pk")
    )
    report_appointment_ids.update(
        assignment.appointment_id for assignment in quota_assignments
    )
    report_appointments = list(
        Appointment.objects.filter(pk__in=report_appointment_ids).order_by("pk")
    )
    sources.extend(
        _source_record(
            "appointment",
            appointment.pk,
            projection={
                "child_id": appointment.child_id,
                "staff_member_id": appointment.staff_member_id,
                "service_id": appointment.service_id,
                "starts_at": appointment.starts_at,
                "ends_at": appointment.ends_at,
                "session_type": appointment.session_type,
                "status": appointment.status,
                "billing_decision": appointment.billing_decision,
                "billing_account_id": appointment.billing_account_id,
            },
        )
        for appointment in report_appointments
    )
    report_participants = list(
        AppointmentParticipant.objects.filter(
            appointment_id__in=report_appointment_ids
        ).order_by("pk")
    )
    sources.extend(
        _source_record(
            "appointment_participant",
            participant.pk,
            projection={
                "appointment_id": participant.appointment_id,
                "child_id": participant.child_id,
                "billing_decision": participant.billing_decision,
                "billing_account_id": participant.billing_account_id,
                "starts_at_snapshot": participant.starts_at_snapshot,
                "ends_at_snapshot": participant.ends_at_snapshot,
                "appointment_status": participant.appointment_status,
            },
        )
        for participant in report_participants
    )
    sources.extend(
        _source_record(
            "appointment_staff_assignment",
            assignment.pk,
            projection={
                "appointment_id": assignment.appointment_id,
                "staff_member_id": assignment.staff_member_id,
                "role": assignment.role,
                "starts_at_snapshot": assignment.starts_at_snapshot,
                "ends_at_snapshot": assignment.ends_at_snapshot,
                "appointment_status": assignment.appointment_status,
            },
        )
        for assignment in quota_assignments
    )
    for quota_row in live_report.quota_rows:
        if quota_row.quota:
            quota = quota_row.quota
            sources.append(
                _source_record(
                    "service_quota",
                    quota.pk,
                    revision_pk=quota.current_revision_id,
                    projection={
                        "service_id": quota.service_id,
                        "planned_sessions": quota.planned_sessions,
                        "starts_on": quota.starts_on,
                        "ends_on": quota.ends_on,
                        "lifecycle_status": quota.lifecycle_status,
                    },
                )
            )
        for staff_row in quota_row.staff_rows:
            allocation = staff_row.allocation
            sources.append(
                _source_record(
                    "staff_allocation",
                    allocation.pk,
                    revision_pk=allocation.current_revision_id,
                    projection={
                        "service_quota_id": allocation.service_quota_id,
                        "funding_source_id": allocation.funding_source_id,
                        "service_id": allocation.service_id,
                        "staff_member_id": allocation.staff_member_id,
                        "allocated_sessions": allocation.allocated_sessions,
                        "session_pay_amount": allocation.session_pay_amount,
                        "starts_on": allocation.starts_on,
                        "ends_on": allocation.ends_on,
                        "lifecycle_status": allocation.lifecycle_status,
                    },
                )
            )
    for row in live_report.recipient_allocation_rows:
        allocation = row.allocation
        sources.append(
            _source_record(
                "recipient_allocation",
                allocation.pk,
                projection={
                    "funding_source_id": allocation.funding_source_id,
                    "child_id": allocation.child_id,
                    "service_id": allocation.service_id,
                    "allocated_sessions": allocation.allocated_sessions,
                    "balance_account_id": allocation.balance_account_id,
                    "valid_from": allocation.valid_from,
                    "valid_until": allocation.valid_until,
                },
            )
        )
    for budget in budgets:
        sources.append(
            _source_record(
                "payroll_budget",
                budget.pk,
                revision_pk=budget.current_revision_id,
                projection={
                    "starts_on": budget.starts_on,
                    "ends_on": budget.ends_on,
                    "planned_amount": budget.planned_amount,
                    "enforcement_mode": budget.enforcement_mode,
                    "lifecycle_status": budget.lifecycle_status,
                },
            )
        )
    for position in fixed_positions:
        sources.append(
            _source_record(
                "fixed_position",
                position.pk,
                revision_pk=position.current_revision_id,
                projection={
                    "payroll_budget_id": position.payroll_budget_id,
                    "staff_member_id": position.staff_member_id,
                    "compensation_scope": position.compensation_scope,
                    "service_id": position.service_id,
                    "period_from": position.period_from,
                    "period_to": position.period_to,
                    "accrual_on": position.accrual_on,
                    "amount": position.amount,
                    "lifecycle_status": position.lifecycle_status,
                },
            )
        )
    sources.extend(
        _source_record(
            "payroll_accrual",
            accrual.pk,
            revision_pk=(
                accrual.payroll_budget_revision_id
                or accrual.grant_allocation_revision_id
                or accrual.grant_fixed_compensation_revision_id
            ),
            projection={
                "staff_member_id": accrual.staff_member_id,
                "service_id": accrual.service_id,
                "work_date": accrual.work_date,
                "accrual_kind": accrual.accrual_kind,
                "amount": accrual.amount,
                "status": accrual.status,
            },
        )
        for accrual in accruals
    )
    payroll_sheet_lines = PayrollSheetLine.objects.filter(
        pk__in=payroll_evidence["payroll_line_ids"]
    ).order_by("pk")
    sources.extend(
        _source_record(
            "payroll_sheet_line",
            line.pk,
            revision_pk=line.payroll_budget_revision_id,
            projection={
                "payroll_sheet_id": line.payroll_sheet_id,
                "payroll_accrual_id": line.payroll_accrual_id,
                "accrual_kind_snapshot": line.accrual_kind_snapshot,
                "appointment_id": line.appointment_id,
                "service_id": line.service_id,
                "funding_source_id": line.funding_source_id,
                "work_date": line.work_date,
                "period_from_snapshot": line.period_from_snapshot,
                "period_to_snapshot": line.period_to_snapshot,
                "duration_minutes": line.duration_minutes,
                "amount": line.amount,
            },
        )
        for line in payroll_sheet_lines
    )
    payroll_sheets = PayrollSheet.objects.filter(
        pk__in=payroll_evidence["payroll_sheet_ids"]
    ).order_by("pk")
    sources.extend(
        _source_record(
            "payroll_sheet",
            sheet.pk,
            projection={
                "staff_member_id": sheet.staff_member_id,
                "date_from": sheet.date_from,
                "date_to": sheet.date_to,
                "status": sheet.status,
                "total_amount": sheet.total_amount,
            },
        )
        for sheet in payroll_sheets
    )
    expenses = list(
        CenterExpense.objects.filter(
            pk__in=expense_evidence["expense_ids"]
        ).order_by("pk")
    )
    sources.extend(
        _source_record(
            "center_expense",
            expense.pk,
            projection={
                "expense_date": expense.expense_date,
                "category_id": expense.category_id,
                "total_amount": expense.total_amount,
                "status": expense.status,
            },
        )
        for expense in expenses
    )
    expense_categories = CenterExpenseCategory.objects.filter(
        pk__in=expense_evidence["expense_category_ids"]
    ).order_by("pk")
    sources.extend(
        _source_record(
            "expense_category",
            category.pk,
            projection={"expense_type": category.expense_type},
        )
        for category in expense_categories
    )
    expense_split_ids = expense_evidence["expense_split_ids"]
    expense_splits = ExpenseFundingSplit.objects.filter(
        pk__in=expense_split_ids
    ).order_by("pk")
    sources.extend(
        _source_record(
            "expense_split",
            split.pk,
            projection={
                "expense_id": split.expense_id,
                "funding_source_id": split.funding_source_id,
                "amount": split.amount,
            },
        )
        for split in expense_splits
    )
    payload_sha256 = canonical_sha256(payload)
    evidence_manifest = _json_value(
        {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "canonicalizer_version": CANONICALIZER_VERSION,
            "payload_sha256": payload_sha256,
            "aliases": alias_rows,
            "sources": sorted(
                sources,
                key=lambda row: (row["kind"], row["source_pk"]),
            ),
            "preflight": preflight_rows,
            "source_sets": {
                "payroll_period_accrual_ids": payroll_evidence[
                    "period_accrual_ids"
                ],
                "payroll_line_ids": payroll_evidence["payroll_line_ids"],
                "payroll_sheet_ids": payroll_evidence["payroll_sheet_ids"],
                "expense_category_ids": expense_evidence["expense_category_ids"],
            },
        }
    )
    _validate_evidence_manifest(evidence_manifest)
    return payload, evidence_manifest


def _database_data_as_of() -> datetime:
    if connection.vendor != "postgresql":
        return timezone.now()
    with connection.cursor() as cursor:
        cursor.execute("SELECT CURRENT_TIMESTAMP")
        value = cursor.fetchone()[0]
    return value


def _set_repeatable_read() -> None:
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")


def _sqlstate(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        state = getattr(current, "sqlstate", None)
        if state:
            return str(state)
        current = current.__cause__ or current.__context__
    return None


def _constraint_name(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        diag = getattr(current, "diag", None)
        constraint_name = getattr(diag, "constraint_name", None)
        if constraint_name:
            return str(constraint_name)
        current = current.__cause__ or current.__context__
    return None


def _is_retryable_transaction_error(exc: BaseException) -> bool:
    state = _sqlstate(exc)
    if state in RETRYABLE_SQLSTATES:
        return True
    return (
        state == "23505"
        and _constraint_name(exc) in RETRYABLE_UNIQUE_CONSTRAINTS
    )


def _review_token(payload_sha256: str, evidence_manifest_sha256: str) -> str:
    return canonical_sha256(
        {
            "payload_sha256": payload_sha256,
            "evidence_manifest_sha256": evidence_manifest_sha256,
        }
    )


def _build_review_once(
    *,
    funding_source_id: int,
    counterparty_id: int | None,
    date_from: date,
    date_to: date,
    expected_snapshot_id: int | None,
) -> DonorReportReview:
    with transaction.atomic():
        _set_repeatable_read()
        funding_source = FundingSource.all_objects.get(pk=funding_source_id)
        counterparty = (
            Counterparty.all_objects.get(pk=counterparty_id)
            if counterparty_id is not None
            else None
        )
        data_as_of = _database_data_as_of()
        report = (
            DonorReport.objects.filter(
                funding_source=funding_source,
                counterparty=counterparty,
                date_from=date_from,
                date_to=date_to,
                report_kind=SNAPSHOT_SCHEMA_VERSION,
            )
            .select_related("current_snapshot")
            .first()
        )
        current_snapshot = report.current_snapshot if report else None
        current_snapshot_id = current_snapshot.pk if current_snapshot else None
        if current_snapshot_id != expected_snapshot_id:
            raise ValidationError(
                {
                    "expected_snapshot_id": (
                        "Цепочка отчета изменилась. Обновите страницу "
                        "и заново выполните проверку."
                    )
                }
            )
        payload, evidence_manifest = build_internal_grant_reconciliation(
            funding_source,
            date_from=date_from,
            date_to=date_to,
            counterparty=counterparty,
            previous_snapshot=current_snapshot,
        )
        payload_sha256 = canonical_sha256(payload)
        evidence_manifest_sha256 = canonical_sha256(evidence_manifest)
        return DonorReportReview(
            funding_source_id=funding_source.pk,
            counterparty_id=counterparty.pk if counterparty else None,
            date_from=date_from,
            date_to=date_to,
            expected_snapshot_id=current_snapshot_id,
            payload=payload,
            evidence_manifest=evidence_manifest,
            payload_sha256=payload_sha256,
            evidence_manifest_sha256=evidence_manifest_sha256,
            review_token=_review_token(
                payload_sha256,
                evidence_manifest_sha256,
            ),
            data_as_of=data_as_of,
        )


def review_donor_report_snapshot(
    *,
    funding_source_id: int,
    counterparty: Counterparty | None,
    date_from: date,
    date_to: date,
    expected_snapshot_id: int | None = None,
) -> DonorReportReview:
    if connection.vendor == "postgresql" and connection.in_atomic_block:
        raise RuntimeError(
            "Donor report review must start outside an existing transaction "
            "so REPEATABLE READ can be set before the first query."
        )
    if date_to < date_from:
        raise ValidationError({"date_to": "Дата окончания не может быть раньше даты начала."})
    counterparty_id = None
    if counterparty is not None:
        if counterparty.pk is None:
            raise ValidationError({"counterparty": "Сначала сохраните контрагента."})
        counterparty_id = counterparty.pk
    for attempt in range(MAX_TRANSACTION_ATTEMPTS):
        try:
            return _build_review_once(
                funding_source_id=funding_source_id,
                counterparty_id=counterparty_id,
                date_from=date_from,
                date_to=date_to,
                expected_snapshot_id=expected_snapshot_id,
            )
        except OperationalError as exc:
            if (
                not _is_retryable_transaction_error(exc)
                or attempt + 1 >= MAX_TRANSACTION_ATTEMPTS
            ):
                raise
    raise RuntimeError("Unreachable donor report review retry state.")


def _close_snapshot_once(
    *,
    funding_source_id: int,
    counterparty_id: int | None,
    date_from: date,
    date_to: date,
    actor: Any,
    reason: str,
    expected_snapshot_id: int | None,
    expected_review_token: str,
) -> DonorReportSnapshot:
    with transaction.atomic():
        _set_repeatable_read()
        funding_source = FundingSource.all_objects.select_for_update().get(
            pk=funding_source_id
        )
        counterparty = (
            Counterparty.all_objects.get(pk=counterparty_id)
            if counterparty_id is not None
            else None
        )
        data_as_of = _database_data_as_of()
        report = (
            DonorReport.objects.select_for_update(of=("self",))
            .filter(
                funding_source=funding_source,
                counterparty=counterparty,
                date_from=date_from,
                date_to=date_to,
                report_kind=SNAPSHOT_SCHEMA_VERSION,
            )
            .select_related("current_snapshot")
            .first()
        )
        if report is None:
            if expected_snapshot_id is not None:
                raise ValidationError(
                    {
                        "expected_snapshot_id": (
                            "Цепочка отчета изменилась. Обновите страницу."
                        )
                    }
                )
            report = DonorReport.objects.create(
                funding_source=funding_source,
                counterparty=counterparty,
                date_from=date_from,
                date_to=date_to,
                report_kind=SNAPSHOT_SCHEMA_VERSION,
            )
            previous = None
        else:
            previous = report.current_snapshot
            if expected_snapshot_id != report.current_snapshot_id:
                raise ValidationError(
                    {
                        "expected_snapshot_id": (
                            "Отчет уже закрыт или исправлен другим пользователем. "
                            "Обновите страницу."
                        )
                    }
                )

        payload, evidence_manifest = build_internal_grant_reconciliation(
            funding_source,
            date_from=date_from,
            date_to=date_to,
            counterparty=counterparty,
            previous_snapshot=previous,
        )
        payload_sha256 = canonical_sha256(payload)
        evidence_manifest_sha256 = canonical_sha256(evidence_manifest)
        actual_review_token = _review_token(
            payload_sha256,
            evidence_manifest_sha256,
        )
        if actual_review_token != expected_review_token:
            raise ValidationError(
                {
                    "expected_review_token": (
                        "Данные изменились после проверки. "
                        "Заново откройте безопасный preview."
                    )
                }
            )
        if previous and previous.payload_sha256 == payload_sha256:
            raise ValidationError(
                {
                    "report": (
                        "Данные отчета не изменились. Исправляющий снимок "
                        "не создается без изменения payload."
                    )
                }
            )
        closed_at = _database_data_as_of()
        snapshot = DonorReportSnapshot.objects.create(
            report=report,
            snapshot_number=(previous.snapshot_number + 1 if previous else 1),
            event_type=(
                DonorReportSnapshot.EventType.CORRECTED
                if previous
                else DonorReportSnapshot.EventType.CLOSED
            ),
            snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
            canonicalizer_version=CANONICALIZER_VERSION,
            payload=payload,
            evidence_manifest=evidence_manifest,
            payload_sha256=payload_sha256,
            evidence_manifest_sha256=evidence_manifest_sha256,
            data_as_of=data_as_of,
            actor=actor,
            actor_role_snapshot=DonorReportSnapshot.ActorRole.DIRECTOR,
            reason=reason,
            closed_at=closed_at,
            supersedes=previous,
        )
        pointer_filter = DonorReport.objects.filter(pk=report.pk)
        if previous is None:
            pointer_filter = pointer_filter.filter(current_snapshot__isnull=True)
        else:
            pointer_filter = pointer_filter.filter(current_snapshot=previous)
        updated = pointer_filter.update(
            current_snapshot=snapshot,
            updated_at=closed_at,
        )
        if updated != 1:
            raise ValidationError(
                {
                    "expected_snapshot_id": (
                        "Отчет изменен конкурентно. Обновите страницу и повторите действие."
                    )
                }
            )
        return snapshot


def close_donor_report_snapshot(
    *,
    funding_source_id: int,
    counterparty: Counterparty | None,
    date_from: date,
    date_to: date,
    actor: Any,
    reason: str,
    expected_review_token: str,
    expected_snapshot_id: int | None = None,
) -> DonorReportSnapshot:
    _require_director(actor)
    if connection.vendor == "postgresql" and connection.in_atomic_block:
        raise RuntimeError(
            "Donor report closure must start outside an existing transaction "
            "so REPEATABLE READ can be set before the first query."
        )
    normalized_reason = _normalize_reason(reason)
    normalized_review_token = (expected_review_token or "").strip().lower()
    if len(normalized_review_token) != 64 or any(
        character not in "0123456789abcdef"
        for character in normalized_review_token
    ):
        raise ValidationError(
            {"expected_review_token": "Сначала выполните безопасную проверку отчета."}
        )
    if date_to < date_from:
        raise ValidationError({"date_to": "Дата окончания не может быть раньше даты начала."})
    counterparty_id = None
    if counterparty is not None:
        if counterparty.pk is None:
            raise ValidationError({"counterparty": "Сначала сохраните контрагента."})
        counterparty_id = counterparty.pk

    for attempt in range(MAX_TRANSACTION_ATTEMPTS):
        try:
            return _close_snapshot_once(
                funding_source_id=funding_source_id,
                counterparty_id=counterparty_id,
                date_from=date_from,
                date_to=date_to,
                actor=actor,
                reason=normalized_reason,
                expected_snapshot_id=expected_snapshot_id,
                expected_review_token=normalized_review_token,
            )
        except (OperationalError, IntegrityError) as exc:
            if (
                not _is_retryable_transaction_error(exc)
                or attempt + 1 >= MAX_TRANSACTION_ATTEMPTS
            ):
                raise
    raise RuntimeError("Unreachable donor report retry state.")
