from datetime import datetime, time, timedelta
from queue import Queue
from threading import Barrier, Thread
from unittest import skipUnless

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentConfirmation,
    AppointmentConfirmationDecision,
    Child,
    Service,
    StaffMember,
    TimeOffRequest,
)
from operations.services import confirmation_decisions as decision_svc


class ConfirmationDecisionFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("decision-admin", password="x", is_staff=True)
        cls.director = User.objects.create_superuser("decision-director", password="x")
        cls.specialist_user = User.objects.create_user("decision-specialist", password="x")
        cls.child = Child.objects.create(last_name="Решение", first_name="Получатель")
        cls.staff = StaffMember.objects.create(
            user=cls.specialist_user,
            full_name="Решение Специалист",
            email="specialist@example.local",
        )
        cls.service = Service.objects.create(name="Решение услуга", code="DECISION")

    def create_confirmation(
        self,
        *,
        target_type=AppointmentConfirmation.TargetType.SPECIALIST,
    ):
        starts_at = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=2), time(10, 0)),
            timezone.get_current_timezone(),
        )
        appointment = Appointment.objects.create(
            child=self.child,
            staff_member=self.staff,
            service=self.service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            status=Appointment.Status.PROPOSED,
        )
        return AppointmentConfirmation.objects.create(
            appointment=appointment,
            target_type=target_type,
            email="target@example.local",
            subject="Согласование",
            message="Подтвердите занятие.",
            sent_by=self.admin,
        )


class ConfirmationDecisionServiceTests(ConfirmationDecisionFixture):
    def test_administrator_can_resolve_pending_confirmation_manually(self):
        confirmation = self.create_confirmation()

        record = decision_svc.resolve_manually(
            confirmation,
            action="confirm",
            reason="Специалист подтвердил по телефону.",
            actor=self.admin,
        )

        confirmation.refresh_from_db()
        self.assertEqual(confirmation.status, AppointmentConfirmation.Status.CONFIRMED)
        self.assertEqual(
            record.source,
            AppointmentConfirmationDecision.Source.ADMINISTRATOR_MANUAL,
        )
        self.assertEqual(
            record.actor_role_snapshot,
            AppointmentConfirmationDecision.ActorRole.ADMINISTRATOR,
        )
        self.assertTrue(record.is_current)

    def test_director_can_override_administrator_decision(self):
        confirmation = self.create_confirmation()
        first = decision_svc.resolve_manually(
            confirmation,
            action="confirm",
            reason="Оперативное подтверждение администратора.",
            actor=self.admin,
        )

        second = decision_svc.resolve_manually(
            confirmation,
            action="decline",
            reason="Руководитель отменил решение после проверки.",
            actor=self.director,
        )

        first.refresh_from_db()
        confirmation.refresh_from_db()
        self.assertFalse(first.is_current)
        self.assertTrue(second.is_current)
        self.assertEqual(second.supersedes, first)
        self.assertEqual(
            second.source,
            AppointmentConfirmationDecision.Source.DIRECTOR_MANUAL,
        )
        self.assertEqual(confirmation.status, AppointmentConfirmation.Status.DECLINED)

    def test_administrator_cannot_override_director_decision(self):
        confirmation = self.create_confirmation()
        decision_svc.resolve_manually(
            confirmation,
            action="decline",
            reason="Окончательное решение руководителя.",
            actor=self.director,
        )

        with self.assertRaises(PermissionDenied):
            decision_svc.resolve_manually(
                confirmation,
                action="confirm",
                reason="Попытка администратора изменить решение.",
                actor=self.admin,
            )

        self.assertEqual(confirmation.decision_history.count(), 1)

    def test_external_response_has_explicit_source(self):
        confirmation = self.create_confirmation(
            target_type=AppointmentConfirmation.TargetType.RECIPIENT
        )

        record = decision_svc.record_external_response(
            confirmation,
            action="confirm",
            note="Подтверждаю.",
        )

        self.assertEqual(
            record.source,
            AppointmentConfirmationDecision.Source.RECIPIENT_RESPONSE,
        )
        self.assertIsNone(record.actor)

    def test_manual_resolution_requires_reason(self):
        confirmation = self.create_confirmation()

        with self.assertRaisesMessage(ValueError, "не короче 5"):
            decision_svc.resolve_manually(
                confirmation,
                action="confirm",
                reason="нет",
                actor=self.admin,
            )

    def test_manual_decision_protects_actor_audit_identity(self):
        confirmation = self.create_confirmation()
        decision_svc.resolve_manually(
            confirmation,
            action="confirm",
            reason="Решение должно сохранить автора аудита.",
            actor=self.admin,
        )

        with self.assertRaises(ProtectedError):
            self.admin.delete()

    def test_decision_source_must_match_actor_role_snapshot(self):
        confirmation = self.create_confirmation()

        with self.assertRaises(ValidationError):
            AppointmentConfirmationDecision.objects.create(
                confirmation=confirmation,
                decision=AppointmentConfirmationDecision.Decision.CONFIRMED,
                source=AppointmentConfirmationDecision.Source.SPECIALIST_RESPONSE,
                actor_role_snapshot=AppointmentConfirmationDecision.ActorRole.DIRECTOR,
            )


