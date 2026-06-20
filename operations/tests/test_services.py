"""Тесты сервисного слоя."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentConfirmation,
    BalanceAccount,
    Child,
    FundingSource,
    LedgerEntry,
    ParentGuardian,
    Payment,
    ProgramBlock,
    Room,
    Service,
    StaffAvailability,
    StaffMember,
    TimeOffRequest,
    TreatmentProgram,
)
from operations.services import (
    appointments as appt_svc,
    billing as billing_svc,
    notifications as notif_svc,
    program_wizard as wizard_svc,
    reports as reports_svc,
    scheduling as sched_svc,
)


def _local(day, clock):
    return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())


class _FixturesMixin:
    @classmethod
    def setUpTestData(cls):
        cls.parent = ParentGuardian.objects.create(
            last_name="Иванов", first_name="Иван",
            phone="+7 900 000-10-01", email="parent@example.local",
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
            name="Логопед", code="LOG", category=Service.Category.SPEECH,
            default_duration_minutes=30, default_price=Decimal("1500"),
        )
        cls.service_afk = Service.objects.create(
            name="АФК", code="AFK", category=Service.Category.PHYSICAL,
            default_duration_minutes=45, default_price=Decimal("1800"),
        )
        cls.room1 = Room.objects.create(name="Кабинет 1")
        cls.room2 = Room.objects.create(name="Кабинет 2")
        cls.funding = FundingSource.objects.create(
            name="Грант", source_type=FundingSource.SourceType.GRANT,
            transfer_policy=FundingSource.TransferPolicy.WITHIN_CHILD,
        )
        cls.account = BalanceAccount.objects.create(
            child=cls.child, funding_source=cls.funding,
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
            child=self.child, staff_member=self.staff_a, service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1, billing_account=self.account,
        )
        self.assertEqual(appt.status, Appointment.Status.CONFIRMED)

    def test_reschedule_creates_new_and_marks_old(self):
        appt = appt_svc.create_appointment(
            child=self.child, staff_member=self.staff_a, service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1, billing_account=self.account,
        )
        new_day = self.day + timedelta(days=2)
        result = appt_svc.reschedule(
            appt,
            starts_at=_local(new_day, time(11, 0)),
            ends_at=_local(new_day, time(11, 30)),
            staff_member=self.staff_b, room=self.room1, note="Клиент попросил",
        )
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.RESCHEDULED)
        self.assertEqual(result.new.source_appointment, appt)
        self.assertEqual(result.new.staff_member, self.staff_b)

    def test_cancel_sets_note_with_reason(self):
        appt = appt_svc.create_appointment(
            child=self.child, staff_member=self.staff_a, service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1, billing_account=self.account,
        )
        appt_svc.cancel(appt, status=Appointment.Status.NO_SHOW, reason_text="Болезнь получателя")
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.NO_SHOW)
        self.assertIn("Болезнь получателя", appt.admin_note)

    def test_record_attendance_completed(self):
        appt = appt_svc.create_appointment(
            child=self.child, staff_member=self.staff_a, service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1, billing_account=self.account,
        )
        appt_svc.record_attendance(appt, action="completed", note="Всё ок")
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.COMPLETED)
        self.assertEqual(appt.attendance_status, Appointment.AttendanceStatus.ATTENDED)
        self.assertEqual(appt.specialist_note, "Всё ок")

    def test_record_attendance_invalid_action(self):
        appt = appt_svc.create_appointment(
            child=self.child, staff_member=self.staff_a, service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1, billing_account=self.account,
        )
        with self.assertRaises(ValueError):
            appt_svc.record_attendance(appt, action="weird")


class BillingServiceTests(_FixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.appt = appt_svc.create_appointment(
            child=self.child, staff_member=self.staff_a, service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1, billing_account=self.account,
        )

    def test_apply_charge_creates_ledger_entry(self):
        result = billing_svc.apply_decision(
            self.appt, decision=Appointment.BillingDecision.CHARGE,
            account=self.account, amount=Decimal("-1"),
            reason="тест", actor=self.user,
        )
        self.appt.refresh_from_db()
        self.assertEqual(self.appt.billing_decision, Appointment.BillingDecision.CHARGE)
        self.assertEqual(self.account.current_balance, Decimal("9"))
        self.assertIsNotNone(result.entry)
        self.assertEqual(result.entry.entry_type, LedgerEntry.EntryType.DEBIT)

    def test_apply_do_not_charge_unlinks_entries(self):
        billing_svc.apply_decision(
            self.appt, decision=Appointment.BillingDecision.CHARGE,
            account=self.account, amount=Decimal("-1"), actor=self.user,
        )
        result = billing_svc.apply_decision(
            self.appt, decision=Appointment.BillingDecision.DO_NOT_CHARGE, actor=self.user,
        )
        self.appt.refresh_from_db()
        self.assertEqual(self.appt.billing_decision, Appointment.BillingDecision.DO_NOT_CHARGE)
        self.assertEqual(LedgerEntry.objects.filter(appointment=self.appt).count(), 0)
        self.assertGreaterEqual(result.removed, 1)

    def test_top_up_creates_payment_and_ledger(self):
        payment = billing_svc.top_up_account(
            self.account, amount=Decimal("5"),
            method=Payment.Method.GRANT_TRANSFER,
            reference="ГР-01", actor=self.user,
        )
        self.assertEqual(self.account.current_balance, Decimal("15"))
        self.assertEqual(LedgerEntry.objects.filter(account=self.account, entry_type=LedgerEntry.EntryType.CREDIT).count(), 1)
        self.assertEqual(payment.amount, Decimal("5"))

    def test_top_up_rejects_non_positive(self):
        with self.assertRaises(ValueError):
            billing_svc.top_up_account(self.account, amount=Decimal("-1"))

    def test_transfer_within_child(self):
        self.account.service_scope = BalanceAccount.ServiceScope.SPECIFIC_SERVICE
        self.account.service = self.service_log
        self.account.save()
        target = BalanceAccount.objects.create(
            child=self.child, funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service_log, initial_amount=Decimal("0"),
        )
        debit, credit = billing_svc.transfer_between_accounts(
            from_account=self.account, to_account=target,
            amount=Decimal("3"), reason="Перераспределение", actor=self.user,
        )
        self.assertEqual(debit.amount, Decimal("-3"))
        self.assertEqual(credit.amount, Decimal("3"))

    def test_transfer_to_other_child_blocked_by_within_child(self):
        other_child = Child.objects.create(last_name="Петров", first_name="С", primary_parent=self.parent)
        target = BalanceAccount.objects.create(
            child=other_child, funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("0"),
        )
        with self.assertRaises(ValueError):
            billing_svc.transfer_between_accounts(
                from_account=self.account, to_account=target,
                amount=Decimal("3"), reason="X",
            )


class SchedulingServiceTests(_FixturesMixin, TestCase):
    def test_find_overlaps_child(self):
        appt_svc.create_appointment(
            child=self.child, staff_member=self.staff_a, service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1, billing_account=self.account,
        )
        report = sched_svc.find_overlaps(
            _local(self.day, time(10, 15)),
            _local(self.day, time(10, 45)),
            child=self.child, staff_member=self.staff_b, room=self.room2,
        )
        self.assertIsNotNone(report.child_conflict)
        self.assertIn("получателя", " ".join(report.human_messages()))

    def test_find_free_slots_filters_overlaps(self):
        appt_svc.create_appointment(
            child=self.child, staff_member=self.staff_a, service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1, billing_account=self.account,
        )
        slots = sched_svc.find_free_slots(
            self.day, 30, staff_member=self.staff_a, child=self.child, room=self.room1
        )
        for slot in slots:
            self.assertNotEqual(slot.time(), time(10, 0))

    def test_is_within_availability_blocked_by_time_off(self):
        TimeOffRequest.objects.create(
            staff_member=self.staff_a,
            request_type=TimeOffRequest.RequestType.SICK,
            starts_on=self.day, ends_on=self.day,
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
            staff_member=self.staff_a, weekday=self.day.weekday(),
            starts_at=time(9, 0), ends_at=time(10, 0),
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

    def test_mass_reschedule_cancels_and_creates_confirmations(self):
        appt_svc.create_appointment(
            child=self.child, staff_member=self.staff_a, service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1, billing_account=self.account,
        )
        appt_svc.create_appointment(
            child=self.child, staff_member=self.staff_a, service=self.service_log,
            starts_at=_local(self.day + timedelta(days=1), time(10, 0)),
            ends_at=_local(self.day + timedelta(days=1), time(10, 30)),
            room=self.room1, billing_account=self.account,
        )
        result = sched_svc.mass_reschedule(
            self.staff_a, date_from=self.day, date_to=self.day + timedelta(days=1),
            reason="Болезнь", actor=self.user,
        )
        self.assertEqual(len(result.cancelled), 2)
        self.assertEqual(len(result.confirmations), 2)
        for a in result.cancelled:
            self.assertEqual(a.status, Appointment.Status.CANCELLED)


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
        self.room1.save(update_fields=["capacity"])
        other_child = Child.objects.create(last_name="Сидоров", first_name="Коля", primary_parent=self.parent)
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
        self.room1.save(update_fields=["capacity"])
        child_b = Child.objects.create(last_name="Петров", first_name="Илья", primary_parent=self.parent)
        child_c = Child.objects.create(last_name="Федоров", first_name="Олег", primary_parent=self.parent)
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
        other_child = Child.objects.create(last_name="Сидоров", first_name="Коля", primary_parent=self.parent)
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
        child_b = Child.objects.create(last_name="Петров", first_name="Илья", primary_parent=self.parent)
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

    def test_create_schedule_assigns_block_and_sequence(self):
        preview = self._preview(requested_count=2)

        result = wizard_svc.create_schedule_from_preview(preview, actor=self.user)

        self.block.refresh_from_db()
        self.assertEqual(len(result.appointments), 2)
        self.assertEqual(self.block.status, ProgramBlock.Status.SCHEDULED)
        self.assertEqual([appt.sequence_number for appt in result.appointments], [1, 2])
        self.assertTrue(all(appt.program_block_id == self.block.pk for appt in result.appointments))

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


class ReportsServiceTests(_FixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        appt_svc.create_appointment(
            child=self.child, staff_member=self.staff_a, service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1, billing_account=self.account,
        )

    def test_tomorrow_overview_returns_summary(self):
        tomorrow = self.day + timedelta(days=1)
        appt_svc.create_appointment(
            child=self.child, staff_member=self.staff_a, service=self.service_log,
            starts_at=_local(tomorrow, time(10, 0)),
            ends_at=_local(tomorrow, time(10, 30)),
            room=self.room1, billing_account=self.account,
        )
        overview = reports_svc.tomorrow_overview(tomorrow)
        self.assertEqual(overview.date, tomorrow)
        self.assertEqual(overview.summary["appointments_count"], 1)

    def test_timesheet_groups_by_day(self):
        appt_svc.create_appointment(
            child=self.child, staff_member=self.staff_a, service=self.service_log,
            starts_at=_local(self.day + timedelta(days=1), time(12, 0)),
            ends_at=_local(self.day + timedelta(days=1), time(12, 30)),
            room=self.room1, billing_account=self.account,
        )
        ts = reports_svc.timesheet(
            self.staff_a, self.day, self.day + timedelta(days=1)
        )
        self.assertEqual(ts.totals.total, 2)
        self.assertEqual(ts.rows[0].total, 1)
        self.assertEqual(ts.rows[1].total, 1)

    def test_grant_report_sums_initial_topups_charges(self):
        billing_svc.top_up_account(
            self.account, amount=Decimal("5"), method=Payment.Method.GRANT_TRANSFER, actor=self.user,
        )
        appt = Appointment.objects.first()
        billing_svc.apply_decision(
            appt, decision=Appointment.BillingDecision.CHARGE,
            account=self.account, amount=Decimal("-1"), actor=self.user,
        )
        report = reports_svc.grant_report(
            self.funding, timezone.localdate() - timedelta(days=1),
            timezone.localdate() + timedelta(days=1),
        )
        self.assertEqual(len(report.rows), 1)
        row = report.rows[0]
        self.assertEqual(row.initial_amount, Decimal("10"))
        self.assertEqual(row.topups, Decimal("5"))
        self.assertEqual(row.charges, Decimal("1"))
        self.assertEqual(row.current_balance, Decimal("14"))


class NotificationsServiceTests(_FixturesMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.appt = appt_svc.create_appointment(
            child=self.child, staff_member=self.staff_a, service=self.service_log,
            starts_at=_local(self.day, time(10, 0)),
            ends_at=_local(self.day, time(10, 30)),
            room=self.room1, billing_account=self.account,
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

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="rehab@example.local",
    )
    def test_build_confirmation_email_contains_url(self):
        email = notif_svc.build_confirmation_email(self.appt)
        self.assertIn("Логопед", email.body)
        self.assertIn("Иванов", email.body)
