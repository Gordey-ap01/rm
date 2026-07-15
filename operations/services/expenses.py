from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum

from operations.models import CenterExpense


class ExpenseValidationError(ValueError):
    """Raised when a center expense is not valid for the requested state."""


@dataclass(frozen=True)
class ExpenseFundingValidation:
    status: str
    total_amount: Decimal
    split_total: Decimal
    difference: Decimal
    requires_balanced_funding: bool

    @property
    def is_balanced(self) -> bool:
        return self.difference == Decimal("0")


BALANCED_FUNDING_STATUSES = (
    CenterExpense.Status.APPROVED,
    CenterExpense.Status.PAID,
)


def expense_status_requires_balanced_funding(status: str) -> bool:
    return status in BALANCED_FUNDING_STATUSES


def calculate_expense_funding_split_total(expense: CenterExpense) -> Decimal:
    if not expense.pk:
        return Decimal("0")
    total = expense.funding_splits.aggregate(total=Sum("amount"))["total"]
    return total or Decimal("0")


def validate_expense_funding_splits(
    expense: CenterExpense,
    *,
    target_status: str | None = None,
) -> ExpenseFundingValidation:
    status = target_status or expense.status
    total_amount = expense.total_amount or Decimal("0")
    split_total = calculate_expense_funding_split_total(expense)
    difference = total_amount - split_total
    result = ExpenseFundingValidation(
        status=status,
        total_amount=total_amount,
        split_total=split_total,
        difference=difference,
        requires_balanced_funding=expense_status_requires_balanced_funding(status),
    )

    if result.requires_balanced_funding and not result.is_balanced:
        raise ExpenseValidationError(
            "Сумма распределения по источникам должна совпадать с суммой расхода "
            f"для статуса {status}: расход {total_amount}, распределено {split_total}."
        )

    if status == CenterExpense.Status.PAID and expense.paid_at is None:
        raise ExpenseValidationError("Для оплаченного расхода нужно указать дату оплаты.")

    return result
