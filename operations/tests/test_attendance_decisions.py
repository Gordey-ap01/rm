from datetime import datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentAttendanceDecision,
    AppointmentParticipant,
    AppointmentScheduleDecision,
    AppointmentStaffAssignment,
    Child,
    FundingSource,
    LedgerEntry,
    Room,
    Service,
    StaffMember,
)
from operations.services import (
    appointments as appointment_svc,
    billing as billing_svc,
    schedule_decisions,
)


def _local(day, clock):
    return timezone.make_aware(
        datetime.combine(day, clock),
        timezone.get_current_timezone(),
    )


class AttendanceDecisionFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.director = User.objects.create_superuser("attendance-director", password="x")
        cls.admin = User.objects.create_user(
            "attendance-admin", password="x", is_staff=True
        )
        cls.specialist_user = User.objects.create_user("attendance-specialist", password="x")
        cls.child = Child.objects.create(last_name="Решения", first_name="Первый")
        cls.second_child = Child.objects.create(last_name="Решения", first_name="Второй")
        cls.staff = StaffMember.objects.create(
            user=cls.specialist_user,
            full_name="Специалист решений",
        )
        cls.assistant = StaffMember.objects.create(full_name="Ассистент решений")
        cls.service = Service.objects.create(
            name="Услуга решений",
            code="ATTENDANCE-DECISIONS",
            default_duration_minutes=30,
            default_price=Decimal("1500.00"),
        )
        cls.room = Room.objects.create(name="Кабинет решений")
        cls.funding = FundingSource.objects.create(
            name="Источник решений",
            source_type=FundingSource.SourceType.GRANT,
            transfer_policy=FundingSource.TransferPolicy.WITHIN_CHILD,
        )

    def setUp(self):
        self.day = timezone.localdate() + timedelta(days=30)

    def appointment(self, *, status=Appointment.Status.CONFIRMED, group=False, day=None):
        day = day or self.day
        appointment = Appointment.objects.create(
            child=self.child,
            staff_member=self.staff,
            service=self.service,
            room=self.room,
            starts_at=_local(day, time(10, 0)),
            ends_at=_local(day, time(10, 30)),
            status=status,
        )
        if group:
            AppointmentParticipant.objects.update_or_create(
                appointment=appointment,
                child=self.second_child,
                defaults={
                    "starts_at_snapshot": appointment.starts_at,
                    "ends_at_snapshot": appointment.ends_at,
                    "appointment_status": appointment.status,
                },
            )
        return appointment


