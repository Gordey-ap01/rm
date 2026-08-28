from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from operations.models import (
    Appointment,
    CenterExpense,
    Child,
    FundingSource,
    LedgerEntry,
    ParentGuardian,
    Payment,
    PayrollAccrual,
    PayrollPayout,
    PayrollSheet,
    PayrollSheetLifecycleEvent,
    Room,
    Service,
    StaffCompensationRule,
    StaffMember,
    TimeOffRequest,
    TimeOffRequestDecision,
)
from operations.services import (
    appointments as appointment_svc,
    billing as billing_svc,
    payroll as payroll_svc,
    time_off_decisions as time_off_svc,
)


def _local_datetime(day, at_time):
    return timezone.make_aware(
        datetime.combine(day, at_time),
        timezone.get_current_timezone(),
    )


class OperationalAcceptanceTests(TestCase):
    """Acceptance tests for the public write-paths shared by centre roles."""

    def setUp(self):
        self.director = User.objects.create_superuser("acceptance-director", password="x")
        self.administrator = User.objects.create_user(
            "acceptance-administrator",
            password="x",
            is_staff=True,
        )
        self.parent = ParentGuardian.objects.create(
            last_name="Приемочный",
            first_name="Представитель",
            phone="+7 900 000-53-01",
        )
        self.child = Child.objects.create(
            last_name="Приемочный",
            first_name="Получатель",
            primary_parent=self.parent,
        )
        self.staff = StaffMember.objects.create(full_name="Приемочный специалист")
        self.service = Service.objects.create(
            name="Приемочная услуга",
            code="ACCEPTANCE-SERVICE",
            default_duration_minutes=30,
            default_price=Decimal("1500.00"),
        )
        self.room = Room.objects.create(name="Приемочный кабинет")
        self.funding_source = FundingSource.objects.create(
            name="Приемочный грант",
            source_type=FundingSource.SourceType.GRANT,
            transfer_policy=FundingSource.TransferPolicy.WITHIN_CHILD,
        )
        self.account = self.child.balance_accounts.create(
            funding_source=self.funding_source,
            unit="sessions",
            service_scope="any",
            initial_amount=Decimal("2.00"),
        )
        self.work_day = timezone.localdate() + timedelta(days=20)

    def test_administrator_to_director_path_keeps_one_financial_fact_per_stage(self):
        appointment = appointment_svc.create_appointment(
            child=self.child,
            staff_member=self.staff,
            service=self.service,
            starts_at=_local_datetime(self.work_day, time(10, 0)),
            ends_at=_local_datetime(self.work_day, time(10, 30)),
            room=self.room,
            billing_account=self.account,
        )
        appointment_svc.record_attendance(
            appointment,
            action="completed",
            actor=self.administrator,
            note="Занятие проведено в приемочном сценарии.",
        )

        first_charge = billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1.00"),
            reason="Проведенное занятие приемочного сценария.",
            actor=self.administrator,
        )
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1.00"),
            reason="Повторная команда не должна списать занятие повторно.",
            actor=self.administrator,
        )

        appointment.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.COMPLETED)
        self.assertEqual(appointment.attendance_status, Appointment.AttendanceStatus.ATTENDED)
        self.assertEqual(
            appointment.staff_assignments.get(staff_member=self.staff).appointment_status,
            Appointment.Status.COMPLETED,
        )
        participant = appointment.participants.get(child=self.child)
        self.assertEqual(participant.appointment_status, Appointment.Status.COMPLETED)
        self.assertEqual(participant.attendance_status, Appointment.AttendanceStatus.ATTENDED)
        self.assertIsNotNone(participant.marked_by_staff_at)
        self.assertEqual(appointment.billing_decision, Appointment.BillingDecision.CHARGE)
        self.assertEqual(self.account.current_balance, Decimal("1.00"))
        self.assertEqual(
            LedgerEntry.objects.filter(
                appointment=appointment,
                entry_type=LedgerEntry.EntryType.DEBIT,
            ).count(),
            1,
        )

        StaffCompensationRule.objects.create(
            staff_member=self.staff,
            service=self.service,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("500.00"),
        )
        generated = payroll_svc.generate_accruals_for_staff(
            self.staff,
            date_from=self.work_day,
            date_to=self.work_day,
            actor=self.administrator,
        )
        accrual = PayrollAccrual.objects.get(staff_member=self.staff)
        self.assertEqual(generated.created, 1)
        self.assertEqual(accrual.appointment, appointment)
        self.assertEqual(accrual.ledger_entry, first_charge.entry)
        self.assertEqual(accrual.amount, Decimal("500.00"))
        self.assertEqual(accrual.status, PayrollAccrual.Status.DRAFT)

        sheet = payroll_svc.create_payroll_sheet_for_staff(
            self.staff,
            date_from=self.work_day,
            date_to=self.work_day,
            generate_missing=False,
            actor=self.administrator,
        )
        with self.assertRaises(PermissionDenied):
            payroll_svc.approve_payroll_sheet(sheet, actor=self.administrator)

        sheet = payroll_svc.approve_payroll_sheet(sheet, actor=self.director)
        sheet = payroll_svc.send_payroll_sheet(
            sheet,
            note="Руководитель проверил табель и передает его в выплату.",
            actor=self.director,
        )
        payout = payroll_svc.record_payroll_payout(
            sheet,
            amount=Decimal("500.00"),
            method=PayrollPayout.Method.BANK_TRANSFER,
            paid_at=self.work_day,
            reference="ACCEPTANCE-PAY-001",
            note="Полная выплата по приемочному табелю.",
            actor=self.director,
        )

        sheet.refresh_from_db()
        accrual.refresh_from_db()
        self.assertEqual(sheet.status, PayrollSheet.Status.PAID)
        self.assertEqual(sheet.total_amount, Decimal("500.00"))
        self.assertEqual(accrual.status, PayrollAccrual.Status.PAID)
        self.assertEqual(payout.amount, sheet.total_amount)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(CenterExpense.objects.count(), 0)
        self.assertEqual(
            list(
                PayrollSheetLifecycleEvent.objects.filter(payroll_sheet=sheet)
                .order_by("occurred_at", "pk")
                .values_list("event_type", flat=True)
            ),
            [
                PayrollSheetLifecycleEvent.EventType.APPROVED,
                PayrollSheetLifecycleEvent.EventType.SENT,
                PayrollSheetLifecycleEvent.EventType.PAID,
            ],
        )

    def test_administrator_vacation_decision_blocks_schedule_until_director_resolves(self):
        vacation_start = self.work_day + timedelta(days=10)
        request = TimeOffRequest.objects.create(
            staff_member=self.staff,
            request_type=TimeOffRequest.RequestType.VACATION,
            starts_on=vacation_start,
            ends_on=vacation_start + timedelta(days=4),
            reason="Плановый отпуск специалиста.",
        )

        administrator_decision = time_off_svc.resolve_manually(
            request,
            action="approve",
            reason="Отпуск внесен администратором для защиты расписания.",
            actor=self.administrator,
        )
        request.refresh_from_db()
        self.assertEqual(request.status, TimeOffRequest.Status.APPROVED)
        self.assertTrue(administrator_decision.awaits_director_review)
        self.assertTrue(time_off_svc.attention_queryset().filter(pk=request.pk).exists())

        with self.assertRaisesMessage(ValidationError, "Недоступность специалиста"):
            appointment_svc.create_appointment(
                child=self.child,
                staff_member=self.staff,
                service=self.service,
                starts_at=_local_datetime(vacation_start, time(10, 0)),
                ends_at=_local_datetime(vacation_start, time(10, 30)),
                room=self.room,
            )

        director_decision = time_off_svc.resolve_manually(
            request,
            action="reject",
            reason="Руководитель отклонил отпуск после проверки плана центра.",
            actor=self.director,
        )
        administrator_decision.refresh_from_db()
        request.refresh_from_db()
        self.assertFalse(administrator_decision.is_current)
        self.assertTrue(director_decision.is_current)
        self.assertEqual(director_decision.supersedes, administrator_decision)
        self.assertEqual(
            director_decision.source,
            TimeOffRequestDecision.Source.DIRECTOR_MANUAL,
        )
        self.assertEqual(request.status, TimeOffRequest.Status.REJECTED)

        with self.assertRaises(PermissionDenied):
            time_off_svc.resolve_manually(
                request,
                action="approve",
                reason="Администратор не должен менять решение руководителя.",
                actor=self.administrator,
            )
