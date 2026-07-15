from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from operations.models import (
    CenterExpense,
    CenterExpenseCategory,
    Counterparty,
    ExpenseFundingSplit,
    FundingSource,
    LedgerEntry,
)
from operations.services import expenses as expenses_svc


class CenterExpenseValidationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = CenterExpenseCategory.objects.create(
            name="Коммунальные платежи",
            expense_type=CenterExpenseCategory.ExpenseType.UTILITIES,
        )
        cls.counterparty = Counterparty.objects.create(
            name="Поставщик услуг",
            counterparty_type=Counterparty.CounterpartyType.VENDOR,
        )
        cls.grant = FundingSource.objects.create(
            name="Грант на развитие",
            source_type=FundingSource.SourceType.GRANT,
        )
        cls.sponsor = FundingSource.objects.create(
            name="Спонсор",
            source_type=FundingSource.SourceType.SPONSOR,
        )

    def _expense(self, amount: str = "1000.00", **kwargs) -> CenterExpense:
        return CenterExpense.objects.create(
            category=self.category,
            counterparty=self.counterparty,
            title="Оплата счета",
            total_amount=Decimal(amount),
            **kwargs,
        )

    def test_draft_expense_allows_unbalanced_funding_splits(self):
        expense = self._expense()
        ExpenseFundingSplit.objects.create(
            expense=expense,
            funding_source=self.grant,
            amount=Decimal("400.00"),
        )

        result = expenses_svc.validate_expense_funding_splits(expense)

        self.assertFalse(result.requires_balanced_funding)
        self.assertFalse(result.is_balanced)
        self.assertEqual(result.split_total, Decimal("400.00"))
        self.assertEqual(result.difference, Decimal("600.00"))

    def test_approved_expense_requires_balanced_funding_splits(self):
        expense = self._expense()
        ExpenseFundingSplit.objects.create(
            expense=expense,
            funding_source=self.grant,
            amount=Decimal("400.00"),
        )

        with self.assertRaises(expenses_svc.ExpenseValidationError):
            expenses_svc.validate_expense_funding_splits(
                expense,
                target_status=CenterExpense.Status.APPROVED,
            )

        ExpenseFundingSplit.objects.create(
            expense=expense,
            funding_source=self.sponsor,
            amount=Decimal("600.00"),
        )

        result = expenses_svc.validate_expense_funding_splits(
            expense,
            target_status=CenterExpense.Status.APPROVED,
        )

        self.assertTrue(result.requires_balanced_funding)
        self.assertTrue(result.is_balanced)
        self.assertEqual(result.split_total, expense.total_amount)

    def test_paid_expense_requires_paid_at(self):
        expense = self._expense()
        ExpenseFundingSplit.objects.create(
            expense=expense,
            funding_source=self.grant,
            amount=Decimal("1000.00"),
        )

        with self.assertRaises(expenses_svc.ExpenseValidationError):
            expenses_svc.validate_expense_funding_splits(
                expense,
                target_status=CenterExpense.Status.PAID,
            )

        expense.paid_at = timezone.localdate()
        result = expenses_svc.validate_expense_funding_splits(
            expense,
            target_status=CenterExpense.Status.PAID,
        )

        self.assertTrue(result.is_balanced)

    def test_funding_splits_do_not_create_ledger_entries(self):
        expense = self._expense()
        before = LedgerEntry.objects.count()

        ExpenseFundingSplit.objects.create(
            expense=expense,
            funding_source=self.grant,
            amount=Decimal("1000.00"),
        )

        self.assertEqual(LedgerEntry.objects.count(), before)

    def test_expense_amount_must_be_positive(self):
        expense = CenterExpense(
            category=self.category,
            title="Некорректный расход",
            total_amount=Decimal("0.00"),
        )

        with self.assertRaises(ValidationError):
            expense.full_clean()

    def test_split_amount_must_be_positive(self):
        expense = self._expense()
        split = ExpenseFundingSplit(
            expense=expense,
            funding_source=self.grant,
            amount=Decimal("0.00"),
        )

        with self.assertRaises(ValidationError):
            split.full_clean()