class AttendanceDecisionServiceTests(AttendanceDecisionFixture):
    def test_administrator_can_mark_without_specialist_response_and_records_author(self):
        appointment = self.appointment()

        appointment_svc.record_attendance(
            appointment,
            action="completed",
            actor=self.admin,
            reason="Проведение подтверждено оператором.",
            note="Занятие состоялось.",
            operation_key=uuid4(),
        )

        decision = appointment.attendance_decisions.get()
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.COMPLETED)
        self.assertEqual(decision.actor, self.admin)
        self.assertEqual(decision.actor_role_snapshot, "administrator")
        self.assertEqual(decision.reason, "Проведение подтверждено оператором.")
        self.assertEqual(decision.status_before, Appointment.Status.CONFIRMED)
        self.assertEqual(decision.status_after, Appointment.Status.COMPLETED)

    def test_specialist_can_mark_own_appointment_but_operator_reason_is_required(self):
        appointment = self.appointment()

        appointment_svc.record_attendance(
            appointment,
            action="completed",
            actor=self.specialist_user,
            note="Отметка специалиста.",
        )
        decision = appointment.attendance_decisions.get()
        self.assertEqual(decision.actor, self.specialist_user)
        self.assertEqual(decision.actor_role_snapshot, "specialist")

        second = self.appointment(day=self.day + timedelta(days=1))
        with self.assertRaisesMessage(ValueError, "основан"):
            appointment_svc.record_attendance(
                second,
                action="completed",
                actor=self.admin,
                reason="нет",
            )

    def test_manual_override_preserves_original_specialist_note_and_group_participants(self):
        appointment = self.appointment(group=True)
        appointment_svc.record_attendance(
            appointment,
            action="completed",
            actor=self.specialist_user,
            reason="Специалист отметил проведение.",
            note="Исходная заметка специалиста.",
            participant_statuses={
                appointment.participants.get(child=self.child).pk: Appointment.AttendanceStatus.ATTENDED,
                appointment.participants.get(child=self.second_child).pk: Appointment.AttendanceStatus.MISSED,
            },
        )

        appointment_svc.record_attendance(
            appointment,
            action="not_completed",
            actor=self.admin,
            reason="Исправлено после проверки журнала.",
            operation_key=uuid4(),
        )

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.NO_SHOW)
        self.assertEqual(appointment.specialist_note, "Исходная заметка специалиста.")
        self.assertEqual(appointment.attendance_decisions.count(), 2)
        self.assertEqual(
            appointment.participants.count(),
            2,
        )
        self.assertEqual(
            list(appointment.attendance_decisions.order_by("decision_number").values_list("decision_number", flat=True)),
            [1, 2],
        )

    def test_specialist_cannot_override_existing_fact_and_director_has_priority(self):
        appointment = self.appointment()
        appointment_svc.record_attendance(
            appointment,
            action="completed",
            actor=self.admin,
            reason="Администратор подтвердил факт.",
            operation_key=uuid4(),
        )
        with self.assertRaises(PermissionDenied):
            appointment_svc.record_attendance(
                appointment,
                action="not_completed",
                actor=self.specialist_user,
                note="Поздняя отметка специалиста.",
            )

        appointment_svc.record_attendance(
            appointment,
            action="not_completed",
            actor=self.director,
            reason="Руководитель переопределил факт.",
            operation_key=uuid4(),
        )
        with self.assertRaises(PermissionDenied):
            appointment_svc.record_attendance(
                appointment,
                action="completed",
                actor=self.admin,
                reason="Попытка отменить решение руководителя.",
                operation_key=uuid4(),
            )

    def test_same_operation_key_is_idempotent_and_payload_conflict_is_rejected(self):
        appointment = self.appointment()
        key = uuid4()
        kwargs = {
            "action": "completed",
            "actor": self.admin,
            "reason": "Повторяемая отметка администратора.",
            "note": "Одна операция.",
            "operation_key": key,
        }
        first = appointment_svc.record_attendance(appointment, **kwargs)
        second = appointment_svc.record_attendance(appointment, **kwargs)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(appointment.attendance_decisions.count(), 1)
        with self.assertRaises(ValueError):
            appointment_svc.record_attendance(
                appointment,
                action="not_completed",
                actor=self.admin,
                reason="Тот же ключ с иным действием.",
                operation_key=key,
            )

    def test_financial_decision_is_separate_and_blocks_later_attendance_correction(self):
        account = self.child.balance_accounts.create(
            funding_source=self.funding,
            unit="sessions",
            service_scope="any",
            initial_amount=Decimal("2.00"),
        )
        appointment = self.appointment()
        appointment.billing_account = account
        appointment.save(update_fields=["billing_account"])
        appointment_svc.record_attendance(
            appointment,
            action="completed",
            actor=self.admin,
            reason="Проведение перед списанием.",
            operation_key=uuid4(),
        )
        self.assertEqual(LedgerEntry.objects.filter(appointment=appointment).count(), 0)
        billing_svc.apply_decision(
            appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=account,
            amount=Decimal("-1.00"),
            reason="Отдельное решение по списанию.",
            actor=self.admin,
        )
        with self.assertRaises(appointment_svc.AppointmentStateConflict):
            appointment_svc.record_attendance(
                appointment,
                action="not_completed",
                actor=self.admin,
                reason="Исправление после финансового факта.",
                operation_key=uuid4(),
            )

    def test_not_completed_rejects_attended_active_participant(self):
        appointment = self.appointment()
        participant = appointment.participants.get(child=self.child)

        with self.assertRaises(ValueError):
            appointment_svc.record_attendance(
                appointment,
                action="not_completed",
                actor=self.admin,
                reason="Неявка противоречит отметке участника.",
                participant_statuses={
                    participant.pk: Appointment.AttendanceStatus.ATTENDED,
                },
                operation_key=uuid4(),
            )

    def test_attendance_decision_is_immutable_and_actor_is_protected(self):
        appointment = self.appointment()
        appointment_svc.record_attendance(
            appointment,
            action="completed",
            actor=self.admin,
            reason="Проверка неизменяемого аудита.",
            operation_key=uuid4(),
        )
        decision = appointment.attendance_decisions.get()
        decision.reason = "подмена"
        with self.assertRaises((ValidationError, ValueError, PermissionDenied)):
            decision.save(update_fields=["reason"])
        with self.assertRaises((ProtectedError, PermissionDenied, ValidationError)):
            decision.delete()

    def test_attendance_orm_rejects_false_director_snapshot_for_administrator(self):
        appointment = self.appointment()
        appointment_svc.record_attendance(
            appointment,
            action="completed",
            actor=self.admin,
            reason="Первое решение для проверки роли.",
            operation_key=uuid4(),
        )
        first = appointment.attendance_decisions.get()
        with self.assertRaises(ValidationError):
            AppointmentAttendanceDecision.objects.create(
                appointment=appointment,
                actor=self.admin,
                actor_role_snapshot="director",
                operation_key=uuid4(),
                fingerprint="role-mismatch",
                decision_number=2,
                action="completed",
                reason="Ложный снимок роли администратора.",
                note="",
                status_before=Appointment.Status.COMPLETED,
                status_after=Appointment.Status.COMPLETED,
                attendance_after=Appointment.AttendanceStatus.ATTENDED,
                starts_at_snapshot=appointment.starts_at,
                ends_at_snapshot=appointment.ends_at,
                participants_before=first.participants_after,
                participants_after=first.participants_after,
                supersedes=first,
            )


