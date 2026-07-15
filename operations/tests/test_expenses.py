from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from operations.models import (
    CenterExpense,
    CenterExpenseCategory,
    Counterparty,
    EquipmentAsset,
    ExpenseFundingSplit,
    FundingSource,
    LedgerEntry,
)
from operations.services import expense_reports as expense_reports_svc, expenses as expenses_svc


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


class EquipmentAssetValidationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.equipment_category = CenterExpenseCategory.objects.create(
            name="Equipment",
            expense_type=CenterExpenseCategory.ExpenseType.EQUIPMENT,
        )
        cls.household_category = CenterExpenseCategory.objects.create(
            name="Household",
            expense_type=CenterExpenseCategory.ExpenseType.HOUSEHOLD,
        )

    def _expense(self, category: CenterExpenseCategory) -> CenterExpense:
        return CenterExpense.objects.create(
            category=category,
            title="Asset purchase",
            total_amount=Decimal("1000.00"),
        )

    def test_asset_can_link_equipment_expense(self):
        expense = self._expense(self.equipment_category)
        asset = EquipmentAsset(
            name="Balance trainer",
            asset_type=EquipmentAsset.AssetType.THERAPY_EQUIPMENT,
            inventory_number="INV-001",
            purchase_expense=expense,
            total_amount=Decimal("1000.00"),
        )

        asset.full_clean()
        asset.save()

        self.assertEqual(asset.purchase_expense, expense)

    def test_asset_rejects_non_equipment_purchase_expense(self):
        expense = self._expense(self.household_category)
        asset = EquipmentAsset(
            name="Wrong link",
            purchase_expense=expense,
            total_amount=Decimal("1000.00"),
        )

        with self.assertRaises(ValidationError):
            asset.full_clean()

    def test_inventory_number_is_unique_when_filled(self):
        EquipmentAsset.objects.create(name="First asset", inventory_number="INV-UNIQUE")
        duplicate = EquipmentAsset(name="Second asset", inventory_number="INV-UNIQUE")

        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_blank_inventory_number_can_repeat(self):
        EquipmentAsset.objects.create(name="First asset")
        duplicate = EquipmentAsset(name="Second asset")

        duplicate.full_clean()
        duplicate.save()

        self.assertEqual(EquipmentAsset.objects.filter(inventory_number="").count(), 2)

    def test_writeoff_does_not_delete_expense_or_create_ledger(self):
        expense = self._expense(self.equipment_category)
        ledger_count = LedgerEntry.objects.count()
        asset = EquipmentAsset.objects.create(
            name="Trainer",
            purchase_expense=expense,
            total_amount=Decimal("1000.00"),
        )

        asset.status = EquipmentAsset.Status.WRITTEN_OFF
        asset.save()

        self.assertTrue(CenterExpense.objects.filter(pk=expense.pk).exists())
        self.assertEqual(LedgerEntry.objects.count(), ledger_count)


class CenterExpenseReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.category = CenterExpenseCategory.objects.create(
            name="Materials",
            expense_type=CenterExpenseCategory.ExpenseType.INVENTORY,
        )
        cls.other_category = CenterExpenseCategory.objects.create(
            name="Rent",
            expense_type=CenterExpenseCategory.ExpenseType.RENT,
        )
        cls.counterparty = Counterparty.objects.create(
            name="Vendor",
            counterparty_type=Counterparty.CounterpartyType.VENDOR,
        )
        cls.grant = FundingSource.objects.create(
            name="Grant",
            source_type=FundingSource.SourceType.GRANT,
        )
        cls.sponsor = FundingSource.objects.create(
            name="Sponsor",
            source_type=FundingSource.SourceType.SPONSOR,
        )

    def _expense(
        self,
        *,
        amount: str = "1000.00",
        expense_date=None,
        category=None,
        status: str = CenterExpense.Status.DRAFT,
        title: str = "Expense",
    ) -> CenterExpense:
        return CenterExpense.objects.create(
            expense_date=expense_date or self.today,
            category=category or self.category,
            counterparty=self.counterparty,
            title=title,
            total_amount=Decimal(amount),
            status=status,
        )

    def test_report_uses_period_and_split_amounts(self):
        inside = self._expense()
        ExpenseFundingSplit.objects.create(
            expense=inside,
            funding_source=self.grant,
            amount=Decimal("400.00"),
        )
        ExpenseFundingSplit.objects.create(
            expense=inside,
            funding_source=self.sponsor,
            amount=Decimal("600.00"),
        )
        outside = self._expense(
            amount="500.00",
            expense_date=self.today - timedelta(days=10),
            title="Outside period",
        )
        ExpenseFundingSplit.objects.create(
            expense=outside,
            funding_source=self.grant,
            amount=Decimal("500.00"),
        )

        report = expense_reports_svc.build_expense_report(
            date_from=self.today,
            date_to=self.today,
        )

        self.assertEqual(report.summary.expense_count, 1)
        self.assertEqual(report.summary.total_amount, Decimal("1000.00"))
        self.assertEqual(report.summary.allocated_amount, Decimal("1000.00"))
        self.assertEqual(report.category_rows[0].total_amount, Decimal("1000.00"))
        funding = {row.funding_source: row.amount for row in report.funding_rows}
        self.assertEqual(funding[self.grant], Decimal("400.00"))
        self.assertEqual(funding[self.sponsor], Decimal("600.00"))

    def test_report_by_funding_source_counts_selected_split_only(self):
        mixed = self._expense()
        ExpenseFundingSplit.objects.create(
            expense=mixed,
            funding_source=self.grant,
            amount=Decimal("400.00"),
        )
        ExpenseFundingSplit.objects.create(
            expense=mixed,
            funding_source=self.sponsor,
            amount=Decimal("600.00"),
        )
        sponsor_only = self._expense(amount="500.00", title="Sponsor only")
        ExpenseFundingSplit.objects.create(
            expense=sponsor_only,
            funding_source=self.sponsor,
            amount=Decimal("500.00"),
        )

        report = expense_reports_svc.build_expense_report(
            date_from=self.today,
            date_to=self.today,
            funding_source_id=self.grant.pk,
        )

        self.assertEqual(report.summary.expense_count, 1)
        self.assertEqual(report.summary.total_amount, Decimal("1000.00"))
        self.assertEqual(report.summary.allocated_amount, Decimal("400.00"))
        self.assertEqual(len(report.funding_rows), 1)
        self.assertEqual(report.funding_rows[0].funding_source, self.grant)
        self.assertEqual(report.category_rows[0].allocated_amount, Decimal("400.00"))

    def test_report_marks_unbalanced_expenses(self):
        expense = self._expense()
        ExpenseFundingSplit.objects.create(
            expense=expense,
            funding_source=self.grant,
            amount=Decimal("300.00"),
        )

        report = expense_reports_svc.build_expense_report(
            date_from=self.today,
            date_to=self.today,
        )

        self.assertEqual(report.summary.unbalanced_count, 1)
        self.assertEqual(report.summary.unallocated_amount, Decimal("700.00"))
        self.assertEqual(report.unbalanced_expenses, [expense])
