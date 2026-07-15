"""Read-only reports for center expenses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Prefetch

from operations.models import (
    CenterExpense,
    CenterExpenseCategory,
    ExpenseFundingSplit,
    FundingSource,
)


@dataclass(frozen=True)
class ExpenseReportSummary:
    expense_count: int
    total_amount: Decimal
    allocated_amount: Decimal
    unallocated_amount: Decimal
    overallocated_amount: Decimal
    unbalanced_count: int


@dataclass(frozen=True)
class ExpenseReportCategoryRow:
    category: CenterExpenseCategory
    expense_count: int
    total_amount: Decimal
    allocated_amount: Decimal
    unallocated_amount: Decimal


@dataclass(frozen=True)
class ExpenseReportFundingRow:
    funding_source: FundingSource
    expense_count: int
    amount: Decimal


@dataclass(frozen=True)
class ExpenseReportStatusRow:
    status: str
    label: str
    expense_count: int
    total_amount: Decimal


@dataclass(frozen=True)
class ExpenseReport:
    date_from: date
    date_to: date
    status: str
    funding_source_id: int | None
    category_id: int | None
    expenses: list[CenterExpense]
    category_rows: list[ExpenseReportCategoryRow]
    funding_rows: list[ExpenseReportFundingRow]
    status_rows: list[ExpenseReportStatusRow]
    unbalanced_expenses: list[CenterExpense]
    summary: ExpenseReportSummary


def _zero() -> Decimal:
    return Decimal("0")


def _selected_split_amount(expense: CenterExpense, funding_source_id: int | None) -> Decimal:
    amount = _zero()
    for split in expense.report_funding_splits:
        if funding_source_id is None or split.funding_source_id == funding_source_id:
            amount += split.amount
    return amount


def _decorate_expense(expense: CenterExpense, funding_source_id: int | None) -> None:
    split_total = sum((split.amount for split in expense.report_funding_splits), _zero())
    selected_split_total = _selected_split_amount(expense, funding_source_id)
    difference = expense.total_amount - split_total
    expense.report_split_total = split_total
    expense.report_selected_split_total = selected_split_total
    expense.report_unallocated_amount = difference
    expense.report_is_unbalanced = difference != 0


def _expense_queryset(
    *,
    date_from: date,
    date_to: date,
    status: str = "",
    funding_source_id: int | None = None,
    category_id: int | None = None,
):
    queryset = CenterExpense.objects.filter(
        expense_date__gte=date_from,
        expense_date__lte=date_to,
    )
    valid_statuses = {choice[0] for choice in CenterExpense.Status.choices}
    if status in valid_statuses:
        queryset = queryset.filter(status=status)
    else:
        queryset = queryset.exclude(status=CenterExpense.Status.CANCELLED)
        status = ""

    if funding_source_id:
        queryset = queryset.filter(funding_splits__funding_source_id=funding_source_id)
    if category_id:
        queryset = queryset.filter(category_id=category_id)

    return (
        queryset.distinct()
        .select_related("category", "counterparty", "document")
        .prefetch_related(
            Prefetch(
                "funding_splits",
                queryset=ExpenseFundingSplit.objects.select_related("funding_source").order_by(
                    "funding_source__name",
                    "pk",
                ),
                to_attr="report_funding_splits",
            )
        )
        .order_by("-expense_date", "-created_at", "-pk")
    )


def build_expense_report(
    *,
    date_from: date,
    date_to: date,
    status: str = "",
    funding_source_id: int | None = None,
    category_id: int | None = None,
) -> ExpenseReport:
    """Build an in-memory report without creating ledger or balance facts."""
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    expenses = list(
        _expense_queryset(
            date_from=date_from,
            date_to=date_to,
            status=status,
            funding_source_id=funding_source_id,
            category_id=category_id,
        )
    )
    for expense in expenses:
        _decorate_expense(expense, funding_source_id)

    category_map: dict[int, dict[str, object]] = {}
    funding_map: dict[int, dict[str, object]] = {}
    status_map: dict[str, dict[str, object]] = {}
    total_amount = _zero()
    allocated_amount = _zero()
    unallocated_amount = _zero()
    overallocated_amount = _zero()
    unbalanced_expenses: list[CenterExpense] = []

    for expense in expenses:
        total_amount += expense.total_amount
        allocated_amount += expense.report_selected_split_total
        if expense.report_unallocated_amount > 0:
            unallocated_amount += expense.report_unallocated_amount
        elif expense.report_unallocated_amount < 0:
            overallocated_amount += abs(expense.report_unallocated_amount)
        if expense.report_is_unbalanced:
            unbalanced_expenses.append(expense)

        category_row = category_map.setdefault(
            expense.category_id,
            {
                "category": expense.category,
                "expense_ids": set(),
                "total_amount": _zero(),
                "allocated_amount": _zero(),
                "unallocated_amount": _zero(),
            },
        )
        category_row["expense_ids"].add(expense.pk)
        category_row["total_amount"] += expense.total_amount
        category_row["allocated_amount"] += expense.report_selected_split_total
        if expense.report_unallocated_amount > 0:
            category_row["unallocated_amount"] += expense.report_unallocated_amount

        status_row = status_map.setdefault(
            expense.status,
            {
                "label": expense.get_status_display(),
                "expense_ids": set(),
                "total_amount": _zero(),
            },
        )
        status_row["expense_ids"].add(expense.pk)
        status_row["total_amount"] += expense.total_amount

        for split in expense.report_funding_splits:
            if funding_source_id and split.funding_source_id != funding_source_id:
                continue
            funding_row = funding_map.setdefault(
                split.funding_source_id,
                {
                    "funding_source": split.funding_source,
                    "expense_ids": set(),
                    "amount": _zero(),
                },
            )
            funding_row["expense_ids"].add(expense.pk)
            funding_row["amount"] += split.amount

    category_rows = [
        ExpenseReportCategoryRow(
            category=row["category"],
            expense_count=len(row["expense_ids"]),
            total_amount=row["total_amount"],
            allocated_amount=row["allocated_amount"],
            unallocated_amount=row["unallocated_amount"],
        )
        for row in category_map.values()
    ]
    category_rows.sort(key=lambda row: (-row.total_amount, row.category.sort_order, row.category.name))

    funding_rows = [
        ExpenseReportFundingRow(
            funding_source=row["funding_source"],
            expense_count=len(row["expense_ids"]),
            amount=row["amount"],
        )
        for row in funding_map.values()
    ]
    funding_rows.sort(key=lambda row: (-row.amount, row.funding_source.name))

    status_order = {choice[0]: index for index, choice in enumerate(CenterExpense.Status.choices)}
    status_rows = [
        ExpenseReportStatusRow(
            status=status_key,
            label=row["label"],
            expense_count=len(row["expense_ids"]),
            total_amount=row["total_amount"],
        )
        for status_key, row in status_map.items()
    ]
    status_rows.sort(key=lambda row: status_order.get(row.status, 99))

    return ExpenseReport(
        date_from=date_from,
        date_to=date_to,
        status=status if status in {choice[0] for choice in CenterExpense.Status.choices} else "",
        funding_source_id=funding_source_id,
        category_id=category_id,
        expenses=expenses[:100],
        category_rows=category_rows,
        funding_rows=funding_rows,
        status_rows=status_rows,
        unbalanced_expenses=unbalanced_expenses[:50],
        summary=ExpenseReportSummary(
            expense_count=len(expenses),
            total_amount=total_amount,
            allocated_amount=allocated_amount,
            unallocated_amount=unallocated_amount,
            overallocated_amount=overallocated_amount,
            unbalanced_count=len(unbalanced_expenses),
        ),
    )
