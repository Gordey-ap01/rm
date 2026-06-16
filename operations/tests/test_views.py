"""Тесты новых views: tomorrow, timesheet, grant_report, mass_reschedule, recommendations, documents, consents, payments."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    Appointment,
    BalanceAccount,
    Child,
    Consent,
    Document,
    FundingSource,
    ParentGuardian,
    Payment,
    ProgramBlock,
    Recommendation,
    Room,
    Service,
    StaffMember,
    TreatmentProgram,
)


def _local_dt(day, clock):
    return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())


class NewViewsTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("admin", password="x", is_staff=True, is_superuser=True)
        cls.parent = ParentGuardian.objects.create(
            last_name="Иванова",
            first_name="Мария",
            phone="+7 900 000-00-01",
            email="rep@example.local",
        )
        cls.child = Child.objects.create(last_name="Иванов", first_name="Ваня", primary_parent=cls.parent)
        cls.staff_user = User.objects.create_user("specialist1", password="x")
        cls.staff = StaffMember.objects.create(
            user=cls.staff_user, full_name="Иванова Н. Г.", specializations="Логопед"
        )
        cls.service = Service.objects.create(
            name="Логопед", code="LOG", category=Service.Category.SPEECH,
            default_duration_minutes=30, default_price=Decimal("1500"),
        )
        cls.service2 = Service.objects.create(
            name="Дефектолог", code="DEF", category=Service.Category.DEFECTOLOGY,
            default_duration_minutes=45, default_price=Decimal("1800"),
        )
        cls.room = Room.objects.create(name="Кабинет 1")
        cls.funding = FundingSource.objects.create(
            name="Личные средства", source_type=FundingSource.SourceType.PERSONAL,
        )
        cls.funding_grant = FundingSource.objects.create(
            name="Грант", source_type=FundingSource.SourceType.GRANT,
        )
        cls.account = BalanceAccount.objects.create(
            child=cls.child, funding_source=cls.funding, unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE, service=cls.service,
            initial_amount=Decimal("10"),
        )
        cls.account_grant = BalanceAccount.objects.create(
            child=cls.child, funding_source=cls.funding_grant, unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE, service=cls.service2,
            initial_amount=Decimal("20"),
        )

    def setUp(self):
        self.client.force_login(self.admin)


class TomorrowViewTests(NewViewsTestBase):
    def test_tomorrow_renders_with_default_date(self):
        response = self.client.get(reverse("tomorrow"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("overview", response.context)

    def test_tomorrow_accepts_date_param(self):
        target = timezone.localdate() + timedelta(days=3)
        response = self.client.get(reverse("tomorrow"), {"date": target.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["overview"].date, target)

    def test_tomorrow_rejects_specialist(self):
        self.client.logout()
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("tomorrow"))
        self.assertEqual(response.status_code, 302)


class StaffTimesheetViewTests(NewViewsTestBase):
    def test_timesheet_renders(self):
        response = self.client.get(reverse("staff_timesheet", args=[self.staff.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("form", response.context)
        self.assertIn("sheet", response.context)

    def test_timesheet_csv_export(self):
        day = timezone.localdate()
        start = _local_dt(day, time(10, 0))
        Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=start, ends_at=start + timedelta(minutes=30),
            billing_account=self.account, status=Appointment.Status.COMPLETED,
        )
        response = self.client.get(
            reverse("staff_timesheet", args=[self.staff.pk]),
            {
                "date_from": (day - timedelta(days=1)).isoformat(),
                "date_to": (day + timedelta(days=1)).isoformat(),
                "csv": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        body = response.content.decode("utf-8")
        self.assertIn("Дата;Всего", body)
        self.assertIn("Итого;", body)

    def test_timesheet_invalid_range_shows_error(self):
        response = self.client.get(
            reverse("staff_timesheet", args=[self.staff.pk]),
            {"date_from": "2099-01-01", "date_to": "2099-01-05"},
        )
        self.assertEqual(response.status_code, 200)


class GrantReportViewTests(NewViewsTestBase):
    def test_grant_report_renders(self):
        response = self.client.get(reverse("grant_report"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("form", response.context)

    def test_grant_report_filtered(self):
        response = self.client.get(
            reverse("grant_report"),
            {
                "funding": self.funding_grant.pk,
                "date_from": "2020-01-01",
                "date_to": "2099-01-01",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("report", response.context)
        self.assertIsNotNone(response.context["report"])

    def test_grant_report_csv_export(self):
        response = self.client.get(
            reverse("grant_report"),
            {
                "funding": self.funding_grant.pk,
                "date_from": "2020-01-01",
                "date_to": "2099-01-01",
                "csv": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        body = response.content.decode("utf-8")
        self.assertIn("Счёт;Начальный", body)

    def test_grant_report_with_funding_pk(self):
        response = self.client.get(
            reverse("grant_report_funding", args=[self.funding_grant.pk]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["funding_id"], self.funding_grant.pk)


class StaffMassRescheduleViewTests(NewViewsTestBase):
    def test_get_renders_form(self):
        response = self.client.get(reverse("staff_mass_reschedule", args=[self.staff.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("staff", response.context)

    def test_post_requires_reason(self):
        today = timezone.localdate()
        response = self.client.post(
            reverse("staff_mass_reschedule", args=[self.staff.pk]),
            {
                "date_from": today.isoformat(),
                "date_to": (today + timedelta(days=7)).isoformat(),
                "reason": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        msgs = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("причин" in m.lower() for m in msgs))

    def test_post_cancels_appointments(self):
        today = timezone.localdate() + timedelta(days=5)
        start = _local_dt(today, time(10, 0))
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=start, ends_at=start + timedelta(minutes=30),
            billing_account=self.account, status=Appointment.Status.CONFIRMED,
        )
        response = self.client.post(
            reverse("staff_mass_reschedule", args=[self.staff.pk]),
            {
                "date_from": today.isoformat(),
                "date_to": (today + timedelta(days=7)).isoformat(),
                "reason": "Болезнь",
            },
        )
        self.assertEqual(response.status_code, 302)
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.CANCELLED)


class RecommendationViewTests(NewViewsTestBase):
    def test_list_renders(self):
        response = self.client.get(reverse("recommendation_list"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("recommendations", response.context)

    def test_create_get(self):
        response = self.client.get(reverse("recommendation_create"))
        self.assertEqual(response.status_code, 200)

    def test_create_prefills_child(self):
        response = self.client.get(reverse("recommendation_create_for_child", args=[self.child.pk]))
        self.assertEqual(response.status_code, 200)

    def test_create_post(self):
        response = self.client.post(
            reverse("recommendation_create"),
            {
                "child": self.child.pk,
                "staff_member": self.staff.pk,
                "category": Recommendation.Category.HOME_TASK,
                "title": "Занятия 2 раза в неделю",
                "body": "Продолжать логопедические занятия дважды в неделю.",
            },
        )
        if response.status_code != 302:
            form = response.context.get("form")
            print("Form errors:", form.errors if form else "no form")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Recommendation.objects.filter(child=self.child).exists())

    def test_acknowledge(self):
        rec = Recommendation.objects.create(
            child=self.child, staff_member=self.staff, title="Тест", body="Тест",
        )
        response = self.client.post(reverse("recommendation_acknowledge", args=[rec.pk]))
        self.assertEqual(response.status_code, 302)
        rec.refresh_from_db()
        self.assertTrue(rec.is_acknowledged)
        self.assertIsNotNone(rec.acknowledged_at)


class DocumentViewTests(NewViewsTestBase):
    def test_list_renders(self):
        response = self.client.get(reverse("document_list"))
        self.assertEqual(response.status_code, 200)

    def test_list_filtered_by_child(self):
        response = self.client.get(reverse("document_list"), {"child_id": self.child.pk})
        self.assertEqual(response.status_code, 200)

    def test_create_get(self):
        response = self.client.get(reverse("document_create"))
        self.assertEqual(response.status_code, 200)

    def test_create_prefills_child(self):
        response = self.client.get(reverse("document_create_for_child", args=[self.child.pk]))
        self.assertEqual(response.status_code, 200)

    def test_create_post(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post(
            reverse("document_create"),
            {
                "child": self.child.pk,
                "category": Document.Category.OTHER,
                "title": "Справка",
                "issued_on": "2024-01-01",
                "expires_on": "2025-01-01",
                "file": SimpleUploadedFile("test.txt", b"hello", content_type="text/plain"),
                "note": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Document.objects.filter(child=self.child, title="Справка").exists())


class ConsentViewTests(NewViewsTestBase):
    def test_list_renders(self):
        response = self.client.get(reverse("consent_list"))
        self.assertEqual(response.status_code, 200)

    def test_list_filtered_by_child(self):
        response = self.client.get(reverse("consent_list"), {"child_id": self.child.pk})
        self.assertEqual(response.status_code, 200)

    def test_create_get(self):
        response = self.client.get(reverse("consent_create"))
        self.assertEqual(response.status_code, 200)

    def test_create_prefills_child(self):
        response = self.client.get(reverse("consent_create_for_child", args=[self.child.pk]))
        self.assertEqual(response.status_code, 200)

    def test_create_post(self):
        response = self.client.post(
            reverse("consent_create"),
            {
                "child": self.child.pk,
                "consent_type": Consent.ConsentType.PERSONAL_DATA,
                "signed_on": "2024-06-01",
                "note": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Consent.objects.filter(child=self.child).exists())


class PaymentViewTests(NewViewsTestBase):
    def test_create_get(self):
        response = self.client.get(reverse("payment_create"))
        self.assertEqual(response.status_code, 200)

    def test_create_prefills_account(self):
        response = self.client.get(reverse("payment_create_for_account", args=[self.account.pk]))
        self.assertEqual(response.status_code, 200)

    def test_create_post(self):
        response = self.client.post(
            reverse("payment_create"),
            {
                "balance_account": self.account.pk,
                "amount": "5",
                "method": Payment.Method.CASH,
                "paid_at": timezone.localdate().isoformat(),
                "reference": "REF-1",
                "comment": "Тест",
            },
        )
        if response.status_code != 302:
            form = response.context.get("form")
            print("Form errors:", form.errors if form else "no form")
            print("Response:", response.content[:500].decode("utf-8", errors="replace"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Payment.objects.filter(balance_account=self.account, amount=Decimal("5")).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal("15"))

    def test_create_post_invalid_amount(self):
        response = self.client.post(
            reverse("payment_create"),
            {
                "balance_account": self.account.pk,
                "amount": "-5",
                "method": Payment.Method.CASH,
                "paid_at": timezone.localdate().isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.objects.filter(amount=Decimal("-5")).exists())


class BalancesViewTests(NewViewsTestBase):
    def test_balances_renders(self):
        response = self.client.get(reverse("balances"))
        self.assertEqual(response.status_code, 200)

    def test_balance_account_create(self):
        response = self.client.post(
            reverse("balance_account_create"),
            {
                "child": self.child.pk,
                "funding_source": self.funding.pk,
                "unit": BalanceAccount.Unit.SESSIONS,
                "service_scope": BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
                "service": self.service.pk,
                "initial_amount": "3",
                "valid_from": "",
                "valid_until": "",
                "status": BalanceAccount.Status.ACTIVE,
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)

    def test_balance_account_edit(self):
        response = self.client.get(reverse("balance_account_edit", args=[self.account.pk]))
        self.assertEqual(response.status_code, 200)


class ScheduleViewTests(NewViewsTestBase):
    def test_schedule_renders(self):
        response = self.client.get(reverse("schedule"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("day", response.context)


class SpecialistHomeTests(NewViewsTestBase):
    def test_specialist_home_renders_for_admin(self):
        response = self.client.get(reverse("specialist_home"))
        self.assertEqual(response.status_code, 200)

    def test_specialist_can_view_home(self):
        self.client.logout()
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("specialist_home"))
        self.assertEqual(response.status_code, 200)


class ConfirmationPublicViewTests(NewViewsTestBase):
    def test_get_renders_page(self):
        from operations.models import AppointmentConfirmation

        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 30)),
            billing_account=self.account,
        )
        conf = AppointmentConfirmation.objects.create(
            appointment=appt, target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
            representative=self.parent, email=self.parent.email,
            subject="Тест", message="Тест",
        )
        self.client.logout()
        response = self.client.get(reverse("appointment_confirmation_public", args=[conf.token]))
        self.assertEqual(response.status_code, 200)


class SpecialistActionsTests(NewViewsTestBase):
    def test_mark_appointment_completed(self):
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() - timedelta(days=1), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() - timedelta(days=1), time(10, 30)),
            billing_account=self.account,
        )
        response = self.client.post(
            reverse("mark_appointment", args=[appt.pk]),
            {"action": "completed", "specialist_note": "Ок"},
        )
        self.assertEqual(response.status_code, 302)
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.COMPLETED)
        self.assertEqual(appt.attendance_status, Appointment.AttendanceStatus.ATTENDED)

    def test_mark_appointment_no_show(self):
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() - timedelta(days=1), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() - timedelta(days=1), time(10, 30)),
            billing_account=self.account,
        )
        response = self.client.post(
            reverse("mark_appointment", args=[appt.pk]),
            {"action": "not_completed"},
        )
        self.assertEqual(response.status_code, 302)
        appt.refresh_from_db()
        self.assertEqual(appt.status, Appointment.Status.NO_SHOW)

    def test_staff_availability_create(self):
        from operations.models import StaffAvailability
        self.client.logout()
        self.client.force_login(self.staff_user)
        response = self.client.post(
            reverse("staff_availability_create"),
            {
                "weekday": 0,
                "starts_at": "09:00",
                "ends_at": "17:00",
                "note": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(StaffAvailability.objects.filter(staff_member=self.staff).exists())

    def test_staff_availability_toggle(self):
        from operations.models import StaffAvailability
        avail = StaffAvailability.objects.create(
            staff_member=self.staff, weekday=0, starts_at=time(9, 0), ends_at=time(17, 0), is_active=True,
        )
        response = self.client.post(reverse("staff_availability_toggle", args=[avail.pk]))
        self.assertEqual(response.status_code, 302)
        avail.refresh_from_db()
        self.assertFalse(avail.is_active)

    def test_time_off_request_create(self):
        from operations.models import TimeOffRequest
        self.client.logout()
        self.client.force_login(self.staff_user)
        starts_on = timezone.localdate() + timedelta(days=10)
        response = self.client.post(
            reverse("time_off_request_create"),
            {
                "request_type": TimeOffRequest.RequestType.DAY_OFF,
                "starts_on": starts_on.isoformat(),
                "ends_on": starts_on.isoformat(),
                "reason": "Личные обстоятельства",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(TimeOffRequest.objects.filter(staff_member=self.staff).exists())

    def test_time_off_decide_reject(self):
        from operations.models import TimeOffRequest
        starts_on = timezone.localdate() + timedelta(days=10)
        req = TimeOffRequest.objects.create(
            staff_member=self.staff,
            request_type=TimeOffRequest.RequestType.DAY_OFF,
            starts_on=starts_on, ends_on=starts_on, reason="X",
        )
        response = self.client.post(
            reverse("time_off_request_decide", args=[req.pk]),
            {"action": "reject", "admin_note": "Не сегодня"},
        )
        self.assertEqual(response.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, TimeOffRequest.Status.REJECTED)


class DrainTasksCommandTests(NewViewsTestBase):
    def test_drain_empty_queue(self):
        from django.core.management import call_command
        call_command("drain_tasks", "--once", verbosity=0)


class SuggestedTransferSlotsTests(NewViewsTestBase):
    def test_returns_empty_when_no_active_staff(self):
        StaffMember.objects.update(status=StaffMember.Status.INACTIVE)
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 30)),
            billing_account=self.account,
        )
        from operations.views.scheduling_helpers import suggested_transfer_slots
        slots = suggested_transfer_slots(appt, days=2, limit=5)
        self.assertEqual(slots, [])

    def test_returns_alternate_slots(self):
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 30)),
            billing_account=self.account,
        )
        from operations.views.scheduling_helpers import suggested_transfer_slots
        slots = suggested_transfer_slots(appt, days=3, limit=10)
        self.assertGreater(len(slots), 0)
        # All returned slots must NOT be the original (day, time) of the appointment.
        for slot in slots:
            if slot["staff"] == self.staff and slot["time"] == "10:00" and slot["date"] == appt.starts_at.date():
                self.fail("Original slot should be excluded")

    def test_skips_conflicts(self):
        # Create a conflicting appointment on day+1 at 11:00 for the same staff
        day = timezone.localdate() + timedelta(days=6)
        Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(day, time(11, 0)),
            ends_at=_local_dt(day, time(11, 30)),
            billing_account=self.account,
        )
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 30)),
            billing_account=self.account,
        )
        from operations.views.scheduling_helpers import suggested_transfer_slots
        slots = suggested_transfer_slots(appt, days=8, limit=20)
        # None of the returned slots should be at 11:00 on the conflict day for this staff
        for slot in slots:
            if slot["staff"] == self.staff and slot["date"] == day and slot["time"] == "11:00":
                self.fail("Conflicting slot should be excluded")

    def test_respects_limit(self):
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 30)),
            billing_account=self.account,
        )
        from operations.views.scheduling_helpers import suggested_transfer_slots
        slots = suggested_transfer_slots(appt, days=7, limit=3)
        self.assertLessEqual(len(slots), 3)


class ProgramBlockWizardViewTests(NewViewsTestBase):
    def setUp(self):
        super().setUp()
        self.program = TreatmentProgram.objects.create(
            child=self.child,
            title="Программа занятий",
            status=TreatmentProgram.Status.ACTIVE,
        )
        self.block = ProgramBlock.objects.create(
            program=self.program,
            number=1,
            title="Логопедический каскад",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=3,
            balance_account=self.account,
        )

    def _wizard_payload(self, day, action="preview"):
        return {
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
            "weekdays": [str(day.weekday())],
            "time_from": "10:00",
            "time_until": "12:00",
            "duration_minutes": "30",
            "requested_count": "2",
            "staff_member": str(self.staff.pk),
            "room": str(self.room.pk),
            "appointment_status": Appointment.Status.PROPOSED,
            "action": action,
        }

    def test_schedule_wizard_get(self):
        response = self.client.get(reverse("program_block_schedule_wizard", args=[self.block.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("form", response.context)
        self.assertEqual(response.context["block"], self.block)

    def test_schedule_wizard_create_creates_appointments(self):
        day = timezone.localdate() + timedelta(days=8)
        response = self.client.post(
            reverse("program_block_schedule_wizard", args=[self.block.pk]),
            self._wizard_payload(day, action="create"),
        )
        self.assertEqual(response.status_code, 302)
        appointments = Appointment.objects.filter(program_block=self.block).order_by("sequence_number")
        self.assertEqual(appointments.count(), 2)
        self.assertEqual([appt.sequence_number for appt in appointments], [1, 2])

    def test_transfer_funds_between_block_accounts(self):
        movable_source = FundingSource.objects.create(
            name="Переносимый грант",
            source_type=FundingSource.SourceType.GRANT,
            transfer_policy=FundingSource.TransferPolicy.WITHIN_CHILD,
        )
        source = BalanceAccount.objects.create(
            child=self.child,
            funding_source=movable_source,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service2,
            initial_amount=Decimal("5"),
        )
        target = BalanceAccount.objects.create(
            child=self.child,
            funding_source=movable_source,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("0"),
        )
        self.block.balance_account = target
        self.block.save(update_fields=["balance_account"])

        response = self.client.post(
            reverse("program_block_transfer_funds", args=[self.block.pk]),
            {
                "from_account": source.pk,
                "to_account": target.pk,
                "amount": "2",
                "reason": "Перенос тест",
            },
        )

        self.assertEqual(response.status_code, 302)
        source.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual(source.current_balance, Decimal("3"))
        self.assertEqual(target.current_balance, Decimal("2"))


class AppointmentDetailAndMoveTests(NewViewsTestBase):
    def test_appointment_detail_renders_with_suggestions(self):
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 30)),
            billing_account=self.account,
        )
        response = self.client.get(reverse("appointment_detail", args=[appt.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("appointment", response.context)
        self.assertIn("suggested_slots", response.context)

    def test_appointment_create_get_with_params(self):
        response = self.client.get(
            reverse("appointment_create"),
            {
                "child_id": self.child.pk,
                "service_id": self.service.pk,
                "staff_id": self.staff.pk,
                "room_id": self.room.pk,
                "date": timezone.localdate().isoformat(),
                "time": "10:00",
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_appointment_edit_get(self):
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 30)),
            billing_account=self.account,
        )
        response = self.client.get(reverse("appointment_edit", args=[appt.pk]))
        self.assertEqual(response.status_code, 200)

    def test_appointment_edit_post_valid(self):
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 30)),
            billing_account=self.account,
        )
        new_day = timezone.localdate() + timedelta(days=6)
        response = self.client.post(
            reverse("appointment_edit", args=[appt.pk]),
            {
                "child": self.child.id,
                "service": self.service.id,
                "staff_member": self.staff.id,
                "room": self.room.id,
                "billing_account": self.account.id,
                "status": Appointment.Status.CONFIRMED,
                "date": new_day.isoformat(),
                "time": "11:00",
                "duration_minutes": "30",
            },
        )
        self.assertEqual(response.status_code, 302)
        appt.refresh_from_db()
        self.assertEqual(timezone.localtime(appt.starts_at).date(), new_day)

    def test_appointment_move_get(self):
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 30)),
            billing_account=self.account,
        )
        response = self.client.get(reverse("appointment_move", args=[appt.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("suggested_slots", response.context)

    def test_appointment_cancel_get(self):
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 30)),
            billing_account=self.account,
        )
        response = self.client.get(reverse("appointment_cancel", args=[appt.pk]))
        self.assertEqual(response.status_code, 200)


class AppointmentBillingTests(NewViewsTestBase):
    def test_appointment_billing_get_redirects_to_detail(self):
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() - timedelta(days=1), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() - timedelta(days=1), time(10, 30)),
            billing_account=self.account,
        )
        response = self.client.get(reverse("appointment_billing", args=[appt.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("appointment_detail", args=[appt.pk]))

    def test_appointment_billing_invalid_returns_400(self):
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() - timedelta(days=1), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() - timedelta(days=1), time(10, 30)),
            billing_account=self.account,
        )
        response = self.client.post(
            reverse("appointment_billing", args=[appt.pk]),
            {
                "billing_decision": "",
                "billing_account": "",
                "amount": "",
                "reason": "",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_appointment_billing_charge(self):
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() - timedelta(days=1), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() - timedelta(days=1), time(10, 30)),
            billing_account=self.account,
        )
        response = self.client.post(
            reverse("appointment_billing", args=[appt.pk]),
            {
                "billing_decision": Appointment.BillingDecision.CHARGE,
                "billing_account": self.account.id,
                "amount": "-1",
                "reason": "OK",
                "next": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        appt.refresh_from_db()
        self.assertEqual(appt.billing_decision, Appointment.BillingDecision.CHARGE)


class RecipientEditTests(NewViewsTestBase):
    def test_recipient_edit_get(self):
        response = self.client.get(reverse("recipient_edit", args=[self.child.pk]))
        self.assertEqual(response.status_code, 200)

    def test_recipient_edit_post(self):
        response = self.client.post(
            reverse("recipient_edit", args=[self.child.pk]),
            {
                "last_name": "Иванов",
                "first_name": "Ваня",
                "middle_name": "Петрович",
                "birth_date": "2017-01-01",
                "status": Child.Status.ACTIVE,
                "primary_parent": self.parent.id,
                "diagnosis": "тест",
                "notes": "примечание",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.child.refresh_from_db()
        self.assertEqual(self.child.middle_name, "Петрович")

    def test_representative_edit_get(self):
        response = self.client.get(reverse("representative_edit", args=[self.parent.pk]))
        self.assertEqual(response.status_code, 200)

    def test_representative_edit_post(self):
        response = self.client.post(
            reverse("representative_edit", args=[self.parent.pk]),
            {
                "last_name": "Иванова",
                "first_name": "Мария",
                "middle_name": "Сергеевна",
                "relationship_type": ParentGuardian.RelationshipType.MOTHER,
                "phone": "+7 900 000-00-99",
                "phone_alt": "",
                "email": self.parent.email,
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.middle_name, "Сергеевна")

    def test_recipient_create_with_representative_id(self):
        response = self.client.get(
            reverse("recipient_create"),
            {"representative_id": self.parent.id},
        )
        self.assertEqual(response.status_code, 200)


class SafeNextUrlTests(NewViewsTestBase):
    def test_safe_next_url_allows_same_host(self):
        from operations.views._common import safe_next_url
        same_host = "http://testserver/somewhere/"

        class FakeReq:
            POST = {"next": same_host}
            GET: dict = {}
            def get_host(self):
                return "testserver"
            def is_secure(self):
                return False
        result = safe_next_url(FakeReq(), "/fallback/")
        self.assertEqual(result, same_host)

    def test_safe_next_url_rejects_external(self):
        from operations.views._common import safe_next_url

        class FakeReq:
            POST: dict = {}
            GET = {"next": "http://evil.com/phish"}
            def get_host(self):
                return "testserver"
            def is_secure(self):
                return False
        result = safe_next_url(FakeReq(), "/fallback/")
        self.assertEqual(result, "/fallback/")

    def test_safe_next_url_falls_back_when_no_next(self):
        from operations.views._common import safe_next_url

        class FakeReq:
            POST: dict = {}
            GET: dict = {}
            def get_host(self):
                return "testserver"
            def is_secure(self):
                return False
        result = safe_next_url(FakeReq(), "/fallback/")
        self.assertEqual(result, "/fallback/")

    def test_csrf_failure_for_non_login(self):
        from operations.views._common import csrf_failure
        response = csrf_failure(self.client.get("/some/random/path/").wsgi_request, "test reason")
        self.assertEqual(response.status_code, 403)

    def test_csrf_failure_for_login_path(self):
        from operations.views._common import csrf_failure
        login_path = reverse("login")
        # Use a real client GET to login URL so MessageMiddleware is active
        response = csrf_failure(self.client.get(login_path).wsgi_request, "test")
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)


class SendConfirmationTaskTests(NewViewsTestBase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_returns_false_for_missing_confirmation(self):
        from operations.tasks import send_appointment_confirmation_email
        result = send_appointment_confirmation_email.call(999999)
        self.assertFalse(result)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_returns_false_for_missing_email(self):
        from operations.models import AppointmentConfirmation
        from operations.tasks import send_appointment_confirmation_email
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 30)),
            billing_account=self.account,
        )
        conf = AppointmentConfirmation.objects.create(
            appointment=appt, target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
            subject="Тест", message="Тест", email="",
        )
        result = send_appointment_confirmation_email.call(conf.pk)
        self.assertFalse(result)
        conf.refresh_from_db()
        self.assertEqual(conf.delivery_status, AppointmentConfirmation.DeliveryStatus.FAILED)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_returns_true_on_success(self):
        from operations.models import AppointmentConfirmation
        from operations.tasks import send_appointment_confirmation_email
        appt = Appointment.objects.create(
            child=self.child, service=self.service, staff_member=self.staff, room=self.room,
            starts_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 0)),
            ends_at=_local_dt(timezone.localdate() + timedelta(days=5), time(10, 30)),
            billing_account=self.account,
        )
        conf = AppointmentConfirmation.objects.create(
            appointment=appt, target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
            subject="Тест", message="Тест", email="test@example.local",
        )
        result = send_appointment_confirmation_email.call(conf.pk)
        self.assertTrue(result)
        conf.refresh_from_db()
        self.assertEqual(conf.delivery_status, AppointmentConfirmation.DeliveryStatus.SENT)
        self.assertIsNotNone(conf.sent_at)
