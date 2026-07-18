from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ValidationError
from django.http import QueryDict
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import AppointmentForm, AppointmentMoveForm
from .models import (
    Appointment,
    AppointmentConfirmation,
    AppointmentParticipant,
    AppointmentRoomOverride,
    AppointmentSeries,
    AppointmentStaffAssignment,
    BalanceAccount,
    CenterLegalProfile,
    Child,
    Consent,
    ContractLegalSnapshot,
    Counterparty,
    Document,
    FundingSource,
    LedgerEntry,
    ParentGuardian,
    Payment,
    ProgramBlock,
    RecipientRepresentative,
    Recommendation,
    Room,
    Service,
    ServiceContract,
    StaffAvailability,
    StaffMember,
    TimeOffRequest,
    TreatmentProgram,
)


class AppointmentWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            "admin", password="admin12345", is_staff=True, is_superuser=True
        )
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

    def test_appointment_save_preserves_existing_participant_decisions(self):
        appointment = self.create_appointment()
        participant = appointment.participants.get(child=self.child)
        participant.billing_decision = Appointment.BillingDecision.CHARGE
        participant.billing_account = self.account
        participant.attendance_status = Appointment.AttendanceStatus.MISSED
        participant.save(
            update_fields=[
                "billing_decision",
                "billing_account",
                "attendance_status",
                "updated_at",
            ]
        )

        appointment.billing_decision = Appointment.BillingDecision.UNDECIDED
        appointment.billing_account = None
        appointment.attendance_status = Appointment.AttendanceStatus.ATTENDED
        appointment.admin_note = "Изменили комментарий"
        appointment.save()

        participant.refresh_from_db()
        self.assertEqual(participant.billing_decision, Appointment.BillingDecision.CHARGE)
        self.assertEqual(participant.billing_account, self.account)
        self.assertEqual(participant.attendance_status, Appointment.AttendanceStatus.MISSED)

    def test_appointment_save_preserves_existing_staff_assignment_fields(self):
        appointment = self.create_appointment()
        assignment = appointment.staff_assignments.get(staff_member=self.staff)
        assignment.role = AppointmentStaffAssignment.Role.SUBSTITUTE
        assignment.override_availability = True
        assignment.override_reason = "Согласованный выход вне графика"
        assignment.save(
            update_fields=[
                "role",
                "override_availability",
                "override_reason",
                "updated_at",
            ]
        )

        appointment.staff_availability_override = False
        appointment.staff_availability_override_reason = ""
        appointment.admin_note = "Изменили комментарий"
        appointment.save()

        assignment.refresh_from_db()
        self.assertEqual(assignment.role, AppointmentStaffAssignment.Role.SUBSTITUTE)
        self.assertTrue(assignment.override_availability)
        self.assertEqual(assignment.override_reason, "Согласованный выход вне графика")

    def test_appointment_edit_preserves_existing_participant_decisions(self):
        appointment = self.create_appointment()
        participant = appointment.participants.get(child=self.child)
        participant.billing_decision = Appointment.BillingDecision.CHARGE
        participant.billing_account = self.account
        participant.attendance_status = Appointment.AttendanceStatus.MISSED
        participant.save(
            update_fields=[
                "billing_decision",
                "billing_account",
                "attendance_status",
                "updated_at",
            ]
        )
        day = timezone.localdate() + timedelta(days=5)
        payload = self.appointment_payload(day, clock="09:00")
        payload["admin_note"] = "Редактирование без сброса участника"

        response = self.client.post(reverse("appointment_edit", args=[appointment.pk]), payload)

        self.assertEqual(response.status_code, 302)
        participant.refresh_from_db()
        self.assertEqual(participant.billing_decision, Appointment.BillingDecision.CHARGE)
        self.assertEqual(participant.billing_account, self.account)
        self.assertEqual(participant.attendance_status, Appointment.AttendanceStatus.MISSED)

    def test_appointment_edit_preserves_partial_snapshot_without_readding_legacy_rows(self):
        appointment = self.create_appointment()
        participant_parent = ParentGuardian.objects.create(
            last_name="Петрова",
            first_name="Анна",
            phone="+7 900 000-00-02",
        )
        participant_child = Child.objects.create(
            last_name="Петров",
            first_name="Илья",
            primary_parent=participant_parent,
        )
        assigned_staff = StaffMember.objects.create(
            full_name="Мария Сергеевна",
            specializations="Психолог",
        )
        appointment.participants.filter(child=self.child).delete()
        participant = AppointmentParticipant.objects.create(
            appointment=appointment,
            child=participant_child,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        appointment.staff_assignments.filter(staff_member=self.staff).delete()
        assignment = AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=assigned_staff,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        local_start = timezone.localtime(appointment.starts_at)

        response = self.client.post(
            reverse("appointment_edit", args=[appointment.pk]),
            {
                "session_type": Appointment.SessionType.INDIVIDUAL,
                "child": self.child.pk,
                "participants": [participant_child.pk],
                "service": self.service.pk,
                "staff_member": self.staff.pk,
                "staff_members": [assigned_staff.pk],
                "room": self.room.pk,
                "billing_account": self.account.pk,
                "status": Appointment.Status.CONFIRMED,
                "admin_note": "Редактирование partial snapshot",
                "date": local_start.date().isoformat(),
                "time": f"{local_start:%H:%M}",
                "duration_minutes": str(appointment.duration_minutes),
            },
        )

        self.assertEqual(response.status_code, 302)
        participant.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(
            list(appointment.participants.values_list("child_id", flat=True)),
            [participant_child.pk],
        )
        self.assertEqual(
            list(appointment.staff_assignments.values_list("staff_member_id", flat=True)),
            [assigned_staff.pk],
        )
        self.assertEqual(participant.appointment_status, Appointment.Status.CONFIRMED)
        self.assertEqual(assignment.appointment_status, Appointment.Status.CONFIRMED)
        self.assertFalse(appointment.participants.filter(child=self.child).exists())
        self.assertFalse(appointment.staff_assignments.filter(staff_member=self.staff).exists())

    def test_create_rejects_schedule_conflict(self):
        day = timezone.localdate() + timedelta(days=11)
        self.create_appointment(day=day)

        response = self.client.post(reverse("appointment_create"), self.appointment_payload(day))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Конфликт")
        self.assertEqual(
            Appointment.objects.filter(child=self.child, starts_at__date=day).count(), 1
        )

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
        self.assertTrue(
            Appointment.objects.filter(
                source_appointment=appointment, starts_at__date=new_day
            ).exists()
        )

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

    def create_group_appointment_with_second_participant(self):
        second_child = Child.objects.create(
            last_name="Петров",
            first_name="Петя",
            primary_parent=self.parent,
        )
        second_account = BalanceAccount.objects.create(
            child=second_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("5"),
        )
        appointment = self.create_appointment()
        appointment.session_type = Appointment.SessionType.GROUP
        appointment.save(update_fields=["session_type", "updated_at"])
        participant = AppointmentParticipant.objects.create(
            appointment=appointment,
            child=second_child,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        return appointment, participant, second_account

    def test_group_participant_billing_charges_only_selected_participant(self):
        appointment, participant, second_account = (
            self.create_group_appointment_with_second_participant()
        )

        response = self.client.post(
            reverse("appointment_billing", args=[appointment.pk]),
            {
                "participant_id": participant.id,
                "billing_decision": Appointment.BillingDecision.CHARGE,
                "billing_account": second_account.id,
                "amount": "-1",
                "reason": "Списать второго участника группы",
            },
        )

        appointment.refresh_from_db()
        participant.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(participant.billing_decision, Appointment.BillingDecision.CHARGE)
        self.assertEqual(participant.billing_account, second_account)
        self.assertEqual(second_account.current_balance, Decimal("4"))
        self.assertEqual(self.account.current_balance, Decimal("10"))
        entry = LedgerEntry.objects.get(
            appointment=appointment, appointment_participant=participant
        )
        self.assertEqual(entry.account, second_account)
        self.assertEqual(entry.amount, Decimal("-1.00"))
        self.assertEqual(appointment.billing_decision, Appointment.BillingDecision.UNDECIDED)

    def test_group_participant_billing_rejects_account_from_another_child(self):
        appointment, participant, _second_account = (
            self.create_group_appointment_with_second_participant()
        )

        response = self.client.post(
            reverse("appointment_billing", args=[appointment.pk]),
            {
                "participant_id": participant.id,
                "billing_decision": Appointment.BillingDecision.CHARGE,
                "billing_account": self.account.id,
                "amount": "-1",
                "reason": "Неверный счет",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            LedgerEntry.objects.filter(
                appointment=appointment, appointment_participant=participant
            ).exists()
        )

    def test_group_billing_queue_resolves_after_all_participants_decided(self):
        appointment, participant, second_account = (
            self.create_group_appointment_with_second_participant()
        )
        primary_participant = appointment.primary_participant

        self.client.post(
            reverse("appointment_billing", args=[appointment.pk]),
            {
                "participant_id": participant.id,
                "billing_decision": Appointment.BillingDecision.CHARGE,
                "billing_account": second_account.id,
                "amount": "-1",
                "reason": "Списать второго участника группы",
            },
        )
        response = self.client.post(
            reverse("appointment_billing", args=[appointment.pk]),
            {
                "participant_id": primary_participant.id,
                "billing_decision": Appointment.BillingDecision.DO_NOT_CHARGE,
                "reason": "Основного участника не списывать",
            },
        )

        appointment.refresh_from_db()
        primary_participant.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            primary_participant.billing_decision, Appointment.BillingDecision.DO_NOT_CHARGE
        )
        self.assertEqual(appointment.billing_decision, Appointment.BillingDecision.UNDECIDED)
        self.assertIsNone(appointment.billing_account)
        self.assertEqual(LedgerEntry.objects.filter(appointment=appointment).count(), 1)
        self.assertNotIn(
            appointment, list(self.client.get(reverse("work_queue")).context["needs_billing"])
        )

    def test_group_appointment_detail_shows_participant_billing_summary(self):
        appointment, participant, second_account = (
            self.create_group_appointment_with_second_participant()
        )
        primary_participant = appointment.primary_participant

        self.client.post(
            reverse("appointment_billing", args=[appointment.pk]),
            {
                "participant_id": participant.id,
                "billing_decision": Appointment.BillingDecision.CHARGE,
                "billing_account": second_account.id,
                "amount": "-1",
                "reason": "Списать второго участника группы",
            },
        )
        self.client.post(
            reverse("appointment_billing", args=[appointment.pk]),
            {
                "participant_id": primary_participant.id,
                "billing_decision": Appointment.BillingDecision.DO_NOT_CHARGE,
                "reason": "Основного участника не списывать",
            },
        )

        response = self.client.get(reverse("appointment_detail", args=[appointment.pk]))

        self.assertContains(response, "Решено по участникам: списать 1, не списывать 1")
        self.assertContains(response, "Счета участников: 1")

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

    def test_dashboard_shows_operational_focus(self):
        appointment = self.create_appointment()
        appointment.status = Appointment.Status.CANCELLED
        appointment.save(update_fields=["status", "updated_at"])
        self.create_appointment(day=timezone.localdate(), clock=time(10, 0))
        low_balance_child = Child.objects.create(last_name="Низкий", first_name="Баланс")
        BalanceAccount.objects.create(
            child=low_balance_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("1"),
        )

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("dashboard_focus_items", response.context)
        self.assertContains(response, "Фокус дня")
        self.assertContains(response, "Перенести отмены")
        self.assertContains(response, reverse("work_queue"))
        self.assertContains(response, "ops-table")
        self.assertContains(response, 'data-label="Время"')
        self.assertContains(response, 'data-label="Остаток"')

    def test_dashboard_staff_load_counts_secondary_assignments(self):
        secondary_staff = StaffMember.objects.create(
            full_name="Ассистент группы",
            specializations="Ассистент",
            email="assistant@example.local",
        )
        appointment = self.create_appointment()
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=secondary_staff,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        staff_load = {
            row["staff_member__full_name"]: row["total"] for row in response.context["staff_load"]
        }
        self.assertEqual(staff_load[secondary_staff.full_name], 1)

    def test_dashboard_tomorrow_prefers_single_snapshot_participant_and_staff(self):
        day = timezone.localdate() + timedelta(days=1)
        participant_child = Child.objects.create(last_name="Dashboard", first_name="Participant")
        assigned_staff = StaffMember.objects.create(full_name="Dashboard Assigned")
        appointment = self.create_appointment(day=day)
        appointment.participants.filter(child=self.child).delete()
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=participant_child,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        appointment.staff_assignments.filter(staff_member=self.staff).delete()
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=assigned_staff,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, participant_child.full_name)
        self.assertContains(response, assigned_staff.full_name)

    def test_secondary_assigned_specialist_sees_appointment_on_home(self):
        secondary_user = User.objects.create_user("specialist2", password="specialist123")
        secondary_staff = StaffMember.objects.create(
            user=secondary_user,
            full_name="Ассистент Группы",
            specializations="Ассистент",
            email="assistant@example.local",
        )
        appointment = self.create_appointment()
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=secondary_staff,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )

        self.client.logout()
        self.client.force_login(secondary_user)
        response = self.client.get(reverse("specialist_home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, appointment.service.name)
        self.assertEqual(response.context["appointments"], [appointment])

    def test_secondary_assigned_specialist_marks_group_participant_attendance(self):
        secondary_user = User.objects.create_user("specialist2", password="specialist123")
        secondary_staff = StaffMember.objects.create(
            user=secondary_user,
            full_name="Ассистент Группы",
            specializations="Ассистент",
            email="assistant@example.local",
        )
        child_b = Child.objects.create(
            last_name="Иванов", first_name="Саша", primary_parent=self.parent
        )
        appointment = self.create_appointment()
        Appointment.objects.filter(pk=appointment.pk).update(
            session_type=Appointment.SessionType.GROUP
        )
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=child_b,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=secondary_staff,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        primary_participant = appointment.participants.get(child=self.child)
        second_participant = appointment.participants.get(child=child_b)

        self.client.logout()
        self.client.force_login(secondary_user)
        response = self.client.post(
            reverse("mark_appointment", args=[appointment.pk]),
            {
                "action": "completed",
                "specialist_note": "Группа отмечена",
                f"participant_status_{primary_participant.pk}": Appointment.AttendanceStatus.MISSED,
                f"participant_status_{second_participant.pk}": Appointment.AttendanceStatus.ATTENDED,
            },
        )

        self.assertRedirects(response, reverse("specialist_home"))
        appointment.refresh_from_db()
        primary_participant.refresh_from_db()
        second_participant.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.COMPLETED)
        self.assertEqual(appointment.attendance_status, Appointment.AttendanceStatus.ATTENDED)
        self.assertEqual(primary_participant.attendance_status, Appointment.AttendanceStatus.MISSED)
        self.assertEqual(
            second_participant.attendance_status, Appointment.AttendanceStatus.ATTENDED
        )
        self.assertTrue(
            appointment.staff_assignments.filter(
                staff_member=secondary_staff,
                appointment_status=Appointment.Status.COMPLETED,
            ).exists()
        )

    def test_unassigned_specialist_cannot_mark_appointment(self):
        other_user = User.objects.create_user("specialist3", password="specialist123")
        StaffMember.objects.create(
            user=other_user,
            full_name="Посторонний специалист",
            specializations="Логопед",
            email="other-specialist@example.local",
        )
        appointment = self.create_appointment()

        self.client.logout()
        self.client.force_login(other_user)
        response = self.client.post(
            reverse("mark_appointment", args=[appointment.pk]),
            {"action": "completed"},
        )

        self.assertRedirects(response, reverse("specialist_home"))
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)
        self.assertEqual(appointment.attendance_status, Appointment.AttendanceStatus.UNKNOWN)

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
        self.assertTrue(
            BalanceAccount.objects.filter(child=self.child, initial_amount=Decimal("4")).exists()
        )

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

        response = self.client.post(
            reverse("appointment_create"), self.appointment_payload(day, clock="11:00")
        )

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

    def test_api_rejects_specialist_calendar_data(self):
        specialist_user = User.objects.create_user("api-specialist", password="x")
        StaffMember.objects.create(user=specialist_user, full_name="API Specialist")
        self.client.force_login(specialist_user)

        response = self.client.get("/api/appointments/")

        self.assertEqual(response.status_code, 403)

    def test_api_exposes_group_participants_and_staff(self):
        self.client.force_login(self.admin)
        parent = ParentGuardian.objects.create(
            last_name="Parent", first_name="API", phone="+70000000002"
        )
        child_a = Child.objects.create(
            last_name="Child", first_name="Group A", primary_parent=parent
        )
        child_b = Child.objects.create(
            last_name="Child", first_name="Group B", primary_parent=parent
        )
        staff_a = StaffMember.objects.create(full_name="First Specialist")
        staff_b = StaffMember.objects.create(full_name="Second Specialist")
        service = Service.objects.create(
            name="Group Service", code="GRP", default_duration_minutes=30
        )
        room = Room.objects.create(
            name="Group Room",
            max_staff_count=2,
            max_recipient_count=2,
            allow_group_sessions=True,
        )
        starts_at = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=21), time(10, 0)),
            timezone.get_current_timezone(),
        )
        appointment = Appointment.objects.create(
            child=child_a,
            service=service,
            staff_member=staff_a,
            room=room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            session_type=Appointment.SessionType.GROUP,
        )
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=child_b,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=staff_b,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )

        response = self.client.get("/api/appointments/")

        self.assertEqual(response.status_code, 200)
        event = next(item for item in response.json() if item["id"] == appointment.id)
        self.assertEqual(event["title"], f"Группа (2) / {service.name}")
        props = event["extendedProps"]
        self.assertEqual(props["participantCount"], 2)
        self.assertIn(child_b.full_name, props["participants"])
        self.assertEqual(props["staffCount"], 2)
        self.assertIn(staff_b.full_name, props["staffMembers"])
        self.assertIn(staff_b.id, props["staffIds"])

    def test_api_group_title_prefers_appointment_title(self):
        self.client.force_login(self.admin)
        parent = ParentGuardian.objects.create(
            last_name="Parent", first_name="API Title", phone="+70000000012"
        )
        child_a = Child.objects.create(
            last_name="Child", first_name="Title A", primary_parent=parent
        )
        child_b = Child.objects.create(
            last_name="Child", first_name="Title B", primary_parent=parent
        )
        staff = StaffMember.objects.create(full_name="Title Specialist")
        service = Service.objects.create(
            name="Title Service", code="GTIT", default_duration_minutes=30
        )
        starts_at = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=22), time(10, 0)),
            timezone.get_current_timezone(),
        )
        appointment = Appointment.objects.create(
            child=child_a,
            service=service,
            staff_member=staff,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            session_type=Appointment.SessionType.GROUP,
            title="Группа коммуникации",
        )
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=child_b,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )

        response = self.client.get("/api/appointments/")

        self.assertEqual(response.status_code, 200)
        event = next(item for item in response.json() if item["id"] == appointment.id)
        self.assertEqual(event["title"], f"Группа коммуникации / {service.name}")

    def test_api_title_uses_single_participant_label_before_legacy_child(self):
        self.client.force_login(self.admin)
        parent = ParentGuardian.objects.create(
            last_name="Parent", first_name="Single API", phone="+70000000013"
        )
        legacy_child = Child.objects.create(
            last_name="Legacy", first_name="Child", primary_parent=parent, color="#111111"
        )
        participant_child = Child.objects.create(
            last_name="Participant",
            first_name="Actual",
            primary_parent=parent,
            color="#abcdef",
        )
        staff = StaffMember.objects.create(full_name="Single API Specialist", color="#222222")
        assigned_staff = StaffMember.objects.create(
            full_name="Actual API Specialist", color="#fedcba"
        )
        service = Service.objects.create(
            name="Single API Service", code="SAPI", default_duration_minutes=30
        )
        starts_at = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=21), time(13, 0)),
            timezone.get_current_timezone(),
        )
        appointment = Appointment.objects.create(
            child=legacy_child,
            service=service,
            staff_member=staff,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
        )
        appointment.participants.filter(child=legacy_child).delete()
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=participant_child,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        appointment.staff_assignments.filter(staff_member=staff).delete()
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=assigned_staff,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )

        response = self.client.get("/api/appointments/")

        self.assertEqual(response.status_code, 200)
        event = next(item for item in response.json() if item["id"] == appointment.id)
        self.assertEqual(event["title"], f"{participant_child.full_name} / {service.name}")
        self.assertEqual(event["extendedProps"]["child"], participant_child.full_name)
        self.assertEqual(event["extendedProps"]["childId"], participant_child.id)
        self.assertEqual(event["extendedProps"]["staffId"], assigned_staff.id)
        self.assertEqual(event["borderColor"], assigned_staff.color)
        self.assertEqual(event["extendedProps"]["childColor"], participant_child.color)

    def test_api_single_participant_uses_participant_billing_account(self):
        self.client.force_login(self.admin)
        parent = ParentGuardian.objects.create(
            last_name="Parent", first_name="API Billing", phone="+70000000014"
        )
        legacy_child = Child.objects.create(
            last_name="Legacy", first_name="Billing", primary_parent=parent
        )
        participant_child = Child.objects.create(
            last_name="Participant", first_name="Billing", primary_parent=parent
        )
        staff = StaffMember.objects.create(full_name="API Billing Specialist")
        service = Service.objects.create(
            name="API Billing Service", code="ABILL", default_duration_minutes=30
        )
        funding = FundingSource.objects.create(name="API Billing Funding")
        legacy_account = BalanceAccount.objects.create(
            child=legacy_child,
            funding_source=funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("5"),
            color="#111111",
        )
        participant_account = BalanceAccount.objects.create(
            child=participant_child,
            funding_source=funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("5"),
            color="#123456",
        )
        starts_at = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=21), time(14, 0)),
            timezone.get_current_timezone(),
        )
        appointment = Appointment.objects.create(
            child=legacy_child,
            service=service,
            staff_member=staff,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            billing_account=legacy_account,
        )
        appointment.participants.filter(child=legacy_child).delete()
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=participant_child,
            billing_account=participant_account,
            billing_decision=Appointment.BillingDecision.CHARGE,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )

        response = self.client.get("/api/appointments/")

        self.assertEqual(response.status_code, 200)
        event = next(item for item in response.json() if item["id"] == appointment.id)
        props = event["extendedProps"]
        self.assertEqual(props["billingAccountId"], participant_account.id)
        self.assertEqual(props["accountColor"], participant_account.color)

    def test_api_move_rejects_secondary_group_participant_and_staff_conflicts(self):
        self.client.force_login(self.admin)
        parent = ParentGuardian.objects.create(
            last_name="Parent", first_name="Move", phone="+70000000003"
        )
        child_a = Child.objects.create(
            last_name="Child", first_name="Move A", primary_parent=parent
        )
        child_b = Child.objects.create(
            last_name="Child", first_name="Move B", primary_parent=parent
        )
        staff_a = StaffMember.objects.create(full_name="Move Specialist A")
        staff_b = StaffMember.objects.create(full_name="Move Specialist B")
        service = Service.objects.create(
            name="Move Group Service", code="MGRP", default_duration_minutes=30
        )
        room = Room.objects.create(
            name="Move Group Room",
            max_staff_count=2,
            max_recipient_count=2,
            allow_group_sessions=True,
        )
        other_room = Room.objects.create(name="Other Room")
        source_start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=21), time(10, 0)),
            timezone.get_current_timezone(),
        )
        target_start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=22), time(11, 0)),
            timezone.get_current_timezone(),
        )
        appointment = Appointment.objects.create(
            child=child_a,
            service=service,
            staff_member=staff_a,
            room=room,
            starts_at=source_start,
            ends_at=source_start + timedelta(minutes=30),
            session_type=Appointment.SessionType.GROUP,
        )
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=child_b,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=staff_b,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        Appointment.objects.create(
            child=child_b,
            service=service,
            staff_member=staff_b,
            room=other_room,
            starts_at=target_start,
            ends_at=target_start + timedelta(minutes=30),
        )

        response = self.client.patch(
            f"/api/appointments/{appointment.pk}/move/",
            data=json.dumps(
                {
                    "starts_at": target_start.isoformat(),
                    "ends_at": (target_start + timedelta(minutes=30)).isoformat(),
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("Move B", detail)
        self.assertIn("Move Specialist B", detail)
        appointment.refresh_from_db()
        self.assertEqual(appointment.starts_at, source_start)

    def test_api_move_preserves_partial_snapshot_without_readding_legacy_child(self):
        self.client.force_login(self.admin)
        parent = ParentGuardian.objects.create(
            last_name="Parent", first_name="Partial API", phone="+70000000014"
        )
        legacy_child = Child.objects.create(
            last_name="Legacy", first_name="API", primary_parent=parent
        )
        participant_child = Child.objects.create(
            last_name="Participant", first_name="API", primary_parent=parent
        )
        staff = StaffMember.objects.create(full_name="Partial API Specialist")
        service = Service.objects.create(
            name="Partial API Service", code="PAPI", default_duration_minutes=30
        )
        source_start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=21), time(12, 0)),
            timezone.get_current_timezone(),
        )
        target_start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=22), time(12, 0)),
            timezone.get_current_timezone(),
        )
        appointment = Appointment.objects.create(
            child=legacy_child,
            service=service,
            staff_member=staff,
            starts_at=source_start,
            ends_at=source_start + timedelta(minutes=30),
        )
        appointment.participants.filter(child=legacy_child).delete()
        participant = AppointmentParticipant.objects.create(
            appointment=appointment,
            child=participant_child,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )

        response = self.client.patch(
            f"/api/appointments/{appointment.pk}/move/",
            data=json.dumps(
                {
                    "starts_at": target_start.isoformat(),
                    "ends_at": (target_start + timedelta(minutes=30)).isoformat(),
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        participant.refresh_from_db()
        self.assertEqual(appointment.starts_at, target_start)
        self.assertEqual(participant.starts_at_snapshot, target_start)
        self.assertEqual(
            list(appointment.participants.values_list("child_id", flat=True)),
            [participant_child.pk],
        )
        self.assertFalse(appointment.participants.filter(child=legacy_child).exists())

    def test_api_move_room_limit_counts_mixed_snapshot_and_legacy_appointments(self):
        self.client.force_login(self.admin)
        parent = ParentGuardian.objects.create(
            last_name="Parent", first_name="API Room", phone="+70000000015"
        )
        children = [
            Child.objects.create(
                last_name=f"Room{i}",
                first_name="Child",
                primary_parent=parent,
            )
            for i in range(4)
        ]
        staff = [StaffMember.objects.create(full_name=f"API Room Staff {i}") for i in range(4)]
        service = Service.objects.create(
            name="API Room Service", code="AROOM", default_duration_minutes=30
        )
        room = Room.objects.create(
            name="API Room Capacity",
            max_staff_count=2,
            max_recipient_count=2,
            allow_group_sessions=True,
        )
        source_start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=21), time(9, 0)),
            timezone.get_current_timezone(),
        )
        target_start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=22), time(10, 0)),
            timezone.get_current_timezone(),
        )
        moving = Appointment.objects.create(
            child=children[0],
            service=service,
            staff_member=staff[0],
            room=room,
            starts_at=source_start,
            ends_at=source_start + timedelta(minutes=30),
        )
        legacy_occupant = Appointment.objects.create(
            child=children[1],
            service=service,
            staff_member=staff[1],
            room=room,
            starts_at=target_start,
            ends_at=target_start + timedelta(minutes=30),
        )
        legacy_occupant.participants.all().delete()
        legacy_occupant.staff_assignments.all().delete()
        Appointment.objects.create(
            child=children[2],
            service=service,
            staff_member=staff[2],
            room=room,
            starts_at=target_start,
            ends_at=target_start + timedelta(minutes=30),
        )

        response = self.client.patch(
            f"/api/appointments/{moving.pk}/move/",
            data=json.dumps(
                {
                    "starts_at": target_start.isoformat(),
                    "ends_at": (target_start + timedelta(minutes=30)).isoformat(),
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("лимиту специалистов", detail)
        self.assertIn("лимиту получателей", detail)
        moving.refresh_from_db()
        self.assertEqual(moving.starts_at, source_start)

    def test_api_move_room_limit_counts_actual_partial_snapshot_participants(self):
        self.client.force_login(self.admin)
        parent = ParentGuardian.objects.create(
            last_name="Parent", first_name="API Partial Capacity", phone="+70000000016"
        )
        children = [
            Child.objects.create(
                last_name=f"PartialRoom{i}",
                first_name="Child",
                primary_parent=parent,
            )
            for i in range(5)
        ]
        staff = [
            StaffMember.objects.create(full_name=f"API Partial Room Staff {i}") for i in range(5)
        ]
        service = Service.objects.create(
            name="API Partial Room Service", code="APROOM", default_duration_minutes=30
        )
        room = Room.objects.create(
            name="API Partial Room Capacity",
            max_staff_count=4,
            max_recipient_count=3,
            allow_group_sessions=True,
        )
        source_start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=21), time(9, 0)),
            timezone.get_current_timezone(),
        )
        target_start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=22), time(10, 0)),
            timezone.get_current_timezone(),
        )
        moving = Appointment.objects.create(
            child=children[0],
            service=service,
            staff_member=staff[0],
            room=room,
            starts_at=source_start,
            ends_at=source_start + timedelta(minutes=30),
        )
        moving.participants.filter(child=children[0]).delete()
        participant = AppointmentParticipant.objects.create(
            appointment=moving,
            child=children[1],
            starts_at_snapshot=moving.starts_at,
            ends_at_snapshot=moving.ends_at,
            appointment_status=moving.status,
        )
        Appointment.objects.create(
            child=children[2],
            service=service,
            staff_member=staff[2],
            room=room,
            starts_at=target_start,
            ends_at=target_start + timedelta(minutes=30),
        )
        Appointment.objects.create(
            child=children[3],
            service=service,
            staff_member=staff[3],
            room=room,
            starts_at=target_start,
            ends_at=target_start + timedelta(minutes=30),
        )

        response = self.client.patch(
            f"/api/appointments/{moving.pk}/move/",
            data=json.dumps(
                {
                    "starts_at": target_start.isoformat(),
                    "ends_at": (target_start + timedelta(minutes=30)).isoformat(),
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        moving.refresh_from_db()
        participant.refresh_from_db()
        self.assertEqual(moving.starts_at, target_start)
        self.assertEqual(participant.starts_at_snapshot, target_start)
        self.assertFalse(moving.participants.filter(child=children[0]).exists())

    def test_api_move_rejects_group_when_room_disallows_groups(self):
        self.client.force_login(self.admin)
        parent = ParentGuardian.objects.create(
            last_name="Parent", first_name="API Group Room Flag", phone="+70000000019"
        )
        child_a = Child.objects.create(
            last_name="GroupFlag", first_name="Child A", primary_parent=parent
        )
        child_b = Child.objects.create(
            last_name="GroupFlag", first_name="Child B", primary_parent=parent
        )
        staff = StaffMember.objects.create(full_name="API Group Flag Staff")
        service = Service.objects.create(
            name="API Group Flag Service", code="AGFLAG", default_duration_minutes=30
        )
        room = Room.objects.create(
            name="API Individual Only Room",
            max_staff_count=2,
            max_recipient_count=4,
            allow_group_sessions=False,
        )
        source_start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=21), time(9, 0)),
            timezone.get_current_timezone(),
        )
        target_start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=22), time(10, 0)),
            timezone.get_current_timezone(),
        )
        moving = Appointment.objects.create(
            child=child_a,
            service=service,
            staff_member=staff,
            room=room,
            starts_at=source_start,
            ends_at=source_start + timedelta(minutes=30),
            session_type=Appointment.SessionType.GROUP,
        )
        AppointmentParticipant.objects.create(
            appointment=moving,
            child=child_b,
            starts_at_snapshot=moving.starts_at,
            ends_at_snapshot=moving.ends_at,
            appointment_status=moving.status,
        )

        response = self.client.patch(
            f"/api/appointments/{moving.pk}/move/",
            data=json.dumps(
                {
                    "starts_at": target_start.isoformat(),
                    "ends_at": (target_start + timedelta(minutes=30)).isoformat(),
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("групповые занятия", response.json()["detail"])
        moving.refresh_from_db()
        self.assertEqual(moving.starts_at, source_start)

    def test_api_move_rejects_secondary_staff_time_off(self):
        self.client.force_login(self.admin)
        parent = ParentGuardian.objects.create(
            last_name="Parent", first_name="TimeOff", phone="+70000000004"
        )
        child = Child.objects.create(last_name="Child", first_name="TimeOff", primary_parent=parent)
        staff_a = StaffMember.objects.create(full_name="Available Specialist")
        staff_b = StaffMember.objects.create(full_name="Unavailable Assistant")
        service = Service.objects.create(
            name="TimeOff Group Service", code="TOFF", default_duration_minutes=30
        )
        room = Room.objects.create(
            name="TimeOff Group Room",
            max_staff_count=2,
            max_recipient_count=2,
            allow_group_sessions=True,
        )
        source_start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=21), time(10, 0)),
            timezone.get_current_timezone(),
        )
        target_day = timezone.localdate() + timedelta(days=22)
        target_start = timezone.make_aware(
            datetime.combine(target_day, time(11, 0)),
            timezone.get_current_timezone(),
        )
        appointment = Appointment.objects.create(
            child=child,
            service=service,
            staff_member=staff_a,
            room=room,
            starts_at=source_start,
            ends_at=source_start + timedelta(minutes=30),
            session_type=Appointment.SessionType.GROUP,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=staff_b,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        TimeOffRequest.objects.create(
            staff_member=staff_b,
            starts_on=target_day,
            ends_on=target_day,
            status=TimeOffRequest.Status.APPROVED,
        )

        response = self.client.patch(
            f"/api/appointments/{appointment.pk}/move/",
            data=json.dumps(
                {
                    "starts_at": target_start.isoformat(),
                    "ends_at": (target_start + timedelta(minutes=30)).isoformat(),
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertIn("Unavailable Assistant", detail)
        appointment.refresh_from_db()
        self.assertEqual(appointment.starts_at, source_start)

    def test_api_move_rejects_stale_staff_availability_override(self):
        self.client.force_login(self.admin)
        parent = ParentGuardian.objects.create(
            last_name="Parent", first_name="Stale Override", phone="+70000000017"
        )
        child = Child.objects.create(
            last_name="Child", first_name="Stale Override", primary_parent=parent
        )
        staff = StaffMember.objects.create(full_name="Stale Override Specialist")
        service = Service.objects.create(
            name="Stale Override Service", code="SOVR", default_duration_minutes=30
        )
        source_start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=21), time(10, 0)),
            timezone.get_current_timezone(),
        )
        target_day = timezone.localdate() + timedelta(days=22)
        target_start = timezone.make_aware(
            datetime.combine(target_day, time(11, 0)),
            timezone.get_current_timezone(),
        )
        appointment = Appointment.objects.create(
            child=child,
            service=service,
            staff_member=staff,
            starts_at=source_start,
            ends_at=source_start + timedelta(minutes=30),
            staff_availability_override=True,
            staff_availability_override_reason="Старое одноразовое разрешение.",
        )
        assignment = appointment.staff_assignments.get(staff_member=staff)
        assignment.override_availability = True
        assignment.override_reason = "Старое одноразовое разрешение."
        assignment.save(update_fields=["override_availability", "override_reason", "updated_at"])
        StaffAvailability.objects.create(
            staff_member=staff,
            weekday=target_day.weekday(),
            starts_at=time(9, 0),
            ends_at=time(10, 0),
        )

        response = self.client.patch(
            f"/api/appointments/{appointment.pk}/move/",
            data=json.dumps(
                {
                    "starts_at": target_start.isoformat(),
                    "ends_at": (target_start + timedelta(minutes=30)).isoformat(),
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Stale Override Specialist", response.json()["detail"])
        appointment.refresh_from_db()
        self.assertEqual(appointment.starts_at, source_start)

    def test_api_move_clears_stale_staff_availability_override_when_available(self):
        self.client.force_login(self.admin)
        parent = ParentGuardian.objects.create(
            last_name="Parent", first_name="Clear Override", phone="+70000000018"
        )
        child = Child.objects.create(
            last_name="Child", first_name="Clear Override", primary_parent=parent
        )
        staff = StaffMember.objects.create(full_name="Clear Override Specialist")
        service = Service.objects.create(
            name="Clear Override Service", code="COVR", default_duration_minutes=30
        )
        source_start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=21), time(10, 0)),
            timezone.get_current_timezone(),
        )
        target_day = timezone.localdate() + timedelta(days=22)
        target_start = timezone.make_aware(
            datetime.combine(target_day, time(9, 30)),
            timezone.get_current_timezone(),
        )
        appointment = Appointment.objects.create(
            child=child,
            service=service,
            staff_member=staff,
            starts_at=source_start,
            ends_at=source_start + timedelta(minutes=30),
            staff_availability_override=True,
            staff_availability_override_reason="Старое одноразовое разрешение.",
        )
        assignment = appointment.staff_assignments.get(staff_member=staff)
        assignment.override_availability = True
        assignment.override_reason = "Старое одноразовое разрешение."
        assignment.save(update_fields=["override_availability", "override_reason", "updated_at"])
        StaffAvailability.objects.create(
            staff_member=staff,
            weekday=target_day.weekday(),
            starts_at=time(9, 0),
            ends_at=time(10, 0),
        )

        response = self.client.patch(
            f"/api/appointments/{appointment.pk}/move/",
            data=json.dumps(
                {
                    "starts_at": target_start.isoformat(),
                    "ends_at": (target_start + timedelta(minutes=30)).isoformat(),
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(appointment.starts_at, target_start)
        self.assertFalse(appointment.staff_availability_override)
        self.assertEqual(appointment.staff_availability_override_reason, "")
        self.assertFalse(assignment.override_availability)
        self.assertEqual(assignment.override_reason, "")


class SchedulingBusinessRulesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.parent = ParentGuardian.objects.create(
            last_name="Parent", first_name="One", phone="+70000000001"
        )
        cls.child_a = Child.objects.create(
            last_name="Child", first_name="A", primary_parent=cls.parent
        )
        cls.child_b = Child.objects.create(
            last_name="Child", first_name="B", primary_parent=cls.parent
        )
        cls.child_c = Child.objects.create(
            last_name="Child", first_name="C", primary_parent=cls.parent
        )
        cls.staff_a = StaffMember.objects.create(full_name="Staff A")
        cls.staff_b = StaffMember.objects.create(full_name="Staff B")
        cls.staff_c = StaffMember.objects.create(full_name="Staff C")
        cls.service = Service.objects.create(name="Speech", code="SP", default_duration_minutes=30)
        cls.room = Room.objects.create(
            name="Shared room", capacity=2, max_staff_count=2, max_recipient_count=2
        )
        cls.funding = FundingSource.objects.create(
            name="Personal", source_type=FundingSource.SourceType.PERSONAL
        )
        cls.account = BalanceAccount.objects.create(
            child=cls.child_a,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("7"),
        )
        cls.account_b = BalanceAccount.objects.create(
            child=cls.child_b,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("7"),
        )

    def local_dt(self, day, clock):
        return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())

    def group_form_data(self, day, *, participants, staff_members, room=None, extra=None):
        data = QueryDict(mutable=True)
        data.update(
            {
                "service": str(self.service.id),
                "room": str((room or self.room).id),
                "status": Appointment.Status.CONFIRMED,
                "admin_note": "",
                "date": day.isoformat(),
                "time": "10:00",
                "duration_minutes": "30",
                "session_type": Appointment.SessionType.GROUP,
            }
        )
        data.setlist("participants", [str(child.id) for child in participants])
        data.setlist("staff_members", [str(staff.id) for staff in staff_members])
        if extra:
            data.update(extra)
        return data

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

    def test_appointment_save_creates_participant_and_staff_assignment(self):
        day = timezone.localdate() + timedelta(days=15)
        starts_at = self.local_dt(day, time(11, 0))

        appointment = Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            billing_account=self.account,
        )

        participant = AppointmentParticipant.objects.get(appointment=appointment)
        assignment = AppointmentStaffAssignment.objects.get(appointment=appointment)
        self.assertEqual(participant.child, self.child_a)
        self.assertEqual(participant.billing_account, self.account)
        self.assertEqual(participant.starts_at_snapshot, appointment.starts_at)
        self.assertEqual(assignment.staff_member, self.staff_a)
        self.assertEqual(assignment.starts_at_snapshot, appointment.starts_at)

    def test_appointment_save_does_not_readd_legacy_rows_to_partial_snapshot(self):
        day = timezone.localdate() + timedelta(days=15)
        starts_at = self.local_dt(day, time(11, 0))
        appointment = Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            billing_account=self.account,
        )
        appointment.participants.filter(child=self.child_a).delete()
        participant = AppointmentParticipant.objects.create(
            appointment=appointment,
            child=self.child_b,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        appointment.staff_assignments.filter(staff_member=self.staff_a).delete()
        assignment = AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=self.staff_b,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )

        appointment.status = Appointment.Status.CANCELLED
        appointment.save(update_fields=["status", "updated_at"])

        participant.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(participant.appointment_status, Appointment.Status.CANCELLED)
        self.assertEqual(assignment.appointment_status, Appointment.Status.CANCELLED)
        self.assertEqual(
            list(appointment.participants.values_list("child_id", flat=True)),
            [self.child_b.pk],
        )
        self.assertEqual(
            list(appointment.staff_assignments.values_list("staff_member_id", flat=True)),
            [self.staff_b.pk],
        )

    def test_child_save_creates_primary_representative_link(self):
        child = Child.objects.create(
            last_name="Linked", first_name="Child", primary_parent=self.parent
        )

        link = RecipientRepresentative.objects.get(child=child, representative=self.parent)
        self.assertTrue(link.is_primary)
        self.assertTrue(link.signs_contract)
        self.assertTrue(link.receives_schedule)

    def test_group_participant_blocks_same_child_elsewhere(self):
        day = timezone.localdate() + timedelta(days=15)
        starts_at = self.local_dt(day, time(12, 0))
        appointment = Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            session_type=Appointment.SessionType.GROUP,
        )
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=self.child_b,
            starts_at_snapshot=starts_at,
            ends_at_snapshot=starts_at + timedelta(minutes=30),
            appointment_status=appointment.status,
        )

        with self.assertRaises(ValidationError):
            Appointment.objects.create(
                child=self.child_b,
                service=self.service,
                staff_member=self.staff_b,
                room=self.room,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(minutes=30),
            )

    def test_room_limits_count_staff_and_recipients_separately(self):
        self.room.capacity = 1
        self.room.max_staff_count = 2
        self.room.max_recipient_count = 2
        self.room.allow_group_sessions = True
        self.room.save(
            update_fields=[
                "capacity",
                "max_staff_count",
                "max_recipient_count",
                "allow_group_sessions",
            ]
        )
        day = timezone.localdate() + timedelta(days=15)
        starts_at = self.local_dt(day, time(13, 0))
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

    def test_room_limits_count_mixed_snapshot_and_legacy_appointments(self):
        self.room.max_staff_count = 2
        self.room.max_recipient_count = 2
        self.room.save(update_fields=["max_staff_count", "max_recipient_count"])
        day = timezone.localdate() + timedelta(days=15)
        starts_at = self.local_dt(day, time(13, 30))
        ends_at = starts_at + timedelta(minutes=30)

        legacy_appointment = Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        legacy_appointment.participants.all().delete()
        legacy_appointment.staff_assignments.all().delete()
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

    def test_room_staff_limit_blocks_even_when_recipient_limit_is_open(self):
        self.room.capacity = 1
        self.room.max_staff_count = 1
        self.room.limit_recipient_count = False
        self.room.save(update_fields=["capacity", "max_staff_count", "limit_recipient_count"])
        day = timezone.localdate() + timedelta(days=15)
        starts_at = self.local_dt(day, time(14, 0))
        ends_at = starts_at + timedelta(minutes=30)

        Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=ends_at,
        )

        with self.assertRaises(ValidationError):
            Appointment.objects.create(
                child=self.child_b,
                service=self.service,
                staff_member=self.staff_b,
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

    def test_appointment_form_saves_group_participants_and_staff_assignments(self):
        self.room.max_staff_count = 2
        self.room.max_recipient_count = 2
        self.room.allow_group_sessions = True
        self.room.save(
            update_fields=["max_staff_count", "max_recipient_count", "allow_group_sessions"]
        )
        day = timezone.localdate() + timedelta(days=16)
        form = AppointmentForm(
            self.group_form_data(
                day,
                participants=[self.child_a, self.child_b],
                staff_members=[self.staff_a, self.staff_b],
                extra={"billing_account": str(self.account.id)},
            )
        )

        self.assertTrue(form.is_valid(), form.errors)
        appointment = form.save()

        self.assertEqual(appointment.session_type, Appointment.SessionType.GROUP)
        self.assertEqual(appointment.child, self.child_a)
        self.assertEqual(appointment.staff_member, self.staff_a)
        self.assertCountEqual(
            appointment.participants.values_list("child_id", flat=True),
            [self.child_a.id, self.child_b.id],
        )
        self.assertCountEqual(
            appointment.staff_assignments.values_list("staff_member_id", flat=True),
            [self.staff_a.id, self.staff_b.id],
        )
        self.assertEqual(
            appointment.staff_assignments.get(staff_member=self.staff_a).role,
            AppointmentStaffAssignment.Role.PRIMARY,
        )
        self.assertEqual(
            appointment.staff_assignments.get(staff_member=self.staff_b).role,
            AppointmentStaffAssignment.Role.ASSISTANT,
        )

    def test_appointment_form_preserves_existing_staff_assignment_role_on_edit(self):
        self.room.max_staff_count = 2
        self.room.max_recipient_count = 2
        self.room.allow_group_sessions = True
        self.room.save(
            update_fields=["max_staff_count", "max_recipient_count", "allow_group_sessions"]
        )
        day = timezone.localdate() + timedelta(days=16)
        starts_at = self.local_dt(day, time(10, 0))
        appointment = Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            session_type=Appointment.SessionType.GROUP,
        )
        assignment = AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=self.staff_b,
            role=AppointmentStaffAssignment.Role.SUBSTITUTE,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        data = self.group_form_data(
            day,
            participants=[self.child_a],
            staff_members=[self.staff_a, self.staff_b],
            extra={
                "child": str(self.child_a.pk),
                "staff_member": str(self.staff_a.pk),
                "session_type": Appointment.SessionType.GROUP,
            },
        )

        form = AppointmentForm(data, instance=appointment)

        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        assignment.refresh_from_db()
        self.assertEqual(assignment.role, AppointmentStaffAssignment.Role.SUBSTITUTE)

    def test_appointment_form_requires_hold_for_room_limit_override(self):
        self.room.max_staff_count = 2
        self.room.max_recipient_count = 1
        self.room.allow_group_sessions = False
        self.room.save(
            update_fields=["max_staff_count", "max_recipient_count", "allow_group_sessions"]
        )
        day = timezone.localdate() + timedelta(days=16)
        data = self.group_form_data(
            day,
            participants=[self.child_a, self.child_b],
            staff_members=[self.staff_a],
        )

        blocked = AppointmentForm(data)

        self.assertFalse(blocked.is_valid())
        self.assertIn("Ограничение кабинета", blocked.errors["__all__"][0])
        self.assertIn("получателей", blocked.room_limit_warning)

        data["room_limit_override"] = "1"
        allowed = AppointmentForm(data)

        self.assertTrue(allowed.is_valid(), allowed.errors)
        appointment = allowed.save()
        self.assertEqual(appointment.participants.count(), 2)
        self.assertTrue(AppointmentRoomOverride.objects.filter(appointment=appointment).exists())

    def test_appointment_form_room_limit_override_bypasses_model_room_limit(self):
        self.room.max_staff_count = 1
        self.room.max_recipient_count = 2
        self.room.allow_group_sessions = True
        self.room.save(
            update_fields=["max_staff_count", "max_recipient_count", "allow_group_sessions"]
        )
        day = timezone.localdate() + timedelta(days=16)
        starts_at = self.local_dt(day, time(10, 0))
        Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
        )
        data = self.group_form_data(
            day,
            participants=[self.child_b],
            staff_members=[self.staff_b],
            extra={"session_type": Appointment.SessionType.INDIVIDUAL},
        )

        blocked = AppointmentForm(data)

        self.assertFalse(blocked.is_valid())
        self.assertIn("Ограничение кабинета", blocked.errors["__all__"][0])
        data["room_limit_override"] = "1"
        allowed = AppointmentForm(data)

        self.assertTrue(allowed.is_valid(), allowed.errors)
        appointment = allowed.save()
        self.assertEqual(appointment.room, self.room)
        self.assertTrue(
            AppointmentRoomOverride.objects.filter(
                appointment=appointment,
                override_type=AppointmentRoomOverride.OverrideType.STAFF_LIMIT,
            ).exists()
        )

    def test_appointment_form_room_limit_counts_mixed_snapshot_and_legacy_appointments(self):
        self.room.max_staff_count = 2
        self.room.max_recipient_count = 2
        self.room.save(update_fields=["max_staff_count", "max_recipient_count"])
        day = timezone.localdate() + timedelta(days=16)
        starts_at = self.local_dt(day, time(10, 0))
        legacy_appointment = Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
        )
        legacy_appointment.participants.all().delete()
        legacy_appointment.staff_assignments.all().delete()
        Appointment.objects.create(
            child=self.child_b,
            service=self.service,
            staff_member=self.staff_b,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
        )
        data = self.group_form_data(
            day,
            participants=[self.child_c],
            staff_members=[self.staff_c],
            extra={"session_type": Appointment.SessionType.INDIVIDUAL},
        )

        blocked = AppointmentForm(data)

        self.assertFalse(blocked.is_valid())
        self.assertIn("Ограничение кабинета", blocked.errors["__all__"][0])
        self.assertIn("при лимите 2", blocked.room_limit_warning)

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

    def test_appointment_move_form_override_covers_assistant_outside_work_time(self):
        self.room.max_staff_count = 2
        self.room.allow_group_sessions = True
        self.room.save(update_fields=["max_staff_count", "allow_group_sessions"])
        day = timezone.localdate() + timedelta(days=16)
        starts_at = self.local_dt(day, time(10, 0))
        appointment = Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            session_type=Appointment.SessionType.GROUP,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=self.staff_b,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        target_day = day + timedelta(days=1)
        StaffAvailability.objects.create(
            staff_member=self.staff_b,
            weekday=target_day.weekday(),
            starts_at=time(9, 0),
            ends_at=time(10, 0),
        )
        data = {
            "date": target_day.isoformat(),
            "time": "10:00",
            "duration_minutes": "30",
            "staff_member": self.staff_a.id,
            "room": self.room.id,
            "admin_note": "Согласовать выход ассистента.",
        }

        blocked = AppointmentMoveForm(data, appointment=appointment)

        self.assertFalse(blocked.is_valid())
        self.assertIn(self.staff_b.full_name, blocked.availability_warning)
        data["staff_availability_override"] = "1"
        allowed = AppointmentMoveForm(data, appointment=appointment)

        self.assertTrue(allowed.is_valid(), allowed.errors)
        moved = allowed.save()
        self.assertTrue(moved.staff_availability_override)
        assistant_assignment = moved.staff_assignments.get(staff_member=self.staff_b)
        primary_assignment = moved.staff_assignments.get(staff_member=self.staff_a)
        self.assertTrue(assistant_assignment.override_availability)
        self.assertIn("рабочего графика", assistant_assignment.override_reason)
        self.assertFalse(primary_assignment.override_availability)

    def test_appointment_move_form_preserves_partial_snapshot_without_legacy_child(self):
        day = timezone.localdate() + timedelta(days=16)
        starts_at = self.local_dt(day, time(10, 0))
        appointment = Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            billing_account=self.account,
        )
        appointment.participants.filter(child=self.child_a).delete()
        participant = AppointmentParticipant.objects.create(
            appointment=appointment,
            child=self.child_b,
            billing_account=self.account_b,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )

        form = AppointmentMoveForm(
            {
                "date": (day + timedelta(days=1)).isoformat(),
                "time": "11:00",
                "duration_minutes": "30",
                "staff_member": self.staff_b.id,
                "room": self.room.id,
                "admin_note": "Перенос partial snapshot.",
            },
            appointment=appointment,
        )

        self.assertTrue(form.is_valid(), form.errors)
        moved = form.save()

        self.assertEqual(moved.child, self.child_b)
        self.assertEqual(moved.billing_account, self.account_b)
        self.assertEqual(moved.participants.count(), 1)
        moved_participant = moved.participants.get(child=self.child_b)
        self.assertEqual(moved_participant.source_participant, participant)
        self.assertFalse(moved.participants.filter(child=self.child_a).exists())

    def test_materialize_series_skips_snapshot_participant_conflict_and_continues(self):
        self.room.limit_staff_count = False
        self.room.limit_recipient_count = False
        self.room.allow_group_sessions = True
        self.room.save(
            update_fields=["limit_staff_count", "limit_recipient_count", "allow_group_sessions"]
        )
        start_day = timezone.localdate() + timedelta(days=16)
        while start_day.weekday() != 0:
            start_day += timedelta(days=1)
        conflict_start = self.local_dt(start_day, time(10, 0))
        conflict = Appointment.objects.create(
            child=self.child_b,
            service=self.service,
            staff_member=self.staff_b,
            room=self.room,
            starts_at=conflict_start,
            ends_at=conflict_start + timedelta(minutes=30),
            session_type=Appointment.SessionType.GROUP,
        )
        AppointmentParticipant.objects.create(
            appointment=conflict,
            child=self.child_a,
            starts_at_snapshot=conflict.starts_at,
            ends_at_snapshot=conflict.ends_at,
            appointment_status=conflict.status,
        )
        series = AppointmentSeries.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            title="Серия с snapshot-конфликтом",
            start_date=start_day,
            end_date=start_day + timedelta(days=1),
            days_of_week="ПН,ВТ",
            time=time(10, 0),
            duration_minutes=30,
            status=AppointmentSeries.Status.ACTIVE,
        )

        created = series.materialize_series()

        self.assertEqual(created, 1)
        self.assertFalse(
            Appointment.objects.filter(
                series=series,
                starts_at__date=start_day,
            ).exists()
        )
        self.assertTrue(
            Appointment.objects.filter(
                series=series,
                starts_at__date=start_day + timedelta(days=1),
            ).exists()
        )

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

    def test_program_block_counts_group_participant_program_assignment(self):
        program = TreatmentProgram.objects.create(child=self.child_b, title="Group participant")
        block = ProgramBlock.objects.create(
            program=program,
            number=1,
            title="Group speech",
            service=self.service,
            planned_sessions=2,
        )
        day = timezone.localdate() + timedelta(days=18)
        starts_at = self.local_dt(day, time(10, 0))
        appointment = Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            session_type=Appointment.SessionType.GROUP,
        )
        participant = AppointmentParticipant.objects.create(
            appointment=appointment,
            child=self.child_b,
            program_block=block,
            billing_account=self.account_b,
            billing_decision=Appointment.BillingDecision.CHARGE,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )

        self.assertEqual(participant.sequence_number, 1)
        self.assertEqual(block.scheduled_count, 1)
        self.assertEqual(block.paid_count, 1)

    def test_appointment_participant_charge_requires_balance_account(self):
        day = timezone.localdate() + timedelta(days=18)
        starts_at = self.local_dt(day, time(11, 0))
        appointment = Appointment.objects.create(
            child=self.child_a,
            service=self.service,
            staff_member=self.staff_a,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            session_type=Appointment.SessionType.GROUP,
        )

        with self.assertRaises(ValidationError):
            AppointmentParticipant.objects.create(
                appointment=appointment,
                child=self.child_b,
                billing_decision=Appointment.BillingDecision.CHARGE,
                starts_at_snapshot=appointment.starts_at,
                ends_at_snapshot=appointment.ends_at,
                appointment_status=appointment.status,
            )

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
        funding = FundingSource.objects.create(
            name="Грант", source_type=FundingSource.SourceType.GRANT
        )
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
            child=self.child,
            title="X",
            file="x.pdf",
            issued_on=timezone.localdate(),
            expires_on=past,
        )
        with self.assertRaises(ValidationError):
            doc.full_clean()

    def test_recipient_target_requires_child(self):
        doc = Document(
            target_type=Document.TargetType.RECIPIENT,
            title="Документ без получателя",
            file="x.pdf",
        )
        with self.assertRaises(ValidationError):
            doc.full_clean()

    def test_counterparty_contract_can_exist_without_child(self):
        counterparty = Counterparty.objects.create(name="Фонд Радость")
        doc = Document.objects.create(
            target_type=Document.TargetType.COUNTERPARTY,
            counterparty=counterparty,
            category=Document.Category.CONTRACT,
            title="Договор фонда",
            file="documents/fund.docx",
        )

        self.assertIsNone(doc.child_id)
        self.assertEqual(doc.target_label, counterparty.name)
        self.assertEqual(str(doc), f"Договор фонда — {counterparty.name}")

    def test_contract_snapshot_rejects_service_contract_with_counterparty_document(self):
        counterparty = Counterparty.objects.create(name="Фонд Радость")
        document = Document.objects.create(
            target_type=Document.TargetType.COUNTERPARTY,
            counterparty=counterparty,
            category=Document.Category.CONTRACT,
            title="Чужой договор фонда",
            file="documents/fund.docx",
        )
        signer = RecipientRepresentative.objects.get(
            child=self.child,
            representative=self.parent,
        )
        contract = ServiceContract.objects.create(
            child=self.child,
            representative_link=signer,
            number="S-MISMATCH",
        )
        snapshot = ContractLegalSnapshot(
            contract_kind=ContractLegalSnapshot.ContractKind.SERVICE,
            service_contract=contract,
            document=document,
        )

        with self.assertRaises(ValidationError):
            snapshot.full_clean()


class CenterLegalProfileTests(TestCase):
    def test_active_profile_is_returned(self):
        old = CenterLegalProfile.objects.create(
            full_name="Автономная некоммерческая организация Старый центр",
            short_name="Старый центр",
            is_active=False,
        )
        active = CenterLegalProfile.objects.create(
            full_name="Автономная некоммерческая организация Радость моя",
            short_name="АНО Радость моя",
            inn="2500000000",
        )

        self.assertEqual(CenterLegalProfile.get_active(), active)
        self.assertNotEqual(CenterLegalProfile.get_active(), old)
        self.assertEqual(str(active), "АНО Радость моя")

    def test_only_one_active_profile_allowed(self):
        CenterLegalProfile.objects.create(full_name="АНО Радость моя")
        duplicate = CenterLegalProfile(full_name="Другой активный профиль")

        with self.assertRaises(ValidationError):
            duplicate.full_clean()


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