class ConfirmationDecisionViewTests(ConfirmationDecisionFixture):
    def test_appointment_detail_shows_manual_resolution_form(self):
        confirmation = self.create_confirmation()
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse("appointment_detail", args=[confirmation.appointment_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse("appointment_confirmation_resolve", args=[confirmation.pk]),
        )
        self.assertContains(response, "Основание решения")

    def test_administrator_can_resolve_from_appointment_detail(self):
        confirmation = self.create_confirmation()
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("appointment_confirmation_resolve", args=[confirmation.pk]),
            {
                "action": "confirm",
                "reason": "Подтверждено администратором по телефону.",
            },
        )

        self.assertRedirects(
            response,
            reverse("appointment_detail", args=[confirmation.appointment_id]),
        )
        confirmation.refresh_from_db()
        self.assertEqual(confirmation.status, AppointmentConfirmation.Status.CONFIRMED)
        self.assertEqual(
            confirmation.decision_history.get(is_current=True).source,
            AppointmentConfirmationDecision.Source.ADMINISTRATOR_MANUAL,
        )

    def test_specialist_cannot_use_manual_resolution_endpoint(self):
        confirmation = self.create_confirmation()
        self.client.force_login(self.specialist_user)

        response = self.client.post(
            reverse("appointment_confirmation_resolve", args=[confirmation.pk]),
            {
                "action": "confirm",
                "reason": "Попытка ручного решения специалистом.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)
        confirmation.refresh_from_db()
        self.assertEqual(confirmation.status, AppointmentConfirmation.Status.PENDING)

    def test_public_response_creates_external_decision_record(self):
        confirmation = self.create_confirmation()

        response = self.client.post(
            reverse("appointment_confirmation_public", args=[confirmation.token]),
            {"action": "confirm", "response_note": "Готов принять занятие."},
        )

        self.assertEqual(response.status_code, 200)
        record = confirmation.decision_history.get(is_current=True)
        self.assertEqual(
            record.source,
            AppointmentConfirmationDecision.Source.SPECIALIST_RESPONSE,
        )

    def test_specialist_cannot_decide_time_off_request_directly(self):
        request = TimeOffRequest.objects.create(
            staff_member=self.staff,
            request_type=TimeOffRequest.RequestType.DAY_OFF,
            starts_on=timezone.localdate() + timedelta(days=3),
            ends_on=timezone.localdate() + timedelta(days=3),
        )
        self.client.force_login(self.specialist_user)

        response = self.client.post(
            reverse("time_off_request_decide", args=[request.pk]),
            {"action": "approve"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)
        request.refresh_from_db()
        self.assertEqual(request.status, TimeOffRequest.Status.PENDING)


class ConfirmationDecisionPostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            "decision-pg-admin",
            password="x",
            is_staff=True,
        )
        child = Child.objects.create(last_name="Решение PG", first_name="Получатель")
        staff = StaffMember.objects.create(full_name="Решение PG Специалист")
        service = Service.objects.create(name="Решение PG услуга", code="DECISION-PG")
        starts_at = timezone.make_aware(
            datetime.combine(
                timezone.localdate() + timedelta(days=2),
                time(10, 0),
            ),
            timezone.get_current_timezone(),
        )
        appointment = Appointment.objects.create(
            child=child,
            staff_member=staff,
            service=service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            status=Appointment.Status.PROPOSED,
        )
        self.confirmation = AppointmentConfirmation.objects.create(
            appointment=appointment,
            target_type=AppointmentConfirmation.TargetType.SPECIALIST,
            email="pg-target@example.local",
            subject="Согласование",
            message="Подтвердите занятие.",
            sent_by=self.admin,
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентная блокировка проверяется только на PostgreSQL.",
    )
    def test_concurrent_manual_decisions_leave_one_current_record(self):
        barrier = Barrier(2)
        errors = Queue()

        def decide(action: str, reason: str) -> None:
            close_old_connections()
            try:
                actor = User.objects.get(pk=self.admin.pk)
                confirmation = AppointmentConfirmation.objects.get(
                    pk=self.confirmation.pk
                )
                barrier.wait(timeout=10)
                decision_svc.resolve_manually(
                    confirmation,
                    action=action,
                    reason=reason,
                    actor=actor,
                )
            except BaseException as exc:
                errors.put(exc)
            finally:
                connection.close()

        threads = [
            Thread(
                target=decide,
                args=("confirm", "Первое конкурентное решение администратора."),
            ),
            Thread(
                target=decide,
                args=("decline", "Второе конкурентное решение администратора."),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        if not errors.empty():
            raise errors.get()

        decisions = self.confirmation.decision_history.order_by("created_at")
        self.assertEqual(decisions.count(), 2)
        self.assertEqual(decisions.filter(is_current=True).count(), 1)
        self.assertIsNotNone(decisions.get(is_current=True).supersedes_id)
