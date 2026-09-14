from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from operations.admin import AppointmentParticipantInline
from operations.models import (
    Appointment,
    AppointmentConfirmation,
    AppointmentParticipant,
    AppointmentStaffAssignment,
    BalanceAccount,
    Child,
    FinancialIntegrityFinding,
    FundingSource,
    LedgerEntry,
    ParentGuardian,
    PayrollAccrual,
    Room,
    Service,
    StaffCompensationRule,
    StaffMember,
)
from operations.services import appointments as appointment_svc, billing as billing_svc


def _local_datetime(day, clock):
    return timezone.make_aware(
        datetime.combine(day, clock), timezone.get_current_timezone()
    )


class ParticipantRemovalGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("participant-guard-admin", password="x")
        cls.funding = FundingSource.objects.create(
            name="Средства для guard-теста",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        cls.service = Service.objects.create(
            name="Guard услуга",
            code="PARTICIPANT-GUARD",
            category=Service.Category.SPEECH,
            default_duration_minutes=30,
            default_price=Decimal("1000"),
        )
        cls.primary_staff = StaffMember.objects.create(
            full_name="Основной специалист guard-теста",
            status=StaffMember.Status.ACTIVE,
        )
        cls.assistant_staff = StaffMember.objects.create(
            full_name="Второй специалист guard-теста",
            status=StaffMember.Status.ACTIVE,
        )
        cls.room = Room.objects.create(
            name="Групповой кабинет guard-теста",
            allow_group_sessions=True,
            capacity=4,
            max_recipient_count=4,
            max_staff_count=2,
        )
        cls.primary_parent = ParentGuardian.objects.create(
            last_name="Первый", first_name="Родитель", phone="+79990000001"
        )
        cls.second_parent = ParentGuardian.objects.create(
            last_name="Второй", first_name="Родитель", phone="+79990000002"
        )
        cls.primary_child = Child.objects.create(
            last_name="Первый", first_name="Получатель", primary_parent=cls.primary_parent
        )
        cls.second_child = Child.objects.create(
            last_name="Второй", first_name="Получатель", primary_parent=cls.second_parent
        )
        cls.second_account = BalanceAccount.objects.create(
            child=cls.second_child,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("5"),
        )

    def setUp(self):
        self.client.force_login(self.admin)
        self.appointment = self._make_group_appointment()
        self.target = self.appointment.participants.get(child=self.second_child)

    def _make_group_appointment(self, *, day_offset=5, source_appointment=None):
        starts_at = _local_datetime(timezone.localdate() + timedelta(days=day_offset), time(10))
        appointment = Appointment.objects.create(
            child=self.primary_child,
            service=self.service,
            staff_member=self.primary_staff,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            status=Appointment.Status.PROPOSED,
            session_type=Appointment.SessionType.GROUP,
            source_appointment=source_appointment,
        )
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=self.second_child,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=self.assistant_staff,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        return appointment

    def _remove_target_via_edit(self):
        return self.client.post(
            reverse("appointment_edit", args=[self.appointment.pk]),
            {
                "session_type": Appointment.SessionType.GROUP,
                "child": self.primary_child.pk,
                "participants": [self.primary_child.pk],
                "service": self.service.pk,
                "staff_member": self.primary_staff.pk,
                "staff_members": [self.primary_staff.pk, self.assistant_staff.pk],
                "room": self.room.pk,
                "program_block": "",
                "billing_account": "",
                "status": self.appointment.status,
                "date": timezone.localtime(self.appointment.starts_at).date().isoformat(),
                "time": timezone.localtime(self.appointment.starts_at).strftime("%H:%M"),
                "duration_minutes": "30",
                "admin_note": "",
            },
        )

    def test_edit_removes_clean_future_native_participant(self):
        response = self._remove_target_via_edit()

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AppointmentParticipant.objects.filter(pk=self.target.pk).exists())
        self.assertEqual(self.appointment.participants.count(), 1)
        self.assertEqual(self.appointment.staff_assignments.count(), 2)

    def test_edit_keeps_participant_and_fact_links_after_facts_exist(self):
        appointment_svc.record_attendance(
            self.appointment,
            action="completed",
            actor=self.admin,
            reason="Проведение подтверждено администратором.",
        )
        decision = billing_svc.apply_decision(
            self.appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.second_account,
            amount=Decimal("-1"),
            participant=self.target,
            actor=self.admin,
        )
        payroll = PayrollAccrual.objects.create(
            dedupe_key=f"participant-guard:{self.target.pk}",
            appointment=self.appointment,
            appointment_participant=self.target,
            staff_member=self.primary_staff,
            service=self.service,
            work_date=timezone.localdate(),
            starts_at_snapshot=self.appointment.starts_at,
            ends_at_snapshot=self.appointment.ends_at,
            duration_minutes=30,
            rate_type_snapshot=StaffCompensationRule.RateType.PER_SESSION,
            rate_amount_snapshot=Decimal("100"),
            session_scope_snapshot=StaffCompensationRule.SessionScope.ALL,
            group_pay_policy_snapshot=StaffCompensationRule.GroupPayPolicy.PER_SESSION,
            charged_participants_count_snapshot=1,
            pay_units_snapshot=1,
            amount=Decimal("100"),
        )
        confirmation = AppointmentConfirmation.objects.create(
            appointment=self.appointment,
            target_type=AppointmentConfirmation.TargetType.RECIPIENT,
            participant=self.target,
            email="participant-guard@example.local",
            subject="Подтверждение занятия",
            message="Подтвердите участие.",
        )

        response = self._remove_target_via_edit()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Нельзя удалить получателя с сохраненными фактами")
        self.assertContains(response, "отметка проведения")
        self.assertContains(response, "решение по списанию")
        self.target.refresh_from_db()
        decision.entry.refresh_from_db()
        payroll.refresh_from_db()
        confirmation.refresh_from_db()
        self.assertEqual(decision.entry.appointment_participant_id, self.target.pk)
        self.assertEqual(payroll.appointment_participant_id, self.target.pk)
        self.assertEqual(confirmation.participant_id, self.target.pk)

    def _assert_direct_removal_blocked(self, reason):
        with self.assertRaisesMessage(ValidationError, reason):
            self.target.delete()
        self.target.refresh_from_db()

    def test_ledger_link_independently_blocks_removal(self):
        entry = LedgerEntry.objects.create(
            account=self.second_account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
            appointment=self.appointment,
            appointment_participant=self.target,
            reason="Проверка сохранности связи участника.",
        )

        self._assert_direct_removal_blocked("операция баланса")
        entry.refresh_from_db()
        self.assertEqual(entry.appointment_participant_id, self.target.pk)

    def test_billing_decision_independently_blocks_removal(self):
        self.target.billing_decision = Appointment.BillingDecision.DO_NOT_CHARGE
        self.target.save(update_fields=["billing_decision"])

        self._assert_direct_removal_blocked("решение по списанию")

    def test_legacy_attendance_mark_independently_blocks_removal(self):
        self.target.attendance_status = Appointment.AttendanceStatus.ATTENDED
        self.target.save(update_fields=["attendance_status"])

        self._assert_direct_removal_blocked("отметка проведения")

    def test_payroll_link_independently_blocks_removal(self):
        payroll = PayrollAccrual.objects.create(
            dedupe_key=f"participant-guard-isolated:{self.target.pk}",
            appointment=self.appointment,
            appointment_participant=self.target,
            staff_member=self.primary_staff,
            service=self.service,
            work_date=timezone.localdate(),
            starts_at_snapshot=self.appointment.starts_at,
            ends_at_snapshot=self.appointment.ends_at,
            duration_minutes=30,
            rate_type_snapshot=StaffCompensationRule.RateType.PER_SESSION,
            rate_amount_snapshot=Decimal("100"),
            session_scope_snapshot=StaffCompensationRule.SessionScope.ALL,
            group_pay_policy_snapshot=StaffCompensationRule.GroupPayPolicy.PER_SESSION,
            charged_participants_count_snapshot=1,
            pay_units_snapshot=1,
            amount=Decimal("100"),
        )

        self._assert_direct_removal_blocked("начисление специалисту")
        payroll.refresh_from_db()
        self.assertEqual(payroll.appointment_participant_id, self.target.pk)

    def test_confirmation_link_independently_blocks_removal(self):
        confirmation = AppointmentConfirmation.objects.create(
            appointment=self.appointment,
            target_type=AppointmentConfirmation.TargetType.RECIPIENT,
            participant=self.target,
            email="isolated-confirmation@example.local",
            subject="Подтверждение занятия",
            message="Подтвердите участие.",
        )

        self._assert_direct_removal_blocked("подтверждение")
        confirmation.refresh_from_db()
        self.assertEqual(confirmation.participant_id, self.target.pk)

    def test_financial_integrity_finding_independently_blocks_removal(self):
        now = timezone.now()
        finding = FinancialIntegrityFinding.objects.create(
            issue_key=f"participant-guard-finding:{self.target.pk}",
            code="participant_guard_test",
            severity=FinancialIntegrityFinding.Severity.WARNING,
            appointment=self.appointment,
            appointment_participant=self.target,
            first_seen_at=now,
            last_seen_at=now,
            message="Связь участника должна сохраниться для финансовой сверки.",
        )

        self._assert_direct_removal_blocked("зафиксированное финансовое расхождение")
        finding.refresh_from_db()
        self.assertEqual(finding.appointment_participant_id, self.target.pk)

    def test_admin_hides_bulk_delete_and_refuses_fact_bearing_participant(self):
        LedgerEntry.objects.create(
            account=self.second_account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
            appointment=self.appointment,
            appointment_participant=self.target,
            reason="Проверка admin-ограждения.",
        )
        request = RequestFactory().get("/admin/operations/appointmentparticipant/")
        request.user = self.admin
        participant_admin = admin.site._registry[AppointmentParticipant]

        self.assertNotIn("delete_selected", participant_admin.get_actions(request))
        self.assertFalse(participant_admin.has_delete_permission(request, self.target))
        self.assertFalse(AppointmentParticipantInline.can_delete)

    def test_rescheduled_participant_lineage_remains_non_deletable(self):
        source = self.appointment.participants.get(child=self.primary_child)
        successor = Appointment(
            child=self.primary_child,
            service=self.service,
            staff_member=self.primary_staff,
            room=self.room,
            starts_at=_local_datetime(timezone.localdate() + timedelta(days=6), time(10)),
            ends_at=_local_datetime(timezone.localdate() + timedelta(days=6), time(10, 30)),
            status=Appointment.Status.PROPOSED,
            source_appointment=self.appointment,
        )
        successor.save(sync_legacy=False)
        moved = AppointmentParticipant.objects.create(
            appointment=successor,
            child=self.primary_child,
            source_participant=source,
            starts_at_snapshot=successor.starts_at,
            ends_at_snapshot=successor.ends_at,
            appointment_status=successor.status,
        )

        with self.assertRaisesMessage(ValidationError, "Узел линии участия нельзя физически удалить"):
            moved.delete()