class ScheduleDecisionServiceTests(AttendanceDecisionFixture):
    def test_admin_accepts_schedule_without_confirmation_and_history_keeps_snapshots(self):
        appointment = self.appointment()
        decision = schedule_decisions.resolve_manually(
            appointment,
            staff_member=self.staff,
            action="confirm",
            reason="Расписание принято оператором.",
            actor=self.admin,
            operation_key=uuid4(),
        )

        self.assertEqual(decision.decision, "confirmed")
        self.assertEqual(decision.actor, self.admin)
        self.assertEqual(decision.staff_member, self.staff)
        self.assertEqual(decision.starts_at_snapshot, appointment.starts_at)
        self.assertEqual(decision.ends_at_snapshot, appointment.ends_at)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)

    def test_director_overrides_schedule_and_lower_roles_cannot(self):
        appointment = self.appointment()
        schedule_decisions.resolve_manually(
            appointment,
            staff_member=self.staff,
            action="confirm",
            reason="Оперативное принятие администратором.",
            actor=self.admin,
            operation_key=uuid4(),
        )
        director_decision = schedule_decisions.resolve_manually(
            appointment,
            staff_member=self.staff,
            action="decline",
            reason="Руководитель изменил решение.",
            actor=self.director,
            operation_key=uuid4(),
        )
        self.assertEqual(director_decision.actor_role_snapshot, "director")
        with self.assertRaises(PermissionDenied):
            schedule_decisions.resolve_manually(
                appointment,
                staff_member=self.staff,
                action="confirm",
                reason="Администратор не меняет итог директора.",
                actor=self.admin,
                operation_key=uuid4(),
            )
        with self.assertRaises(PermissionDenied):
            schedule_decisions.resolve_manually(
                appointment,
                staff_member=self.staff,
                action="confirm",
                reason="Специалист не меняет итог директора.",
                actor=self.specialist_user,
                operation_key=uuid4(),
            )

    def test_schedule_operation_key_is_idempotent_and_does_not_create_confirmation(self):
        appointment = self.appointment()
        key = uuid4()
        first = schedule_decisions.resolve_manually(
            appointment,
            staff_member=self.staff,
            action="confirm",
            reason="Одна операция расписания.",
            actor=self.admin,
            operation_key=key,
        )
        second = schedule_decisions.resolve_manually(
            appointment,
            staff_member=self.staff,
            action="confirm",
            reason="Одна операция расписания.",
            actor=self.admin,
            operation_key=key,
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(appointment.schedule_decisions.count(), 1)
        self.assertFalse(appointment.confirmations.exists())

    def test_schedule_decision_is_immutable_and_current_is_scoped_to_staff(self):
        appointment = self.appointment()
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=self.assistant,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        first = schedule_decisions.resolve_manually(
            appointment,
            staff_member=self.staff,
            action="confirm",
            reason="Первое решение по специалисту.",
            actor=self.admin,
            operation_key=uuid4(),
        )
        second = schedule_decisions.resolve_manually(
            appointment,
            staff_member=self.assistant,
            action="decline",
            reason="Решение по ассистенту отдельно.",
            actor=self.admin,
            operation_key=uuid4(),
        )
        self.assertEqual(schedule_decisions.current_decision(appointment, self.staff.pk), first)
        self.assertEqual(schedule_decisions.current_decision(appointment, self.assistant.pk), second)
        first.reason = "подмена"
        with self.assertRaises((ValidationError, ValueError, PermissionDenied)):
            first.save(update_fields=["reason"])
        with self.assertRaises((ProtectedError, PermissionDenied, ValidationError)):
            first.delete()

    def test_schedule_orm_rejects_false_director_and_unassigned_staff(self):
        appointment = self.appointment()
        first = schedule_decisions.resolve_manually(
            appointment,
            staff_member=self.staff,
            action="confirm",
            reason="Первое решение для проверки назначения.",
            actor=self.admin,
            operation_key=uuid4(),
        )
        with self.assertRaises(ValidationError):
            AppointmentScheduleDecision.objects.create(
                appointment=appointment,
                staff_member=self.staff,
                actor=self.admin,
                actor_role_snapshot="director",
                operation_key=uuid4(),
                fingerprint="role-mismatch",
                decision_number=2,
                decision="confirmed",
                reason="Ложный снимок роли администратора.",
                starts_at_snapshot=appointment.starts_at,
                ends_at_snapshot=appointment.ends_at,
                supersedes=first,
            )
        with self.assertRaises(ValidationError):
            AppointmentScheduleDecision.objects.create(
                appointment=appointment,
                staff_member=self.assistant,
                actor=self.admin,
                actor_role_snapshot="administrator",
                operation_key=uuid4(),
                fingerprint="unassigned-staff",
                decision_number=1,
                decision="confirmed",
                reason="Неназначенный специалист.",
                starts_at_snapshot=appointment.starts_at,
                ends_at_snapshot=appointment.ends_at,
            )


class AttendanceDecisionViewTests(AttendanceDecisionFixture):
    def test_specialist_cannot_use_operator_attendance_or_schedule_posts(self):
        appointment = self.appointment()
        self.client.force_login(self.specialist_user)
        attendance = self.client.post(
            reverse("appointment_attendance_decide", args=[appointment.pk]),
            {
                "action": "completed",
                "reason": "Специалист пытается решить через операторский POST.",
                "operation_key": str(uuid4()),
            },
        )
        schedule = self.client.post(
            reverse("appointment_schedule_decide", args=[appointment.pk]),
            {
                "staff_member": str(self.staff.pk),
                "action": "confirm",
                "reason": "Специалист пытается принять расписание.",
                "operation_key": str(uuid4()),
                "expected_schedule": (
                    f"{appointment.starts_at.isoformat()}|{appointment.ends_at.isoformat()}"
                ),
            },
        )
        self.assertEqual(attendance.status_code, 302)
        self.assertEqual(schedule.status_code, 302)
        self.assertFalse(appointment.attendance_decisions.exists())
        self.assertFalse(appointment.schedule_decisions.exists())

    def test_admin_attendance_post_and_director_schedule_post_keep_actual_author(self):
        appointment = self.appointment()
        schedule_appointment = self.appointment(day=self.day + timedelta(days=1))
        self.client.force_login(self.admin)
        participant_statuses = {
            f"participant_status_{participant.pk}": Appointment.AttendanceStatus.ATTENDED
            for participant in appointment.participants.all()
        }
        response = self.client.post(
            reverse("appointment_attendance_decide", args=[appointment.pk]),
            {
                "action": "completed",
                "reason": "Администратор подтвердил проведение.",
                "note": "Операторская заметка.",
                "operation_key": str(uuid4()),
                **participant_statuses,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(appointment.attendance_decisions.get().actor, self.admin)

        self.client.force_login(self.director)
        response = self.client.post(
            reverse("appointment_schedule_decide", args=[schedule_appointment.pk]),
            {
                "staff_member": str(self.staff.pk),
                "action": "confirm",
                "reason": "Руководитель принял расписание.",
                "operation_key": str(uuid4()),
                "expected_schedule": (
                    f"{schedule_appointment.starts_at.isoformat()}|"
                    f"{schedule_appointment.ends_at.isoformat()}"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(schedule_appointment.schedule_decisions.get().actor, self.director)

    def test_stale_schedule_post_is_rejected_without_history(self):
        appointment = self.appointment()
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("appointment_schedule_decide", args=[appointment.pk]),
            {
                "staff_member": str(self.staff.pk),
                "action": "confirm",
                "reason": "Старый снимок расписания отклоняется.",
                "operation_key": str(uuid4()),
                "expected_schedule": "stale-start|stale-end",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(appointment.schedule_decisions.exists())

    def test_schedule_post_falls_back_to_legacy_appointment_staff(self):
        appointment = self.appointment()
        appointment.staff_assignments.all().delete()
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("appointment_schedule_decide", args=[appointment.pk]),
            {
                "staff_member": str(self.staff.pk),
                "action": "confirm",
                "reason": "Принято по legacy назначению занятия.",
                "operation_key": str(uuid4()),
                "expected_schedule": (
                    f"{appointment.starts_at.isoformat()}|{appointment.ends_at.isoformat()}"
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        decision = appointment.schedule_decisions.get()
        self.assertEqual(decision.staff_member, self.staff)
