"""Тесты сервисного слоя."""

from __future__ import annotations

import zipfile
from datetime import datetime, time, timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from operations import schedule_validation as schedule_rules
from operations.models import (
    Appointment,
    AppointmentConfirmation,
    AppointmentParticipant,
    AppointmentRescheduleChain,
    AppointmentReschedulePlan,
    AppointmentRescheduleStep,
    AppointmentRescheduleStepDependency,
    AppointmentRoomOverride,
    AppointmentStaffAssignment,
    BalanceAccount,
    Child,
    FinancialIntegrityCheckRun,
    FinancialIntegrityFinding,
    FundingServiceQuota,
    FundingSource,
    FundingStaffAllocation,
    GrantRecipientAllocation,
    LedgerEntry,
    ParentGuardian,
    Payment,
    PayrollAccrual,
    PayrollSheet,
    ProgramBlock,
    Room,
    Service,
    StaffAvailability,
    StaffCompensationRule,
    StaffMember,
    TimeOffRequest,
    TreatmentProgram,
)
from operations.services import (
    appointments as appt_svc,
    billing as billing_svc,
    financial_facts as financial_facts_svc,
    financial_integrity as financial_integrity_svc,
    financial_integrity_checks as financial_integrity_checks_svc,
    import_preview as import_preview_svc,
    notifications as notif_svc,
    payroll as payroll_svc,
    program_wizard as wizard_svc,
    reports as reports_svc,
    rescheduling_plans as plan_svc,
    scheduling as sched_svc,
)


def _local(day, clock):
    return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())


class _FixturesMixin:
    @classmethod
    def setUpTestData(cls):
        cls.parent = ParentGuardian.objects.create(
            last_name="Иванов",
            first_name="Иван",
            phone="+7 900 000-10-01",
            email="parent@example.local",
        )
        cls.child = Child.objects.create(
            last_name="Иванов", first_name="Петя", primary_parent=cls.parent
        )
        cls.staff_a = StaffMember.objects.create(
            full_name="Анна А.", specializations="Логопед", status=StaffMember.Status.ACTIVE
        )
        cls.staff_b = StaffMember.objects.create(
            full_name="Борис Б.", specializations="Логопед", status=StaffMember.Status.ACTIVE
        )
        cls.staff_afk = StaffMember.objects.create(
            full_name="Виктор В.", specializations="АФК", status=StaffMember.Status.ACTIVE
        )
        cls.service_log = Service.objects.create(
            name="Логопед",
            code="LOG",
            category=Service.Category.SPEECH,
            default_duration_minutes=30,
            default_price=Decimal("1500"),
        )
        cls.service_afk = Service.objects.create(
            name="АФК",
            code="AFK",
            category=Service.Category.PHYSICAL,
            default_duration_minutes=45,
            default_price=Decimal("1800"),
        )
        cls.room1 = Room.objects.create(name="Кабинет 1")
        cls.room2 = Room.objects.create(name="Кабинет 2")
        cls.funding = FundingSource.objects.create(
            name="Грант",
            source_type=FundingSource.SourceType.GRANT,
            transfer_policy=FundingSource.TransferPolicy.WITHIN_CHILD,
        )
        cls.account = BalanceAccount.objects.create(
            child=cls.child,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("10"),
        )
        cls.user = User.objects.create_user("admin", password="x", is_staff=True)

    def setUp(self):
        self.day = timezone.localdate() + timedelta(days=30)


class AppointmentServiceTests(_FixturesMixin, TestCase):
    def test_create_with_default_validation(self):
        appt = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        self.assertEqual(appt.status, Appointment.Status.CONFIRMED)

    def test_reschedule_creates_new_and_marks_old(self):
        appt = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        new_day = self.day + timedelta(days=2)
        result = appt_svc.reschedule(
            appt,
            starts_at=_local(new_day, time(11, 0)),
            ends_at=_local(new_day, time(11, 30)),
            staff_member=self.staff_b,
            room=self.room1,
            note="Клиент попросил",
        )
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.RESCHEDULED)
        self.assertEqual(result.new.source_appointment, appt)
        self.assertEqual(result.new.staff_member, self.staff_b)

    def test_reschedule_preserves_partial_snapshot_without_readding_legacy_child(self):
        participant_parent = ParentGuardian.objects.create(
            last_name="Snapshot",
            first_name="Parent",
            phone="+7 900 000-20-01",
        )
        participant_child = Child.objects.create(
            last_name="Snapshot",
            first_name="Only",
            primary_parent=participant_parent,
        )
        participant_account = BalanceAccount.objects.create(
            child=participant_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("4"),
        )
        appt = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        appt.participants.filter(child=self.child).delete()
        participant = AppointmentParticipant.objects.create(
            appointment=appt,
            child=participant_child,
            billing_account=participant_account,
            starts_at_snapshot=appt.starts_at,
            ends_at_snapshot=appt.ends_at,
            appointment_status=appt.status,
        )

        result = appt_svc.reschedule(
            appt,
            starts_at=_local(self.day + timedelta(days=1), time(11, 0)),
            ends_at=_local(self.day + timedelta(days=1), time(11, 30)),
            staff_member=self.staff_b,
            room=self.room1,
            note="Partial snapshot",
        )

        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.RESCHEDULED)
        self.assertEqual(
            list(appt.participants.values_list("child_id", flat=True)),
            [participant_child.pk],
        )
        self.assertEqual(result.new.child, participant_child)
        self.assertEqual(result.new.billing_account, participant_account)
        self.assertEqual(result.new.participants.count(), 1)
        moved_participant = result.new.participants.get(child=participant_child)
        self.assertEqual(moved_participant.source_participant, participant)
        self.assertFalse(result.new.participants.filter(child=self.child).exists())

    def test_cancel_sets_note_with_reason(self):
        appt = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        appt_svc.cancel(appt, status=Appointment.Status.NO_SHOW, reason_text="Болезнь получателя")
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.NO_SHOW)
        self.assertIn("Болезнь получателя", appt.admin_note)

    def test_cancel_preserves_partial_snapshot_without_readding_legacy_child(self):
        participant_parent = ParentGuardian.objects.create(
            last_name="Cancel",
            first_name="Parent",
            phone="+7 900 000-20-02",
        )
        participant_child = Child.objects.create(
            last_name="Cancel",
            first_name="Only",
            primary_parent=participant_parent,
        )
        appt = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        appt.participants.filter(child=self.child).delete()
        participant = AppointmentParticipant.objects.create(
            appointment=appt,
            child=participant_child,
            starts_at_snapshot=appt.starts_at,
            ends_at_snapshot=appt.ends_at,
            appointment_status=appt.status,
        )

        appt_svc.cancel(appt, status=Appointment.Status.CANCELLED, reason_text="Family request")

        appt.refresh_from_db()
        participant.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.CANCELLED)
        self.assertEqual(participant.appointment_status, Appointment.Status.CANCELLED)
        self.assertEqual(
            list(appt.participants.values_list("child_id", flat=True)),
            [participant_child.pk],
        )

    def test_record_attendance_completed(self):
        appt = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        appt_svc.record_attendance(appt, action="completed", note="Всё ок")
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.COMPLETED)
        self.assertEqual(appt.attendance_status, Appointment.AttendanceStatus.ATTENDED)
        self.assertEqual(appt.specialist_note, "Всё ок")

    def test_record_attendance_updates_participant_snapshot(self):
        appt = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )

        appt_svc.record_attendance(appt, action="completed", note="Готово")

        participant = appt.participants.get(child=self.child)
        self.assertEqual(participant.appointment_status, Appointment.Status.COMPLETED)
        self.assertEqual(participant.attendance_status, Appointment.AttendanceStatus.ATTENDED)
        self.assertEqual(participant.specialist_note, "Готово")
        self.assertIsNotNone(participant.marked_by_staff_at)

    def test_record_attendance_invalid_action(self):
        appt = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        with self.assertRaises(ValueError):
            appt_svc.record_attendance(appt, action="weird")


class ScheduleValidationTests(_FixturesMixin, TestCase):
    def test_group_conflicts_use_participant_and_staff_snapshots(self):
        second_child = Child.objects.create(last_name="Группа", first_name="Второй")
        appt = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        AppointmentParticipant.objects.create(
            appointment=appt,
            child=second_child,
            starts_at_snapshot=appt.starts_at,
            ends_at_snapshot=appt.ends_at,
            appointment_status=appt.status,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=appt,
            staff_member=self.staff_b,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=appt.starts_at,
            ends_at_snapshot=appt.ends_at,
            appointment_status=appt.status,
        )

        conflicts = schedule_rules.appointment_group_conflicts(
            _local(self.day, time(10, 15)),
            _local(self.day, time(10, 45)),
            [second_child],
            [self.staff_b],
            self.room2,
        )

        self.assertTrue(conflicts["child"].filter(pk=appt.pk).exists())
        self.assertTrue(conflicts["staff"].filter(pk=appt.pk).exists())
        self.assertIn("у получателя уже есть занятие в это время", schedule_rules.conflict_messages(conflicts))
        self.assertIn("специалист уже занят в это время", schedule_rules.conflict_messages(conflicts))

    def test_room_limit_counts_snapshot_staff_and_recipients(self):
        second_child = Child.objects.create(last_name="Кабинет", first_name="Второй")
        appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(11, 0)),
            ends_at=_local(self.day, time(11, 30)),
            room=self.room1,
            billing_account=self.account,
        )

        conflicts = schedule_rules.appointment_group_conflicts(
            _local(self.day, time(11, 15)),
            _local(self.day, time(11, 45)),
            [second_child],
            [self.staff_b],
            self.room1,
        )

        self.assertTrue(conflicts["room_over_limit"])
        self.assertTrue(conflicts["room_limit_reasons"]["staff"])
        self.assertTrue(conflicts["room_limit_reasons"]["recipients"])
        self.assertEqual(conflicts["room_limit_reasons"]["staff_total"], 2)
        self.assertEqual(conflicts["room_limit_reasons"]["recipient_total"], 2)
        self.assertIn("кабинет превышает правила вместимости", schedule_rules.conflict_messages(conflicts))

    def test_room_limit_allows_capacity_until_configured_limit(self):
        second_child = Child.objects.create(last_name="Кабинет", first_name="Разрешен")
        room = Room.objects.create(
            name="Групповой зал",
            limit_staff_count=True,
            max_staff_count=2,
            limit_recipient_count=True,
            max_recipient_count=2,
            allow_group_sessions=True,
        )
        appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(12, 0)),
            ends_at=_local(self.day, time(12, 30)),
            room=room,
            billing_account=self.account,
        )

        conflicts = schedule_rules.appointment_group_conflicts(
            _local(self.day, time(12, 15)),
            _local(self.day, time(12, 45)),
            [second_child],
            [self.staff_b],
            room,
        )

        self.assertFalse(conflicts["room_over_limit"])
        self.assertFalse(conflicts["room"].exists())
        self.assertNotIn("кабинет превышает правила вместимости", schedule_rules.conflict_messages(conflicts))

    def test_model_validation_uses_snapshot_participants_and_staff(self):
        second_child = Child.objects.create(last_name="Snapshot", first_name="Participant")
        source = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(9, 0)),
            ends_at=_local(self.day, time(9, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        AppointmentParticipant.objects.create(
            appointment=source,
            child=second_child,
            starts_at_snapshot=source.starts_at,
            ends_at_snapshot=source.ends_at,
            appointment_status=source.status,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=source,
            staff_member=self.staff_b,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=source.starts_at,
            ends_at_snapshot=source.ends_at,
            appointment_status=source.status,
        )
        blocker = appt_svc.create_appointment(
            child=second_child,
            staff_member=self.staff_b,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room2,
        )

        source.starts_at = blocker.starts_at
        source.ends_at = blocker.ends_at

        with self.assertRaises(ValidationError) as ctx:
            source.full_clean()
        message = str(ctx.exception)
        self.assertIn("у получателя уже есть занятие", message)
        self.assertIn("специалист уже занят", message)

    def test_model_validation_counts_snapshot_group_room_capacity(self):
        second_child = Child.objects.create(last_name="Capacity", first_name="Second")
        third_child = Child.objects.create(last_name="Capacity", first_name="Occupant")
        room = Room.objects.create(
            name="Capacity Snapshot Room",
            limit_staff_count=True,
            max_staff_count=3,
            limit_recipient_count=True,
            max_recipient_count=2,
            allow_group_sessions=True,
        )
        source = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(9, 0)),
            ends_at=_local(self.day, time(9, 30)),
            room=room,
            billing_account=self.account,
        )
        AppointmentParticipant.objects.create(
            appointment=source,
            child=second_child,
            starts_at_snapshot=source.starts_at,
            ends_at_snapshot=source.ends_at,
            appointment_status=source.status,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=source,
            staff_member=self.staff_b,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=source.starts_at,
            ends_at_snapshot=source.ends_at,
            appointment_status=source.status,
        )
        occupant = appt_svc.create_appointment(
            child=third_child,
            staff_member=self.staff_afk,
            service=self.service_log,
            starts_at=_local(self.day, time(11, 0)),
            ends_at=_local(self.day, time(11, 30)),
            room=room,
        )

        source.starts_at = occupant.starts_at
        source.ends_at = occupant.ends_at

        with self.assertRaises(ValidationError) as ctx:
            source.full_clean()
        self.assertIn("кабинет уже занят по лимиту получателей", str(ctx.exception))

    def test_model_validation_checks_snapshot_staff_availability(self):
        self.room1.max_staff_count = 2
        self.room1.save(update_fields=["max_staff_count", "updated_at"])
        source = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=source,
            staff_member=self.staff_b,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=source.starts_at,
            ends_at_snapshot=source.ends_at,
            appointment_status=source.status,
        )
        TimeOffRequest.objects.create(
            staff_member=self.staff_b,
            starts_on=self.day,
            ends_on=self.day,
            status=TimeOffRequest.Status.APPROVED,
        )

        with self.assertRaises(ValidationError) as ctx:
            source.full_clean()
        message = str(ctx.exception)
        self.assertIn(self.staff_b.full_name, message)
        self.assertIn("отпуск", message)


