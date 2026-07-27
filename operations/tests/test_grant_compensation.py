"""Payroll-budget and fixed grant compensation contracts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO
from queue import Queue
from threading import Barrier, Thread
from time import sleep
from unittest import skipUnless

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import (
    DatabaseError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

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
from operations.services import (
    grant_compensation as compensation_svc,
    grant_plans as grant_plans_svc,
)


class GrantCompensationServiceTests(TestCase):
    def setUp(self):
        self.director = User.objects.create_superuser(
            "grant-comp-director",
            password="x",
        )
        self.admin = User.objects.create_user(
            "grant-comp-admin",
            password="x",
            is_staff=True,
        )
        self.funding = FundingSource.objects.create(
            name="Грант фиксированной оплаты",
            source_type=FundingSource.SourceType.GRANT,
        )
        self.service = Service.objects.create(
            name="Логопедическая помощь по гранту",
            code="GRANT-FIXED",
        )
        self.staff = StaffMember.objects.create(full_name="Специалист грантового проекта")
        self.period_start = date(2026, 1, 1)
        self.period_end = date(2026, 12, 31)

    def _create_budget(
        self,
        *,
        funding: FundingSource | None = None,
        starts_on: date | None = None,
        ends_on: date | None = None,
        amount: Decimal = Decimal("600000.00"),
    ) -> FundingPayrollBudget:
        return compensation_svc.create_payroll_budget(
            funding_source=funding or self.funding,
            starts_on=starts_on or self.period_start,
            ends_on=ends_on or self.period_end,
            planned_amount=amount,
            enforcement_mode=FundingPayrollBudget.EnforcementMode.HARD,
            note="Годовой бюджет оплаты труда",
            actor=self.director,
            reason="Утвержден бюджет оплаты труда.",
        )

    def _create_fixed(
        self,
        budget: FundingPayrollBudget,
        *,
        scope: str = GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY,
        service: Service | None = None,
        assignment_label: str = "",
        period_from: date = date(2026, 1, 1),
        period_to: date = date(2026, 3, 31),
        accrual_on: date = date(2026, 3, 31),
        amount: Decimal = Decimal("90000.00"),
    ) -> GrantFixedCompensation:
        if (
            service is None
            and scope == GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
        ):
            service = self.service
        return compensation_svc.create_fixed_compensation(
            payroll_budget=budget,
            staff_member=self.staff,
            compensation_scope=scope,
            service=service,
            assignment_label=assignment_label,
            period_from=period_from,
            period_to=period_to,
            accrual_on=accrual_on,
            amount=amount,
            note="Позиция проекта",
            actor=self.director,
            reason="Утверждена фиксированная оплата.",
        )

    def _create_session_allocation(
        self,
        *,
        starts_on: date = date(2026, 1, 1),
        ends_on: date = date(2026, 3, 31),
    ) -> FundingStaffAllocation:
        return grant_plans_svc.create_staff_allocation(
            service_quota=None,
            funding_source=self.funding,
            service=self.service,
            staff_member=self.staff,
            allocated_sessions=20,
            session_pay_amount=Decimal("500.00"),
            starts_on=starts_on,
            ends_on=ends_on,
            note="Сдельная оплата",
            actor=self.director,
            reason="Утверждена сдельная ставка.",
        )

    def test_create_budget_records_director_revision(self):
        budget = self._create_budget()

        revision = budget.revisions.get()
        self.assertEqual(revision.revision_number, 1)
        self.assertEqual(
            revision.event_type,
            FundingPayrollBudgetRevision.EventType.CREATED,
        )
        self.assertEqual(revision.actor, self.director)
        self.assertEqual(
            revision.actor_role_snapshot,
            FundingPayrollBudgetRevision.ActorRole.DIRECTOR,
        )
        self.assertEqual(budget.current_revision, revision)

    def test_administrator_cannot_create_budget_or_fixed_position(self):
        with self.assertRaises(PermissionDenied):
            compensation_svc.create_payroll_budget(
                funding_source=self.funding,
                starts_on=self.period_start,
                ends_on=self.period_end,
                planned_amount=Decimal("1000.00"),
                enforcement_mode=FundingPayrollBudget.EnforcementMode.HARD,
                note="",
                actor=self.admin,
                reason="Попытка администратора.",
            )

        budget = self._create_budget()
        with self.assertRaises(PermissionDenied):
            compensation_svc.create_fixed_compensation(
                payroll_budget=budget,
                staff_member=self.staff,
                compensation_scope=(
                    GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
                ),
                service=self.service,
                assignment_label="",
                period_from=date(2026, 1, 1),
                period_to=date(2026, 1, 31),
                accrual_on=date(2026, 1, 31),
                amount=Decimal("1000.00"),
                note="",
                actor=self.admin,
                reason="Попытка администратора.",
            )
        self.assertFalse(GrantFixedCompensation.objects.exists())

    def test_overlapping_budget_is_rejected_including_closed_history(self):
        first = self._create_budget(
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 6, 30),
        )
        compensation_svc.close_payroll_budget(
            first,
            actor=self.director,
            reason="Первый бюджет завершен.",
            expected_revision_id=first.current_revision_id,
        )

        with self.assertRaises(ValidationError) as caught:
            self._create_budget(
                starts_on=date(2026, 6, 30),
                ends_on=date(2026, 12, 31),
            )

        self.assertIn("starts_on", caught.exception.message_dict)
        self.assertEqual(FundingPayrollBudget.objects.count(), 1)

    def test_budget_revision_preserves_history_and_rejects_stale_token(self):
        budget = self._create_budget()
        stale_revision_id = budget.current_revision_id

        revised = compensation_svc.revise_payroll_budget(
            budget,
            starts_on=self.period_start,
            ends_on=self.period_end,
            planned_amount=Decimal("650000.00"),
            enforcement_mode=FundingPayrollBudget.EnforcementMode.WARNING,
            note="Уточненный бюджет",
            actor=self.director,
            reason="Получено дополнительное соглашение.",
            expected_revision_id=stale_revision_id,
        )

        with self.assertRaises(ValidationError) as caught:
            compensation_svc.revise_payroll_budget(
                budget,
                starts_on=self.period_start,
                ends_on=self.period_end,
                planned_amount=Decimal("700000.00"),
                enforcement_mode=FundingPayrollBudget.EnforcementMode.HARD,
                note="Устаревшая форма",
                actor=self.director,
                reason="Повторная редакция старой формы.",
                expected_revision_id=stale_revision_id,
            )

        self.assertIn("expected_revision_id", caught.exception.message_dict)
        revisions = list(revised.revisions.order_by("revision_number"))
        self.assertEqual(
            [item.planned_amount for item in revisions],
            [Decimal("600000.00"), Decimal("650000.00")],
        )
        self.assertEqual(revisions[1].supersedes, revisions[0])

    def test_budget_period_cannot_exclude_fixed_position(self):
        budget = self._create_budget()
        self._create_fixed(
            budget,
            period_from=date(2026, 2, 1),
            period_to=date(2026, 4, 30),
            accrual_on=date(2026, 4, 30),
        )

        with self.assertRaises(ValidationError) as caught:
            compensation_svc.revise_payroll_budget(
                budget,
                starts_on=date(2026, 3, 1),
                ends_on=self.period_end,
                planned_amount=budget.planned_amount,
                enforcement_mode=budget.enforcement_mode,
                note=budget.note,
                actor=self.director,
                reason="Попытка сузить период бюджета.",
                expected_revision_id=budget.current_revision_id,
            )

        self.assertIn("ends_on", caught.exception.message_dict)
        budget.refresh_from_db()
        self.assertEqual(budget.starts_on, self.period_start)

    def test_budget_close_requires_positions_closed_and_keeps_history(self):
        budget = self._create_budget()
        fixed = self._create_fixed(budget)

        with self.assertRaisesMessage(ValidationError, "Сначала закройте"):
            compensation_svc.close_payroll_budget(
                budget,
                actor=self.director,
                reason="Проект завершен.",
                expected_revision_id=budget.current_revision_id,
            )

        compensation_svc.close_fixed_compensation(
            fixed,
            actor=self.director,
            reason="Позиция завершена.",
            expected_revision_id=fixed.current_revision_id,
        )
        compensation_svc.close_payroll_budget(
            budget,
            actor=self.director,
            reason="Проект завершен.",
            expected_revision_id=budget.current_revision_id,
        )

        budget.refresh_from_db()
        fixed.refresh_from_db()
        self.assertEqual(
            budget.lifecycle_status,
            FundingPayrollBudget.LifecycleStatus.CLOSED,
        )
        self.assertEqual(
            fixed.lifecycle_status,
            GrantFixedCompensation.LifecycleStatus.CLOSED,
        )
        self.assertEqual(budget.revisions.count(), 2)
        self.assertEqual(fixed.revisions.count(), 2)

    def test_archived_source_rejects_budget_and_fixed_revisions(self):
        budget = self._create_budget()
        fixed = self._create_fixed(budget)
        self.funding.archive()

        with self.assertRaisesMessage(ValidationError, "только для чтения"):
            compensation_svc.revise_payroll_budget(
                budget,
                starts_on=budget.starts_on,
                ends_on=budget.ends_on,
                planned_amount=budget.planned_amount,
                enforcement_mode=budget.enforcement_mode,
                note=budget.note,
                actor=self.director,
                reason="Попытка изменить архив.",
                expected_revision_id=budget.current_revision_id,
            )
        with self.assertRaisesMessage(ValidationError, "только для чтения"):
            compensation_svc.revise_fixed_compensation(
                fixed,
                period_from=fixed.period_from,
                period_to=fixed.period_to,
                accrual_on=fixed.accrual_on,
                amount=fixed.amount,
                note=fixed.note,
                actor=self.director,
                reason="Попытка изменить архив.",
                expected_revision_id=fixed.current_revision_id,
            )

    def test_service_delivery_scope_normalizes_project_role_fields(self):
        budget = self._create_budget()

        fixed = self._create_fixed(
            budget,
            assignment_label="  Не должна сохраниться  ",
        )

        self.assertEqual(fixed.assignment_label, "")
        self.assertIsNone(fixed.assignment_key)
        self.assertEqual(fixed.service, self.service)

    def test_project_role_requires_label_and_normalizes_identity_key(self):
        budget = self._create_budget()

        with self.assertRaises(ValidationError) as caught:
            self._create_fixed(
                budget,
                scope=GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
                service=None,
                assignment_label="",
            )
        self.assertIn("assignment_label", caught.exception.message_dict)

        fixed = self._create_fixed(
            budget,
            scope=GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
            service=None,
            assignment_label="  Координатор   Проекта  ",
        )
        self.assertEqual(fixed.assignment_label, "Координатор   Проекта")
        self.assertEqual(fixed.assignment_key, "координатор проекта")

    def test_fixed_position_must_fit_budget_and_accrual_period(self):
        budget = self._create_budget(
            starts_on=date(2026, 2, 1),
            ends_on=date(2026, 11, 30),
        )

        with self.assertRaises(ValidationError) as outside_budget:
            self._create_fixed(
                budget,
                period_from=date(2026, 1, 1),
                period_to=date(2026, 3, 31),
                accrual_on=date(2026, 3, 31),
            )
        self.assertIn("period_to", outside_budget.exception.message_dict)

        with self.assertRaises(ValidationError) as outside_position:
            self._create_fixed(
                budget,
                period_from=date(2026, 2, 1),
                period_to=date(2026, 3, 31),
                accrual_on=date(2026, 4, 1),
            )
        self.assertIn("accrual_on", outside_position.exception.message_dict)

    def test_overlapping_fixed_identity_is_rejected(self):
        budget = self._create_budget()
        self._create_fixed(
            budget,
            period_from=date(2026, 1, 1),
            period_to=date(2026, 3, 31),
        )

        with self.assertRaises(ValidationError):
            self._create_fixed(
                budget,
                period_from=date(2026, 3, 31),
                period_to=date(2026, 6, 30),
                accrual_on=date(2026, 6, 30),
            )

    def test_different_project_roles_can_overlap_and_are_additive(self):
        budget = self._create_budget()
        self._create_session_allocation()

        coordinator = self._create_fixed(
            budget,
            scope=GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
            service=None,
            assignment_label="Координатор проекта",
        )
        supervisor = self._create_fixed(
            budget,
            scope=GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
            service=None,
            assignment_label="Супервизор проекта",
        )

        self.assertNotEqual(coordinator.assignment_key, supervisor.assignment_key)
        self.assertEqual(GrantFixedCompensation.objects.count(), 2)
        self.assertEqual(FundingStaffAllocation.objects.count(), 1)

    def test_session_allocation_blocks_fixed_service_delivery(self):
        self._create_session_allocation()
        budget = self._create_budget()

        with self.assertRaises(ValidationError) as caught:
            self._create_fixed(budget)

        self.assertIn("service", caught.exception.message_dict)
        self.assertFalse(GrantFixedCompensation.objects.exists())

    def test_fixed_service_delivery_blocks_session_allocation(self):
        budget = self._create_budget()
        self._create_fixed(budget)

        with self.assertRaises(ValidationError) as caught:
            self._create_session_allocation()

        self.assertIn("starts_on", caught.exception.message_dict)
        self.assertFalse(FundingStaffAllocation.objects.exists())

    def test_non_overlapping_fixed_and_session_allocation_are_allowed(self):
        budget = self._create_budget()
        self._create_fixed(
            budget,
            period_from=date(2026, 1, 1),
            period_to=date(2026, 3, 31),
        )

        allocation = self._create_session_allocation(
            starts_on=date(2026, 4, 1),
            ends_on=date(2026, 6, 30),
        )

        self.assertIsNotNone(allocation.pk)

    def test_session_allocation_revision_cannot_expand_into_fixed_period(self):
        allocation = self._create_session_allocation(
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 3, 31),
        )
        budget = self._create_budget()
        self._create_fixed(
            budget,
            period_from=date(2026, 4, 1),
            period_to=date(2026, 6, 30),
            accrual_on=date(2026, 6, 30),
        )

        with self.assertRaises(ValidationError) as caught:
            grant_plans_svc.revise_staff_allocation(
                allocation,
                allocated_sessions=20,
                session_pay_amount=allocation.session_pay_amount,
                starts_on=date(2026, 1, 1),
                ends_on=date(2026, 6, 30),
                note=allocation.note,
                actor=self.director,
                reason="Попытка расширить сдельный период.",
                expected_revision_id=allocation.current_revision_id,
            )

        self.assertIn("starts_on", caught.exception.message_dict)

    def test_fixed_revision_preserves_budget_provenance_and_history(self):
        budget = self._create_budget()
        fixed = self._create_fixed(budget)
        original_budget_revision = budget.current_revision
        compensation_svc.revise_payroll_budget(
            budget,
            starts_on=budget.starts_on,
            ends_on=budget.ends_on,
            planned_amount=Decimal("650000.00"),
            enforcement_mode=budget.enforcement_mode,
            note="Бюджет уточнен",
            actor=self.director,
            reason="Получено дополнительное соглашение.",
            expected_revision_id=budget.current_revision_id,
        )

        revised = compensation_svc.revise_fixed_compensation(
            fixed,
            period_from=fixed.period_from,
            period_to=fixed.period_to,
            accrual_on=fixed.accrual_on,
            amount=Decimal("95000.00"),
            note="Сумма уточнена",
            actor=self.director,
            reason="Утверждено изменение суммы.",
            expected_revision_id=fixed.current_revision_id,
        )

        revisions = list(revised.revisions.order_by("revision_number"))
        budget.refresh_from_db()
        self.assertEqual(
            revisions[0].budget_revision_at_decision,
            original_budget_revision,
        )
        self.assertEqual(
            revisions[1].budget_revision_at_decision,
            budget.current_revision,
        )
        self.assertEqual(
            [item.amount for item in revisions],
            [Decimal("90000.00"), Decimal("95000.00")],
        )
        self.assertEqual(revisions[1].supersedes, revisions[0])

    def test_direct_root_and_revision_mutation_or_delete_is_blocked(self):
        budget = self._create_budget()
        fixed = self._create_fixed(budget)

        budget.planned_amount = Decimal("1.00")
        with self.assertRaisesMessage(ValidationError, "нельзя изменять напрямую"):
            budget.save()
        fixed.amount = Decimal("1.00")
        with self.assertRaisesMessage(ValidationError, "нельзя изменять напрямую"):
            fixed.save()

        budget_revision = budget.revisions.get()
        budget_revision.note = "Попытка изменения"
        with self.assertRaisesMessage(ValidationError, "нельзя изменять"):
            budget_revision.save()
        with self.assertRaisesMessage(ValidationError, "нельзя удалить"):
            budget_revision.delete()

        fixed_revision = fixed.revisions.get()
        fixed_revision.note = "Попытка изменения"
        with self.assertRaisesMessage(ValidationError, "нельзя изменять"):
            fixed_revision.save()
        with self.assertRaisesMessage(ValidationError, "нельзя удалить"):
            fixed_revision.delete()

        with self.assertRaisesMessage(ValidationError, "нельзя удалить"):
            fixed.delete()
        with self.assertRaisesMessage(ValidationError, "нельзя удалить"):
            budget.delete()

    def test_integrity_command_accepts_service_created_plan(self):
        budget = self._create_budget()
        self._create_fixed(budget)
        output = StringIO()

        call_command("check_grant_plan_integrity", strict=True, stdout=output)

        self.assertIn("Findings: 0", output.getvalue())

    def test_integrity_command_rejects_missing_current_revision(self):
        budget = self._create_budget()
        if connection.vendor == "postgresql":
            with self.assertRaises(DatabaseError):
                FundingPayrollBudget.objects.filter(pk=budget.pk).update(
                    current_revision=None
                )
            return

        FundingPayrollBudget.objects.filter(pk=budget.pk).update(current_revision=None)
        with self.assertRaises(CommandError):
            call_command(
                "check_grant_plan_integrity",
                strict=True,
                stdout=StringIO(),
                stderr=StringIO(),
            )


class GrantCompensationPostgreSQLConstraintTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.director = User.objects.create_superuser(
            "grant-comp-pg-constraints",
            password="x",
        )
        self.funding = FundingSource.objects.create(
            name="Грант PostgreSQL ограничений",
            source_type=FundingSource.SourceType.GRANT,
        )
        self.service = Service.objects.create(
            name="Услуга PostgreSQL ограничений",
            code="GRANT-PG-CONSTRAINT",
        )
        self.staff = StaffMember.objects.create(full_name="Специалист PostgreSQL")

    def _create_budget(self) -> FundingPayrollBudget:
        return compensation_svc.create_payroll_budget(
            funding_source=self.funding,
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
            planned_amount=Decimal("500000.00"),
            enforcement_mode=FundingPayrollBudget.EnforcementMode.HARD,
            note="",
            actor=self.director,
            reason="Создан бюджет PostgreSQL.",
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "Exclusion constraint проверяется только на PostgreSQL.",
    )
    def test_budget_exclusion_constraint_blocks_service_bypass(self):
        self._create_budget()

        with self.assertRaises(DatabaseError):
            FundingPayrollBudget.objects.create(
                funding_source=self.funding,
                starts_on=date(2026, 6, 1),
                ends_on=date(2027, 5, 31),
                planned_amount=Decimal("100000.00"),
                enforcement_mode=FundingPayrollBudget.EnforcementMode.HARD,
            )

    @skipUnless(
        connection.vendor == "postgresql",
        "Revision trigger проверяется только на PostgreSQL.",
    )
    def test_revision_trigger_blocks_queryset_update_and_delete(self):
        budget = self._create_budget()
        revision = budget.current_revision

        with self.assertRaises(DatabaseError):
            FundingPayrollBudgetRevision.objects.filter(pk=revision.pk).update(
                note="Обход сервиса"
            )

        with self.assertRaises(DatabaseError):
            FundingPayrollBudgetRevision.objects.filter(pk=revision.pk).delete()

    @skipUnless(
        connection.vendor == "postgresql",
        "Budget-containment trigger проверяется только на PostgreSQL.",
    )
    def test_fixed_budget_guard_blocks_service_bypass(self):
        budget = self._create_budget()

        invalid = GrantFixedCompensation(
            payroll_budget=budget,
            staff_member=self.staff,
            compensation_scope=(
                GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
            ),
            service=self.service,
            period_from=date(2025, 12, 1),
            period_to=date(2026, 1, 31),
            accrual_on=date(2026, 1, 31),
            amount=Decimal("10000.00"),
        )
        with self.assertRaises(DatabaseError):
            GrantFixedCompensation.objects.bulk_create([invalid])

    @skipUnless(
        connection.vendor == "postgresql",
        "Межтабличные trigger-guards проверяются только на PostgreSQL.",
    )
    def test_cross_table_guards_block_bypass_in_both_directions(self):
        budget = self._create_budget()
        allocation = grant_plans_svc.create_staff_allocation(
            service_quota=None,
            funding_source=self.funding,
            service=self.service,
            staff_member=self.staff,
            allocated_sessions=10,
            session_pay_amount=Decimal("500.00"),
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 3, 31),
            note="",
            actor=self.director,
            reason="Создана сдельная ставка.",
        )

        with self.assertRaises(DatabaseError):
            GrantFixedCompensation.objects.create(
                payroll_budget=budget,
                staff_member=self.staff,
                compensation_scope=(
                    GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
                ),
                service=self.service,
                period_from=date(2026, 1, 1),
                period_to=date(2026, 3, 31),
                accrual_on=date(2026, 3, 31),
                amount=Decimal("50000.00"),
            )

        self.assertIsNotNone(allocation.pk)
        fixed = compensation_svc.create_fixed_compensation(
            payroll_budget=budget,
            staff_member=self.staff,
            compensation_scope=(
                GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
            ),
            service=self.service,
            assignment_label="",
            period_from=date(2026, 4, 1),
            period_to=date(2026, 6, 30),
            accrual_on=date(2026, 6, 30),
            amount=Decimal("50000.00"),
            note="",
            actor=self.director,
            reason="Создана фиксированная позиция.",
        )
        self.assertIsNotNone(fixed.pk)

        with self.assertRaises(DatabaseError):
            FundingStaffAllocation.objects.create(
                funding_source=self.funding,
                service=self.service,
                staff_member=self.staff,
                allocated_sessions=10,
                session_pay_amount=Decimal("500.00"),
                starts_on=date(2026, 4, 1),
                ends_on=date(2026, 6, 30),
            )

    @skipUnless(
        connection.vendor == "postgresql",
        "Root projection guards проверяются только на PostgreSQL.",
    )
    def test_root_identity_and_projection_guards_block_queryset_bypass(self):
        budget = self._create_budget()
        other_funding = FundingSource.objects.create(
            name="Другой грант PostgreSQL",
            source_type=FundingSource.SourceType.GRANT,
        )

        with self.assertRaises(DatabaseError):
            FundingPayrollBudget.objects.filter(pk=budget.pk).update(
                funding_source=other_funding
            )
        with self.assertRaises(DatabaseError):
            FundingPayrollBudget.objects.filter(pk=budget.pk).update(
                planned_amount=Decimal("1.00")
            )

        previous_budget_revision = budget.current_revision
        budget = compensation_svc.revise_payroll_budget(
            budget,
            starts_on=budget.starts_on,
            ends_on=budget.ends_on,
            planned_amount=Decimal("490000.00"),
            enforcement_mode=budget.enforcement_mode,
            note="Уточненный бюджет.",
            actor=self.director,
            reason="Проверяется движение указателя редакции только вперед.",
            expected_revision_id=previous_budget_revision.pk,
        )
        with self.assertRaises(DatabaseError):
            FundingPayrollBudget.objects.filter(pk=budget.pk).update(
                starts_on=previous_budget_revision.starts_on,
                ends_on=previous_budget_revision.ends_on,
                planned_amount=previous_budget_revision.planned_amount,
                enforcement_mode=previous_budget_revision.enforcement_mode,
                lifecycle_status=previous_budget_revision.lifecycle_status,
                note=previous_budget_revision.note,
                current_revision=previous_budget_revision,
            )

        fixed = compensation_svc.create_fixed_compensation(
            payroll_budget=budget,
            staff_member=self.staff,
            compensation_scope=(
                GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
            ),
            service=self.service,
            assignment_label="",
            period_from=date(2026, 1, 1),
            period_to=date(2026, 3, 31),
            accrual_on=date(2026, 3, 31),
            amount=Decimal("50000.00"),
            note="",
            actor=self.director,
            reason="Создана позиция для проверки root guard.",
        )
        other_staff = StaffMember.objects.create(full_name="Другой специалист PostgreSQL")

        with self.assertRaises(DatabaseError):
            GrantFixedCompensation.objects.filter(pk=fixed.pk).update(
                staff_member=other_staff
            )
        with self.assertRaises(DatabaseError):
            GrantFixedCompensation.objects.filter(pk=fixed.pk).update(
                amount=Decimal("1.00")
            )

        previous_fixed_revision = fixed.current_revision
        fixed = compensation_svc.revise_fixed_compensation(
            fixed,
            period_from=fixed.period_from,
            period_to=fixed.period_to,
            accrual_on=fixed.accrual_on,
            amount=Decimal("51000.00"),
            note="Уточненная фиксированная позиция.",
            actor=self.director,
            reason="Проверяется движение указателя редакции только вперед.",
            expected_revision_id=previous_fixed_revision.pk,
        )
        with self.assertRaises(DatabaseError):
            GrantFixedCompensation.objects.filter(pk=fixed.pk).update(
                period_from=previous_fixed_revision.period_from,
                period_to=previous_fixed_revision.period_to,
                accrual_on=previous_fixed_revision.accrual_on,
                amount=previous_fixed_revision.amount,
                lifecycle_status=previous_fixed_revision.lifecycle_status,
                note=previous_fixed_revision.note,
                current_revision=previous_fixed_revision,
            )

    @skipUnless(
        connection.vendor == "postgresql",
        "Scope CHECK проверяется только на PostgreSQL.",
    )
    def test_project_role_requires_nonempty_assignment_key_at_database_level(self):
        budget = self._create_budget()
        invalid = GrantFixedCompensation(
            payroll_budget=budget,
            staff_member=self.staff,
            compensation_scope=GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
            service=None,
            assignment_label="Координатор",
            assignment_key="",
            period_from=date(2026, 1, 1),
            period_to=date(2026, 3, 31),
            accrual_on=date(2026, 3, 31),
            amount=Decimal("50000.00"),
            lifecycle_status=GrantFixedCompensation.LifecycleStatus.ACTIVE,
            note="",
        )

        with self.assertRaises(DatabaseError):
            GrantFixedCompensation.objects.bulk_create([invalid])

    @skipUnless(
        connection.vendor == "postgresql",
        "Revision chain guards проверяются только на PostgreSQL.",
    )
    def test_revision_chain_guards_block_cross_root_supersedes(self):
        first_budget = self._create_budget()
        second_funding = FundingSource.objects.create(
            name="Второй грант цепочки редакций",
            source_type=FundingSource.SourceType.GRANT,
        )
        second_budget = compensation_svc.create_payroll_budget(
            funding_source=second_funding,
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
            planned_amount=Decimal("300000.00"),
            enforcement_mode=FundingPayrollBudget.EnforcementMode.HARD,
            note="",
            actor=self.director,
            reason="Создан второй бюджет для проверки цепочки.",
        )
        invalid_budget_revision = FundingPayrollBudgetRevision(
            payroll_budget=second_budget,
            revision_number=2,
            event_type=FundingPayrollBudgetRevision.EventType.REVISED,
            starts_on=second_budget.starts_on,
            ends_on=second_budget.ends_on,
            planned_amount=second_budget.planned_amount,
            enforcement_mode=second_budget.enforcement_mode,
            lifecycle_status=second_budget.lifecycle_status,
            note=second_budget.note,
            actor=self.director,
            actor_role_snapshot=FundingPayrollBudgetRevision.ActorRole.DIRECTOR,
            reason="Попытка связать разные корни бюджета.",
            supersedes=first_budget.current_revision,
            decided_at=second_budget.current_revision.decided_at,
        )

        with self.assertRaises(DatabaseError):
            FundingPayrollBudgetRevision.objects.bulk_create([invalid_budget_revision])

        first_fixed = compensation_svc.create_fixed_compensation(
            payroll_budget=first_budget,
            staff_member=self.staff,
            compensation_scope=GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
            service=None,
            assignment_label="Координатор первого проекта",
            period_from=date(2026, 1, 1),
            period_to=date(2026, 3, 31),
            accrual_on=date(2026, 3, 31),
            amount=Decimal("50000.00"),
            note="",
            actor=self.director,
            reason="Создана первая роль для проверки цепочки.",
        )
        second_fixed = compensation_svc.create_fixed_compensation(
            payroll_budget=second_budget,
            staff_member=self.staff,
            compensation_scope=GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
            service=None,
            assignment_label="Координатор второго проекта",
            period_from=date(2026, 1, 1),
            period_to=date(2026, 3, 31),
            accrual_on=date(2026, 3, 31),
            amount=Decimal("40000.00"),
            note="",
            actor=self.director,
            reason="Создана вторая роль для проверки цепочки.",
        )
        invalid_fixed_revision = GrantFixedCompensationRevision(
            fixed_compensation=second_fixed,
            revision_number=2,
            event_type=GrantFixedCompensationRevision.EventType.REVISED,
            budget_revision_at_decision=second_budget.current_revision,
            compensation_scope=second_fixed.compensation_scope,
            service=second_fixed.service,
            assignment_label=second_fixed.assignment_label,
            assignment_key=second_fixed.assignment_key,
            period_from=second_fixed.period_from,
            period_to=second_fixed.period_to,
            accrual_on=second_fixed.accrual_on,
            amount=second_fixed.amount,
            lifecycle_status=second_fixed.lifecycle_status,
            note=second_fixed.note,
            actor=self.director,
            actor_role_snapshot=GrantFixedCompensationRevision.ActorRole.DIRECTOR,
            reason="Попытка связать разные корни позиции.",
            supersedes=first_fixed.current_revision,
            decided_at=second_fixed.current_revision.decided_at,
        )

        with self.assertRaises(DatabaseError):
            GrantFixedCompensationRevision.objects.bulk_create([invalid_fixed_revision])
    @skipUnless(
        connection.vendor == "postgresql",
        "Deferred terminal guards проверяются только на PostgreSQL.",
    )
    def test_terminal_guards_block_missing_current_and_dangling_successors(self):
        root_without_revision = FundingPayrollBudget(
            funding_source=self.funding,
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
            planned_amount=Decimal("500000.00"),
            enforcement_mode=FundingPayrollBudget.EnforcementMode.HARD,
        )
        with self.assertRaises(DatabaseError):
            FundingPayrollBudget.objects.bulk_create([root_without_revision])

        budget = self._create_budget()
        dangling_budget_revision = FundingPayrollBudgetRevision(
            payroll_budget=budget,
            revision_number=2,
            event_type=FundingPayrollBudgetRevision.EventType.REVISED,
            starts_on=budget.starts_on,
            ends_on=budget.ends_on,
            planned_amount=Decimal("490000.00"),
            enforcement_mode=budget.enforcement_mode,
            lifecycle_status=budget.lifecycle_status,
            note="Неподключенная редакция.",
            actor=self.director,
            actor_role_snapshot=FundingPayrollBudgetRevision.ActorRole.DIRECTOR,
            reason="Проверка терминального указателя бюджета.",
            supersedes=budget.current_revision,
            decided_at=budget.current_revision.decided_at,
        )
        with self.assertRaises(DatabaseError):
            FundingPayrollBudgetRevision.objects.bulk_create([dangling_budget_revision])

        fixed_root_without_revision = GrantFixedCompensation(
            payroll_budget=budget,
            staff_member=self.staff,
            compensation_scope=(
                GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
            ),
            service=self.service,
            period_from=date(2026, 1, 1),
            period_to=date(2026, 3, 31),
            accrual_on=date(2026, 3, 31),
            amount=Decimal("50000.00"),
        )
        with self.assertRaises(DatabaseError):
            GrantFixedCompensation.objects.bulk_create([fixed_root_without_revision])

        fixed = compensation_svc.create_fixed_compensation(
            payroll_budget=budget,
            staff_member=self.staff,
            compensation_scope=(
                GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
            ),
            service=self.service,
            assignment_label="",
            period_from=date(2026, 1, 1),
            period_to=date(2026, 3, 31),
            accrual_on=date(2026, 3, 31),
            amount=Decimal("50000.00"),
            note="",
            actor=self.director,
            reason="Создана позиция для terminal guard.",
        )
        dangling_fixed_revision = GrantFixedCompensationRevision(
            fixed_compensation=fixed,
            revision_number=2,
            event_type=GrantFixedCompensationRevision.EventType.REVISED,
            budget_revision_at_decision=budget.current_revision,
            compensation_scope=fixed.compensation_scope,
            service=fixed.service,
            assignment_label=fixed.assignment_label,
            assignment_key=fixed.assignment_key,
            period_from=fixed.period_from,
            period_to=fixed.period_to,
            accrual_on=fixed.accrual_on,
            amount=Decimal("51000.00"),
            lifecycle_status=fixed.lifecycle_status,
            note="Неподключенная редакция.",
            actor=self.director,
            actor_role_snapshot=GrantFixedCompensationRevision.ActorRole.DIRECTOR,
            reason="Проверка терминального указателя позиции.",
            supersedes=fixed.current_revision,
            decided_at=fixed.current_revision.decided_at,
        )
        with self.assertRaises(DatabaseError):
            GrantFixedCompensationRevision.objects.bulk_create(
                [dangling_fixed_revision]
            )

    @skipUnless(
        connection.vendor == "postgresql",
        "Lifecycle/provenance guards проверяются только на PostgreSQL.",
    )
    def test_revision_guards_block_stale_budget_reopen_and_noncanonical_role(self):
        budget = self._create_budget()
        initial_budget_revision = budget.current_revision
        fixed = compensation_svc.create_fixed_compensation(
            payroll_budget=budget,
            staff_member=self.staff,
            compensation_scope=GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
            service=None,
            assignment_label="Координатор проекта",
            period_from=date(2026, 1, 1),
            period_to=date(2026, 3, 31),
            accrual_on=date(2026, 3, 31),
            amount=Decimal("50000.00"),
            note="",
            actor=self.director,
            reason="Создана роль для проверки provenance.",
        )
        invalid_key_root = GrantFixedCompensation(
            payroll_budget=budget,
            staff_member=self.staff,
            compensation_scope=GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
            service=None,
            assignment_label="Другая проектная роль",
            assignment_key="ДРУГАЯ ПРОЕКТНАЯ РОЛЬ",
            period_from=date(2026, 4, 1),
            period_to=date(2026, 6, 30),
            accrual_on=date(2026, 6, 30),
            amount=Decimal("40000.00"),
        )
        with self.assertRaisesMessage(
            DatabaseError,
            "assignment key must be canonical",
        ):
            GrantFixedCompensation.objects.bulk_create([invalid_key_root])

        budget = compensation_svc.revise_payroll_budget(
            budget,
            starts_on=budget.starts_on,
            ends_on=budget.ends_on,
            planned_amount=Decimal("490000.00"),
            enforcement_mode=budget.enforcement_mode,
            note="Уточненный бюджет.",
            actor=self.director,
            reason="Создана новая редакция бюджета.",
            expected_revision_id=initial_budget_revision.pk,
        )
        stale_provenance = GrantFixedCompensationRevision(
            fixed_compensation=fixed,
            revision_number=2,
            event_type=GrantFixedCompensationRevision.EventType.REVISED,
            budget_revision_at_decision=initial_budget_revision,
            compensation_scope=fixed.compensation_scope,
            service=fixed.service,
            assignment_label=fixed.assignment_label,
            assignment_key=fixed.assignment_key,
            period_from=fixed.period_from,
            period_to=fixed.period_to,
            accrual_on=fixed.accrual_on,
            amount=Decimal("51000.00"),
            lifecycle_status=fixed.lifecycle_status,
            note="Устаревшая provenance.",
            actor=self.director,
            actor_role_snapshot=GrantFixedCompensationRevision.ActorRole.DIRECTOR,
            reason="Попытка сохранить устаревшую редакцию бюджета.",
            supersedes=fixed.current_revision,
            decided_at=fixed.current_revision.decided_at,
        )
        with self.assertRaises(DatabaseError):
            GrantFixedCompensationRevision.objects.bulk_create([stale_provenance])

        fixed = compensation_svc.close_fixed_compensation(
            fixed,
            actor=self.director,
            reason="Позиция закрыта для проверки повторного открытия.",
            expected_revision_id=fixed.current_revision_id,
        )
        budget = compensation_svc.close_payroll_budget(
            budget,
            actor=self.director,
            reason="Бюджет закрыт для проверки повторного открытия.",
            expected_revision_id=budget.current_revision_id,
        )
        reopen_budget = FundingPayrollBudgetRevision(
            payroll_budget=budget,
            revision_number=budget.current_revision.revision_number + 1,
            event_type=FundingPayrollBudgetRevision.EventType.REVISED,
            starts_on=budget.starts_on,
            ends_on=budget.ends_on,
            planned_amount=budget.planned_amount,
            enforcement_mode=budget.enforcement_mode,
            lifecycle_status=FundingPayrollBudget.LifecycleStatus.ACTIVE,
            note=budget.note,
            actor=self.director,
            actor_role_snapshot=FundingPayrollBudgetRevision.ActorRole.DIRECTOR,
            reason="Попытка повторно открыть бюджет.",
            supersedes=budget.current_revision,
            decided_at=budget.current_revision.decided_at,
        )
        with self.assertRaises(DatabaseError):
            FundingPayrollBudgetRevision.objects.bulk_create([reopen_budget])

        reopen_fixed = GrantFixedCompensationRevision(
            fixed_compensation=fixed,
            revision_number=fixed.current_revision.revision_number + 1,
            event_type=GrantFixedCompensationRevision.EventType.REVISED,
            budget_revision_at_decision=budget.current_revision,
            compensation_scope=fixed.compensation_scope,
            service=fixed.service,
            assignment_label=fixed.assignment_label,
            assignment_key=fixed.assignment_key,
            period_from=fixed.period_from,
            period_to=fixed.period_to,
            accrual_on=fixed.accrual_on,
            amount=fixed.amount,
            lifecycle_status=GrantFixedCompensation.LifecycleStatus.ACTIVE,
            note=fixed.note,
            actor=self.director,
            actor_role_snapshot=GrantFixedCompensationRevision.ActorRole.DIRECTOR,
            reason="Попытка повторно открыть фиксированную позицию.",
            supersedes=fixed.current_revision,
            decided_at=fixed.current_revision.decided_at,
        )
        with self.assertRaises(DatabaseError):
            GrantFixedCompensationRevision.objects.bulk_create([reopen_fixed])

class GrantCompensationPostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.director = User.objects.create_superuser(
            "grant-comp-pg-concurrency",
            password="x",
        )
        self.funding = FundingSource.objects.create(
            name="Конкурентный грант оплаты труда",
            source_type=FundingSource.SourceType.GRANT,
        )
        self.service = Service.objects.create(
            name="Конкурентная услуга fixed",
            code="GRANT-PG-FIXED",
        )
        self.staff = StaffMember.objects.create(full_name="Конкурентный специалист fixed")

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентное создание бюджетов проверяется только на PostgreSQL.",
    )
    def test_two_first_overlapping_budgets_are_serialized_by_source(self):
        barrier = Barrier(2)
        outcomes = Queue()

        def create_budget(amount: str) -> None:
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                budget = compensation_svc.create_payroll_budget(
                    funding_source=FundingSource.all_objects.get(pk=self.funding.pk),
                    starts_on=date(2026, 1, 1),
                    ends_on=date(2026, 12, 31),
                    planned_amount=Decimal(amount),
                    enforcement_mode=FundingPayrollBudget.EnforcementMode.HARD,
                    note="",
                    actor=User.objects.get(pk=self.director.pk),
                    reason="Параллельное создание бюджета.",
                )
                outcomes.put(budget.pk)
            except (DatabaseError, ValidationError):
                outcomes.put("rejected")
            finally:
                connections.close_all()

        threads = [
            Thread(target=create_budget, args=("100000.00",)),
            Thread(target=create_budget, args=("200000.00",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertEqual(sum(isinstance(result, int) for result in results), 1, results)
        self.assertEqual(results.count("rejected"), 1, results)
        self.assertEqual(FundingPayrollBudget.objects.count(), 1)

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентное создание fixed-позиций проверяется только на PostgreSQL.",
    )
    def test_two_first_overlapping_fixed_positions_are_serialized(self):
        budget = compensation_svc.create_payroll_budget(
            funding_source=self.funding,
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
            planned_amount=Decimal("500000.00"),
            enforcement_mode=FundingPayrollBudget.EnforcementMode.HARD,
            note="",
            actor=self.director,
            reason="Создан бюджет конкуренции.",
        )
        barrier = Barrier(2)
        outcomes = Queue()

        def create_fixed(amount: str) -> None:
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                fixed = compensation_svc.create_fixed_compensation(
                    payroll_budget=FundingPayrollBudget.objects.get(pk=budget.pk),
                    staff_member=StaffMember.objects.get(pk=self.staff.pk),
                    compensation_scope=(
                        GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
                    ),
                    service=Service.objects.get(pk=self.service.pk),
                    assignment_label="",
                    period_from=date(2026, 1, 1),
                    period_to=date(2026, 3, 31),
                    accrual_on=date(2026, 3, 31),
                    amount=Decimal(amount),
                    note="",
                    actor=User.objects.get(pk=self.director.pk),
                    reason="Параллельное создание позиции.",
                )
                outcomes.put(fixed.pk)
            except (DatabaseError, ValidationError):
                outcomes.put("rejected")
            finally:
                connections.close_all()

        threads = [
            Thread(target=create_fixed, args=("50000.00",)),
            Thread(target=create_fixed, args=("60000.00",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertEqual(sum(isinstance(result, int) for result in results), 1, results)
        self.assertEqual(results.count("rejected"), 1, results)
        self.assertEqual(GrantFixedCompensation.objects.count(), 1)

    @skipUnless(
        connection.vendor == "postgresql",
        "Budget/fixed TOCTOU проверяется только на PostgreSQL.",
    )
    def test_direct_fixed_insert_rechecks_budget_after_source_lock(self):
        budget = compensation_svc.create_payroll_budget(
            funding_source=self.funding,
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
            planned_amount=Decimal("500000.00"),
            enforcement_mode=FundingPayrollBudget.EnforcementMode.HARD,
            note="",
            actor=self.director,
            reason="Создан бюджет проверки TOCTOU.",
        )
        barrier = Barrier(2)
        outcomes = Queue()

        def close_budget() -> None:
            close_old_connections()
            try:
                with transaction.atomic():
                    FundingSource.all_objects.select_for_update().get(pk=self.funding.pk)
                    barrier.wait(timeout=10)
                    sleep(0.5)
                    compensation_svc.close_payroll_budget(
                        FundingPayrollBudget.objects.get(pk=budget.pk),
                        actor=User.objects.get(pk=self.director.pk),
                        reason="Бюджет закрыт во время конкурентной вставки.",
                        expected_revision_id=budget.current_revision_id,
                    )
                outcomes.put("closed")
            except (DatabaseError, ValidationError):
                outcomes.put("close_failed")
            finally:
                connections.close_all()

        def insert_fixed_directly() -> None:
            close_old_connections()
            try:
                with transaction.atomic():
                    barrier.wait(timeout=10)
                    with connections["default"].cursor() as cursor:
                        cursor.execute(
                            """
                            INSERT INTO operations_grantfixedcompensation (
                                created_at,
                                updated_at,
                                payroll_budget_id,
                                staff_member_id,
                                compensation_scope,
                                service_id,
                                assignment_label,
                                assignment_key,
                                period_from,
                                period_to,
                                accrual_on,
                                amount,
                                lifecycle_status,
                                note,
                                current_revision_id
                            ) VALUES (
                                CURRENT_TIMESTAMP,
                                CURRENT_TIMESTAMP,
                                %s,
                                %s,
                                %s,
                                %s,
                                '',
                                NULL,
                                %s,
                                %s,
                                %s,
                                %s,
                                %s,
                                '',
                                NULL
                            )
                            """,
                            [
                                budget.pk,
                                self.staff.pk,
                                GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY,
                                self.service.pk,
                                date(2026, 1, 1),
                                date(2026, 3, 31),
                                date(2026, 3, 31),
                                Decimal("50000.00"),
                                GrantFixedCompensation.LifecycleStatus.ACTIVE,
                            ],
                        )
                outcomes.put("inserted")
            except DatabaseError:
                outcomes.put("rejected")
            finally:
                connections.close_all()

        threads = [Thread(target=close_budget), Thread(target=insert_fixed_directly)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertCountEqual(results, ["closed", "rejected"])
        budget.refresh_from_db()
        self.assertEqual(
            budget.lifecycle_status,
            FundingPayrollBudget.LifecycleStatus.CLOSED,
        )
        self.assertFalse(GrantFixedCompensation.objects.exists())

class GrantCompensationViewTests(TestCase):
    def setUp(self):
        self.director = User.objects.create_superuser(
            "grant-comp-view-director",
            password="x",
        )
        self.operator = User.objects.create_user(
            "grant-comp-view-operator",
            password="x",
            is_staff=True,
        )
        self.specialist = User.objects.create_user(
            "grant-comp-view-specialist",
            password="x",
        )
        self.funding = FundingSource.objects.create(
            name="Грант для UI фиксированной оплаты",
            source_type=FundingSource.SourceType.GRANT,
        )
        self.service = Service.objects.create(
            name="Грантовая услуга для UI",
            code="GRANT-FIXED-UI",
        )
        self.staff = StaffMember.objects.create(full_name="Сотрудник проекта для UI")

    def _create_budget(self) -> FundingPayrollBudget:
        return compensation_svc.create_payroll_budget(
            funding_source=self.funding,
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
            planned_amount=Decimal("500000.00"),
            enforcement_mode=FundingPayrollBudget.EnforcementMode.HARD,
            note="Бюджет из view-теста",
            actor=self.director,
            reason="Утвержден бюджет для проверки интерфейса.",
        )

    def _create_fixed(
        self,
        budget: FundingPayrollBudget,
        *,
        scope: str = GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY,
    ) -> GrantFixedCompensation:
        is_service = scope == GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
        return compensation_svc.create_fixed_compensation(
            payroll_budget=budget,
            staff_member=self.staff,
            compensation_scope=scope,
            service=self.service if is_service else None,
            assignment_label="Координатор проекта" if not is_service else "",
            period_from=date(2026, 1, 1),
            period_to=date(2026, 3, 31),
            accrual_on=date(2026, 3, 31),
            amount=Decimal("90000.00"),
            note="Позиция из view-теста",
            actor=self.director,
            reason="Утверждена позиция для проверки интерфейса.",
        )

    def _report_params(self) -> dict[str, object]:
        return {
            "funding": self.funding.pk,
            "date_from": "2026-01-01",
            "date_to": "2026-12-31",
        }

    def test_director_creates_budget_and_fixed_project_role_through_views(self):
        self.client.force_login(self.director)

        budget_response = self.client.post(
            reverse("funding_payroll_budget_create"),
            {
                "funding_source": self.funding.pk,
                "starts_on": "2026-01-01",
                "ends_on": "2026-12-31",
                "planned_amount": "500000.00",
                "enforcement_mode": FundingPayrollBudget.EnforcementMode.HARD,
                "note": "Годовой бюджет",
                "reason": "Руководитель утвердил годовой бюджет.",
            },
        )

        self.assertEqual(budget_response.status_code, 302)
        budget = FundingPayrollBudget.objects.get(funding_source=self.funding)
        self.assertEqual(budget.revisions.count(), 1)

        fixed_response = self.client.post(
            reverse("grant_fixed_compensation_create"),
            {
                "payroll_budget": budget.pk,
                "staff_member": self.staff.pk,
                "compensation_scope": GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
                "service": "",
                "assignment_label": "Координатор проекта",
                "period_from": "2026-01-01",
                "period_to": "2026-03-31",
                "accrual_on": "2026-03-31",
                "amount": "90000.00",
                "note": "Фиксированная проектная роль",
                "reason": "Руководитель утвердил проектную роль.",
            },
        )

        self.assertEqual(fixed_response.status_code, 302)
        fixed = GrantFixedCompensation.objects.get(payroll_budget=budget)
        self.assertEqual(
            fixed.compensation_scope,
            GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
        )
        self.assertIsNone(fixed.service_id)
        self.assertEqual(fixed.assignment_label, "Координатор проекта")
        self.assertEqual(fixed.revisions.count(), 1)

    def test_fixed_create_rejects_oversized_ids_without_server_error(self):
        oversized_id = "9" * 5000
        self.client.force_login(self.director)

        get_response = self.client.get(
            reverse("grant_fixed_compensation_create"),
            {"funding": oversized_id},
        )
        post_response = self.client.post(
            reverse("grant_fixed_compensation_create"),
            {
                "payroll_budget": oversized_id,
                "staff_member": self.staff.pk,
                "compensation_scope": (
                    GrantFixedCompensation.CompensationScope.PROJECT_ROLE
                ),
                "service": "",
                "assignment_label": "Координатор проекта",
                "period_from": "2026-01-01",
                "period_to": "2026-03-31",
                "accrual_on": "2026-03-31",
                "amount": "90000.00",
                "note": "",
                "reason": "Проверка слишком длинного идентификатора.",
            },
        )

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(post_response.status_code, 200)
        self.assertFalse(GrantFixedCompensation.objects.exists())
    def test_director_edits_fixed_position_and_preserves_identity(self):
        budget = self._create_budget()
        fixed = self._create_fixed(
            budget,
            scope=GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
        )
        self.client.force_login(self.director)

        response = self.client.post(
            reverse("grant_fixed_compensation_edit", args=[fixed.pk]),
            {
                "payroll_budget": budget.pk,
                "staff_member": self.staff.pk,
                "compensation_scope": fixed.compensation_scope,
                "service": "",
                "assignment_label": fixed.assignment_label,
                "period_from": "2026-01-01",
                "period_to": "2026-04-30",
                "accrual_on": "2026-04-30",
                "amount": "120000.00",
                "note": "Уточненная позиция",
                "reason": "Руководитель уточнил срок и сумму позиции.",
                "expected_revision_id": fixed.current_revision_id,
            },
        )

        self.assertEqual(response.status_code, 302)
        fixed.refresh_from_db()
        self.assertEqual(fixed.staff_member, self.staff)
        self.assertEqual(fixed.assignment_label, "Координатор проекта")
        self.assertEqual(fixed.period_to, date(2026, 4, 30))
        self.assertEqual(fixed.amount, Decimal("120000.00"))
        self.assertEqual(fixed.revisions.count(), 2)

    def test_stale_budget_form_does_not_overwrite_newer_revision(self):
        budget = self._create_budget()
        stale_revision_id = budget.current_revision_id
        compensation_svc.revise_payroll_budget(
            budget,
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
            planned_amount=Decimal("550000.00"),
            enforcement_mode=FundingPayrollBudget.EnforcementMode.HARD,
            note="Новая редакция",
            actor=self.director,
            reason="Параллельно уточнен бюджет проекта.",
            expected_revision_id=stale_revision_id,
        )
        self.client.force_login(self.director)

        response = self.client.post(
            reverse("funding_payroll_budget_edit", args=[budget.pk]),
            {
                "funding_source": self.funding.pk,
                "starts_on": "2026-01-01",
                "ends_on": "2026-12-31",
                "planned_amount": "400000.00",
                "enforcement_mode": FundingPayrollBudget.EnforcementMode.HARD,
                "note": "Устаревшая форма",
                "reason": "Попытка сохранить устаревшую форму.",
                "expected_revision_id": stale_revision_id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "План уже изменен другим пользователем")
        budget.refresh_from_db()
        self.assertEqual(budget.planned_amount, Decimal("550000.00"))
        self.assertEqual(budget.revisions.count(), 2)

    def test_stale_budget_close_does_not_close_newer_revision(self):
        budget = self._create_budget()
        stale_revision_id = budget.current_revision_id
        budget = compensation_svc.revise_payroll_budget(
            budget,
            starts_on=budget.starts_on,
            ends_on=budget.ends_on,
            planned_amount=Decimal("550000.00"),
            enforcement_mode=budget.enforcement_mode,
            note="Новая редакция перед закрытием",
            actor=self.director,
            reason="Бюджет уточнен перед закрытием.",
            expected_revision_id=stale_revision_id,
        )
        self.client.force_login(self.director)

        response = self.client.post(
            reverse("funding_payroll_budget_close", args=[budget.pk]),
            {
                "reason": "Попытка закрыть устаревшую редакцию.",
                "expected_revision_id": stale_revision_id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "План уже изменен другим пользователем")
        budget.refresh_from_db()
        self.assertEqual(budget.lifecycle_status, FundingPayrollBudget.LifecycleStatus.ACTIVE)
        self.assertEqual(budget.revisions.count(), 2)

    def test_stale_fixed_edit_does_not_overwrite_newer_revision(self):
        budget = self._create_budget()
        fixed = self._create_fixed(budget)
        stale_revision_id = fixed.current_revision_id
        fixed = compensation_svc.revise_fixed_compensation(
            fixed,
            period_from=fixed.period_from,
            period_to=date(2026, 4, 30),
            accrual_on=date(2026, 4, 30),
            amount=Decimal("100000.00"),
            note="Новая редакция позиции",
            actor=self.director,
            reason="Позиция уточнена перед второй формой.",
            expected_revision_id=stale_revision_id,
        )
        self.client.force_login(self.director)

        response = self.client.post(
            reverse("grant_fixed_compensation_edit", args=[fixed.pk]),
            {
                "payroll_budget": budget.pk,
                "staff_member": self.staff.pk,
                "compensation_scope": fixed.compensation_scope,
                "service": self.service.pk,
                "assignment_label": "",
                "period_from": "2026-01-01",
                "period_to": "2026-03-31",
                "accrual_on": "2026-03-31",
                "amount": "80000.00",
                "note": "Устаревшая форма позиции",
                "reason": "Попытка перезаписать новую редакцию позиции.",
                "expected_revision_id": stale_revision_id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "План уже изменен другим пользователем")
        fixed.refresh_from_db()
        self.assertEqual(fixed.period_to, date(2026, 4, 30))
        self.assertEqual(fixed.amount, Decimal("100000.00"))
        self.assertEqual(fixed.revisions.count(), 2)

    def test_stale_fixed_close_does_not_close_newer_revision(self):
        budget = self._create_budget()
        fixed = self._create_fixed(budget)
        stale_revision_id = fixed.current_revision_id
        fixed = compensation_svc.revise_fixed_compensation(
            fixed,
            period_from=fixed.period_from,
            period_to=fixed.period_to,
            accrual_on=fixed.accrual_on,
            amount=Decimal("100000.00"),
            note="Новая редакция перед закрытием",
            actor=self.director,
            reason="Позиция уточнена перед закрытием.",
            expected_revision_id=stale_revision_id,
        )
        self.client.force_login(self.director)

        response = self.client.post(
            reverse("grant_fixed_compensation_close", args=[fixed.pk]),
            {
                "reason": "Попытка закрыть устаревшую редакцию позиции.",
                "expected_revision_id": stale_revision_id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "План уже изменен другим пользователем")
        fixed.refresh_from_db()
        self.assertEqual(
            fixed.lifecycle_status,
            GrantFixedCompensation.LifecycleStatus.ACTIVE,
        )
        self.assertEqual(fixed.revisions.count(), 2)
    def test_operator_reads_report_and_history_without_write_controls(self):
        budget = self._create_budget()
        fixed = self._create_fixed(budget)
        self.client.force_login(self.operator)

        response = self.client.get(reverse("grant_report"), self._report_params())

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_manage_grants"])
        self.assertContains(response, "Оплата труда проекта")
        self.assertContains(
            response,
            reverse("funding_payroll_budget_history", args=[budget.pk]),
        )
        self.assertContains(
            response,
            reverse("grant_fixed_compensation_history", args=[fixed.pk]),
        )
        for url in (
            reverse("funding_payroll_budget_create"),
            reverse("funding_payroll_budget_edit", args=[budget.pk]),
            reverse("funding_payroll_budget_close", args=[budget.pk]),
            reverse("grant_fixed_compensation_create"),
            reverse("grant_fixed_compensation_edit", args=[fixed.pk]),
            reverse("grant_fixed_compensation_close", args=[fixed.pk]),
        ):
            with self.subTest(url=url):
                self.assertNotContains(response, url)
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url, {}).status_code, 403)

        for url in (
            reverse("funding_payroll_budget_history", args=[budget.pk]),
            reverse("grant_fixed_compensation_history", args=[fixed.pk]),
        ):
            with self.subTest(url=url):
                history_response = self.client.get(url)
                self.assertEqual(history_response.status_code, 200)
                self.assertContains(history_response, "№1")

    def test_specialist_cannot_read_compensation_history(self):
        budget = self._create_budget()
        fixed = self._create_fixed(budget)
        self.client.force_login(self.specialist)

        for url in (
            reverse("funding_payroll_budget_history", args=[budget.pk]),
            reverse("grant_fixed_compensation_history", args=[fixed.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_archived_source_remains_readable_but_cannot_be_changed(self):
        budget = self._create_budget()
        fixed = self._create_fixed(budget)
        self.funding.archive()

        self.client.force_login(self.operator)
        report_response = self.client.get(reverse("grant_report"), self._report_params())
        self.assertEqual(report_response.status_code, 200)
        self.assertContains(report_response, "Оплата труда проекта")
        self.assertEqual(
            self.client.get(
                reverse("grant_fixed_compensation_history", args=[fixed.pk])
            ).status_code,
            200,
        )

        self.client.force_login(self.director)
        budget_create_response = self.client.post(
            reverse("funding_payroll_budget_create"),
            {
                "funding_source": self.funding.pk,
                "starts_on": "2027-01-01",
                "ends_on": "2027-12-31",
                "planned_amount": "100000.00",
                "enforcement_mode": FundingPayrollBudget.EnforcementMode.HARD,
                "note": "",
                "reason": "Попытка создать бюджет архивного гранта.",
            },
        )
        self.assertEqual(budget_create_response.status_code, 200)
        self.assertEqual(FundingPayrollBudget.objects.filter(funding_source=self.funding).count(), 1)

        fixed_create_response = self.client.post(
            reverse("grant_fixed_compensation_create"),
            {
                "payroll_budget": budget.pk,
                "staff_member": self.staff.pk,
                "compensation_scope": GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
                "service": "",
                "assignment_label": "Новая роль",
                "period_from": "2026-04-01",
                "period_to": "2026-06-30",
                "accrual_on": "2026-06-30",
                "amount": "50000.00",
                "note": "",
                "reason": "Попытка создать позицию архивного гранта.",
            },
        )
        self.assertEqual(fixed_create_response.status_code, 200)
        self.assertEqual(GrantFixedCompensation.objects.filter(payroll_budget=budget).count(), 1)

        for url in (
            reverse("funding_payroll_budget_edit", args=[budget.pk]),
            reverse("funding_payroll_budget_close", args=[budget.pk]),
            reverse("grant_fixed_compensation_edit", args=[fixed.pk]),
            reverse("grant_fixed_compensation_close", args=[fixed.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_fixed_position_must_close_before_budget(self):
        budget = self._create_budget()
        fixed = self._create_fixed(budget)
        self.client.force_login(self.director)

        blocked_response = self.client.post(
            reverse("funding_payroll_budget_close", args=[budget.pk]),
            {
                "reason": "Попытка закрыть бюджет раньше позиции.",
                "expected_revision_id": budget.current_revision_id,
            },
        )
        self.assertEqual(blocked_response.status_code, 200)
        self.assertContains(blocked_response, "Сначала закройте активные фиксированные позиции")
        budget.refresh_from_db()
        self.assertEqual(budget.lifecycle_status, FundingPayrollBudget.LifecycleStatus.ACTIVE)

        fixed_response = self.client.post(
            reverse("grant_fixed_compensation_close", args=[fixed.pk]),
            {
                "reason": "Проектная позиция завершена по плану.",
                "expected_revision_id": fixed.current_revision_id,
            },
        )
        self.assertEqual(fixed_response.status_code, 302)
        fixed.refresh_from_db()
        self.assertEqual(
            fixed.lifecycle_status,
            GrantFixedCompensation.LifecycleStatus.CLOSED,
        )

        budget.refresh_from_db()
        budget_response = self.client.post(
            reverse("funding_payroll_budget_close", args=[budget.pk]),
            {
                "reason": "Все позиции закрыты, бюджет завершен.",
                "expected_revision_id": budget.current_revision_id,
            },
        )
        self.assertEqual(budget_response.status_code, 302)
        budget.refresh_from_db()
        self.assertEqual(
            budget.lifecycle_status,
            FundingPayrollBudget.LifecycleStatus.CLOSED,
        )
