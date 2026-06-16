from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import AppointmentForm, AppointmentMoveForm
from .models import (
    Appointment,
    AppointmentConfirmation,
    BalanceAccount,
    Child,
    Consent,
    Document,
    FundingSource,
    LedgerEntry,
    ParentGuardian,
    Payment,
    ProgramBlock,
    Recommendation,
    Room,
    Service,
    StaffAvailability,
    StaffMember,
    TimeOffRequest,
    TreatmentProgram,
)


class AppointmentWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("admin", password="admin12345", is_staff=True, is_superuser=True)
        cls.specialist_user = User.objects.create_user("specialist1", password="specialist123")
        cls.parent = ParentGuardian.objects.create(
            last_name="Иванова",
            first_name="Мария",
            phone="+7 900 000-00-01",
            email="representative@example.local",
        )
        cls.child = Child.objects.create(
            last_name="Иванов",
            first_name="Ваня",
            primary_parent=cls.parent,
        )
        cls.staff = StaffMember.objects.create(
            user=cls.specialist_user,
            full_name="Наталья Геннадьевна",
            specializations="Логопед",
            email="specialist@example.local",
        )
        cls.service = Service.objects.create(
            name="Логопед",
            code="LOG",
            category=Service.Category.SPEECH,
            default_duration_minutes=30,
            default_price=Decimal("1500"),
        )
        cls.room = Room.objects.create(name="Кабинет 1")
        cls.funding = FundingSource.objects.create(
            name="Личные средства",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        cls.account = BalanceAccount.objects.create(
            child=cls.child,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("10"),
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def local_dt(self, day, clock):
        return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())

    def appointment_payload(self, day, clock="09:00"):
        return {
            "child": self.child.id,
            "service": self.service.id,
            "staff_member": self.staff.id,
            "room": self.room.id,
            "billing_account": self.account.id,
            "status": Appointment.Status.CONFIRMED,
            "admin_note": "Тест",
            "date": day.isoformat(),
            "time": clock,
            "duration_minutes": "30",
        }

    def create_appointment(self, day=None, clock=time(9, 0)):
        day = day or timezone.localdate() + timedelta(days=5)
        starts_at = self.local_dt(day, clock)
        return Appointment.objects.create(
            child=self.child,
            service=self.service,
            staff_member=self.staff,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            billing_account=self.account,
        )

    def test_admin_can_create_appointment_from_calendar(self):
        day = timezone.localdate() + timedelta(days=10)

        response = self.client.post(reverse("appointment_create"), self.appointment_payload(day))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Appointment.objects.filter(child=self.child, starts_at__date=day).exists())

    def test_create_rejects_schedule_conflict(self):
        day = timezone.localdate() + timedelta(days=11)
        self.create_appointment(day=day)

        response = self.client.post(reverse("appointment_create"), self.appointment_payload(day))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Конфликт")
        self.assertEqual(Appointment.objects.filter(child=self.child, starts_at__date=day).count(), 1)

    def test_move_marks_old_appointment_rescheduled_and_creates_new_one(self):
        appointment = self.create_appointment()
        new_day = timezone.localdate() + timedelta(days=12)

        response = self.client.post(
            reverse("appointment_move", args=[appointment.pk]),
            {
                "date": new_day.isoformat(),
                "time": "10:00",
                "duration_minutes": "30",
                "staff_member": self.staff.id,
                "room": self.room.id,
                "admin_note": "Перенос",
            },
        )

        appointment.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(appointment.status, Appointment.Status.RESCHEDULED)
        self.assertTrue(Appointment.objects.filter(source_appointment=appointment, starts_at__date=new_day).exists())

    def test_cancel_does_not_create_ledger_entry(self):
        appointment = self.create_appointment()

        response = self.client.post(
            reverse("appointment_cancel", args=[appointment.pk]),
            {"status": Appointment.Status.CANCELLED, "reason": "sick", "admin_note": "Болезнь"},
        )

        appointment.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(appointment.status, Appointment.Status.CANCELLED)
        self.assertEqual(appointment.billing_decision, Appointment.BillingDecision.UNDECIDED)
        self.assertFalse(LedgerEntry.objects.filter(appointment=appointment).exists())

    def test_billing_decision_creates_ledger_only_after_admin_action(self):
        appointment = self.create_appointment()

        response = self.client.post(
            reverse("appointment_billing", args=[appointment.pk]),
            {
                "billing_decision": Appointment.BillingDecision.CHARGE,
                "billing_account": self.account.id,
                "amount": "-1",
                "reason": "Администратор решил списать",
            },
        )

        appointment.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(appointment.billing_decision, Appointment.BillingDecision.CHARGE)
        self.assertEqual(LedgerEntry.objects.filter(appointment=appointment).count(), 1)
        self.assertEqual(self.account.current_balance, Decimal("9"))

    def test_work_queue_lists_completed_appointment_without_billing_decision(self):
        appointment = self.create_appointment()
        appointment.status = Appointment.Status.COMPLETED
        appointment.attendance_status = Appointment.AttendanceStatus.ATTENDED
        appointment.save(update_fields=["status", "attendance_status", "updated_at"])

        response = self.client.get(reverse("work_queue"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(appointment, list(response.context["needs_billing"]))

    def test_quick_do_not_charge_returns_to_work_queue(self):
        appointment = self.create_appointment()
        appointment.status = Appointment.Status.CANCELLED
        appointment.save(update_fields=["status", "updated_at"])

        response = self.client.post(
            reverse("appointment_billing", args=[appointment.pk]),
            {
                "next": reverse("work_queue"),
                "billing_decision": Appointment.BillingDecision.DO_NOT_CHARGE,
                "reason": "Не списывать из очереди",
            },
        )

        appointment.refresh_from_db()
        self.assertRedirects(response, reverse("work_queue"))
        self.assertEqual(appointment.billing_decision, Appointment.BillingDecision.DO_NOT_CHARGE)
        self.assertFalse(LedgerEntry.objects.filter(appointment=appointment).exists())

    def test_specialist_is_redirected_to_specialist_home_from_dashboard(self):
        self.client.logout()
        self.client.force_login(self.specialist_user)

        response = self.client.get(reverse("dashboard"))

        self.assertRedirects(response, reverse("specialist_home"))

    def test_child_search_finds_by_child_name_and_parent_phone(self):
        response_by_name = self.client.get(reverse("recipient_list"), {"q": "Иванов"})
        response_by_phone = self.client.get(reverse("recipient_list"), {"q": "000-00-01"})

        self.assertEqual(response_by_name.status_code, 200)
        self.assertContains(response_by_name, self.child.full_name)
        self.assertContains(response_by_phone, self.child.full_name)

    def test_child_detail_shows_contacts_balances_and_appointments(self):
        appointment = self.create_appointment()

        response = self.client.get(reverse("recipient_detail", args=[self.child.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.parent.phone)
        self.assertContains(response, self.account.funding_source.name)
        self.assertContains(response, appointment.service.name)

    def test_create_appointment_can_be_prefilled_from_child_card(self):
        response = self.client.get(reverse("appointment_create"), {"child_id": self.child.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'value="{self.child.pk}" selected')

    def test_specialist_cannot_open_child_register(self):
        self.client.logout()
        self.client.force_login(self.specialist_user)

        response = self.client.get(reverse("recipient_list"))

        self.assertEqual(response.status_code, 302)

    def test_admin_can_create_representative(self):
        response = self.client.post(
            reverse("representative_create"),
            {
                "last_name": "Петрова",
                "first_name": "Ольга",
                "middle_name": "Игоревна",
                "relationship_type": ParentGuardian.RelationshipType.OTHER,
                "phone": "+7 900 000-00-02",
                "phone_alt": "",
                "email": "",
                "notes": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(ParentGuardian.objects.filter(phone="+7 900 000-00-02").exists())

    def test_admin_can_create_recipient(self):
        response = self.client.post(
            reverse("recipient_create"),
            {
                "last_name": "Петров",
                "first_name": "Петр",
                "middle_name": "",
                "birth_date": "2018-01-01",
                "status": Child.Status.ACTIVE,
                "primary_parent": self.parent.id,
                "diagnosis": "",
                "notes": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Child.objects.filter(last_name="Петров", first_name="Петр").exists())

    def test_admin_can_create_balance_account_from_recipient_card(self):
        response = self.client.post(
            reverse("balance_account_create"),
            {
                "child": self.child.id,
                "funding_source": self.funding.id,
                "unit": BalanceAccount.Unit.SESSIONS,
                "service_scope": BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
                "service": self.service.id,
                "initial_amount": "4",
                "valid_from": "",
                "valid_until": "",
                "status": BalanceAccount.Status.ACTIVE,
                "notes": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(BalanceAccount.objects.filter(child=self.child, initial_amount=Decimal("4")).exists())

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_admin_can_send_confirmation_email_to_representative(self):
        from operations.tasks import send_appointment_confirmation_email

        appointment = self.create_appointment()

        response = self.client.post(
            reverse("appointment_send_confirmation", args=[appointment.pk]),
            {
                "target_type": AppointmentConfirmation.TargetType.REPRESENTATIVE,
                "subject": "Подтвердите занятие",
                "message": "Проверьте дату и подтвердите занятие.",
            },
        )

        confirmation = AppointmentConfirmation.objects.get(appointment=appointment)
        self.assertEqual(response.status_code, 302)
        # View enqueues task; run it inline for assertions.
        send_appointment_confirmation_email.call(confirmation.pk)
        confirmation.refresh_from_db()
        self.assertEqual(confirmation.delivery_status, AppointmentConfirmation.DeliveryStatus.SENT)
        self.assertEqual(confirmation.email, self.parent.email)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(str(confirmation.token), mail.outbox[0].body)

    def test_public_confirmation_marks_appointment_confirmed(self):
        appointment = self.create_appointment()
        appointment.status = Appointment.Status.PROPOSED
        appointment.save(update_fields=["status", "updated_at"])
        confirmation = AppointmentConfirmation.objects.create(
            appointment=appointment,
            target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
            representative=self.parent,
            email=self.parent.email,
            subject="Подтвердите занятие",
            message="Тест",
        )
        self.client.logout()

        response = self.client.post(
            reverse("appointment_confirmation_public", args=[confirmation.token]),
            {"action": "confirm", "response_note": "Будем"},
        )

        confirmation.refresh_from_db()
        appointment.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(confirmation.status, AppointmentConfirmation.Status.CONFIRMED)
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)

    def test_specialist_can_request_time_off_and_admin_can_approve(self):
        self.client.logout()
        self.client.force_login(self.specialist_user)
        starts_on = timezone.localdate() + timedelta(days=3)

        response = self.client.post(
            reverse("time_off_request_create"),
            {
                "request_type": TimeOffRequest.RequestType.DAY_OFF,
                "starts_on": starts_on.isoformat(),
                "ends_on": starts_on.isoformat(),
                "reason": "Личные обстоятельства",
            },
        )

        time_off = TimeOffRequest.objects.get(staff_member=self.staff)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(time_off.status, TimeOffRequest.Status.PENDING)

        self.client.logout()
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("time_off_request_decide", args=[time_off.pk]),
            {"action": "approve", "admin_note": "Ок"},
        )

        time_off.refresh_from_db()
        self.assertRedirects(response, reverse("work_queue"))
        self.assertEqual(time_off.status, TimeOffRequest.Status.APPROVED)

    def test_staff_availability_blocks_outside_window(self):
        day = timezone.localdate() + timedelta(days=20)
        StaffAvailability.objects.create(
            staff_member=self.staff,
            weekday=day.weekday(),
            starts_at=time(9, 0),
            ends_at=time(10, 0),
        )

        response = self.client.post(reverse("appointment_create"), self.appointment_payload(day, clock="11:00"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Недоступность специалиста")
        self.assertContains(response, "Удерживать, чтобы предложить специалисту выйти вне графика")
        self.assertFalse(Appointment.objects.filter(child=self.child, starts_at__date=day).exists())

    def test_staff_availability_override_creates_appointment_from_form(self):
        day = timezone.localdate() + timedelta(days=20)
        StaffAvailability.objects.create(
            staff_member=self.staff,
            weekday=day.weekday(),
            starts_at=time(9, 0),
            ends_at=time(10, 0),
        )
        payload = self.appointment_payload(day, clock="11:00")
        payload["staff_availability_override"] = "1"

        response = self.client.post(reverse("appointment_create"), payload)

        self.assertEqual(response.status_code, 302)
        appointment = Appointment.objects.get(child=self.child, starts_at__date=day)
        self.assertTrue(appointment.staff_availability_override)
        self.assertIn("рабочего графика", appointment.staff_availability_override_reason)


class ApiAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("admin", password="admin12345", is_staff=True)

    def test_api_rejects_anonymous_calendar_data(self):
        response = self.client.get("/api/appointments/")
        self.assertEqual(response.status_code, 401)

    def test_api_allows_authenticated_calendar_data(self):
        self.client.force_login(self.admin)
        response = self.client.get("/api/appointments/")
        self.assertEqual(response.status_code, 200)


class SchedulingBusinessRulesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.parent = ParentGuardian.objects.create(last_name="Parent", first_name="One", phone="+70000000001")
        cls.child_a = Child.objects.create(last_name="Child", first_name="A", primary_parent=cls.parent)
        cls.child_b = Child.objects.create(last_name="Child", first_name="B", primary_parent=cls.parent)
        cls.child_c = Child.objects.create(last_name="Child", first_name="C", primary_parent=cls.parent)
        cls.staff_a = StaffMember.objects.create(full_name="Staff A")
        cls.staff_b = StaffMember.objects.create(full_name="Staff B")
        cls.staff_c = StaffMember.objects.create(full_name="Staff C")
        cls.service = Service.objects.create(name="Speech", code="SP", default_duration_minutes=30)
        cls.room = Room.objects.create(name="Shared room", capacity=2)
        cls.funding = FundingSource.objects.create(name="Personal", source_type=FundingSource.SourceType.PERSONAL)
        cls.account = BalanceAccount.objects.create(
            child=cls.child_a,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("7"),
        )

    def local_dt(self, day, clock):
        return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())

    def test_room_capacity_allows_several_specialists_until_full(self):
        day = timezone.localdate() + timedelta(days=15)
        starts_at = self.local_dt(day, time(10, 0))
        ends_at = starts_at + timedelta(minutes=30)

        Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        Appointment.objects.create(
            child=self.child_b,
            service=self.service,
            staff_member=self.staff_b,
            room=self.room,
            starts_at=starts_at,
            ends_at=ends_at,
        )

        with self.assertRaises(ValidationError):
            Appointment.objects.create(
                child=self.child_c,
                service=self.service,
                staff_member=self.staff_c,
                room=self.room,
                starts_at=starts_at,
                ends_at=ends_at,
            )

    def test_appointment_outside_specialist_work_time_is_rejected(self):
        day = timezone.localdate() + timedelta(days=16)
        starts_at = self.local_dt(day, time(19, 0))

        with self.assertRaises(ValidationError):
            Appointment.objects.create(
                child=self.child_a,
                service=self.service,
                staff_member=self.staff_a,
                room=self.room,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(minutes=30),
            )

    def test_appointment_outside_specialist_work_time_can_be_overridden(self):
        day = timezone.localdate() + timedelta(days=16)
        starts_at = self.local_dt(day, time(19, 0))

        appointment = Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            staff_availability_override=True,
            staff_availability_override_reason="Администратор согласует выход вне графика.",
        )

        self.assertTrue(appointment.staff_availability_override)

    def test_appointment_form_override_saves_outside_specialist_work_time(self):
        day = timezone.localdate() + timedelta(days=16)
        form = AppointmentForm(
            {
                "child": self.child_a.id,
                "service": self.service.id,
                "staff_member": self.staff_a.id,
                "room": self.room.id,
                "status": Appointment.Status.CONFIRMED,
                "admin_note": "",
                "date": day.isoformat(),
                "time": "19:00",
                "duration_minutes": "30",
                "staff_availability_override": "1",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        appointment = form.save()
        self.assertTrue(appointment.staff_availability_override)
        self.assertIn("09:00-18:00", appointment.staff_availability_override_reason)

    def test_appointment_move_form_override_saves_outside_specialist_work_time(self):
        day = timezone.localdate() + timedelta(days=16)
        starts_at = self.local_dt(day, time(10, 0))
        appointment = Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
        )

        form = AppointmentMoveForm(
            {
                "date": (day + timedelta(days=1)).isoformat(),
                "time": "19:00",
                "duration_minutes": "30",
                "staff_member": self.staff_b.id,
                "room": self.room.id,
                "staff_availability_override": "1",
                "admin_note": "Согласовать выход специалиста.",
            },
            appointment=appointment,
        )

        self.assertTrue(form.is_valid(), form.errors)
        moved = form.save()
        self.assertTrue(moved.staff_availability_override)
        self.assertIn("09:00-18:00", moved.staff_availability_override_reason)

    def test_program_block_numbers_appointments_and_counts_payment_decisions(self):
        program = TreatmentProgram.objects.create(child=self.child_a, title="Base program")
        block = ProgramBlock.objects.create(
            program=program,
            number=1,
            title="Speech block",
            service=self.service,
            staff_member=self.staff_a,
            planned_sessions=2,
            balance_account=self.account,
        )
        day = timezone.localdate() + timedelta(days=17)
        first_start = self.local_dt(day, time(10, 0))
        second_start = self.local_dt(day + timedelta(days=1), time(10, 0))

        first = Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=first_start,
            ends_at=first_start + timedelta(minutes=30),
            program_block=block,
            billing_account=self.account,
            billing_decision=Appointment.BillingDecision.CHARGE,
        )
        second = Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=second_start,
            ends_at=second_start + timedelta(minutes=30),
            program_block=block,
        )

        self.assertEqual(first.sequence_number, 1)
        self.assertEqual(second.sequence_number, 2)
        self.assertEqual(block.scheduled_count, 2)
        self.assertEqual(block.paid_count, 1)

    def test_appointment_form_rejects_program_block_from_other_recipient(self):
        program = TreatmentProgram.objects.create(child=self.child_b, title="Other program")
        block = ProgramBlock.objects.create(
            program=program,
            number=1,
            title="Other block",
            service=self.service,
            planned_sessions=1,
        )
        day = timezone.localdate() + timedelta(days=18)

        form = AppointmentForm(
            {
                "child": self.child_a.id,
                "service": self.service.id,
                "staff_member": self.staff_a.id,
                "room": self.room.id,
                "program_block": block.id,
                "status": Appointment.Status.CONFIRMED,
                "admin_note": "",
                "date": day.isoformat(),
                "time": "10:00",
                "duration_minutes": "30",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("program_block", form.errors)

    def test_balance_warning_levels_follow_7_3_1_thresholds(self):
        self.assertEqual(self.account.warning_level, "notice")
        self.account.initial_amount = Decimal("3")
        self.assertEqual(self.account.warning_level, "warning")
        self.account.initial_amount = Decimal("1")
        self.assertEqual(self.account.warning_level, "critical")
        self.account.initial_amount = Decimal("0")
        self.assertEqual(self.account.warning_level, "exhausted")


class SoftDeleteMixinTests(TestCase):
    def setUp(self):
        self.parent = ParentGuardian.objects.create(
            last_name="Иванов", first_name="Иван", phone="+7 900 000-00-99"
        )
        self.child = Child.objects.create(
            last_name="Иванов", first_name="Петя", primary_parent=self.parent
        )

    def test_archive_hides_from_default_manager(self):
        self.child.archive()
        self.assertTrue(self.child.is_archived)
        self.assertFalse(Child.objects.filter(pk=self.child.pk).exists())
        self.assertTrue(Child.all_objects.filter(pk=self.child.pk).exists())

    def test_restore_returns_to_default_manager(self):
        self.child.archive()
        self.child.restore()
        self.assertFalse(self.child.is_archived)
        self.assertTrue(Child.objects.filter(pk=self.child.pk).exists())

    def test_queryset_delete_is_soft(self):
        qs = Child.objects.filter(pk=self.child.pk)
        qs.delete()
        self.child.refresh_from_db()
        self.assertTrue(self.child.is_archived)

    def test_queryset_hard_delete_removes_row(self):
        Child.objects.filter(pk=self.child.pk).hard_delete()
        self.assertFalse(Child.all_objects.filter(pk=self.child.pk).exists())

    def test_alive_and_dead_querysets(self):
        other = Child.objects.create(
            last_name="Сидоров", first_name="Коля", primary_parent=self.parent
        )
        self.child.archive()
        self.assertIn(self.child, list(Child.all_objects.dead()))
        self.assertIn(other, list(Child.objects.alive()))
        self.assertNotIn(self.child, list(Child.objects.alive()))

    def test_apply_to_balance_account_and_funding_source(self):
        funding = FundingSource.objects.create(name="Грант", source_type=FundingSource.SourceType.GRANT)
        self.assertFalse(funding.is_archived)
        funding.archive()
        self.assertFalse(FundingSource.objects.filter(pk=funding.pk).exists())


class RecommendationTests(TestCase):
    def setUp(self):
        self.parent = ParentGuardian.objects.create(
            last_name="Иванов", first_name="Иван", phone="+7 900 000-00-77"
        )
        self.child = Child.objects.create(
            last_name="Иванов", first_name="Петя", primary_parent=self.parent
        )
        self.staff = StaffMember.objects.create(full_name="Иванова А.А.", specializations="Логопед")
        self.user = User.objects.create_user("rec_admin", password="x", is_staff=True)

    def test_create_recommendation(self):
        rec = Recommendation.objects.create(
            child=self.child,
            staff_member=self.staff,
            category=Recommendation.Category.HOME_TASK,
            title="Логопедические упражнения",
            body="Делать 10 минут в день",
        )
        self.assertEqual(str(rec), f"Логопедические упражнения — {self.child}")
        self.assertFalse(rec.is_acknowledged)

    def test_acknowledge_sets_timestamp(self):
        rec = Recommendation.objects.create(
            child=self.child,
            title="Т",
            body="Б",
        )
        rec.acknowledge(actor=self.user)
        rec.refresh_from_db()
        self.assertTrue(rec.is_acknowledged)
        self.assertIsNotNone(rec.acknowledged_at)
        self.assertEqual(rec.acknowledged_by, self.user)

    def test_clean_clears_acknowledged_state_when_unacknowledged(self):
        rec = Recommendation.objects.create(
            child=self.child, title="Т", body="Б", is_acknowledged=True
        )
        rec.is_acknowledged = False
        rec.full_clean()
        rec.save()
        rec.refresh_from_db()
        self.assertIsNone(rec.acknowledged_at)
        self.assertIsNone(rec.acknowledged_by)


class DocumentTests(TestCase):
    def setUp(self):
        self.parent = ParentGuardian.objects.create(
            last_name="Иванов", first_name="Иван", phone="+7 900 000-00-55"
        )
        self.child = Child.objects.create(
            last_name="Иванов", first_name="Петя", primary_parent=self.parent
        )

    def test_create_document(self):
        doc = Document.objects.create(
            child=self.child,
            category=Document.Category.MEDICAL_REPORT,
            title="Заключение невролога",
            file="documents/test.pdf",
            issued_on=timezone.localdate(),
        )
        self.assertEqual(str(doc), f"Заключение невролога — {self.child}")
        self.assertFalse(doc.is_expired)
        self.assertFalse(doc.expires_soon)

    def test_is_expired_when_past(self):
        past = timezone.localdate() - timedelta(days=10)
        doc = Document.objects.create(
            child=self.child,
            title="Старый",
            file="documents/old.pdf",
            expires_on=past,
        )
        self.assertTrue(doc.is_expired)

    def test_expires_soon_within_30_days(self):
        soon = timezone.localdate() + timedelta(days=15)
        doc = Document.objects.create(
            child=self.child, title="Скоро", file="documents/x.pdf", expires_on=soon
        )
        self.assertTrue(doc.expires_soon)
        self.assertFalse(doc.is_expired)

    def test_clean_validates_dates(self):
        past = timezone.localdate() - timedelta(days=10)
        doc = Document(
            child=self.child, title="X", file="x.pdf",
            issued_on=timezone.localdate(), expires_on=past,
        )
        with self.assertRaises(ValidationError):
            doc.full_clean()


class ConsentTests(TestCase):
    def setUp(self):
        self.parent = ParentGuardian.objects.create(
            last_name="Иванов", first_name="Иван", phone="+7 900 000-00-44"
        )
        self.child = Child.objects.create(
            last_name="Иванов", first_name="Петя", primary_parent=self.parent
        )

    def test_create_consent(self):
        consent = Consent.objects.create(
            child=self.child,
            consent_type=Consent.ConsentType.PERSONAL_DATA,
            signed_on=timezone.localdate(),
        )
        self.assertTrue(consent.is_valid)

    def test_consent_invalid_when_not_signed(self):
        consent = Consent.objects.create(
            child=self.child, consent_type=Consent.ConsentType.PHOTO_VIDEO
        )
        self.assertFalse(consent.is_valid)

    def test_consent_invalid_when_expired(self):
        past = timezone.localdate() - timedelta(days=1)
        consent = Consent.objects.create(
            child=self.child,
            consent_type=Consent.ConsentType.PERSONAL_DATA,
            signed_on=past - timedelta(days=365),
            expires_on=past,
        )
        self.assertFalse(consent.is_valid)

    def test_clean_validates_dates(self):
        consent = Consent(
            child=self.child,
            consent_type=Consent.ConsentType.OTHER,
            signed_on=timezone.localdate(),
            expires_on=timezone.localdate() - timedelta(days=10),
        )
        with self.assertRaises(ValidationError):
            consent.full_clean()


class PaymentTests(TestCase):
    def setUp(self):
        self.parent = ParentGuardian.objects.create(
            last_name="Иванов", first_name="Иван", phone="+7 900 000-00-33"
        )
        self.child = Child.objects.create(
            last_name="Иванов", first_name="Петя", primary_parent=self.parent
        )
        self.funding = FundingSource.objects.create(
            name="Грант", source_type=FundingSource.SourceType.GRANT
        )
        self.account = BalanceAccount.objects.create(
            child=self.child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("0"),
        )

    def test_create_payment(self):
        payment = Payment.objects.create(
            balance_account=self.account,
            amount=Decimal("5"),
            method=Payment.Method.GRANT_TRANSFER,
            paid_at=timezone.localdate(),
            reference="ГР-2026-01",
        )
        self.assertEqual(str(payment), f"+5 {self.account}")
        self.assertEqual(self.account.payments.count(), 1)

    def test_clean_rejects_non_positive_amount(self):
        payment = Payment(
            balance_account=self.account,
            amount=Decimal("-1"),
            method=Payment.Method.CASH,
        )
        with self.assertRaises(ValidationError):
            payment.full_clean()