class BillingServiceTests(_FixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.appt = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )

    def test_apply_charge_creates_ledger_entry(self):
        result = billing_svc.apply_decision(
            self.appt,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            reason="тест",
            actor=self.user,
        )
        self.appt.refresh_from_db()
        self.assertEqual(self.appt.billing_decision, Appointment.BillingDecision.CHARGE)
        self.assertEqual(self.account.current_balance, Decimal("9"))
        self.assertIsNotNone(result.entry)
        self.assertEqual(result.entry.entry_type, LedgerEntry.EntryType.DEBIT)

    def test_apply_charge_rejects_session_account_for_another_child(self):
        second_parent = ParentGuardian.objects.create(
            last_name="Петров", first_name="Петр", phone="+7 900 000-10-02"
        )
        second_child = Child.objects.create(
            last_name="Петров", first_name="Илья", primary_parent=second_parent
        )
        second_account = BalanceAccount.objects.create(
            child=second_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("5"),
        )

        with self.assertRaisesMessage(ValueError, "Счёт не принадлежит получателю занятия."):
            billing_svc.apply_decision(
                self.appt,
                decision=Appointment.BillingDecision.CHARGE,
                account=second_account,
                amount=Decimal("-1"),
                actor=self.user,
            )

    def test_apply_charge_preserves_partial_snapshot_without_readding_legacy_child(self):
        participant_parent = ParentGuardian.objects.create(
            last_name="Billing",
            first_name="Parent",
            phone="+7 900 000-20-03",
        )
        participant_child = Child.objects.create(
            last_name="Billing",
            first_name="Only",
            primary_parent=participant_parent,
        )
        participant_account = BalanceAccount.objects.create(
            child=participant_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("3"),
        )
        self.appt.participants.filter(child=self.child).delete()
        participant = AppointmentParticipant.objects.create(
            appointment=self.appt,
            child=participant_child,
            starts_at_snapshot=self.appt.starts_at,
            ends_at_snapshot=self.appt.ends_at,
            appointment_status=self.appt.status,
        )

        result = billing_svc.apply_decision(
            self.appt,
            decision=Appointment.BillingDecision.CHARGE,
            account=participant_account,
            amount=Decimal("-1"),
            actor=self.user,
        )

        self.appt.refresh_from_db()
        participant.refresh_from_db()
        self.assertIsNone(self.appt.billing_account)
        self.assertEqual(self.appt.billing_decision, Appointment.BillingDecision.UNDECIDED)
        self.assertEqual(participant.billing_decision, Appointment.BillingDecision.CHARGE)
        self.assertEqual(participant.billing_account, participant_account)
        self.assertEqual(result.entry.appointment_participant, participant)
        self.assertEqual(
            list(self.appt.participants.values_list("child_id", flat=True)),
            [participant_child.pk],
        )

    def test_apply_decision_rejects_group_without_specific_participant(self):
        second_child = Child.objects.create(last_name="Группа", first_name="Без выбора")
        AppointmentParticipant.objects.create(
            appointment=self.appt,
            child=second_child,
            starts_at_snapshot=self.appt.starts_at,
            ends_at_snapshot=self.appt.ends_at,
            appointment_status=self.appt.status,
        )

        with self.assertRaisesMessage(
            ValueError,
            "Для группового занятия нужно выбрать конкретного участника.",
        ):
            billing_svc.apply_decision(
                self.appt,
                decision=Appointment.BillingDecision.CHARGE,
                account=self.account,
                amount=Decimal("-1"),
                actor=self.user,
            )

        self.assertFalse(LedgerEntry.objects.filter(appointment=self.appt).exists())

    def test_sync_ledger_for_decision_rejects_group_without_specific_participant(self):
        second_child = Child.objects.create(last_name="Группа", first_name="Сервис")
        AppointmentParticipant.objects.create(
            appointment=self.appt,
            child=second_child,
            starts_at_snapshot=self.appt.starts_at,
            ends_at_snapshot=self.appt.ends_at,
            appointment_status=self.appt.status,
        )

        with self.assertRaisesMessage(
            ValueError,
            "Для группового занятия нужно выбрать конкретного участника.",
        ):
            appt_svc.sync_ledger_for_decision(
                self.appt,
                account=self.account,
                amount=Decimal("-1"),
                reason="test",
                actor=self.user,
            )

        self.assertFalse(LedgerEntry.objects.filter(appointment=self.appt).exists())

    def test_apply_do_not_charge_unlinks_entries(self):
        billing_svc.apply_decision(
            self.appt,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            actor=self.user,
        )
        result = billing_svc.apply_decision(
            self.appt,
            decision=Appointment.BillingDecision.DO_NOT_CHARGE,
            actor=self.user,
        )
        self.appt.refresh_from_db()
        self.assertEqual(self.appt.billing_decision, Appointment.BillingDecision.DO_NOT_CHARGE)
        self.assertEqual(LedgerEntry.objects.filter(appointment=self.appt).count(), 0)
        self.assertGreaterEqual(result.removed, 1)

    def test_top_up_creates_payment_and_ledger(self):
        payment = billing_svc.top_up_account(
            self.account,
            amount=Decimal("5"),
            method=Payment.Method.GRANT_TRANSFER,
            reference="ГР-01",
            actor=self.user,
        )
        self.assertEqual(self.account.current_balance, Decimal("15"))
        self.assertEqual(
            LedgerEntry.objects.filter(
                account=self.account, entry_type=LedgerEntry.EntryType.CREDIT
            ).count(),
            1,
        )
        self.assertEqual(payment.amount, Decimal("5"))

    def test_group_participant_ledger_entry_validates_against_participant_account(self):
        second_parent = ParentGuardian.objects.create(
            last_name="Петров", first_name="Петр", phone="+7 900 000-10-02"
        )
        second_child = Child.objects.create(
            last_name="Петров", first_name="Илья", primary_parent=second_parent
        )
        second_account = BalanceAccount.objects.create(
            child=second_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("5"),
        )
        participant = AppointmentParticipant.objects.create(
            appointment=self.appt,
            child=second_child,
            billing_account=second_account,
            billing_decision=Appointment.BillingDecision.CHARGE,
            starts_at_snapshot=self.appt.starts_at,
            ends_at_snapshot=self.appt.ends_at,
            appointment_status=self.appt.status,
        )
        entry = LedgerEntry(
            account=second_account,
            appointment=self.appt,
            appointment_participant=participant,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
        )

        entry.full_clean()

    def test_ledger_entry_rejects_participant_from_another_appointment(self):
        second_parent = ParentGuardian.objects.create(
            last_name="Сидоров", first_name="Сидор", phone="+7 900 000-10-03"
        )
        second_child = Child.objects.create(
            last_name="Сидоров", first_name="Павел", primary_parent=second_parent
        )
        second_account = BalanceAccount.objects.create(
            child=second_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("5"),
        )
        participant = AppointmentParticipant.objects.create(
            appointment=self.appt,
            child=second_child,
            billing_account=second_account,
            billing_decision=Appointment.BillingDecision.CHARGE,
            starts_at_snapshot=self.appt.starts_at,
            ends_at_snapshot=self.appt.ends_at,
            appointment_status=self.appt.status,
        )
        other = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day + timedelta(days=1), time(10, 0)),
            ends_at=_local(self.day + timedelta(days=1), time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        entry = LedgerEntry(
            account=second_account,
            appointment=other,
            appointment_participant=participant,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
        )

        with self.assertRaises(ValidationError):
            entry.full_clean()

    def test_top_up_rejects_non_positive(self):
        with self.assertRaises(ValueError):
            billing_svc.top_up_account(self.account, amount=Decimal("-1"))

    def test_transfer_within_child(self):
        self.account.service_scope = BalanceAccount.ServiceScope.SPECIFIC_SERVICE
        self.account.service = self.service_log
        self.account.save()
        target = BalanceAccount.objects.create(
            child=self.child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service_log,
            initial_amount=Decimal("0"),
        )
        debit, credit = billing_svc.transfer_between_accounts(
            from_account=self.account,
            to_account=target,
            amount=Decimal("3"),
            reason="Перераспределение",
            actor=self.user,
        )
        self.assertEqual(debit.amount, Decimal("-3"))
        self.assertEqual(credit.amount, Decimal("3"))

    def test_transfer_to_other_child_blocked_by_within_child(self):
        other_child = Child.objects.create(
            last_name="Петров", first_name="С", primary_parent=self.parent
        )
        target = BalanceAccount.objects.create(
            child=other_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("0"),
        )
        with self.assertRaises(ValueError):
            billing_svc.transfer_between_accounts(
                from_account=self.account,
                to_account=target,
                amount=Decimal("3"),
                reason="X",
            )


class SchedulingServiceTests(_FixturesMixin, TestCase):
    def test_find_overlaps_child(self):
        appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        report = sched_svc.find_overlaps(
            _local(self.day, time(10, 15)),
            _local(self.day, time(10, 45)),
            child=self.child,
            staff_member=self.staff_b,
            room=self.room2,
        )
        self.assertIsNotNone(report.child_conflict)
        self.assertIn("получателя", " ".join(report.human_messages()))

    def test_find_free_slots_filters_overlaps(self):
        appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        slots = sched_svc.find_free_slots(
            self.day, 30, staff_member=self.staff_a, child=self.child, room=self.room1
        )
        for slot in slots:
            self.assertNotEqual(slot.time(), time(10, 0))

    def test_find_overlaps_counts_mixed_snapshot_and_legacy_room_usage(self):
        self.room1.max_staff_count = 2
        self.room1.max_recipient_count = 2
        self.room1.save(update_fields=["max_staff_count", "max_recipient_count"])
        second_child = Child.objects.create(
            last_name="Петров", first_name="Илья", primary_parent=self.parent
        )
        third_child = Child.objects.create(
            last_name="Сидоров", first_name="Максим", primary_parent=self.parent
        )
        starts_at = _local(self.day, time(10, 0))
        ends_at = starts_at + timedelta(minutes=30)
        legacy_appointment = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=starts_at,
            ends_at=ends_at,
            room=self.room1,
            billing_account=self.account,
        )
        legacy_appointment.participants.all().delete()
        legacy_appointment.staff_assignments.all().delete()
        appt_svc.create_appointment(
            child=second_child,
            staff_member=self.staff_b,
            service=self.service_log,
            starts_at=starts_at,
            ends_at=ends_at,
            room=self.room1,
        )

        report = sched_svc.find_overlaps(
            starts_at,
            ends_at,
            child=third_child,
            staff_member=self.staff_afk,
            room=self.room1,
        )

        self.assertIsNotNone(report.room_conflict)
        self.assertEqual(report.room_staff_occupancy, 2)
        self.assertEqual(report.room_recipient_occupancy, 2)

    def test_find_overlaps_blocks_group_when_room_disallows_groups(self):
        self.room1.allow_group_sessions = False
        self.room1.max_staff_count = 2
        self.room1.max_recipient_count = 5
        self.room1.save(
            update_fields=["allow_group_sessions", "max_staff_count", "max_recipient_count"]
        )
        second_child = Child.objects.create(
            last_name="Петров", first_name="Илья", primary_parent=self.parent
        )

        report = sched_svc.find_overlaps(
            _local(self.day, time(10, 0)),
            _local(self.day, time(10, 30)),
            children=[self.child, second_child],
            staff_members=[self.staff_a],
            room=self.room1,
        )

        self.assertTrue(report.has_conflict)
        self.assertTrue(report.room_over_limit)
        self.assertIsNone(report.room_conflict)
        self.assertTrue(report.room_limit_reasons["group"])
        self.assertIn("групповое", " ".join(report.human_messages()))

    def test_is_within_availability_blocked_by_time_off(self):
        TimeOffRequest.objects.create(
            staff_member=self.staff_a,
            request_type=TimeOffRequest.RequestType.SICK,
            starts_on=self.day,
            ends_on=self.day,
            status=TimeOffRequest.Status.APPROVED,
        )
        reason = sched_svc.is_within_availability(
            self.staff_a,
            _local(self.day, time(10, 0)),
            _local(self.day, time(10, 30)),
        )
        self.assertIn("отпуск", reason)

    def test_is_within_availability_uses_staff_window(self):
        StaffAvailability.objects.create(
            staff_member=self.staff_a,
            weekday=self.day.weekday(),
            starts_at=time(9, 0),
            ends_at=time(10, 0),
        )
        # 10:30-11:00 не попадает
        reason = sched_svc.is_within_availability(
            self.staff_a, _local(self.day, time(10, 30)), _local(self.day, time(11, 0))
        )
        self.assertIn("рабочего графика", reason)

    def test_find_free_slots_respects_staff_availability(self):
        StaffAvailability.objects.create(
            staff_member=self.staff_a,
            weekday=self.day.weekday(),
            starts_at=time(9, 0),
            ends_at=time(10, 0),
        )
        slots = sched_svc.find_free_slots(
            self.day,
            30,
            staff_member=self.staff_a,
            child=self.child,
            room=self.room1,
        )
        self.assertIn(time(9, 0), [slot.time() for slot in slots])
        self.assertNotIn(time(10, 30), [slot.time() for slot in slots])

    def test_find_free_slots_blocks_group_room_rules(self):
        self.room1.allow_group_sessions = False
        self.room1.max_staff_count = 2
        self.room1.max_recipient_count = 5
        self.room1.save(
            update_fields=["allow_group_sessions", "max_staff_count", "max_recipient_count"]
        )
        second_child = Child.objects.create(
            last_name="Петров", first_name="Илья", primary_parent=self.parent
        )

        slots = sched_svc.find_free_slots(
            self.day,
            30,
            children=[self.child, second_child],
            staff_members=[self.staff_a],
            room=self.room1,
        )

        self.assertEqual(slots, [])

    def test_find_free_slots_checks_all_staff_availability(self):
        TimeOffRequest.objects.create(
            staff_member=self.staff_b,
            request_type=TimeOffRequest.RequestType.VACATION,
            starts_on=self.day,
            ends_on=self.day,
            status=TimeOffRequest.Status.APPROVED,
        )

        slots = sched_svc.find_free_slots(
            self.day,
            30,
            child=self.child,
            staff_members=[self.staff_a, self.staff_b],
        )

        self.assertEqual(slots, [])

    def test_mass_reschedule_cancels_and_creates_confirmations(self):
        appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day + timedelta(days=1), time(10, 0)),
            ends_at=_local(self.day + timedelta(days=1), time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        result = sched_svc.mass_reschedule(
            self.staff_a,
            date_from=self.day,
            date_to=self.day + timedelta(days=1),
            reason="Болезнь",
            actor=self.user,
        )
        self.assertEqual(len(result.cancelled), 2)
        self.assertEqual(len(result.confirmations), 2)
        for a in result.cancelled:
            self.assertEqual(a.status, Appointment.Status.CANCELLED)

    def test_mass_reschedule_uses_group_participants_and_staff_assignments(self):
        second_parent = ParentGuardian.objects.create(
            last_name="Петров",
            first_name="Петр",
            phone="+7 900 000-10-02",
            email="second-parent@example.local",
        )
        second_child = Child.objects.create(
            last_name="Петров",
            first_name="Илья",
            primary_parent=second_parent,
        )
        appointment = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        appointment.session_type = Appointment.SessionType.GROUP
        appointment.save(update_fields=["session_type", "updated_at"])
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=second_child,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=self.staff_b,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        participant_conflict_start = _local(self.day, time(9, 0))
        appt_svc.create_appointment(
            child=second_child,
            staff_member=self.staff_afk,
            service=self.service_log,
            starts_at=participant_conflict_start,
            ends_at=participant_conflict_start + timedelta(minutes=30),
            room=self.room2,
        )

        result = sched_svc.mass_reschedule(
            self.staff_b,
            date_from=self.day,
            date_to=self.day,
            reason="Болезнь",
            actor=self.user,
        )

        self.assertEqual(result.cancelled, [appointment])
        self.assertEqual(len(result.confirmations), 2)
        self.assertEqual(
            {confirmation.email for confirmation in result.confirmations},
            {"parent@example.local", "second-parent@example.local"},
        )
        self.assertEqual(
            {confirmation.participant.child for confirmation in result.confirmations},
            {self.child, second_child},
        )
        self.assertNotIn(
            participant_conflict_start,
            result.suggested_slots_by_appointment[appointment.pk],
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CANCELLED)

    def test_mass_reschedule_targets_same_representative_per_group_participant(self):
        second_child = Child.objects.create(
            last_name="Sibling",
            first_name="Second",
            primary_parent=self.parent,
        )
        appointment = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        appointment.session_type = Appointment.SessionType.GROUP
        appointment.save(update_fields=["session_type", "updated_at"])
        second_participant = AppointmentParticipant.objects.create(
            appointment=appointment,
            child=second_child,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )

        result = sched_svc.mass_reschedule(
            self.staff_a,
            date_from=self.day,
            date_to=self.day,
            reason="Staff unavailable",
            actor=self.user,
        )

        self.assertEqual(result.cancelled, [appointment])
        self.assertEqual(len(result.confirmations), 2)
        self.assertEqual(
            [confirmation.representative for confirmation in result.confirmations],
            [self.parent, self.parent],
        )
        self.assertEqual(
            {confirmation.participant_id for confirmation in result.confirmations},
            {
                appointment.participants.get(child=self.child).pk,
                second_participant.pk,
            },
        )


class ReschedulingPlanServiceTests(_FixturesMixin, TestCase):
    def _appointment(self):
        return appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )

    def _reschedule_chain_fixture(self):
        first_source = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(11, 0)),
            ends_at=_local(self.day, time(11, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        second_source = self._appointment()
        plan = AppointmentReschedulePlan.objects.create(
            status=AppointmentReschedulePlan.Status.READY,
            plan_type=AppointmentReschedulePlan.PlanType.MANUAL,
            reason="Chain fixture",
            created_by=self.user,
        )
        first = AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=1,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=first_source,
            proposed_starts_at=_local(self.day, time(12, 0)),
            proposed_ends_at=_local(self.day, time(12, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
        )
        second = AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=2,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=second_source,
            proposed_starts_at=_local(self.day, time(11, 0)),
            proposed_ends_at=_local(self.day, time(11, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
        )
        chain_result = plan_svc.create_chain_for_steps(
            plan,
            step_ids=[first.pk, second.pk],
            dependencies=[
                {
                    "predecessor_step_id": first.pk,
                    "successor_step_id": second.pk,
                    "reason": "first step frees second target slot",
                }
            ],
            title="Buffer chain",
            actor=self.user,
        )
        first.refresh_from_db()
        second.refresh_from_db()
        return plan, chain_result.chain, first, second, first_source, second_source

    def test_create_plan_persists_valid_steps_without_changing_appointment(self):
        appointment = self._appointment()

        plan = plan_svc.create_plan_for_appointment(
            appointment, actor=self.user, days=2, limit=3
        )

        appointment.refresh_from_db()
        self.assertEqual(plan.status, AppointmentReschedulePlan.Status.READY)
        self.assertEqual(plan.root_appointment, appointment)
        self.assertEqual(plan.created_by, self.user)
        self.assertGreater(plan.steps.count(), 0)
        step = plan.steps.order_by("position").first()
        self.assertEqual(step.status, AppointmentRescheduleStep.Status.VALID)
        self.assertEqual(step.source_appointment, appointment)
        self.assertEqual(len(step.participant_snapshot), 1)
        self.assertEqual(len(step.staff_snapshot), 1)
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)
        self.assertFalse(Appointment.objects.filter(source_appointment=appointment).exists())
        self.assertFalse(LedgerEntry.objects.filter(appointment=appointment).exists())

    def test_reschedule_chain_schema_keeps_existing_steps_optional(self):
        appointment = self._appointment()
        plan = plan_svc.create_plan_for_appointment(
            appointment, actor=self.user, days=2, limit=1
        )
        step = plan.steps.get()

        self.assertIsNone(step.chain_id)
        self.assertIsNone(step.chain_position)
        self.assertFalse(step.chain_required)

        chain = AppointmentRescheduleChain.objects.create(
            plan=plan,
            title="Буферная цепочка",
            status=AppointmentRescheduleChain.Status.DRAFT,
            created_by=self.user,
        )
        step.chain = chain
        step.chain_position = 1
        step.chain_required = True
        step.full_clean()
        step.save(update_fields=["chain", "chain_position", "chain_required", "updated_at"])

        step.refresh_from_db()
        self.assertEqual(step.chain, chain)
        self.assertEqual(step.chain_position, 1)
        self.assertTrue(step.chain_required)

    def test_reschedule_step_dependency_validates_plan_and_self_edge(self):
        appointment = self._appointment()
        plan = AppointmentReschedulePlan.objects.create(
            status=AppointmentReschedulePlan.Status.READY,
            plan_type=AppointmentReschedulePlan.PlanType.MANUAL,
            root_appointment=appointment,
            reason="Проверка зависимостей",
            created_by=self.user,
        )
        chain = AppointmentRescheduleChain.objects.create(plan=plan, title="Цепочка")
        first = AppointmentRescheduleStep.objects.create(
            plan=plan,
            chain=chain,
            chain_position=1,
            chain_required=True,
            position=1,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=appointment,
            proposed_starts_at=_local(self.day, time(11, 0)),
            proposed_ends_at=_local(self.day, time(11, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
        )
        second = AppointmentRescheduleStep.objects.create(
            plan=plan,
            chain=chain,
            chain_position=2,
            chain_required=True,
            position=2,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=appointment,
            proposed_starts_at=_local(self.day, time(12, 0)),
            proposed_ends_at=_local(self.day, time(12, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
        )
        dependency = AppointmentRescheduleStepDependency.objects.create(
            plan=plan,
            chain=chain,
            predecessor_step=first,
            successor_step=second,
            relation_type=AppointmentRescheduleStepDependency.RelationType.FREES_TARGET_SLOT,
            reason="Первый шаг освобождает окно для второго.",
        )

        self.assertEqual(dependency.chain, chain)
        self.assertEqual(list(chain.dependencies.all()), [dependency])

        invalid = AppointmentRescheduleStepDependency(
            plan=plan,
            chain=chain,
            predecessor_step=first,
            successor_step=first,
        )
        with self.assertRaises(ValidationError):
            invalid.full_clean()

        other_plan = AppointmentReschedulePlan.objects.create(
            status=AppointmentReschedulePlan.Status.READY,
            plan_type=AppointmentReschedulePlan.PlanType.MANUAL,
            root_appointment=appointment,
            reason="Другой план",
            created_by=self.user,
        )
        mismatch = AppointmentRescheduleStepDependency(
            plan=other_plan,
            chain=chain,
            predecessor_step=first,
            successor_step=second,
        )
        with self.assertRaises(ValidationError):
            mismatch.full_clean()

    def test_create_chain_for_steps_builds_dependencies_without_applying(self):
        first_source = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(11, 0)),
            ends_at=_local(self.day, time(11, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        second_source = self._appointment()
        plan = AppointmentReschedulePlan.objects.create(
            status=AppointmentReschedulePlan.Status.READY,
            plan_type=AppointmentReschedulePlan.PlanType.MANUAL,
            reason="Буферная цепочка",
            created_by=self.user,
        )
        first = AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=2,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=first_source,
            proposed_starts_at=_local(self.day, time(12, 0)),
            proposed_ends_at=_local(self.day, time(12, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
        )
        second = AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=1,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=second_source,
            proposed_starts_at=_local(self.day, time(11, 0)),
            proposed_ends_at=_local(self.day, time(11, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
        )

        result = plan_svc.create_chain_for_steps(
            plan,
            step_ids=[second.pk, first.pk],
            dependencies=[
                {
                    "predecessor_step_id": first.pk,
                    "successor_step_id": second.pk,
                    "reason": "Первый перенос освобождает окно для второго.",
                }
            ],
            title="Буферная цепочка",
            actor=self.user,
        )

        self.assertEqual(result.chain.status, AppointmentRescheduleChain.Status.DRAFT)
        self.assertEqual(result.chain.created_by, self.user)
        self.assertEqual(result.chain.validation_summary["structural"], "ok")
        self.assertEqual(result.chain.validation_summary["topological_step_ids"], [first.pk, second.pk])
        self.assertEqual([step.pk for step in result.steps], [first.pk, second.pk])
        self.assertEqual(len(result.dependencies), 1)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.chain, result.chain)
        self.assertEqual(first.chain_position, 1)
        self.assertTrue(first.chain_required)
        self.assertEqual(second.chain, result.chain)
        self.assertEqual(second.chain_position, 2)
        self.assertTrue(second.chain_required)
        self.assertEqual(AppointmentRescheduleStepDependency.objects.count(), 1)
        first_source.refresh_from_db()
        second_source.refresh_from_db()
        self.assertEqual(first_source.status, Appointment.Status.CONFIRMED)
        self.assertEqual(second_source.status, Appointment.Status.CONFIRMED)
        self.assertFalse(Appointment.objects.filter(source_appointment__in=[first_source, second_source]).exists())

    def test_create_chain_for_steps_rejects_alternatives_for_same_source(self):
        appointment = self._appointment()
        plan = AppointmentReschedulePlan.objects.create(
            status=AppointmentReschedulePlan.Status.READY,
            plan_type=AppointmentReschedulePlan.PlanType.MANUAL,
            reason="Альтернативы одного занятия",
            created_by=self.user,
        )
        first = AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=1,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=appointment,
            proposed_starts_at=_local(self.day, time(11, 0)),
            proposed_ends_at=_local(self.day, time(11, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
        )
        second = AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=2,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=appointment,
            proposed_starts_at=_local(self.day, time(12, 0)),
            proposed_ends_at=_local(self.day, time(12, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
        )

        with self.assertRaises(ValidationError):
            plan_svc.create_chain_for_steps(
                plan,
                step_ids=[first.pk, second.pk],
                dependencies=[(first.pk, second.pk)],
                actor=self.user,
            )

        self.assertFalse(AppointmentRescheduleChain.objects.exists())
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.chain_id)
        self.assertIsNone(second.chain_id)

    def test_create_chain_for_steps_rejects_terminal_plan(self):
        first_source = self._appointment()
        second_source = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(11, 0)),
            ends_at=_local(self.day, time(11, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        plan = AppointmentReschedulePlan.objects.create(
            status=AppointmentReschedulePlan.Status.CANCELLED,
            plan_type=AppointmentReschedulePlan.PlanType.MANUAL,
            reason="Отмененный план",
            created_by=self.user,
        )
        first = AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=1,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=first_source,
            proposed_starts_at=_local(self.day, time(12, 0)),
            proposed_ends_at=_local(self.day, time(12, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
        )
        second = AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=2,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=second_source,
            proposed_starts_at=_local(self.day, time(13, 0)),
            proposed_ends_at=_local(self.day, time(13, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
        )

        with self.assertRaises(ValidationError):
            plan_svc.create_chain_for_steps(
                plan,
                step_ids=[first.pk, second.pk],
                dependencies=[(first.pk, second.pk)],
                actor=self.user,
            )

        self.assertFalse(AppointmentRescheduleChain.objects.exists())

    def test_create_chain_for_steps_rejects_cycles(self):
        first_source = self._appointment()
        second_source = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(11, 0)),
            ends_at=_local(self.day, time(11, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        plan = AppointmentReschedulePlan.objects.create(
            status=AppointmentReschedulePlan.Status.READY,
            plan_type=AppointmentReschedulePlan.PlanType.MANUAL,
            reason="Цикл зависимостей",
            created_by=self.user,
        )
        first = AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=1,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=first_source,
            proposed_starts_at=_local(self.day, time(12, 0)),
            proposed_ends_at=_local(self.day, time(12, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
        )
        second = AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=2,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=second_source,
            proposed_starts_at=_local(self.day, time(13, 0)),
            proposed_ends_at=_local(self.day, time(13, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
        )

        with self.assertRaises(ValidationError):
            plan_svc.create_chain_for_steps(
                plan,
                step_ids=[first.pk, second.pk],
                dependencies=[(first.pk, second.pk), (second.pk, first.pk)],
                actor=self.user,
            )

        self.assertFalse(AppointmentRescheduleChain.objects.exists())
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.chain_id)
        self.assertIsNone(second.chain_id)

    def test_revalidate_chain_marks_ready_without_applying(self):
        plan, chain, first, second, first_source, second_source = self._reschedule_chain_fixture()

        result = plan_svc.revalidate_chain(chain)

        chain.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        first_source.refresh_from_db()
        second_source.refresh_from_db()
        self.assertEqual(result.ready_steps, 2)
        self.assertEqual(result.stale_steps, 0)
        self.assertEqual(result.blocked_steps, 0)
        self.assertEqual(chain.status, AppointmentRescheduleChain.Status.READY)
        self.assertEqual(chain.validation_summary["structural"], "ok")
        self.assertEqual(chain.validation_summary["topological_step_ids"], [first.pk, second.pk])
        self.assertEqual(first.status, AppointmentRescheduleStep.Status.VALID)
        self.assertEqual(second.status, AppointmentRescheduleStep.Status.VALID)
        self.assertEqual(first_source.status, Appointment.Status.CONFIRMED)
        self.assertEqual(second_source.status, Appointment.Status.CONFIRMED)
        self.assertFalse(Appointment.objects.filter(source_appointment__in=[first_source, second_source]).exists())
        self.assertEqual(plan.status, AppointmentReschedulePlan.Status.READY)

    def test_revalidate_chain_marks_stale_when_target_window_becomes_busy(self):
        _, chain, first, second, *_ = self._reschedule_chain_fixture()
        blocker_parent = ParentGuardian.objects.create(
            last_name="Blocker",
            first_name="Parent",
            phone="+7 900 000-77-02",
        )
        blocker_child = Child.objects.create(
            last_name="Blocker",
            first_name="Child",
            primary_parent=blocker_parent,
        )
        Appointment.objects.create(
            child=blocker_child,
            staff_member=first.proposed_primary_staff,
            service=self.service_log,
            starts_at=first.proposed_starts_at,
            ends_at=first.proposed_ends_at,
            room=first.proposed_room,
        )

        result = plan_svc.revalidate_chain(chain)

        chain.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(result.ready_steps, 1)
        self.assertEqual(result.stale_steps, 1)
        self.assertEqual(result.blocked_steps, 0)
        self.assertEqual(chain.status, AppointmentRescheduleChain.Status.STALE)
        self.assertEqual(first.status, AppointmentRescheduleStep.Status.STALE)
        self.assertEqual(second.status, AppointmentRescheduleStep.Status.VALID)
        self.assertTrue(first.validation_messages)

    def test_revalidate_chain_blocks_declined_confirmation(self):
        _, chain, first, second, *_ = self._reschedule_chain_fixture()
        AppointmentConfirmation.objects.create(
            appointment=first.source_appointment,
            reschedule_step=first,
            target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
            email="declined@example.local",
            subject="Chain confirmation",
            message="Please confirm",
            status=AppointmentConfirmation.Status.DECLINED,
        )

        result = plan_svc.revalidate_chain(chain)

        chain.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(result.ready_steps, 1)
        self.assertEqual(result.stale_steps, 0)
        self.assertEqual(result.blocked_steps, 1)
        self.assertEqual(chain.status, AppointmentRescheduleChain.Status.STALE)
        self.assertEqual(
            first.confirmation_status,
            AppointmentRescheduleStep.ConfirmationStatus.DECLINED,
        )
        self.assertEqual(second.status, AppointmentRescheduleStep.Status.VALID)
        self.assertIn("confirmation_blocked", [issue["code"] for issue in chain.validation_summary["issues"]])

    def test_apply_chain_applies_steps_atomically(self):
        plan, chain, first, second, first_source, second_source = self._reschedule_chain_fixture()
        plan_svc.revalidate_chain(chain)

        result = plan_svc.apply_chain(chain, actor=self.user)

        chain.refresh_from_db()
        plan.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        first_source.refresh_from_db()
        second_source.refresh_from_db()
        self.assertEqual(len(result.applied_steps), 2)
        self.assertEqual(chain.status, AppointmentRescheduleChain.Status.APPLIED)
        self.assertEqual(chain.applied_by, self.user)
        self.assertEqual(chain.validation_summary["applied"], 2)
        self.assertEqual(first.status, AppointmentRescheduleStep.Status.APPLIED)
        self.assertEqual(second.status, AppointmentRescheduleStep.Status.APPLIED)
        self.assertEqual(first_source.status, Appointment.Status.RESCHEDULED)
        self.assertEqual(second_source.status, Appointment.Status.RESCHEDULED)
        self.assertEqual(plan.status, AppointmentReschedulePlan.Status.APPLIED)
        self.assertEqual(plan.applied_by, self.user)
        first_new = first.created_appointment
        second_new = second.created_appointment
        self.assertEqual(first_new.source_appointment, first_source)
        self.assertEqual(second_new.source_appointment, second_source)
        self.assertEqual(first_new.starts_at, first.proposed_starts_at)
        self.assertEqual(second_new.starts_at, second.proposed_starts_at)
        self.assertFalse(LedgerEntry.objects.filter(appointment__in=[first_new, second_new]).exists())

    def test_apply_chain_requires_ready_chain(self):
        _, chain, first, second, first_source, second_source = self._reschedule_chain_fixture()

        with self.assertRaisesMessage(ValidationError, "ready"):
            plan_svc.apply_chain(chain, actor=self.user)

        chain.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        first_source.refresh_from_db()
        second_source.refresh_from_db()
        self.assertEqual(chain.status, AppointmentRescheduleChain.Status.DRAFT)
        self.assertEqual(first.status, AppointmentRescheduleStep.Status.VALID)
        self.assertEqual(second.status, AppointmentRescheduleStep.Status.VALID)
        self.assertEqual(first_source.status, Appointment.Status.CONFIRMED)
        self.assertEqual(second_source.status, Appointment.Status.CONFIRMED)
        self.assertFalse(Appointment.objects.filter(source_appointment__in=[first_source, second_source]).exists())

    def test_apply_chain_persists_stale_revalidation_without_failed_status(self):
        _, chain, first, second, first_source, second_source = self._reschedule_chain_fixture()
        plan_svc.revalidate_chain(chain)
        appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=first.proposed_starts_at,
            ends_at=first.proposed_ends_at,
            room=self.room1,
            billing_account=self.account,
        )

        with self.assertRaisesMessage(ValidationError, "not ready"):
            plan_svc.apply_chain(chain, actor=self.user)

        chain.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        first_source.refresh_from_db()
        second_source.refresh_from_db()
        self.assertEqual(chain.status, AppointmentRescheduleChain.Status.STALE)
        self.assertNotIn("apply_error", chain.validation_summary)
        self.assertEqual(first.status, AppointmentRescheduleStep.Status.STALE)
        self.assertEqual(second.status, AppointmentRescheduleStep.Status.VALID)
        self.assertEqual(first_source.status, Appointment.Status.CONFIRMED)
        self.assertEqual(second_source.status, Appointment.Status.CONFIRMED)
        self.assertFalse(Appointment.objects.filter(source_appointment__in=[first_source, second_source]).exists())

    def test_apply_chain_rolls_back_when_later_step_fails(self):
        _, chain, first, second, first_source, second_source = self._reschedule_chain_fixture()
        plan_svc.revalidate_chain(chain)
        original_apply_step = plan_svc.apply_step
        calls = []

        def fail_on_second_step(step, *args, **kwargs):
            if calls:
                raise ValidationError("second step failed")
            calls.append(step.pk)
            return original_apply_step(step, *args, **kwargs)

        with (
            mock.patch.object(plan_svc, "apply_step", side_effect=fail_on_second_step),
            self.assertRaisesMessage(ValidationError, "second step failed"),
        ):
            plan_svc.apply_chain(chain, actor=self.user)

        chain.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        first_source.refresh_from_db()
        second_source.refresh_from_db()
        self.assertEqual(chain.status, AppointmentRescheduleChain.Status.FAILED)
        self.assertIn("second step failed", chain.validation_summary["apply_error"])
        self.assertEqual(first.status, AppointmentRescheduleStep.Status.VALID)
        self.assertEqual(second.status, AppointmentRescheduleStep.Status.VALID)
        self.assertEqual(first_source.status, Appointment.Status.CONFIRMED)
        self.assertEqual(second_source.status, Appointment.Status.CONFIRMED)
        self.assertFalse(Appointment.objects.filter(source_appointment__in=[first_source, second_source]).exists())

    def test_revalidate_marks_step_stale_when_window_becomes_busy(self):
        appointment = self._appointment()
        plan = plan_svc.create_plan_for_appointment(
            appointment, actor=self.user, days=2, limit=1
        )
        step = plan.steps.get()

        blocker_parent = ParentGuardian.objects.create(
            last_name="Blocker",
            first_name="Parent",
            phone="+7 900 000-77-01",
        )
        blocker_child = Child.objects.create(
            last_name="Blocker",
            first_name="Child",
            primary_parent=blocker_parent,
        )
        Appointment.objects.create(
            child=blocker_child,
            staff_member=step.proposed_primary_staff,
            service=self.service_log,
            starts_at=step.proposed_starts_at,
            ends_at=step.proposed_ends_at,
            room=step.proposed_room,
        )

        result = plan_svc.revalidate_plan(plan)

        step.refresh_from_db()
        self.assertEqual(result.stale_steps, 1)
        self.assertEqual(result.plan.status, AppointmentReschedulePlan.Status.NEEDS_RECHECK)
        self.assertEqual(step.status, AppointmentRescheduleStep.Status.STALE)
        self.assertIn("специалист уже занят", ", ".join(step.validation_messages))

    def test_revalidate_plan_rejects_terminal_statuses_without_mutation(self):
        appointment = self._appointment()
        plan = plan_svc.create_plan_for_appointment(
            appointment, actor=self.user, days=2, limit=1
        )
        step = plan.steps.get()
        step.status = AppointmentRescheduleStep.Status.STALE
        step.validation_messages = ["Сохраненная причина"]
        step.conflict_snapshot = {"old": "snapshot"}
        step.requires_staff_override = True
        step.requires_room_override = True
        step.save(
            update_fields=[
                "status",
                "validation_messages",
                "conflict_snapshot",
                "requires_staff_override",
                "requires_room_override",
                "updated_at",
            ]
        )

        for status in (
            AppointmentReschedulePlan.Status.APPLIED,
            AppointmentReschedulePlan.Status.CANCELLED,
        ):
            with self.subTest(status=status):
                plan.status = status
                plan.validation_summary = {"locked": status}
                plan.save(update_fields=["status", "validation_summary", "updated_at"])

                with self.assertRaisesMessage(ValidationError, "нельзя перепроверять"):
                    plan_svc.revalidate_plan(plan)

                plan.refresh_from_db()
                step.refresh_from_db()
                self.assertEqual(plan.status, status)
                self.assertEqual(plan.validation_summary, {"locked": status})
                self.assertEqual(step.status, AppointmentRescheduleStep.Status.STALE)
                self.assertEqual(step.validation_messages, ["Сохраненная причина"])
                self.assertEqual(step.conflict_snapshot, {"old": "snapshot"})
                self.assertTrue(step.requires_staff_override)
                self.assertTrue(step.requires_room_override)

    def test_terminal_plan_rejects_step_actions_without_mutation(self):
        self.staff_a.email = "staff-terminal-plan@example.local"
        self.staff_a.save(update_fields=["email", "updated_at"])
        appointment = self._appointment()
        plan = plan_svc.create_plan_for_appointment(
            appointment, actor=self.user, days=2, limit=1
        )
        move_step = plan.steps.get()
        plan.status = AppointmentReschedulePlan.Status.CANCELLED
        plan.validation_summary = {"locked": "cancelled"}
        plan.save(update_fields=["status", "validation_summary", "updated_at"])

        with self.assertRaisesMessage(ValidationError, "нельзя изменять"):
            plan_svc.create_confirmations_for_step(move_step, actor=self.user)
        with self.assertRaisesMessage(ValidationError, "нельзя изменять"):
            plan_svc.apply_step(move_step, actor=self.user)

        move_step.refresh_from_db()
        appointment.refresh_from_db()
        self.assertEqual(move_step.status, AppointmentRescheduleStep.Status.VALID)
        self.assertIsNone(move_step.created_appointment_id)
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)
        self.assertFalse(AppointmentConfirmation.objects.filter(reschedule_step=move_step).exists())
        self.assertFalse(Appointment.objects.filter(source_appointment=appointment).exists())

        review_plan = AppointmentReschedulePlan.objects.create(
            status=AppointmentReschedulePlan.Status.APPLIED,
            plan_type=AppointmentReschedulePlan.PlanType.STAFF_ABSENCE,
            root_appointment=appointment,
            staff_member=self.staff_a,
            reason="Завершенный ручной разбор",
            created_by=self.user,
        )
        review_step = AppointmentRescheduleStep.objects.create(
            plan=review_plan,
            position=1,
            action_type=AppointmentRescheduleStep.ActionType.REVIEW_CONFLICT,
            status=AppointmentRescheduleStep.Status.PENDING,
            source_appointment=appointment,
        )
        appointment.status = Appointment.Status.CANCELLED
        appointment.save(update_fields=["status", "updated_at"])

        with self.assertRaisesMessage(ValidationError, "нельзя изменять"):
            plan_svc.mark_review_conflict_step_resolved(review_step, actor=self.user)

        review_step.refresh_from_db()
        self.assertEqual(review_step.status, AppointmentRescheduleStep.Status.PENDING)
        self.assertEqual(review_step.admin_note, "")

    def test_terminal_plan_rejects_chain_actions_without_mutation(self):
        plan, chain, first, second, first_source, second_source = self._reschedule_chain_fixture()
        chain.validation_summary = {"locked": "draft"}
        chain.save(update_fields=["validation_summary", "updated_at"])
        plan.status = AppointmentReschedulePlan.Status.CANCELLED
        plan.validation_summary = {"locked": "cancelled"}
        plan.save(update_fields=["status", "validation_summary", "updated_at"])

        with self.assertRaisesMessage(ValidationError, "нельзя перепроверять"):
            plan_svc.revalidate_chain(chain)

        plan.refresh_from_db()
        chain.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(plan.status, AppointmentReschedulePlan.Status.CANCELLED)
        self.assertEqual(plan.validation_summary, {"locked": "cancelled"})
        self.assertEqual(chain.status, AppointmentRescheduleChain.Status.DRAFT)
        self.assertEqual(chain.validation_summary, {"locked": "draft"})
        self.assertEqual(first.status, AppointmentRescheduleStep.Status.VALID)
        self.assertEqual(second.status, AppointmentRescheduleStep.Status.VALID)

        plan.status = AppointmentReschedulePlan.Status.READY
        plan.save(update_fields=["status", "updated_at"])
        plan_svc.revalidate_chain(chain)
        plan.status = AppointmentReschedulePlan.Status.APPLIED
        plan.validation_summary = {"locked": "applied"}
        plan.save(update_fields=["status", "validation_summary", "updated_at"])

        with self.assertRaisesMessage(ValidationError, "нельзя изменять"):
            plan_svc.apply_chain(chain, actor=self.user)

        plan.refresh_from_db()
        chain.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        first_source.refresh_from_db()
        second_source.refresh_from_db()
        self.assertEqual(plan.status, AppointmentReschedulePlan.Status.APPLIED)
        self.assertEqual(plan.validation_summary, {"locked": "applied"})
        self.assertEqual(chain.status, AppointmentRescheduleChain.Status.READY)
        self.assertEqual(first.status, AppointmentRescheduleStep.Status.VALID)
        self.assertEqual(second.status, AppointmentRescheduleStep.Status.VALID)
        self.assertEqual(first_source.status, Appointment.Status.CONFIRMED)
        self.assertEqual(second_source.status, Appointment.Status.CONFIRMED)
        self.assertFalse(Appointment.objects.filter(source_appointment__in=[first_source, second_source]).exists())

    def test_apply_valid_step_creates_new_appointment_without_billing_decision(self):
        appointment = self._appointment()
        plan = plan_svc.create_plan_for_appointment(
            appointment, actor=self.user, days=2, limit=1
        )
        step = plan.steps.get()

        applied = plan_svc.apply_step(step, actor=self.user)

        appointment.refresh_from_db()
        applied.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.RESCHEDULED)
        self.assertEqual(applied.status, AppointmentRescheduleStep.Status.APPLIED)
        self.assertEqual(plan.status, AppointmentReschedulePlan.Status.APPLIED)
        self.assertEqual(plan.applied_by, self.user)
        new_appointment = applied.created_appointment
        self.assertEqual(new_appointment.source_appointment, appointment)
        self.assertEqual(new_appointment.billing_decision, Appointment.BillingDecision.UNDECIDED)
        self.assertFalse(LedgerEntry.objects.filter(appointment=appointment).exists())

    def test_apply_step_skips_alternatives_for_same_source_only(self):
        appointment = self._appointment()
        second_parent = ParentGuardian.objects.create(
            last_name="Other",
            first_name="Parent",
            phone="+7 900 000-77-11",
        )
        second_child = Child.objects.create(
            last_name="Other",
            first_name="Child",
            primary_parent=second_parent,
        )
        second_appointment = Appointment.objects.create(
            child=second_child,
            staff_member=self.staff_b,
            service=self.service_log,
            starts_at=_local(self.day, time(12, 0)),
            ends_at=_local(self.day, time(12, 30)),
            room=self.room1,
        )
        plan = AppointmentReschedulePlan.objects.create(
            status=AppointmentReschedulePlan.Status.READY,
            plan_type=AppointmentReschedulePlan.PlanType.MANUAL,
            root_appointment=appointment,
            reason="Проверка альтернатив",
            created_by=self.user,
        )
        step = AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=1,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=appointment,
            proposed_starts_at=_local(self.day, time(11, 0)),
            proposed_ends_at=_local(self.day, time(11, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
        )
        alternative = AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=2,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=appointment,
            proposed_starts_at=_local(self.day, time(13, 0)),
            proposed_ends_at=_local(self.day, time(13, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
        )
        other_source_step = AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=3,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=second_appointment,
            proposed_starts_at=_local(self.day, time(14, 0)),
            proposed_ends_at=_local(self.day, time(14, 30)),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_b,
        )

        plan_svc.apply_step(step, actor=self.user)

        alternative.refresh_from_db()
        other_source_step.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(alternative.status, AppointmentRescheduleStep.Status.SKIPPED)
        self.assertIn("другой вариант", alternative.admin_note)
        self.assertEqual(other_source_step.status, AppointmentRescheduleStep.Status.VALID)
        self.assertEqual(plan.status, AppointmentReschedulePlan.Status.READY)

        plan_svc.apply_step(other_source_step, actor=self.user)

        other_source_step.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(other_source_step.status, AppointmentRescheduleStep.Status.APPLIED)
        self.assertEqual(plan.status, AppointmentReschedulePlan.Status.APPLIED)

    def test_apply_step_with_room_override_creates_room_override(self):
        appointment = self._appointment()
        proposed_start = _local(self.day, time(11, 0))
        blocker_parent = ParentGuardian.objects.create(
            last_name="Room",
            first_name="Blocker",
            phone="+7 900 000-77-10",
        )
        blocker_child = Child.objects.create(
            last_name="Room",
            first_name="Blocker",
            primary_parent=blocker_parent,
        )
        Appointment.objects.create(
            child=blocker_child,
            staff_member=self.staff_b,
            service=self.service_log,
            starts_at=proposed_start,
            ends_at=proposed_start + timedelta(minutes=30),
            room=self.room1,
        )
        plan = AppointmentReschedulePlan.objects.create(
            status=AppointmentReschedulePlan.Status.READY,
            plan_type=AppointmentReschedulePlan.PlanType.SINGLE_MOVE,
            root_appointment=appointment,
            reason="Проверка override кабинета",
            created_by=self.user,
        )
        step = AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=1,
            action_type=AppointmentRescheduleStep.ActionType.MOVE,
            status=AppointmentRescheduleStep.Status.VALID,
            source_appointment=appointment,
            proposed_starts_at=proposed_start,
            proposed_ends_at=proposed_start + timedelta(minutes=30),
            proposed_room=self.room1,
            proposed_primary_staff=self.staff_a,
            requires_room_override=True,
            validation_messages=["кабинет превышает правила вместимости"],
        )

        with self.assertRaisesMessage(ValidationError, "override кабинета"):
            plan_svc.apply_step(step, actor=self.user)

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)

        applied = plan_svc.apply_step(step, actor=self.user, allow_room_override=True)

        appointment.refresh_from_db()
        plan.refresh_from_db()
        applied.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.RESCHEDULED)
        self.assertEqual(applied.status, AppointmentRescheduleStep.Status.APPLIED)
        self.assertEqual(plan.status, AppointmentReschedulePlan.Status.APPLIED)
        override = AppointmentRoomOverride.objects.get(
            appointment=applied.created_appointment
        )
        self.assertEqual(override.created_by, self.user)
        self.assertEqual(applied.created_appointment.room, self.room1)

    def test_group_plan_snapshot_includes_participants_and_staff(self):
        appointment = self._appointment()
        appointment.session_type = Appointment.SessionType.GROUP
        appointment.save(update_fields=["session_type", "updated_at"])
        second_parent = ParentGuardian.objects.create(
            last_name="Group",
            first_name="Parent",
            phone="+7 900 000-77-02",
        )
        second_child = Child.objects.create(
            last_name="Group",
            first_name="Second",
            primary_parent=second_parent,
        )
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=second_child,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=self.staff_b,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )

        plan = plan_svc.create_plan_for_appointment(
            appointment, actor=self.user, days=2, limit=1
        )
        step = plan.steps.get()

        self.assertEqual(len(step.participant_snapshot), 2)
        self.assertEqual(len(step.staff_snapshot), 2)

    def test_create_staff_absence_plan_persists_pending_steps_without_cancelling(self):
        appointment = self._appointment()

        plan = plan_svc.create_staff_absence_plan(
            self.staff_a,
            date_from=self.day,
            date_to=self.day,
            reason="Больничный",
            actor=self.user,
        )

        appointment.refresh_from_db()
        self.assertEqual(plan.plan_type, AppointmentReschedulePlan.PlanType.STAFF_ABSENCE)
        self.assertEqual(plan.status, AppointmentReschedulePlan.Status.READY)
        self.assertEqual(plan.staff_member, self.staff_a)
        self.assertEqual(plan.steps.count(), 1)
        step = plan.steps.get()
        self.assertEqual(step.action_type, AppointmentRescheduleStep.ActionType.REVIEW_CONFLICT)
        self.assertEqual(step.status, AppointmentRescheduleStep.Status.PENDING)
        self.assertEqual(step.source_appointment, appointment)
        self.assertEqual(step.proposed_primary_staff, self.staff_a)
        self.assertIn("Больничный", " ".join(step.validation_messages))
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)
        self.assertFalse(AppointmentConfirmation.objects.filter(appointment=appointment).exists())

    def test_mark_review_conflict_resolved_requires_non_active_source(self):
        appointment = self._appointment()
        plan = plan_svc.create_staff_absence_plan(
            self.staff_a,
            date_from=self.day,
            date_to=self.day,
            reason="Больничный",
            actor=self.user,
        )
        step = plan.steps.get()

        with self.assertRaisesMessage(ValidationError, "Сначала перенесите или отмените"):
            plan_svc.mark_review_conflict_step_resolved(step, actor=self.user)

        appointment.status = Appointment.Status.CANCELLED
        appointment.save(update_fields=["status", "updated_at"])

        resolved = plan_svc.mark_review_conflict_step_resolved(step, actor=self.user)

        resolved.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(resolved.status, AppointmentRescheduleStep.Status.SKIPPED)
        self.assertIn("Разобрано вручную", resolved.admin_note)
        self.assertEqual(plan.status, AppointmentReschedulePlan.Status.APPLIED)
        self.assertEqual(plan.applied_by, self.user)

    def test_create_confirmations_for_step_targets_representative_and_specialist(self):
        self.staff_a.email = "staff-plan@example.local"
        self.staff_a.save(update_fields=["email", "updated_at"])
        appointment = self._appointment()
        plan = plan_svc.create_plan_for_appointment(
            appointment, actor=self.user, days=2, limit=1
        )
        step = plan.steps.get()

        result = plan_svc.create_confirmations_for_step(step, actor=self.user)

        self.assertEqual(len(result.created), 2)
        self.assertEqual(result.existing, [])
        self.assertEqual(
            {
                confirmation.target_type
                for confirmation in AppointmentConfirmation.objects.filter(reschedule_step=step)
            },
            {
                AppointmentConfirmation.TargetType.REPRESENTATIVE,
                AppointmentConfirmation.TargetType.SPECIALIST,
            },
        )
        representative_confirmation = AppointmentConfirmation.objects.get(
            reschedule_step=step,
            target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
        )
        self.assertEqual(representative_confirmation.participant.child, self.child)
        self.assertIn("Новое время", representative_confirmation.message)
        self.assertIn("не переносит занятие автоматически", representative_confirmation.message)

    def test_create_confirmations_for_step_does_not_duplicate_existing_targets(self):
        self.staff_a.email = "staff-plan-duplicate@example.local"
        self.staff_a.save(update_fields=["email", "updated_at"])
        appointment = self._appointment()
        plan = plan_svc.create_plan_for_appointment(
            appointment, actor=self.user, days=2, limit=1
        )
        step = plan.steps.get()
        first = plan_svc.create_confirmations_for_step(step, actor=self.user)

        second = plan_svc.create_confirmations_for_step(step, actor=self.user)

        self.assertEqual(len(first.created), 2)
        self.assertEqual(second.created, [])
        self.assertEqual(len(second.existing), 2)
        self.assertEqual(AppointmentConfirmation.objects.filter(reschedule_step=step).count(), 2)

    def test_step_confirmation_status_waits_after_confirmations_are_created(self):
        self.staff_a.email = "staff-plan-waiting@example.local"
        self.staff_a.save(update_fields=["email", "updated_at"])
        appointment = self._appointment()
        plan = plan_svc.create_plan_for_appointment(
            appointment, actor=self.user, days=2, limit=1
        )
        step = plan.steps.get()

        result = plan_svc.create_confirmations_for_step(step, actor=self.user)

        result.step.refresh_from_db()
        self.assertEqual(
            result.step.confirmation_status,
            AppointmentRescheduleStep.ConfirmationStatus.WAITING,
        )
        self.assertEqual(result.step.confirmation_summary["total"], 2)
        self.assertEqual(result.step.confirmation_summary["pending"], 2)
        with self.assertRaisesMessage(ValidationError, "без ответа"):
            plan_svc.apply_step(result.step, actor=self.user)

    def test_step_confirmation_status_becomes_approved_after_all_confirm(self):
        self.staff_a.email = "staff-plan-approved@example.local"
        self.staff_a.save(update_fields=["email", "updated_at"])
        appointment = self._appointment()
        plan = plan_svc.create_plan_for_appointment(
            appointment, actor=self.user, days=2, limit=1
        )
        step = plan.steps.get()
        plan_svc.create_confirmations_for_step(step, actor=self.user)
        AppointmentConfirmation.objects.filter(reschedule_step=step).update(
            status=AppointmentConfirmation.Status.CONFIRMED,
            responded_at=timezone.now(),
        )

        refreshed = plan_svc.refresh_step_confirmation_status(step)

        self.assertEqual(
            refreshed.confirmation_status,
            AppointmentRescheduleStep.ConfirmationStatus.APPROVED,
        )
        self.assertEqual(refreshed.confirmation_summary["confirmed"], 2)
        applied = plan_svc.apply_step(refreshed, actor=self.user)
        self.assertEqual(applied.status, AppointmentRescheduleStep.Status.APPLIED)

    def test_step_confirmation_status_declined_blocks_apply(self):
        self.staff_a.email = "staff-plan-declined@example.local"
        self.staff_a.save(update_fields=["email", "updated_at"])
        appointment = self._appointment()
        plan = plan_svc.create_plan_for_appointment(
            appointment, actor=self.user, days=2, limit=1
        )
        step = plan.steps.get()
        plan_svc.create_confirmations_for_step(step, actor=self.user)
        AppointmentConfirmation.objects.filter(
            reschedule_step=step,
            target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
        ).update(
            status=AppointmentConfirmation.Status.DECLINED,
            responded_at=timezone.now(),
        )

        refreshed = plan_svc.refresh_step_confirmation_status(step)

        self.assertEqual(
            refreshed.confirmation_status,
            AppointmentRescheduleStep.ConfirmationStatus.DECLINED,
        )
        self.assertEqual(refreshed.confirmation_summary["declined"], 1)
        with self.assertRaisesMessage(ValidationError, "Есть отказ"):
            plan_svc.apply_step(refreshed, actor=self.user)


class ProgramWizardServiceTests(_FixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.program = TreatmentProgram.objects.create(
            child=self.child,
            title="Программа после консультации",
            status=TreatmentProgram.Status.ACTIVE,
        )
        self.block = ProgramBlock.objects.create(
            program=self.program,
            number=1,
            title="Логопедический каскад",
            service=self.service_log,
            staff_member=self.staff_a,
            planned_sessions=4,
            balance_account=self.account,
        )

    def _preview(self, **overrides):
        params = {
            "date_from": self.day,
            "date_to": self.day,
            "weekdays": {self.day.weekday()},
            "time_from": time(10, 0),
            "time_until": time(12, 0),
            "duration_minutes": 30,
            "staff_member": self.staff_a,
            "room": self.room1,
            "requested_count": 2,
        }
        params.update(overrides)
        return wizard_svc.suggest_program_block_slots(self.block, **params)

    def test_suggest_allows_second_specialist_when_room_capacity_allows(self):
        self.room1.capacity = 2
        self.room1.max_staff_count = 2
        self.room1.max_recipient_count = 2
        self.room1.save(update_fields=["capacity", "max_staff_count", "max_recipient_count"])
        other_child = Child.objects.create(
            last_name="Сидоров", first_name="Коля", primary_parent=self.parent
        )
        appt_svc.create_appointment(
            child=other_child,
            staff_member=self.staff_b,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
        )

        preview = self._preview(requested_count=1)

        self.assertEqual(len(preview.slots), 1)
        self.assertEqual(preview.slots[0].starts_at.time(), time(10, 0))
        self.assertEqual(preview.slots[0].room_occupancy, 1)
        self.assertEqual(preview.slots[0].room_capacity, 2)

    def test_suggest_skips_slot_when_room_capacity_is_full(self):
        self.room1.capacity = 2
        self.room1.max_staff_count = 2
        self.room1.max_recipient_count = 2
        self.room1.save(update_fields=["capacity", "max_staff_count", "max_recipient_count"])
        child_b = Child.objects.create(
            last_name="Петров", first_name="Илья", primary_parent=self.parent
        )
        child_c = Child.objects.create(
            last_name="Федоров", first_name="Олег", primary_parent=self.parent
        )
        appt_svc.create_appointment(
            child=child_b,
            staff_member=self.staff_b,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
        )
        appt_svc.create_appointment(
            child=child_c,
            staff_member=self.staff_afk,
            service=self.service_afk,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 45)),
            room=self.room1,
        )

        preview = self._preview(requested_count=1)

        self.assertEqual(len(preview.slots), 1)
        self.assertNotEqual(preview.slots[0].starts_at.time(), time(10, 0))

    def test_auto_suggest_uses_another_staff_and_room_when_primary_is_busy(self):
        other_child = Child.objects.create(
            last_name="Сидоров", first_name="Коля", primary_parent=self.parent
        )
        appt_svc.create_appointment(
            child=other_child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
        )

        preview = self._preview(staff_member=None, room=None, requested_count=1)

        self.assertEqual(len(preview.slots), 1)
        slot = preview.slots[0]
        self.assertEqual(slot.starts_at.time(), time(10, 0))
        self.assertNotEqual(slot.staff_member, self.staff_a)
        self.assertEqual(slot.room, self.room2)
        self.assertIn("свободный", slot.selection_note)

    def test_auto_suggest_respects_room_capacity_across_rooms(self):
        child_b = Child.objects.create(
            last_name="Петров", first_name="Илья", primary_parent=self.parent
        )
        appt_svc.create_appointment(
            child=child_b,
            staff_member=self.staff_b,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
        )

        preview = self._preview(staff_member=None, room=None, requested_count=1)

        self.assertEqual(len(preview.slots), 1)
        self.assertEqual(preview.slots[0].starts_at.time(), time(10, 0))
        self.assertEqual(preview.slots[0].room, self.room2)

    def test_balance_limit_caps_requested_sessions(self):
        low_account = BalanceAccount.objects.create(
            child=self.child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service_log,
            initial_amount=Decimal("1"),
        )
        self.block.balance_account = low_account
        self.block.save(update_fields=["balance_account"])

        preview = self._preview(requested_count=3)

        self.assertEqual(preview.allowed_count, 1)
        self.assertTrue(preview.limited_by_balance)
        self.assertEqual(len(preview.slots), 1)

    def test_balance_limit_counts_existing_participant_reserves(self):
        low_account = BalanceAccount.objects.create(
            child=self.child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service_log,
            initial_amount=Decimal("2"),
        )
        self.block.balance_account = low_account
        self.block.save(update_fields=["balance_account"])
        appointment = Appointment.objects.create(
            child=self.child,
            service=self.service_log,
            staff_member=self.staff_a,
            room=self.room1,
            starts_at=_local(self.day, time(9, 0)),
            ends_at=_local(self.day, time(9, 30)),
            status=Appointment.Status.RESERVED,
            billing_account=low_account,
            program_block=self.block,
        )
        self.assertTrue(
            AppointmentParticipant.objects.filter(
                appointment=appointment,
                child=self.child,
                program_block=self.block,
            ).exists()
        )

        preview = self._preview(requested_count=3)

        self.assertEqual(preview.funded_remaining, 1)
        self.assertEqual(preview.allowed_count, 1)

    def test_balance_limit_treats_charge_without_account_as_unpaid_reserve(self):
        low_account = BalanceAccount.objects.create(
            child=self.child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service_log,
            initial_amount=Decimal("2"),
        )
        self.block.balance_account = low_account
        self.block.save(update_fields=["balance_account"])
        appointment = Appointment.objects.create(
            child=self.child,
            service=self.service_log,
            staff_member=self.staff_a,
            room=self.room1,
            starts_at=_local(self.day, time(9, 0)),
            ends_at=_local(self.day, time(9, 30)),
            status=Appointment.Status.RESERVED,
            billing_account=low_account,
            program_block=self.block,
        )
        AppointmentParticipant.objects.filter(
            appointment=appointment,
            child=self.child,
            program_block=self.block,
        ).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=None,
        )

        preview = self._preview(requested_count=3)

        self.assertEqual(preview.funded_remaining, 1)
        self.assertEqual(preview.allowed_count, 1)

    def test_create_schedule_assigns_block_and_sequence(self):
        preview = self._preview(requested_count=2)

        result = wizard_svc.create_schedule_from_preview(preview, actor=self.user)

        self.block.refresh_from_db()
        self.assertEqual(len(result.appointments), 2)
        self.assertEqual(self.block.status, ProgramBlock.Status.SCHEDULED)
        self.assertEqual([appt.sequence_number for appt in result.appointments], [1, 2])
        self.assertTrue(all(appt.program_block_id == self.block.pk for appt in result.appointments))
        participant_sequences = list(
            AppointmentParticipant.objects.filter(
                appointment__in=result.appointments,
                child=self.child,
                program_block=self.block,
            )
            .order_by("appointment__starts_at")
            .values_list("sequence_number", flat=True)
        )
        self.assertEqual(participant_sequences, [1, 2])

    def test_preview_slots_do_not_overlap_each_other_when_duration_exceeds_step(self):
        preview = self._preview(
            requested_count=3,
            time_from=time(9, 0),
            time_until=time(12, 0),
            duration_minutes=45,
        )

        for previous, current in zip(preview.slots, preview.slots[1:], strict=False):
            self.assertGreaterEqual(current.starts_at, previous.ends_at)

        result = wizard_svc.create_schedule_from_preview(preview, actor=self.user)

        self.assertEqual(len(result.appointments), 3)

    def test_create_schedule_rejects_stale_preview_with_local_time(self):
        preview = self._preview(requested_count=1)
        stale_slot = preview.slots[0]

        appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_b,
            service=self.service_log,
            starts_at=stale_slot.starts_at,
            ends_at=stale_slot.ends_at,
            room=self.room2,
        )

        with self.assertRaisesMessage(ValidationError, "Нажмите «Подобрать окна» ещё раз"):
            wizard_svc.create_schedule_from_preview(preview, actor=self.user)

        self.assertFalse(Appointment.objects.filter(program_block=self.block).exists())


def _minimal_xlsx(rows: list[list[str]]) -> bytes:
    def cell_ref(row_index: int, col_index: int) -> str:
        letters = ""
        number = col_index + 1
        while number:
            number, remainder = divmod(number - 1, 26)
            letters = chr(ord("A") + remainder) + letters
        return f"{letters}{row_index}"

    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for col_index, value in enumerate(row):
            escaped = (
                value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            cells.append(
                f'<c r="{cell_ref(row_index, col_index)}" t="inlineStr"><is><t>{escaped}</t></is></c>'
            )
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    sheet_xml = (
        f'<worksheet xmlns="{import_preview_svc.XLSX_MAIN_NS}"><sheetData>'
        f'{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    workbook_xml = (
        f'<workbook xmlns="{import_preview_svc.XLSX_MAIN_NS}" '
        f'xmlns:r="{import_preview_svc.XLSX_REL_NS}"><sheets>'
        '<sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels_xml = (
        f'<Relationships xmlns="{import_preview_svc.PACKAGE_REL_NS}">'
        '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return payload.getvalue()


class ImportPreviewServiceTests(TestCase):
    def test_csv_preview_validates_rows_and_existing_recipient(self):
        Child.objects.create(last_name="Иванов", first_name="Петр")
        uploaded = SimpleUploadedFile(
            "recipients.csv",
            (
                "Фамилия получателя;Имя получателя;Email представителя\n"
                "Иванов;Петр;bad-email\n"
                "Иванов;Петр;parent@example.local\n"
            ).encode(),
            content_type="text/csv",
        )

        preview = import_preview_svc.preview_recipient_import(uploaded)

        self.assertEqual(preview.total_rows, 2)
        self.assertEqual(preview.invalid_count, 1)
        self.assertIn("Email", preview.rows[0].errors[0])
        self.assertTrue(any("базе" in warning for warning in preview.rows[0].warnings))
        self.assertTrue(any("строка" in warning for warning in preview.rows[1].warnings))

    def test_xlsx_preview_maps_recipient_headers(self):
        uploaded = SimpleUploadedFile(
            "recipients.xlsx",
            _minimal_xlsx(
                [
                    ["Фамилия ребенка", "Имя ребенка", "Телефон"],
                    ["Сидоров", "Илья", "+7 900"],
                ]
            ),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        preview = import_preview_svc.preview_recipient_import(uploaded)

        self.assertEqual(preview.total_rows, 1)
        self.assertEqual(preview.valid_count, 1)
        self.assertEqual(preview.rows[0].values["recipient_last_name"], "Сидоров")
        self.assertEqual(preview.rows[0].values["representative_phone"], "+7 900")


class ReportsServiceTests(_FixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )

    def _make_mixed_funding_group_charge(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        appointment.session_type = Appointment.SessionType.GROUP
        appointment.status = Appointment.Status.COMPLETED
        appointment.attendance_status = Appointment.AttendanceStatus.ATTENDED
        appointment.save(
            update_fields=["session_type", "status", "attendance_status", "updated_at"]
        )
        primary_participant = appointment.participants.get(child=self.child)
        primary_participant.billing_decision = Appointment.BillingDecision.CHARGE
        primary_participant.billing_account = self.account
        primary_participant.appointment_status = Appointment.Status.COMPLETED
        primary_participant.save(
            update_fields=[
                "billing_decision",
                "billing_account",
                "appointment_status",
                "updated_at",
            ]
        )
        personal_funding = FundingSource.objects.create(
            name="Личные mixed",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        second_parent = ParentGuardian.objects.create(
            last_name="Смешанный", first_name="Родитель", phone="+7 900 000-10-11"
        )
        second_child = Child.objects.create(
            last_name="Смешанный", first_name="Участник", primary_parent=second_parent
        )
        second_account = BalanceAccount.objects.create(
            child=second_child,
            funding_source=personal_funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("5"),
        )
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=second_child,
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=second_account,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=Appointment.Status.COMPLETED,
        )
        return appointment

    def _make_same_funding_group_charge(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        appointment.session_type = Appointment.SessionType.GROUP
        appointment.status = Appointment.Status.COMPLETED
        appointment.attendance_status = Appointment.AttendanceStatus.ATTENDED
        appointment.save(
            update_fields=["session_type", "status", "attendance_status", "updated_at"]
        )
        primary_participant = appointment.participants.get(child=self.child)
        primary_participant.billing_decision = Appointment.BillingDecision.CHARGE
        primary_participant.billing_account = self.account
        primary_participant.appointment_status = Appointment.Status.COMPLETED
        primary_participant.save(
            update_fields=[
                "billing_decision",
                "billing_account",
                "appointment_status",
                "updated_at",
            ]
        )
        second_parent = ParentGuardian.objects.create(
            last_name="Грантовый", first_name="Родитель", phone="+7 900 000-10-12"
        )
        second_child = Child.objects.create(
            last_name="Грантовый", first_name="Участник", primary_parent=second_parent
        )
        second_account = BalanceAccount.objects.create(
            child=second_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("5"),
        )
        second_participant = AppointmentParticipant.objects.create(
            appointment=appointment,
            child=second_child,
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=second_account,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=Appointment.Status.COMPLETED,
        )
        for participant, account in (
            (primary_participant, self.account),
            (second_participant, second_account),
        ):
            LedgerEntry.objects.create(
                appointment=appointment,
                appointment_participant=participant,
                account=account,
                entry_type=LedgerEntry.EntryType.DEBIT,
                amount=Decimal("-1"),
                price_snapshot=appointment.service.default_price,
                reason="Тестовое списание участника группы.",
                created_by=self.user,
            )
        return appointment

    def test_tomorrow_overview_returns_summary(self):
        tomorrow = self.day + timedelta(days=1)
        appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(tomorrow, time(10, 0)),
            ends_at=_local(tomorrow, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        overview = reports_svc.tomorrow_overview(tomorrow)
        self.assertEqual(overview.date, tomorrow)
        self.assertEqual(overview.summary["appointments_count"], 1)

    def test_appointment_string_is_group_aware(self):
        appointment = self._make_same_funding_group_charge()
        appointment.room.allow_group_sessions = True
        appointment.room.max_recipient_count = 2
        appointment.room.save(
            update_fields=["allow_group_sessions", "max_recipient_count", "updated_at"]
        )
        appointment.title = ""
        appointment.save(update_fields=["title", "updated_at"])

        label_without_title = str(appointment)

        self.assertIn(self.child.full_name, label_without_title)
        self.assertIn("Грантовый Участник", label_without_title)
        appointment.title = "Группа коммуникации"
        appointment.save(update_fields=["title", "updated_at"])
        self.assertIn("Группа коммуникации", str(appointment))

    def test_tomorrow_overview_excludes_group_after_all_participant_billing_decided(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        appointment.session_type = Appointment.SessionType.GROUP
        appointment.status = Appointment.Status.COMPLETED
        appointment.attendance_status = Appointment.AttendanceStatus.ATTENDED
        appointment.save(
            update_fields=["session_type", "status", "attendance_status", "updated_at"]
        )
        primary_participant = appointment.participants.get(child=self.child)
        primary_participant.billing_decision = Appointment.BillingDecision.CHARGE
        primary_participant.billing_account = self.account
        primary_participant.appointment_status = Appointment.Status.COMPLETED
        primary_participant.save(
            update_fields=[
                "billing_decision",
                "billing_account",
                "appointment_status",
                "updated_at",
            ]
        )
        second_parent = ParentGuardian.objects.create(
            last_name="Сидоров", first_name="Сидор", phone="+7 900 000-10-03"
        )
        second_child = Child.objects.create(
            last_name="Сидоров", first_name="Павел", primary_parent=second_parent
        )
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=second_child,
            billing_decision=Appointment.BillingDecision.DO_NOT_CHARGE,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=Appointment.Status.COMPLETED,
        )

        overview = reports_svc.tomorrow_overview(self.day)

        self.assertNotIn(appointment, overview.needs_billing)
        self.assertEqual(overview.summary["needs_billing_count"], 0)

    def test_tomorrow_overview_includes_group_with_undecided_participant(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        appointment.session_type = Appointment.SessionType.GROUP
        appointment.status = Appointment.Status.COMPLETED
        appointment.attendance_status = Appointment.AttendanceStatus.ATTENDED
        appointment.billing_decision = Appointment.BillingDecision.CHARGE
        appointment.billing_account = self.account
        appointment.save(
            update_fields=[
                "session_type",
                "status",
                "attendance_status",
                "billing_decision",
                "billing_account",
                "updated_at",
            ]
        )
        second_child = Child.objects.create(last_name="Завтра", first_name="Без решения")
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=second_child,
            billing_decision=Appointment.BillingDecision.UNDECIDED,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=Appointment.Status.COMPLETED,
        )

        overview = reports_svc.tomorrow_overview(self.day)

        self.assertIn(appointment, overview.needs_billing)
        self.assertEqual(overview.summary["needs_billing_count"], 1)

    def test_timesheet_groups_by_day(self):
        appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day + timedelta(days=1), time(12, 0)),
            ends_at=_local(self.day + timedelta(days=1), time(12, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        ts = reports_svc.timesheet(self.staff_a, self.day, self.day + timedelta(days=1))
        self.assertEqual(ts.totals.total, 2)
        self.assertEqual(ts.rows[0].total, 1)
        self.assertEqual(ts.rows[1].total, 1)

    def test_timesheet_counts_secondary_staff_assignments(self):
        starts_at = _local(self.day + timedelta(days=1), time(12, 0))
        appointment = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            room=self.room1,
            billing_account=self.account,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=self.staff_b,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=Appointment.Status.COMPLETED,
        )

        ts = reports_svc.timesheet(self.staff_b, self.day, self.day + timedelta(days=1))

        self.assertEqual(ts.totals.total, 1)
        self.assertEqual(ts.totals.completed, 1)
        self.assertEqual(ts.totals.hours, Decimal("0.5"))

    def test_timesheet_includes_legacy_rows_when_assignments_exist(self):
        legacy_appointment = Appointment.objects.create(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day + timedelta(days=1), time(12, 0)),
            ends_at=_local(self.day + timedelta(days=1), time(12, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        AppointmentStaffAssignment.objects.filter(
            appointment=legacy_appointment,
            staff_member=self.staff_a,
        ).delete()

        ts = reports_svc.timesheet(self.staff_a, self.day, self.day + timedelta(days=1))

        self.assertEqual(ts.totals.total, 2)
        self.assertTrue(
            any(
                line.appointment == legacy_appointment and line.assignment is None
                for line in ts.pay_lines
            )
        )

    def test_timesheet_accrues_pay_only_after_charge_decision(self):
        StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("700"),
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)

        before_charge = reports_svc.timesheet(self.staff_a, self.day, self.day)

        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            actor=self.user,
        )
        after_charge = reports_svc.timesheet(self.staff_a, self.day, self.day)

        self.assertEqual(before_charge.totals.payable, 0)
        self.assertEqual(before_charge.totals.pay_amount, Decimal("0"))
        self.assertEqual(after_charge.totals.payable, 1)
        self.assertEqual(after_charge.totals.pay_amount, Decimal("700"))
        self.assertEqual(after_charge.pay_lines[0].rule.service, self.service_log)

    def test_timesheet_handles_charge_without_compensation_rule(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            actor=self.user,
        )

        ts = reports_svc.timesheet(self.staff_a, self.day, self.day)

        self.assertEqual(ts.totals.payable, 1)
        self.assertEqual(ts.totals.pay_amount, Decimal("0"))
        self.assertTrue(ts.pay_lines[0].payable)
        self.assertIsNone(ts.pay_lines[0].rule)
        self.assertEqual(ts.pay_lines[0].amount, Decimal("0"))
        self.assertIn("ставка не задана", ts.pay_lines[0].note)

    def test_timesheet_uses_single_participant_billing_label(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        appointment.billing_decision = Appointment.BillingDecision.UNDECIDED
        appointment.billing_account = None
        appointment.save(update_fields=["billing_decision", "billing_account", "updated_at"])
        AppointmentParticipant.objects.filter(appointment=appointment).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=self.account,
        )

        ts = reports_svc.timesheet(self.staff_a, self.day, self.day)

        self.assertEqual(ts.pay_lines[0].billing_decision, "Списать")

    def test_timesheet_ignores_legacy_charge_when_participants_exist(self):
        StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("700"),
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        appointment.billing_decision = Appointment.BillingDecision.CHARGE
        appointment.billing_account = self.account
        appointment.save(update_fields=["billing_decision", "billing_account", "updated_at"])
        AppointmentParticipant.objects.filter(appointment=appointment).update(
            billing_decision=Appointment.BillingDecision.UNDECIDED,
            billing_account=None,
        )

        ts = reports_svc.timesheet(self.staff_a, self.day, self.day)

        self.assertEqual(ts.totals.payable, 0)
        self.assertFalse(ts.pay_lines[0].payable)
        self.assertEqual(ts.pay_lines[0].amount, Decimal("0"))

    def test_timesheet_summarizes_group_participant_billing_labels(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        AppointmentParticipant.objects.filter(appointment=appointment).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=self.account,
        )
        second_child = Child.objects.create(last_name="Табель", first_name="Группа")
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=second_child,
            billing_decision=Appointment.BillingDecision.UNDECIDED,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )

        ts = reports_svc.timesheet(self.staff_a, self.day, self.day)

        self.assertEqual(ts.pay_lines[0].billing_decision, "списать: 1, не решено: 1")

    def test_timesheet_ignores_charge_participant_without_account(self):
        StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("700"),
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        AppointmentParticipant.objects.filter(appointment=appointment).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=None,
        )

        ts = reports_svc.timesheet(self.staff_a, self.day, self.day)

        self.assertEqual(ts.totals.payable, 0)
        self.assertEqual(ts.totals.pay_amount, Decimal("0"))
        self.assertFalse(ts.pay_lines[0].payable)

    def test_timesheet_prefers_funding_specific_hourly_rule(self):
        StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("1000"),
        )
        StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            funding_source=self.funding,
            rate_type=StaffCompensationRule.RateType.HOURLY,
            amount=Decimal("600"),
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            actor=self.user,
        )

        ts = reports_svc.timesheet(self.staff_a, self.day, self.day)

        self.assertEqual(ts.totals.pay_amount, Decimal("300"))
        self.assertEqual(ts.pay_lines[0].rule.funding_source, self.funding)

    def test_timesheet_prefers_duration_specific_rule(self):
        StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("700"),
        )
        StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("550"),
            min_duration_minutes=30,
            max_duration_minutes=30,
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            actor=self.user,
        )

        ts = reports_svc.timesheet(self.staff_a, self.day, self.day)

        self.assertEqual(ts.totals.pay_amount, Decimal("550"))
        self.assertEqual(ts.pay_lines[0].rule.min_duration_minutes, 30)
        self.assertEqual(ts.pay_lines[0].rule.max_duration_minutes, 30)

    def test_timesheet_uses_grant_allocation_rate(self):
        FundingStaffAllocation.objects.create(
            funding_source=self.funding,
            service=self.service_log,
            staff_member=self.staff_a,
            allocated_sessions=10,
            session_pay_amount=Decimal("520"),
            starts_on=self.day,
            ends_on=self.day,
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            actor=self.user,
        )

        ts = reports_svc.timesheet(self.staff_a, self.day, self.day)

        self.assertEqual(ts.totals.pay_amount, Decimal("520"))
        self.assertIsNone(ts.pay_lines[0].rule)
        self.assertTrue(ts.pay_lines[0].has_rate)
        self.assertIn("520", ts.pay_lines[0].rate_label)
        self.assertIn("грантовая квота", ts.pay_lines[0].rate_label)
        self.assertIn("ставка из распределения", ts.pay_lines[0].note)

    def test_timesheet_uses_generic_rule_for_mixed_group_funding(self):
        generic_rule = StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("700"),
        )
        FundingStaffAllocation.objects.create(
            funding_source=self.funding,
            service=self.service_log,
            staff_member=self.staff_a,
            allocated_sessions=10,
            session_pay_amount=Decimal("520"),
            starts_on=self.day,
            ends_on=self.day,
        )
        self._make_mixed_funding_group_charge()

        ts = reports_svc.timesheet(self.staff_a, self.day, self.day)

        self.assertEqual(ts.totals.payable, 1)
        self.assertEqual(ts.totals.pay_amount, Decimal("700"))
        self.assertEqual(ts.pay_lines[0].rule, generic_rule)
        self.assertIsNone(ts.pay_lines[0].funding_source)
        self.assertTrue(ts.pay_lines[0].has_rate)
        self.assertIn("700", ts.pay_lines[0].rate_label)
        self.assertIn("смешанные источники финансирования", ts.pay_lines[0].note)

    def test_financial_fact_links_single_participant_ledger(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        result = billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            actor=self.user,
        )

        fact = financial_facts_svc.appointment_charge_fact(appointment)

        participant = appointment.participants.get()
        self.assertTrue(fact.is_charged)
        self.assertEqual(fact.funding_source, self.funding)
        self.assertEqual(fact.funding_source_ids, frozenset({self.funding.pk}))
        self.assertEqual(fact.appointment_participant, participant)
        self.assertEqual(fact.ledger_entry, result.entry)
        self.assertEqual(fact.ledger_entries, (result.entry,))
        self.assertFalse(fact.has_mixed_funding)
        self.assertFalse(fact.missing_debit_ledger)
        self.assertEqual(fact.billing_decision_label, "Списать")

    def test_financial_fact_detects_mixed_group_funding_without_single_source(self):
        appointment = self._make_mixed_funding_group_charge()

        fact = financial_facts_svc.appointment_charge_fact(appointment)

        self.assertTrue(fact.is_charged)
        self.assertIsNone(fact.funding_source)
        self.assertIsNone(fact.appointment_participant)
        self.assertIsNone(fact.ledger_entry)
        self.assertEqual(len(fact.charged_participants), 2)
        self.assertEqual(len(fact.funding_source_ids), 2)
        self.assertTrue(fact.has_mixed_funding)
        self.assertTrue(fact.missing_debit_ledger)
        self.assertIn("смешанные источники финансирования", fact.note)

    def test_financial_fact_uses_legacy_charge_only_without_participants(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        appointment.participants.all().delete()
        Appointment.objects.filter(pk=appointment.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=self.account,
        )
        result = LedgerEntry.objects.create(
            appointment=appointment,
            account=self.account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
            price_snapshot=appointment.service.default_price,
            reason="Legacy test charge",
            created_by=self.user,
        )
        appointment.refresh_from_db()

        fact = financial_facts_svc.appointment_charge_fact(appointment)

        self.assertTrue(fact.is_charged)
        self.assertEqual(fact.funding_source, self.funding)
        self.assertEqual(fact.funding_source_ids, frozenset({self.funding.pk}))
        self.assertIsNone(fact.appointment_participant)
        self.assertEqual(fact.ledger_entry, result)
        self.assertFalse(fact.missing_debit_ledger)
        self.assertEqual(fact.note, "списано по занятию")

    def test_financial_fact_exposes_missing_legacy_debit_ledger(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        appointment.participants.all().delete()
        Appointment.objects.filter(pk=appointment.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=self.account,
        )
        appointment.refresh_from_db()
        fact = financial_facts_svc.appointment_charge_fact(appointment)

        self.assertTrue(fact.is_charged)
        self.assertIsNone(fact.ledger_entry)
        self.assertTrue(fact.missing_debit_ledger)

    def test_financial_fact_ignores_legacy_charge_when_participants_exist(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        appointment.billing_decision = Appointment.BillingDecision.CHARGE
        appointment.billing_account = self.account
        appointment.save(update_fields=["billing_decision", "billing_account", "updated_at"])
        AppointmentParticipant.objects.filter(appointment=appointment).update(
            billing_decision=Appointment.BillingDecision.UNDECIDED,
            billing_account=None,
        )

        fact = financial_facts_svc.appointment_charge_fact(appointment)

        self.assertFalse(fact.is_charged)
        self.assertEqual(fact.funding_source_ids, frozenset())
        self.assertEqual(fact.note, "нет решения «Списать» по участникам")
        self.assertEqual(fact.billing_decision_label, "Не решено")

    def test_financial_integrity_audit_has_no_error_for_valid_charge(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            actor=self.user,
        )

        issues = financial_integrity_svc.audit_appointments([appointment])

        self.assertEqual(issues, [])

    def test_financial_integrity_audit_flags_participant_charge_without_account(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        participant = appointment.participants.get()
        AppointmentParticipant.objects.filter(pk=participant.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=None,
        )

        issues = financial_integrity_svc.audit_appointments([appointment])

        issue = self._single_issue(
            issues,
            financial_integrity_svc.FinancialIssueCode.PARTICIPANT_CHARGE_WITHOUT_ACCOUNT,
        )
        self.assertEqual(issue.severity, financial_integrity_svc.FinancialIssueSeverity.ERROR)
        self.assertEqual(issue.participant.pk, participant.pk)

    def test_financial_integrity_audit_flags_missing_debit_ledger(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        participant = appointment.participants.get()
        AppointmentParticipant.objects.filter(pk=participant.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=self.account,
        )

        issues = financial_integrity_svc.audit_appointments([appointment])

        issue = self._single_issue(
            issues,
            financial_integrity_svc.FinancialIssueCode.MISSING_DEBIT_LEDGER,
        )
        self.assertEqual(issue.severity, financial_integrity_svc.FinancialIssueSeverity.ERROR)
        self.assertEqual(issue.participant.pk, participant.pk)

    def test_financial_integrity_audit_flags_stale_legacy_charge(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        Appointment.objects.filter(pk=appointment.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=self.account,
        )
        AppointmentParticipant.objects.filter(appointment=appointment).update(
            billing_decision=Appointment.BillingDecision.UNDECIDED,
            billing_account=None,
        )
        appointment.refresh_from_db()

        issues = financial_integrity_svc.audit_appointments([appointment])

        issue = self._single_issue(
            issues,
            financial_integrity_svc.FinancialIssueCode.STALE_LEGACY_CHARGE_WITH_PARTICIPANTS,
        )
        self.assertEqual(issue.severity, financial_integrity_svc.FinancialIssueSeverity.WARNING)
        self.assertEqual(issue.account, self.account)

    def test_financial_integrity_audit_flags_stale_debit_ledger(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        entry = LedgerEntry.objects.create(
            appointment=appointment,
            account=self.account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
            reason="Stale test ledger",
            created_by=self.user,
        )

        issues = financial_integrity_svc.audit_appointments([appointment])

        issue = self._single_issue(
            issues,
            financial_integrity_svc.FinancialIssueCode.STALE_DEBIT_LEDGER_WITHOUT_CHARGE_FACT,
        )
        self.assertEqual(issue.severity, financial_integrity_svc.FinancialIssueSeverity.WARNING)
        self.assertEqual(issue.ledger_entry, entry)

    def test_financial_integrity_audit_marks_mixed_funding_as_info(self):
        appointment = self._make_mixed_funding_group_charge()
        for participant in appointment.participants.select_related("billing_account"):
            LedgerEntry.objects.create(
                appointment=appointment,
                appointment_participant=participant,
                account=participant.billing_account,
                entry_type=LedgerEntry.EntryType.DEBIT,
                amount=Decimal("-1"),
                reason="Mixed funding test ledger",
                created_by=self.user,
            )

        issues = financial_integrity_svc.audit_appointments([appointment])

        issue = self._single_issue(
            issues,
            financial_integrity_svc.FinancialIssueCode.MIXED_FUNDING_GROUP,
        )
        self.assertEqual(issue.severity, financial_integrity_svc.FinancialIssueSeverity.INFO)
        self.assertFalse(
            any(
                item.code == financial_integrity_svc.FinancialIssueCode.MISSING_DEBIT_LEDGER
                for item in issues
            )
        )

    def test_financial_integrity_issue_key_is_stable_for_same_source_objects(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        participant = appointment.participants.get()
        issue = financial_integrity_svc.FinancialIntegrityIssue(
            code=financial_integrity_svc.FinancialIssueCode.PARTICIPANT_CHARGE_WITHOUT_ACCOUNT,
            severity=financial_integrity_svc.FinancialIssueSeverity.ERROR,
            message="First wording",
            appointment=appointment,
            participant=participant,
        )
        same_source_issue = financial_integrity_svc.FinancialIntegrityIssue(
            code=issue.code,
            severity=financial_integrity_svc.FinancialIssueSeverity.WARNING,
            message="Updated wording",
            appointment=appointment,
            participant=participant,
        )

        issue_key = financial_integrity_svc.financial_integrity_issue_key(issue)

        self.assertEqual(len(issue_key), 64)
        self.assertEqual(
            issue_key,
            financial_integrity_svc.financial_integrity_issue_key(same_source_issue),
        )

    def test_financial_integrity_issue_key_distinguishes_participant_context(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        participant = appointment.participants.get()
        issue = financial_integrity_svc.FinancialIntegrityIssue(
            code=financial_integrity_svc.FinancialIssueCode.PARTICIPANT_CHARGE_WITHOUT_ACCOUNT,
            severity=financial_integrity_svc.FinancialIssueSeverity.ERROR,
            message="Participant issue",
            appointment=appointment,
            participant=participant,
        )
        appointment_issue = financial_integrity_svc.FinancialIntegrityIssue(
            code=issue.code,
            severity=issue.severity,
            message=issue.message,
            appointment=appointment,
        )

        self.assertNotEqual(
            financial_integrity_svc.financial_integrity_issue_key(issue),
            financial_integrity_svc.financial_integrity_issue_key(appointment_issue),
        )

    def test_financial_integrity_issue_key_distinguishes_ledger_context(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        first_entry = LedgerEntry.objects.create(
            appointment=appointment,
            account=self.account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
            reason="First stale test ledger",
            created_by=self.user,
        )
        second_entry = LedgerEntry.objects.create(
            appointment=appointment,
            account=self.account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
            reason="Second stale test ledger",
            created_by=self.user,
        )
        first_issue = financial_integrity_svc.FinancialIntegrityIssue(
            code=financial_integrity_svc.FinancialIssueCode.STALE_DEBIT_LEDGER_WITHOUT_CHARGE_FACT,
            severity=financial_integrity_svc.FinancialIssueSeverity.WARNING,
            message="Stale ledger",
            appointment=appointment,
            ledger_entry=first_entry,
            account=self.account,
            funding_source=self.account.funding_source,
        )
        second_issue = financial_integrity_svc.FinancialIntegrityIssue(
            code=first_issue.code,
            severity=first_issue.severity,
            message=first_issue.message,
            appointment=appointment,
            ledger_entry=second_entry,
            account=self.account,
            funding_source=self.account.funding_source,
        )

        self.assertNotEqual(
            financial_integrity_svc.financial_integrity_issue_key(first_issue),
            financial_integrity_svc.financial_integrity_issue_key(second_issue),
        )

    def test_financial_integrity_check_run_creates_persisted_finding(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        participant = appointment.participants.get()
        AppointmentParticipant.objects.filter(pk=participant.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=None,
        )

        run = financial_integrity_checks_svc.run_financial_integrity_check(
            appointments=[appointment],
            requested_by=self.user,
        )

        finding = FinancialIntegrityFinding.objects.get()
        self.assertEqual(run.status, FinancialIntegrityCheckRun.Status.COMPLETED)
        self.assertEqual(run.candidate_count, 1)
        self.assertEqual(run.issue_count, 1)
        self.assertEqual(run.error_count, 1)
        self.assertEqual(run.warning_count, 0)
        self.assertEqual(run.info_count, 0)
        self.assertEqual(finding.status, FinancialIntegrityFinding.Status.OPEN)
        self.assertEqual(
            finding.code,
            financial_integrity_svc.FinancialIssueCode.PARTICIPANT_CHARGE_WITHOUT_ACCOUNT,
        )
        self.assertEqual(finding.severity, FinancialIntegrityFinding.Severity.ERROR)
        self.assertEqual(finding.appointment, appointment)
        self.assertEqual(finding.appointment_participant.pk, participant.pk)
        self.assertEqual(finding.first_seen_run, run)
        self.assertEqual(finding.last_seen_run, run)
        self.assertEqual(finding.payload["appointment_id"], appointment.pk)
        self.assertEqual(finding.payload["participant_id"], participant.pk)
        self.assertTrue(finding.participant_name)

    def test_financial_integrity_check_run_does_not_duplicate_same_finding(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        participant = appointment.participants.get()
        AppointmentParticipant.objects.filter(pk=participant.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=None,
        )

        first_run = financial_integrity_checks_svc.run_financial_integrity_check(
            appointments=[appointment],
            requested_by=self.user,
        )
        second_run = financial_integrity_checks_svc.run_financial_integrity_check(
            appointments=[appointment],
            requested_by=self.user,
        )

        finding = FinancialIntegrityFinding.objects.get()
        self.assertEqual(FinancialIntegrityFinding.objects.count(), 1)
        self.assertEqual(finding.first_seen_run, first_run)
        self.assertEqual(finding.last_seen_run, second_run)

    def test_financial_integrity_check_run_resolves_unseen_finding(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        participant = appointment.participants.get()
        AppointmentParticipant.objects.filter(pk=participant.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=None,
        )
        financial_integrity_checks_svc.run_financial_integrity_check(
            appointments=[appointment],
            requested_by=self.user,
        )
        AppointmentParticipant.objects.filter(pk=participant.pk).update(
            billing_decision=Appointment.BillingDecision.UNDECIDED,
            billing_account=None,
        )

        resolve_run = financial_integrity_checks_svc.run_financial_integrity_check(
            appointments=[appointment],
            requested_by=self.user,
        )

        finding = FinancialIntegrityFinding.objects.get()
        self.assertEqual(resolve_run.issue_count, 0)
        self.assertEqual(finding.status, FinancialIntegrityFinding.Status.RESOLVED)
        self.assertEqual(finding.resolved_run, resolve_run)
        self.assertIsNotNone(finding.resolved_at)

    def test_financial_integrity_check_run_reopens_resolved_finding(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        participant = appointment.participants.get()
        AppointmentParticipant.objects.filter(pk=participant.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=None,
        )
        financial_integrity_checks_svc.run_financial_integrity_check(
            appointments=[appointment],
            requested_by=self.user,
        )
        AppointmentParticipant.objects.filter(pk=participant.pk).update(
            billing_decision=Appointment.BillingDecision.UNDECIDED,
            billing_account=None,
        )
        financial_integrity_checks_svc.run_financial_integrity_check(
            appointments=[appointment],
            requested_by=self.user,
        )
        AppointmentParticipant.objects.filter(pk=participant.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=None,
        )

        reopen_run = financial_integrity_checks_svc.run_financial_integrity_check(
            appointments=[appointment],
            requested_by=self.user,
        )

        finding = FinancialIntegrityFinding.objects.get()
        self.assertEqual(reopen_run.issue_count, 1)
        self.assertEqual(finding.status, FinancialIntegrityFinding.Status.OPEN)
        self.assertEqual(finding.last_seen_run, reopen_run)
        self.assertIsNone(finding.resolved_at)
        self.assertIsNone(finding.resolved_run)

    def test_financial_integrity_check_run_counts_severities(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        participant = appointment.participants.get()
        AppointmentParticipant.objects.filter(pk=participant.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=None,
        )
        stale_appointment = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_b,
            service=self.service_log,
            room=self.room2,
            starts_at=_local(self.day, time(11, 0)),
            ends_at=_local(self.day, time(11, 30)),
        )
        LedgerEntry.objects.create(
            appointment=stale_appointment,
            account=self.account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
            reason="Stale check run ledger",
            created_by=self.user,
        )

        run = financial_integrity_checks_svc.run_financial_integrity_check(
            appointments=[appointment, stale_appointment],
            requested_by=self.user,
        )

        self.assertEqual(run.issue_count, 2)
        self.assertEqual(run.error_count, 1)
        self.assertEqual(run.warning_count, 1)
        self.assertEqual(run.info_count, 0)
        self.assertEqual(FinancialIntegrityFinding.objects.count(), 2)

    def test_run_financial_integrity_check_command_persists_findings(self):
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        LedgerEntry.objects.create(
            appointment=appointment,
            account=self.account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
            reason="Stale command ledger",
            created_by=self.user,
        )
        stdout = StringIO()

        call_command("run_financial_integrity_check", stdout=stdout)

        run = FinancialIntegrityCheckRun.objects.get()
        finding = FinancialIntegrityFinding.objects.get()
        self.assertEqual(run.run_type, FinancialIntegrityCheckRun.RunType.MANAGEMENT_COMMAND)
        self.assertEqual(run.status, FinancialIntegrityCheckRun.Status.COMPLETED)
        self.assertEqual(run.issue_count, 1)
        self.assertEqual(run.warning_count, 1)
        self.assertEqual(
            finding.code,
            financial_integrity_svc.FinancialIssueCode.STALE_DEBIT_LEDGER_WITHOUT_CHARGE_FACT,
        )
        self.assertIn("Financial integrity check", stdout.getvalue())
        self.assertIn("1 issues", stdout.getvalue())

    def _single_issue(
        self,
        issues: list[financial_integrity_svc.FinancialIntegrityIssue],
        code: str,
    ) -> financial_integrity_svc.FinancialIntegrityIssue:
        matching = [issue for issue in issues if issue.code == code]
        self.assertEqual(len(matching), 1, [issue.code for issue in issues])
        return matching[0]

    def test_payroll_generation_is_idempotent_for_charged_assignment(self):
        StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("700"),
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            actor=self.user,
        )

        first = payroll_svc.generate_accruals_for_staff(
            self.staff_a,
            date_from=self.day,
            date_to=self.day,
            actor=self.user,
        )
        second = payroll_svc.generate_accruals_for_staff(
            self.staff_a,
            date_from=self.day,
            date_to=self.day,
            actor=self.user,
        )

        accrual = PayrollAccrual.objects.get(staff_member=self.staff_a)
        self.assertEqual(first.created, 1)
        self.assertEqual(second.updated, 1)
        self.assertEqual(PayrollAccrual.objects.count(), 1)
        self.assertEqual(accrual.amount, Decimal("700"))
        self.assertEqual(accrual.status, PayrollAccrual.Status.DRAFT)

    def test_payroll_generation_ignores_legacy_charge_when_participants_exist(self):
        StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("700"),
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        appointment.billing_decision = Appointment.BillingDecision.CHARGE
        appointment.billing_account = self.account
        appointment.save(update_fields=["billing_decision", "billing_account", "updated_at"])
        AppointmentParticipant.objects.filter(appointment=appointment).update(
            billing_decision=Appointment.BillingDecision.UNDECIDED,
            billing_account=None,
        )

        result = payroll_svc.generate_accruals_for_staff(
            self.staff_a,
            date_from=self.day,
            date_to=self.day,
            actor=self.user,
        )

        self.assertEqual(result.skipped_no_charge, 1)
        self.assertFalse(PayrollAccrual.objects.exists())

    def test_payroll_generation_includes_legacy_rows_when_assignments_exist(self):
        StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("700"),
        )
        normalized_appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            normalized_appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            actor=self.user,
        )
        legacy_appointment = Appointment.objects.create(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day + timedelta(days=1), time(12, 0)),
            ends_at=_local(self.day + timedelta(days=1), time(12, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        billing_svc.apply_decision(
            legacy_appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            actor=self.user,
        )
        AppointmentStaffAssignment.objects.filter(
            appointment=legacy_appointment,
            staff_member=self.staff_a,
        ).delete()

        result = payroll_svc.generate_accruals_for_staff(
            self.staff_a,
            date_from=self.day,
            date_to=self.day + timedelta(days=1),
            actor=self.user,
        )

        self.assertEqual(result.created, 2)
        self.assertEqual(PayrollAccrual.objects.count(), 2)
        legacy_accrual = PayrollAccrual.objects.get(appointment=legacy_appointment)
        self.assertIsNone(legacy_accrual.staff_assignment)
        self.assertEqual(legacy_accrual.amount, Decimal("700"))

    def test_payroll_generation_prefers_duration_specific_rule(self):
        StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("700"),
        )
        duration_rule = StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("550"),
            min_duration_minutes=30,
            max_duration_minutes=30,
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            actor=self.user,
        )

        result = payroll_svc.generate_accruals_for_staff(
            self.staff_a,
            date_from=self.day,
            date_to=self.day,
            actor=self.user,
        )

        accrual = PayrollAccrual.objects.get(staff_member=self.staff_a)
        self.assertEqual(result.created, 1)
        self.assertEqual(accrual.pay_rule, duration_rule)
        self.assertEqual(accrual.amount, Decimal("550"))

    def test_payroll_generation_uses_grant_allocation_rate(self):
        FundingStaffAllocation.objects.create(
            funding_source=self.funding,
            service=self.service_log,
            staff_member=self.staff_a,
            allocated_sessions=10,
            session_pay_amount=Decimal("520"),
            starts_on=self.day,
            ends_on=self.day,
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            actor=self.user,
        )

        result = payroll_svc.generate_accruals_for_staff(
            self.staff_a,
            date_from=self.day,
            date_to=self.day,
            actor=self.user,
        )

        accrual = PayrollAccrual.objects.get(staff_member=self.staff_a)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.skipped_no_rule, 0)
        self.assertIsNone(accrual.pay_rule)
        self.assertEqual(accrual.rate_type_snapshot, StaffCompensationRule.RateType.PER_SESSION)
        self.assertEqual(accrual.rate_amount_snapshot, Decimal("520"))
        self.assertEqual(accrual.amount, Decimal("520"))

    def test_payroll_generation_does_not_link_single_participant_for_group_charge(self):
        FundingStaffAllocation.objects.create(
            funding_source=self.funding,
            service=self.service_log,
            staff_member=self.staff_a,
            allocated_sessions=10,
            session_pay_amount=Decimal("520"),
            starts_on=self.day,
            ends_on=self.day,
        )
        self._make_same_funding_group_charge()

        result = payroll_svc.generate_accruals_for_staff(
            self.staff_a,
            date_from=self.day,
            date_to=self.day,
            actor=self.user,
        )

        accrual = PayrollAccrual.objects.get(staff_member=self.staff_a)
        self.assertEqual(result.created, 1)
        self.assertEqual(accrual.funding_source, self.funding)
        self.assertIsNone(accrual.pay_rule)
        self.assertIsNone(accrual.appointment_participant)
        self.assertIsNone(accrual.ledger_entry)
        self.assertEqual(accrual.rate_amount_snapshot, Decimal("520"))
        self.assertEqual(accrual.amount, Decimal("520"))
        self.assertIn("списано участников: 2", accrual.note)

    def test_payroll_generation_uses_generic_rule_for_mixed_group_funding(self):
        generic_rule = StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            rate_type=StaffCompensationRule.RateType.PER_SESSION,
            amount=Decimal("700"),
        )
        FundingStaffAllocation.objects.create(
            funding_source=self.funding,
            service=self.service_log,
            staff_member=self.staff_a,
            allocated_sessions=10,
            session_pay_amount=Decimal("520"),
            starts_on=self.day,
            ends_on=self.day,
        )
        self._make_mixed_funding_group_charge()

        result = payroll_svc.generate_accruals_for_staff(
            self.staff_a,
            date_from=self.day,
            date_to=self.day,
            actor=self.user,
        )

        accrual = PayrollAccrual.objects.get(staff_member=self.staff_a)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.skipped_no_rule, 0)
        self.assertEqual(accrual.pay_rule, generic_rule)
        self.assertIsNone(accrual.funding_source)
        self.assertIsNone(accrual.appointment_participant)
        self.assertEqual(accrual.amount, Decimal("700"))
        self.assertIn("смешанные источники финансирования", accrual.note)

    def test_payroll_sheet_creation_and_approval(self):
        StaffCompensationRule.objects.create(
            staff_member=self.staff_a,
            service=self.service_log,
            rate_type=StaffCompensationRule.RateType.HOURLY,
            amount=Decimal("600"),
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            actor=self.user,
        )

        sheet = payroll_svc.create_payroll_sheet_for_staff(
            self.staff_a,
            date_from=self.day,
            date_to=self.day,
            actor=self.user,
        )
        payroll_svc.approve_payroll_sheet(sheet, actor=self.user)

        sheet.refresh_from_db()
        accrual = PayrollAccrual.objects.get(staff_member=self.staff_a)
        self.assertEqual(sheet.total_amount, Decimal("300"))
        self.assertEqual(sheet.lines.count(), 1)
        self.assertEqual(sheet.status, PayrollSheet.Status.APPROVED)
        self.assertEqual(accrual.status, PayrollAccrual.Status.APPROVED)

    def test_grant_report_sums_initial_topups_charges(self):
        billing_svc.top_up_account(
            self.account,
            amount=Decimal("5"),
            method=Payment.Method.GRANT_TRANSFER,
            actor=self.user,
        )
        appt = Appointment.objects.first()
        billing_svc.apply_decision(
            appt,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            actor=self.user,
        )
        report = reports_svc.grant_report(
            self.funding,
            timezone.localdate() - timedelta(days=1),
            timezone.localdate() + timedelta(days=1),
        )
        self.assertEqual(len(report.rows), 1)
        row = report.rows[0]
        self.assertEqual(row.initial_amount, Decimal("10"))
        self.assertEqual(row.topups, Decimal("5"))
        self.assertEqual(row.charges, Decimal("1"))
        self.assertEqual(row.current_balance, Decimal("14"))

    def test_grant_report_counts_group_participant_account_appointments(self):
        second_parent = ParentGuardian.objects.create(
            last_name="Петров", first_name="Петр", phone="+7 900 000-10-02"
        )
        second_child = Child.objects.create(
            last_name="Петров", first_name="Илья", primary_parent=second_parent
        )
        second_account = BalanceAccount.objects.create(
            child=second_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("5"),
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        appointment.session_type = Appointment.SessionType.GROUP
        appointment.status = Appointment.Status.COMPLETED
        appointment.save(update_fields=["session_type", "status", "updated_at"])
        participant = AppointmentParticipant.objects.create(
            appointment=appointment,
            child=second_child,
            billing_account=second_account,
            billing_decision=Appointment.BillingDecision.CHARGE,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=Appointment.Status.COMPLETED,
        )
        entry = LedgerEntry.objects.create(
            account=second_account,
            appointment=appointment,
            appointment_participant=participant,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
            reason="Списание группового участника",
            created_by=self.user,
        )
        LedgerEntry.objects.filter(pk=entry.pk).update(created_at=appointment.starts_at)

        report = reports_svc.grant_report(self.funding, self.day, self.day)

        row = next(item for item in report.rows if item.account == second_account)
        self.assertEqual(row.appointments_count, 1)
        self.assertEqual(row.completed_count, 1)
        self.assertEqual(row.charges, Decimal("1"))
        self.assertEqual(row.current_balance, Decimal("4"))

    def test_grant_report_includes_recipient_allocations(self):
        GrantRecipientAllocation.objects.create(
            funding_source=self.funding,
            child=self.child,
            service=self.service_log,
            allocated_sessions=10,
            balance_account=self.account,
            valid_from=timezone.localdate() - timedelta(days=1),
            valid_until=self.day + timedelta(days=30),
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            actor=self.user,
        )

        report = reports_svc.grant_report(
            self.funding,
            timezone.localdate() - timedelta(days=1),
            timezone.localdate() + timedelta(days=1),
        )

        row = report.recipient_allocation_rows[0]
        self.assertEqual(row.child, self.child)
        self.assertEqual(row.service, self.service_log)
        self.assertEqual(row.allocated_sessions, 10)
        self.assertEqual(row.charged_sessions, Decimal("1"))
        self.assertEqual(row.remaining_sessions, Decimal("9"))

    def test_grant_report_recipient_allocation_filters_service_and_valid_dates(self):
        GrantRecipientAllocation.objects.create(
            funding_source=self.funding,
            child=self.child,
            service=self.service_log,
            allocated_sessions=10,
            balance_account=self.account,
            valid_from=self.day,
            valid_until=self.day,
        )
        log_appt = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            log_appt,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            actor=self.user,
        )
        LedgerEntry.objects.filter(appointment=log_appt).update(created_at=log_appt.starts_at)
        afk_appt = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_afk,
            service=self.service_afk,
            starts_at=_local(self.day, time(11, 0)),
            ends_at=_local(self.day, time(11, 45)),
            room=self.room2,
            billing_account=self.account,
        )
        billing_svc.apply_decision(
            afk_appt,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            actor=self.user,
        )
        LedgerEntry.objects.filter(appointment=afk_appt).update(created_at=afk_appt.starts_at)
        future_appt = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day + timedelta(days=2), time(10, 0)),
            ends_at=_local(self.day + timedelta(days=2), time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        billing_svc.apply_decision(
            future_appt,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            actor=self.user,
        )
        LedgerEntry.objects.filter(appointment=future_appt).update(
            created_at=future_appt.starts_at
        )

        report = reports_svc.grant_report(self.funding, self.day, self.day + timedelta(days=2))

        row = report.recipient_allocation_rows[0]
        self.assertEqual(row.charged_sessions, Decimal("1"))

    def test_grant_report_includes_service_quota_and_staff_allocation(self):
        quota = FundingServiceQuota.objects.create(
            funding_source=self.funding,
            service=self.service_log,
            planned_sessions=300,
        )
        FundingStaffAllocation.objects.create(
            service_quota=quota,
            funding_source=self.funding,
            service=self.service_log,
            staff_member=self.staff_a,
            allocated_sessions=10,
            session_pay_amount=Decimal("500"),
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            actor=self.user,
        )

        report = reports_svc.grant_report(self.funding, self.day, self.day)

        quota_row = report.quota_rows[0]
        staff_row = quota_row.staff_rows[0]
        self.assertEqual(quota_row.planned_sessions, 300)
        self.assertEqual(quota_row.allocated_sessions, 10)
        self.assertEqual(quota_row.charged_sessions, 1)
        self.assertEqual(quota_row.remaining_sessions, 299)
        self.assertEqual(staff_row.staff_member, self.staff_a)
        self.assertEqual(staff_row.charged_sessions, 1)
        self.assertEqual(staff_row.remaining_sessions, 9)
        self.assertEqual(staff_row.session_pay_amount, Decimal("500"))

    def test_grant_report_quota_ignores_legacy_charge_when_participants_exist(self):
        quota = FundingServiceQuota.objects.create(
            funding_source=self.funding,
            service=self.service_log,
            planned_sessions=300,
        )
        FundingStaffAllocation.objects.create(
            service_quota=quota,
            funding_source=self.funding,
            service=self.service_log,
            staff_member=self.staff_a,
            allocated_sessions=10,
            session_pay_amount=Decimal("500"),
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        appointment.billing_decision = Appointment.BillingDecision.CHARGE
        appointment.billing_account = self.account
        appointment.save(update_fields=["billing_decision", "billing_account", "updated_at"])
        AppointmentParticipant.objects.filter(appointment=appointment).update(
            billing_decision=Appointment.BillingDecision.UNDECIDED,
            billing_account=None,
        )

        report = reports_svc.grant_report(self.funding, self.day, self.day)

        quota_row = report.quota_rows[0]
        staff_row = quota_row.staff_rows[0]
        self.assertEqual(quota_row.charged_sessions, 0)
        self.assertEqual(staff_row.charged_sessions, 0)

    def test_grant_report_counts_staff_allocation_fact_inside_allocation_dates(self):
        quota = FundingServiceQuota.objects.create(
            funding_source=self.funding,
            service=self.service_log,
            planned_sessions=300,
        )
        first_period = FundingStaffAllocation.objects.create(
            service_quota=quota,
            funding_source=self.funding,
            service=self.service_log,
            staff_member=self.staff_a,
            allocated_sessions=10,
            starts_on=self.day,
            ends_on=self.day + timedelta(days=9),
        )
        second_period = FundingStaffAllocation.objects.create(
            service_quota=quota,
            funding_source=self.funding,
            service=self.service_log,
            staff_member=self.staff_a,
            allocated_sessions=10,
            starts_on=self.day + timedelta(days=10),
            ends_on=self.day + timedelta(days=20),
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            actor=self.user,
        )

        report = reports_svc.grant_report(self.funding, self.day, self.day + timedelta(days=20))

        staff_rows = {row.allocation.pk: row for row in report.quota_rows[0].staff_rows}
        self.assertEqual(staff_rows[first_period.pk].charged_sessions, 1)
        self.assertEqual(staff_rows[first_period.pk].remaining_sessions, 9)
        self.assertEqual(staff_rows[second_period.pk].charged_sessions, 0)
        self.assertEqual(staff_rows[second_period.pk].remaining_sessions, 10)

    def test_grant_report_counts_quota_fact_inside_quota_dates(self):
        quota = FundingServiceQuota.objects.create(
            funding_source=self.funding,
            service=self.service_log,
            planned_sessions=10,
            starts_on=self.day,
            ends_on=self.day,
        )
        FundingStaffAllocation.objects.create(
            service_quota=quota,
            funding_source=self.funding,
            service=self.service_log,
            staff_member=self.staff_a,
            allocated_sessions=10,
            starts_on=self.day,
            ends_on=self.day,
        )
        appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            actor=self.user,
        )
        later_appointment = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day + timedelta(days=1), time(10, 0)),
            ends_at=_local(self.day + timedelta(days=1), time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        billing_svc.apply_decision(
            later_appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            actor=self.user,
        )

        report = reports_svc.grant_report(self.funding, self.day, self.day + timedelta(days=1))

        quota_row = report.quota_rows[0]
        self.assertEqual(quota_row.charged_sessions, 1)
        self.assertEqual(quota_row.remaining_sessions, 9)

    def test_grant_report_direct_allocation_ignores_unallocated_staff_fact(self):
        FundingStaffAllocation.objects.create(
            service_quota=None,
            funding_source=self.funding,
            service=self.service_log,
            staff_member=self.staff_a,
            allocated_sessions=10,
            starts_on=self.day,
            ends_on=self.day,
        )
        allocated_appointment = Appointment.objects.get(staff_member=self.staff_a)
        billing_svc.apply_decision(
            allocated_appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            actor=self.user,
        )
        other_staff_appointment = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_b,
            service=self.service_log,
            starts_at=_local(self.day, time(11, 0)),
            ends_at=_local(self.day, time(11, 30)),
            room=self.room2,
            billing_account=self.account,
        )
        billing_svc.apply_decision(
            other_staff_appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
            actor=self.user,
        )

        report = reports_svc.grant_report(self.funding, self.day, self.day)

        quota_row = report.quota_rows[0]
        self.assertIsNone(quota_row.quota)
        self.assertEqual(quota_row.charged_sessions, 1)
        self.assertEqual(quota_row.remaining_sessions, 9)


class NotificationsServiceTests(_FixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.appt = appt_svc.create_appointment(
            child=self.child,
            staff_member=self.staff_a,
            service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1,
            billing_account=self.account,
        )
        self.confirmation = AppointmentConfirmation.objects.create(
            appointment=self.appt,
            target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
            representative=self.parent,
            email=self.parent.email,
            subject="Test",
            message="Test body",
            sent_by=self.user,
        )

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_send_confirmation_email_marks_sent(self):
        self.assertTrue(notif_svc.send_confirmation_email(self.confirmation.pk))
        self.confirmation.refresh_from_db()
        self.assertEqual(
            self.confirmation.delivery_status,
            AppointmentConfirmation.DeliveryStatus.SENT,
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Test body", mail.outbox[0].body)
        self.assertIn(f"/confirmations/{self.confirmation.token}/", mail.outbox[0].body)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="rehab@example.local",
    )
    def test_build_confirmation_email_contains_url(self):
        email = notif_svc.build_confirmation_email(self.appt)
        self.assertIn("Логопед", email.body)
        self.assertIn("Иванов", email.body)

    def test_build_confirmation_email_uses_group_participants_and_staff(self):
        second_child = Child.objects.create(
            last_name="Петров", first_name="Илья", primary_parent=self.parent
        )
        assistant = StaffMember.objects.create(full_name="Борис Б.")
        AppointmentParticipant.objects.create(
            appointment=self.appt,
            child=second_child,
            starts_at_snapshot=self.appt.starts_at,
            ends_at_snapshot=self.appt.ends_at,
            appointment_status=self.appt.status,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=self.appt,
            staff_member=assistant,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=self.appt.starts_at,
            ends_at_snapshot=self.appt.ends_at,
            appointment_status=self.appt.status,
        )

        email = notif_svc.build_confirmation_email(self.appt)

        self.assertIn(self.child.full_name, email.body)
        self.assertIn(second_child.full_name, email.body)
        self.assertIn(self.staff_a.full_name, email.body)
        self.assertIn(assistant.full_name, email.body)
