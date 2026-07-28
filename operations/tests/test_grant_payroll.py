"""Grant fixed compensation accrual and payroll budget acceptance."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from queue import Queue
from threading import Barrier, Thread
from unittest import skipUnless

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from operations.models import (
    Appointment,
    BalanceAccount,
    Child,
    FundingPayrollBudget,
    FundingSource,
    GrantFixedCompensation,
    ParentGuardian,
    PayrollAccrual,
    PayrollSheet,
    PayrollSheetLifecycleEvent,
    Service,
    StaffCompensationRule,
    StaffMember,
)
from operations.services import (
    grant_compensation as grant_compensation_svc,
    payroll as payroll_svc,
)


class GrantPayrollFixtureMixin:
    def setUp(self):
        super().setUp()
        self.director = User.objects.create_superuser(
            "grant-payroll-director",
            password="x",
        )
        self.funding = FundingSource.objects.create(
            name="Грант payroll 59A-2",
            source_type=FundingSource.SourceType.GRANT,
        )
        self.service = Service.objects.create(
            name="Услуга грантового payroll",
            code="GRANT-PAYROLL-59A2",
            default_duration_minutes=30,
        )
        self.staff = StaffMember.objects.create(full_name="Сотрудник проекта")
        self.day = date(2026, 6, 30)

    def create_budget(
        self,
        *,
        amount: Decimal = Decimal("10000.00"),
        enforcement_mode: str = FundingPayrollBudget.EnforcementMode.HARD,
    ) -> FundingPayrollBudget:
        return grant_compensation_svc.create_payroll_budget(
            funding_source=self.funding,
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
            planned_amount=amount,
            enforcement_mode=enforcement_mode,
            note="Бюджет теста 59A-2",
            actor=self.director,
            reason="Утвержден бюджет для теста 59A-2.",
        )

    def create_fixed(
        self,
        budget: FundingPayrollBudget,
        *,
        staff: StaffMember | None = None,
        amount: Decimal = Decimal("4000.00"),
        scope: str = GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
        assignment_label: str = "Координатор проекта",
    ) -> GrantFixedCompensation:
        return grant_compensation_svc.create_fixed_compensation(
            payroll_budget=budget,
            staff_member=staff or self.staff,
            compensation_scope=scope,
            service=(
                self.service
                if scope == GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
                else None
            ),
            assignment_label=(
                ""
                if scope == GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
                else assignment_label
            ),
            period_from=date(2026, 6, 1),
            period_to=self.day,
            accrual_on=self.day,
            amount=amount,
            note="Фиксированная позиция теста",
            actor=self.director,
            reason="Утверждена фиксированная позиция.",
        )

    def create_appointment_accrual(
        self,
        *,
        amount: Decimal = Decimal("500.00"),
    ) -> PayrollAccrual:
        starts_at = timezone.make_aware(
            datetime.combine(self.day, time(10, 0)),
            timezone.get_current_timezone(),
        )
        return PayrollAccrual.objects.create(
            dedupe_key=f"appointment-59a2:{self.staff.pk}:{self.day}",
            accrual_kind=PayrollAccrual.AccrualKind.APPOINTMENT,
            staff_member=self.staff,
            service=self.service,
            funding_source=self.funding,
            work_date=self.day,
            starts_at_snapshot=starts_at,
            ends_at_snapshot=starts_at + timedelta(minutes=30),
            duration_minutes=30,
            rate_type_snapshot=StaffCompensationRule.RateType.PER_SESSION,
            rate_amount_snapshot=amount,
            session_scope_snapshot=StaffCompensationRule.SessionScope.ALL,
            group_pay_policy_snapshot=StaffCompensationRule.GroupPayPolicy.PER_SESSION,
            charged_participants_count_snapshot=1,
            pay_units_snapshot=1,
            amount=amount,
        )


class GrantPayrollServiceTests(GrantPayrollFixtureMixin, TestCase):
    def test_fixed_generation_is_root_idempotent_without_fake_appointment(self):
        budget = self.create_budget()
        fixed = self.create_fixed(budget)

        first = payroll_svc.generate_accruals_for_staff(
            self.staff,
            date_from=self.day,
            date_to=self.day,
            actor=self.director,
        )
        second = payroll_svc.generate_accruals_for_staff(
            self.staff,
            date_from=self.day,
            date_to=self.day,
            actor=self.director,
        )

        accrual = PayrollAccrual.objects.get()
        self.assertEqual(first.created, 1)
        self.assertEqual(second.updated, 1)
        self.assertEqual(accrual.dedupe_key, f"grant-fixed:{fixed.pk}")
        self.assertEqual(accrual.accrual_kind, PayrollAccrual.AccrualKind.GRANT_FIXED)
        self.assertEqual(accrual.grant_fixed_compensation_revision, fixed.current_revision)
        self.assertEqual(accrual.payroll_budget_revision, budget.current_revision)
        self.assertEqual(accrual.period_from_snapshot, date(2026, 6, 1))
        self.assertEqual(accrual.period_to_snapshot, self.day)
        self.assertIsNone(accrual.appointment)
        self.assertIsNone(accrual.service)
        self.assertIsNone(accrual.duration_minutes)
        self.assertIsNone(accrual.rate_amount_snapshot)

    def test_mixed_sheet_preserves_amounts_and_fixed_adds_no_minutes(self):
        budget = self.create_budget()
        self.create_fixed(budget)
        appointment_accrual = self.create_appointment_accrual()

        sheet = payroll_svc.create_payroll_sheet_for_staff(
            self.staff,
            date_from=self.day,
            date_to=self.day,
            actor=self.director,
        )

        lines = list(sheet.lines.order_by("accrual_kind_snapshot"))
        self.assertEqual(len(lines), 2)
        self.assertEqual(sheet.total_amount, Decimal("4500.00"))
        self.assertEqual(
            {line.accrual_kind_snapshot for line in lines},
            {
                PayrollAccrual.AccrualKind.APPOINTMENT,
                PayrollAccrual.AccrualKind.GRANT_FIXED,
            },
        )
        self.assertEqual(sum((line.duration_minutes or 0) for line in lines), 30)
        fixed_line = next(
            line
            for line in lines
            if line.accrual_kind_snapshot == PayrollAccrual.AccrualKind.GRANT_FIXED
        )
        self.assertIsNone(fixed_line.appointment)
        self.assertIsNone(fixed_line.service)
        self.assertIsNone(fixed_line.duration_minutes)
        self.assertEqual(fixed_line.line_label, "Координатор проекта")
        self.assertEqual(appointment_accrual.amount, Decimal("500.00"))
        draft_usage = grant_compensation_svc.payroll_budget_usage(budget)
        self.assertEqual(draft_usage.consumed, Decimal("0"))
        self.assertEqual(draft_usage.draft_commitment, Decimal("4500.00"))
        self.assertEqual(draft_usage.available, Decimal("10000.00"))
        self.assertEqual(draft_usage.forecast_available, Decimal("5500.00"))

        payroll_svc.approve_payroll_sheet(sheet, actor=self.director)
        sheet.refresh_from_db()
        appointment_accrual.refresh_from_db()
        fixed_line.refresh_from_db()
        self.assertEqual(sheet.status, PayrollSheet.Status.APPROVED)
        self.assertEqual(appointment_accrual.amount, Decimal("500.00"))
        self.assertEqual(appointment_accrual.payroll_budget_revision, budget.current_revision)
        self.assertEqual(fixed_line.payroll_budget_revision, budget.current_revision)
        event = PayrollSheetLifecycleEvent.objects.get(
            payroll_sheet=sheet,
            event_type=PayrollSheetLifecycleEvent.EventType.APPROVED,
        )
        self.assertEqual(event.payroll_budget_revision, budget.current_revision)
        self.assertEqual(event.budget_overage_amount, Decimal("0"))
        with self.assertRaisesMessage(ValidationError, "нельзя удалить"):
            event.delete()
        approved_usage = grant_compensation_svc.payroll_budget_usage(budget)
        self.assertEqual(approved_usage.consumed, Decimal("4500.00"))
        self.assertEqual(approved_usage.draft_commitment, Decimal("0"))
        self.assertEqual(approved_usage.available, Decimal("5500.00"))
        self.assertEqual(approved_usage.forecast_available, Decimal("5500.00"))

    def test_service_delivery_fixed_suppresses_session_accrual(self):
        budget = self.create_budget()
        self.create_fixed(
            budget,
            scope=GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY,
        )
        StaffCompensationRule.objects.create(
            staff_member=self.staff,
            service=self.service,
            funding_source=self.funding,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("500.00"),
        )
        parent = ParentGuardian.objects.create(
            last_name="Тестов",
            first_name="Родитель",
            phone="+7 900 000-59-02",
        )
        child = Child.objects.create(
            last_name="Тестов",
            first_name="Получатель",
            primary_parent=parent,
        )
        account = BalanceAccount.objects.create(
            child=child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            initial_amount=1,
        )
        starts_at = timezone.make_aware(
            datetime.combine(self.day, time(11, 0)),
            timezone.get_current_timezone(),
        )
        Appointment.objects.create(
            child=child,
            staff_member=self.staff,
            service=self.service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            status=Appointment.Status.COMPLETED,
            attendance_status=Appointment.AttendanceStatus.ATTENDED,
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=account,
        )

        result = payroll_svc.generate_accruals_for_staff(
            self.staff,
            date_from=self.day,
            date_to=self.day,
            actor=self.director,
        )

        self.assertEqual(result.skipped_fixed_service, 1)
        self.assertEqual(PayrollAccrual.objects.count(), 1)
        self.assertEqual(
            PayrollAccrual.objects.get().accrual_kind,
            PayrollAccrual.AccrualKind.GRANT_FIXED,
        )

    def test_warning_overage_requires_reason_and_records_snapshot(self):
        budget = self.create_budget(
            amount=Decimal("100.00"),
            enforcement_mode=FundingPayrollBudget.EnforcementMode.WARNING,
        )
        self.create_fixed(budget, amount=Decimal("120.00"))
        sheet = payroll_svc.create_payroll_sheet_for_staff(
            self.staff,
            date_from=self.day,
            date_to=self.day,
            actor=self.director,
        )

        with self.assertRaisesMessage(ValueError, "отдельное основание"):
            payroll_svc.approve_payroll_sheet(sheet, actor=self.director)

        payroll_svc.approve_payroll_sheet(
            sheet,
            actor=self.director,
            note="Осознанное превышение грантового бюджета.",
        )

        event = PayrollSheetLifecycleEvent.objects.get(
            payroll_sheet=sheet,
            event_type=PayrollSheetLifecycleEvent.EventType.APPROVED,
        )
        self.assertEqual(event.payroll_budget_revision, budget.current_revision)
        self.assertEqual(event.budget_overage_amount, Decimal("20.00"))
        self.assertIn("Осознанное превышение", event.note)

    def test_hard_budget_cannot_be_overridden(self):
        budget = self.create_budget(amount=Decimal("100.00"))
        self.create_fixed(budget, amount=Decimal("120.00"))
        sheet = payroll_svc.create_payroll_sheet_for_staff(
            self.staff,
            date_from=self.day,
            date_to=self.day,
            actor=self.director,
        )

        with self.assertRaisesMessage(ValueError, "жесткий бюджет"):
            payroll_svc.approve_payroll_sheet(
                sheet,
                actor=self.director,
                note="Руководитель пытается переопределить лимит.",
            )

        sheet.refresh_from_db()
        self.assertEqual(sheet.status, PayrollSheet.Status.DRAFT)
        self.assertFalse(sheet.lifecycle_events.exists())

    def test_fixed_revision_reprices_only_unattached_draft(self):
        budget = self.create_budget()
        fixed = self.create_fixed(budget)
        payroll_svc.generate_accruals_for_staff(
            self.staff,
            date_from=self.day,
            date_to=self.day,
            actor=self.director,
        )

        fixed = grant_compensation_svc.revise_fixed_compensation(
            fixed,
            period_from=date(2026, 6, 1),
            period_to=self.day,
            accrual_on=self.day,
            amount=Decimal("4500.00"),
            note="Новая сумма",
            actor=self.director,
            reason="Согласовано изменение суммы.",
            expected_revision_id=fixed.current_revision_id,
        )
        payroll_svc.generate_accruals_for_staff(
            self.staff,
            date_from=self.day,
            date_to=self.day,
            actor=self.director,
        )
        accrual = PayrollAccrual.objects.get()
        self.assertEqual(accrual.amount, Decimal("4500.00"))
        self.assertEqual(accrual.grant_fixed_compensation_revision, fixed.current_revision)

        sheet = payroll_svc.create_payroll_sheet_for_staff(
            self.staff,
            date_from=self.day,
            date_to=self.day,
            actor=self.director,
            generate_missing=False,
        )
        with self.assertRaisesMessage(ValidationError, "действующий расчетный лист"):
            grant_compensation_svc.revise_fixed_compensation(
                fixed,
                period_from=date(2026, 6, 1),
                period_to=self.day,
                accrual_on=self.day,
                amount=Decimal("5000.00"),
                note="Еще одна сумма",
                actor=self.director,
                reason="Попытка изменить закрепленную сумму.",
                expected_revision_id=fixed.current_revision_id,
            )
        self.assertEqual(sheet.status, PayrollSheet.Status.DRAFT)

    def test_budget_cannot_be_reduced_below_consumed(self):
        budget = self.create_budget()
        self.create_fixed(budget)
        sheet = payroll_svc.create_payroll_sheet_for_staff(
            self.staff,
            date_from=self.day,
            date_to=self.day,
            actor=self.director,
        )
        payroll_svc.approve_payroll_sheet(sheet, actor=self.director)

        with self.assertRaisesMessage(ValidationError, "ниже уже утвержденной суммы"):
            grant_compensation_svc.revise_payroll_budget(
                budget,
                starts_on=budget.starts_on,
                ends_on=budget.ends_on,
                planned_amount=Decimal("3999.00"),
                enforcement_mode=budget.enforcement_mode,
                note=budget.note,
                actor=self.director,
                reason="Попытка уменьшить ниже потребленного.",
                expected_revision_id=budget.current_revision_id,
            )


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row locks are required")
class GrantPayrollPostgreSQLConcurrencyTests(
    GrantPayrollFixtureMixin,
    TransactionTestCase,
):
    reset_sequences = True

    def test_approval_uses_contract_financial_lock_order(self):
        budget = self.create_budget()
        self.create_fixed(budget)
        sheet = payroll_svc.create_payroll_sheet_for_staff(
            self.staff,
            date_from=self.day,
            date_to=self.day,
            actor=self.director,
        )

        with CaptureQueriesContext(connection) as queries:
            payroll_svc.approve_payroll_sheet(sheet, actor=self.director)

        locking_sql = [
            query["sql"].lower() for query in queries if "for update" in query["sql"].lower()
        ]

        def lock_index(table: str) -> int:
            return next(index for index, sql in enumerate(locking_sql) if f'"{table}"' in sql)

        self.assertLess(
            lock_index("operations_fundingsource"),
            lock_index("operations_fundingpayrollbudget"),
        )
        self.assertLess(
            lock_index("operations_fundingpayrollbudget"),
            lock_index("operations_payrollsheet"),
        )
        self.assertLess(
            lock_index("operations_payrollsheet"),
            lock_index("operations_payrollsheetline"),
        )
        self.assertLess(
            lock_index("operations_payrollsheetline"),
            lock_index("operations_payrollaccrual"),
        )

    def test_approved_budget_event_rejects_queryset_mutation(self):
        budget = self.create_budget()
        self.create_fixed(budget)
        sheet = payroll_svc.create_payroll_sheet_for_staff(
            self.staff,
            date_from=self.day,
            date_to=self.day,
            actor=self.director,
        )
        payroll_svc.approve_payroll_sheet(sheet, actor=self.director)
        event = PayrollSheetLifecycleEvent.objects.get(payroll_sheet=sheet)

        with self.assertRaises(DatabaseError), transaction.atomic():
            PayrollSheetLifecycleEvent.objects.filter(pk=event.pk).update(
                note="Подмена provenance",
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            PayrollSheetLifecycleEvent.objects.filter(pk=event.pk).delete()

    def test_parallel_approvals_cannot_exceed_hard_budget(self):
        budget = self.create_budget(amount=Decimal("100.00"))
        second_staff = StaffMember.objects.create(full_name="Второй сотрудник проекта")
        self.create_fixed(
            budget,
            amount=Decimal("80.00"),
            assignment_label="Координатор первого направления",
        )
        self.create_fixed(
            budget,
            staff=second_staff,
            amount=Decimal("80.00"),
            assignment_label="Координатор второго направления",
        )
        sheets = []
        for staff in (self.staff, second_staff):
            sheets.append(
                payroll_svc.create_payroll_sheet_for_staff(
                    staff,
                    date_from=self.day,
                    date_to=self.day,
                    actor=self.director,
                )
            )

        barrier = Barrier(2)
        outcomes: Queue[tuple[str, str]] = Queue()

        def approve(sheet_id: int) -> None:
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                payroll_svc.approve_payroll_sheet(
                    PayrollSheet.objects.get(pk=sheet_id),
                    actor=User.objects.get(pk=self.director.pk),
                )
                outcomes.put(("approved", ""))
            except Exception as exc:
                outcomes.put((type(exc).__name__, str(exc)))
            finally:
                connections.close_all()

        threads = [Thread(target=approve, args=(sheet.pk,)) for sheet in sheets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

        results = [outcomes.get_nowait() for _ in threads]
        self.assertEqual(sum(kind == "approved" for kind, _ in results), 1, results)
        self.assertEqual(sum("жесткий бюджет" in detail for _, detail in results), 1, results)
        approved_total = sum(
            PayrollSheet.objects.filter(status=PayrollSheet.Status.APPROVED).values_list(
                "total_amount",
                flat=True,
            ),
            Decimal("0"),
        )
        self.assertEqual(approved_total, Decimal("80.00"))
        self.assertLessEqual(approved_total, budget.planned_amount)
