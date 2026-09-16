"""Contracts for the durable appointment-confirmation email outbox."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from io import StringIO
from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import close_old_connections, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentConfirmation,
    AppointmentParticipant,
    AppointmentRescheduleStep,
    Child,
    ConfirmationEmailDelivery,
    ParentGuardian,
    Room,
    Service,
    StaffMember,
)
from operations.services import (
    confirmation_email_outbox as outbox_svc,
    rescheduling_plans as plan_svc,
    schedule_decisions,
)


def _local_dt(day, clock):
    return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RM_PUBLIC_BASE_URL="https://center.example.test",
    EMAIL_TIMEOUT=5,
)
class ConfirmationEmailOutboxTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("outbox-admin", password="x", is_staff=True)
        self.parent = ParentGuardian.objects.create(
            last_name="Очередь",
            first_name="Родитель",
            email="outbox-parent@example.local",
        )
        self.child = Child.objects.create(
            last_name="Очередь",
            first_name="Ребенок",
            primary_parent=self.parent,
        )
        self.staff = StaffMember.objects.create(
            full_name="Очередь Специалист",
            email="outbox-staff@example.local",
        )
        self.service = Service.objects.create(name="Очередь услуга", code="OUTBOX")
        self.room = Room.objects.create(name="Очередь кабинет")
        starts_at = _local_dt(timezone.localdate() + timedelta(days=3), time(10, 0))
        self.appointment = Appointment.objects.create(
            child=self.child,
            staff_member=self.staff,
            service=self.service,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            status=Appointment.Status.PROPOSED,
        )

    def confirmation(self, *, target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE):
        return AppointmentConfirmation.objects.create(
            appointment=self.appointment,
            target_type=target_type,
            representative=self.parent
            if target_type == AppointmentConfirmation.TargetType.REPRESENTATIVE
            else None,
            email=self.parent.email
            if target_type == AppointmentConfirmation.TargetType.REPRESENTATIVE
            else self.staff.email,
            subject="Подтвердите занятие",
            message="Проверьте время и подтвердите занятие.",
            sent_by=self.admin,
        )

    def queue(self, confirmation=None):
        return outbox_svc.queue_confirmation(confirmation or self.confirmation())

    def claim(self, confirmation_id):
        delivery = outbox_svc.claim_delivery(confirmation_id=confirmation_id)
        self.assertIsNotNone(delivery)
        return delivery

    def test_queue_rollback_leaves_no_delivery(self):
        confirmation = self.confirmation()

        with transaction.atomic():
            outbox_svc.queue_confirmation(confirmation)
            transaction.set_rollback(True)

        self.assertFalse(
            ConfirmationEmailDelivery.objects.filter(confirmation=confirmation).exists()
        )

    def test_queue_is_deduplicated_and_repeated_legacy_task_sends_once(self):
        confirmation = self.confirmation()
        first = self.queue(confirmation)
        second = self.queue(confirmation)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ConfirmationEmailDelivery.objects.count(), 1)
        from operations.tasks import send_appointment_confirmation_email

        with patch(
            "operations.services.confirmation_email_outbox.EmailMessage.send", return_value=1
        ) as send:
            self.assertTrue(send_appointment_confirmation_email.call(confirmation.pk))
            self.assertTrue(send_appointment_confirmation_email.call(confirmation.pk))

        first.refresh_from_db()
        confirmation.refresh_from_db()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(first.status, ConfirmationEmailDelivery.Status.SENT)
        self.assertEqual(confirmation.delivery_status, AppointmentConfirmation.DeliveryStatus.SENT)

    def test_retry_then_success_clears_safe_error(self):
        delivery = self.queue()
        before_attempt = timezone.now()
        with patch(
            "operations.services.confirmation_email_outbox.EmailMessage.send",
            side_effect=OSError("smtp://secret@example.local"),
        ):
            outbox_svc.process_due()

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, ConfirmationEmailDelivery.Status.RETRY)
        self.assertEqual(delivery.attempts, 1)
        self.assertTrue(delivery.last_error)
        self.assertNotIn("secret@example.local", delivery.last_error)
        self.assertGreaterEqual(delivery.next_attempt_at, before_attempt + timedelta(seconds=60))

        ConfirmationEmailDelivery.objects.filter(pk=delivery.pk).update(
            next_attempt_at=timezone.now()
        )
        with patch(
            "operations.services.confirmation_email_outbox.EmailMessage.send", return_value=1
        ):
            outbox_svc.process_due()

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, ConfirmationEmailDelivery.Status.SENT)
        self.assertEqual(delivery.attempts, 2)
        self.assertEqual(delivery.last_error, "")

    def test_max_attempts_becomes_terminal_failure(self):
        delivery = self.queue()
        with patch(
            "operations.services.confirmation_email_outbox.EmailMessage.send", return_value=0
        ):
            for attempt in range(5):
                ConfirmationEmailDelivery.objects.filter(pk=delivery.pk).update(
                    next_attempt_at=timezone.now()
                )
                outbox_svc.process_due()
                delivery.refresh_from_db()
                self.assertEqual(delivery.attempts, attempt + 1)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, ConfirmationEmailDelivery.Status.FAILED)
        self.assertIsNone(outbox_svc.claim_delivery(confirmation_id=delivery.confirmation_id))

    def test_missing_or_stale_claim_cannot_send(self):
        delivery = self.queue()
        claim = self.claim(delivery.confirmation_id)

        with patch(
            "operations.services.confirmation_email_outbox.EmailMessage.send", return_value=1
        ) as send:
            self.assertFalse(outbox_svc.deliver_claim(999999, uuid4()))
            self.assertFalse(outbox_svc.deliver_claim(claim.pk, uuid4()))

        self.assertEqual(send.call_count, 0)

    def test_expired_claim_is_recovered_with_new_token(self):
        delivery = self.queue()
        first = self.claim(delivery.confirmation_id)
        ConfirmationEmailDelivery.objects.filter(pk=first.pk).update(
            locked_until=timezone.now() - timedelta(seconds=1)
        )

        second = self.claim(delivery.confirmation_id)

        self.assertNotEqual(first.claim_token, second.claim_token)
        self.assertEqual(second.attempts, 2)
        self.assertEqual(second.status, ConfirmationEmailDelivery.Status.PROCESSING)
        with patch(
            "operations.services.confirmation_email_outbox.EmailMessage.send", return_value=1
        ) as send:
            self.assertFalse(outbox_svc.deliver_claim(first.pk, first.claim_token))
        self.assertEqual(send.call_count, 0)

    def test_cancelled_or_changed_appointment_is_not_sent(self):
        for mutation in ("cancelled", "changed"):
            with self.subTest(mutation=mutation):
                confirmation = self.confirmation()
                delivery = self.queue(confirmation)
                if mutation == "cancelled":
                    self.appointment.status = Appointment.Status.CANCELLED
                    self.appointment.save(update_fields=["status", "updated_at"])
                else:
                    self.appointment.starts_at += timedelta(hours=1)
                    self.appointment.ends_at += timedelta(hours=1)
                    self.appointment.save(update_fields=["starts_at", "ends_at", "updated_at"])

                with patch(
                    "operations.services.confirmation_email_outbox.EmailMessage.send",
                    return_value=1,
                ) as send:
                    outbox_svc.process_due()

                delivery.refresh_from_db()
                self.assertEqual(delivery.status, ConfirmationEmailDelivery.Status.CANCELLED)
                self.assertEqual(send.call_count, 0)

                self.appointment.refresh_from_db()
                self.appointment.status = Appointment.Status.PROPOSED
                self.appointment.starts_at -= (
                    timedelta(hours=1) if mutation == "changed" else timedelta()
                )
                self.appointment.ends_at -= (
                    timedelta(hours=1) if mutation == "changed" else timedelta()
                )
                self.appointment.save(
                    update_fields=["status", "starts_at", "ends_at", "updated_at"]
                )

    def test_replied_confirmation_and_operator_schedule_acceptance_are_not_sent(self):
        confirmation = self.confirmation()
        delivery = self.queue(confirmation)
        confirmation.status = AppointmentConfirmation.Status.CONFIRMED
        confirmation.responded_at = timezone.now()
        confirmation.save(update_fields=["status", "responded_at", "updated_at"])

        with patch(
            "operations.services.confirmation_email_outbox.EmailMessage.send", return_value=1
        ) as send:
            outbox_svc.process_due()
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, ConfirmationEmailDelivery.Status.CANCELLED)
        self.assertEqual(send.call_count, 0)

        specialist_confirmation = self.confirmation(
            target_type=AppointmentConfirmation.TargetType.SPECIALIST
        )
        specialist_delivery = self.queue(specialist_confirmation)
        schedule_decisions.resolve_manually(
            self.appointment,
            staff_member=self.staff,
            action="confirm",
            reason="Оператор подтвердил расписание.",
            actor=self.admin,
        )

        with patch(
            "operations.services.confirmation_email_outbox.EmailMessage.send", return_value=1
        ) as send:
            outbox_svc.process_due()
        specialist_delivery.refresh_from_db()
        self.assertEqual(specialist_delivery.status, ConfirmationEmailDelivery.Status.CANCELLED)
        self.assertEqual(send.call_count, 0)

    def test_queue_snapshot_contains_public_absolute_link(self):
        confirmation = self.confirmation()

        delivery = self.queue(confirmation)

        self.assertIn(
            f"https://center.example.test/confirmations/{confirmation.token}/",
            delivery.body,
        )
        self.assertEqual(delivery.email, confirmation.email)
        self.assertTrue(delivery.fingerprint)

    def test_malformed_public_base_url_rejects_queue_without_delivery(self):
        confirmation = self.confirmation()

        with (
            override_settings(RM_PUBLIC_BASE_URL="https://center.example.test/?query=forbidden"),
            self.assertRaises(ValueError),
        ):
            outbox_svc.queue_confirmation(confirmation)

        self.assertFalse(
            ConfirmationEmailDelivery.objects.filter(confirmation=confirmation).exists()
        )

    def test_changed_target_composition_is_not_sent(self):
        participant = self.appointment.participants.get(child=self.child)
        confirmation = AppointmentConfirmation.objects.create(
            appointment=self.appointment,
            target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
            representative=self.parent,
            participant=participant,
            email=self.parent.email,
            subject="Состав занятия изменился",
            message="Подтвердите занятие.",
            sent_by=self.admin,
        )
        delivery = self.queue(confirmation)
        participant.appointment_status = Appointment.Status.CANCELLED
        participant.save(update_fields=["appointment_status", "updated_at"])

        with patch(
            "operations.services.confirmation_email_outbox.EmailMessage.send", return_value=1
        ) as send:
            outbox_svc.process_due()

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, ConfirmationEmailDelivery.Status.CANCELLED)
        send.assert_not_called()

    def test_changed_group_composition_or_labels_is_not_sent(self):
        self.room.allow_group_sessions = True
        self.room.max_recipient_count = 2
        self.room.save(
            update_fields=["allow_group_sessions", "max_recipient_count", "updated_at"]
        )
        self.appointment.session_type = Appointment.SessionType.GROUP
        self.appointment.save(update_fields=["session_type", "updated_at"])
        primary_participant = self.appointment.participants.get(child=self.child)
        other_parent = ParentGuardian.objects.create(
            last_name="Группа", first_name="Другой родитель", email="other-group@example.local"
        )
        other_child = Child.objects.create(
            last_name="Группа", first_name="Другой ребенок", primary_parent=other_parent
        )
        other_participant = AppointmentParticipant.objects.create(
            appointment=self.appointment,
            child=other_child,
            starts_at_snapshot=self.appointment.starts_at,
            ends_at_snapshot=self.appointment.ends_at,
            appointment_status=Appointment.Status.PROPOSED,
        )

        def ordinary_delivery(subject):
            confirmation = AppointmentConfirmation.objects.create(
                appointment=self.appointment,
                target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
                representative=self.parent,
                participant=primary_participant,
                email=self.parent.email,
                subject=subject,
                message="Подтвердите занятие.",
                sent_by=self.admin,
            )
            return self.queue(confirmation)

        def proposed_room_delivery():
            self.staff.email = "outbox-proposed-room@example.local"
            self.staff.save(update_fields=["email", "updated_at"])
            plan = plan_svc.create_plan_for_appointment(
                self.appointment, actor=self.admin, days=2, limit=1
            )
            step = plan.steps.get()
            self.assertEqual(
                step.action_type,
                AppointmentRescheduleStep.ActionType.MOVE,
                step.validation_messages,
            )
            result = plan_svc.create_confirmations_for_step(step, actor=self.admin)
            return ConfirmationEmailDelivery.objects.get(confirmation=result.created[0]), step

        mutations = (
            "unrelated participant becomes inactive",
            "service label changes",
            "proposed room label changes",
        )

        for label in mutations:
            with self.subTest(label=label):
                if label == "unrelated participant becomes inactive":
                    delivery = ordinary_delivery("Группа изменилась")
                    other_participant.appointment_status = Appointment.Status.CANCELLED
                    other_participant.save(update_fields=["appointment_status", "updated_at"])
                elif label == "service label changes":
                    delivery = ordinary_delivery("Услуга изменилась")
                    self.service.name = "Переименованная услуга"
                    self.service.save(update_fields=["name", "updated_at"])
                else:
                    other_participant.appointment_status = Appointment.Status.PROPOSED
                    other_participant.save(update_fields=["appointment_status", "updated_at"])
                    delivery, step = proposed_room_delivery()
                    step.proposed_room.name = "Переименованный кабинет"
                    step.proposed_room.save(update_fields=["name", "updated_at"])

                with patch(
                    "operations.services.confirmation_email_outbox.EmailMessage.send",
                    return_value=1,
                ) as send:
                    outbox_svc.process_due()

                delivery.refresh_from_db()
                self.assertEqual(delivery.status, ConfirmationEmailDelivery.Status.CANCELLED)
                send.assert_not_called()

    def test_past_appointment_is_not_sent(self):
        delivery = self.queue()
        past_now = self.appointment.starts_at + timedelta(minutes=1)

        with (
            patch(
                "operations.services.confirmation_email_outbox.timezone.now", return_value=past_now
            ),
            patch(
                "operations.services.confirmation_email_outbox.EmailMessage.send", return_value=1
            ) as send,
        ):
            outbox_svc.process_due()

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, ConfirmationEmailDelivery.Status.CANCELLED)
        send.assert_not_called()

    def test_view_and_step_roll_back_confirmation_when_outbox_creation_fails(self):
        client = Client()
        client.force_login(self.admin)
        with patch(
            "operations.views.confirmations.email_outbox.queue_confirmation",
            side_effect=ValueError("invalid public URL"),
        ):
            response = client.post(
                reverse("appointment_send_confirmation", args=[self.appointment.pk]),
                {
                    "target_type": AppointmentConfirmation.TargetType.REPRESENTATIVE,
                    "subject": "Откат карточки",
                    "message": "Не должно сохраниться.",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AppointmentConfirmation.objects.filter(subject="Откат карточки").exists())
        self.assertFalse(ConfirmationEmailDelivery.objects.exists())

        self.staff.email = "outbox-rollback-plan@example.local"
        self.staff.save(update_fields=["email", "updated_at"])
        plan = plan_svc.create_plan_for_appointment(
            self.appointment, actor=self.admin, days=2, limit=1
        )
        step = plan.steps.get()
        with (
            patch(
                "operations.services.confirmation_email_outbox.queue_confirmation",
                side_effect=ValueError("invalid public URL"),
            ),
            self.assertRaises(ValidationError),
        ):
            plan_svc.create_confirmations_for_step(step, actor=self.admin)

        self.assertFalse(AppointmentConfirmation.objects.filter(reschedule_step=step).exists())
        self.assertFalse(ConfirmationEmailDelivery.objects.exists())

    def test_confirmation_view_and_step_service_create_delivery_in_same_flow(self):
        client = Client()
        client.force_login(self.admin)
        response = client.post(
            reverse("appointment_send_confirmation", args=[self.appointment.pk]),
            {
                "target_type": AppointmentConfirmation.TargetType.REPRESENTATIVE,
                "subject": "Из карточки занятия",
                "message": "Подтвердите время.",
            },
        )
        self.assertEqual(response.status_code, 302)
        direct = AppointmentConfirmation.objects.get(subject="Из карточки занятия")
        self.assertTrue(ConfirmationEmailDelivery.objects.filter(confirmation=direct).exists())

        self.staff.email = "outbox-plan-staff@example.local"
        self.staff.save(update_fields=["email", "updated_at"])
        plan = plan_svc.create_plan_for_appointment(
            self.appointment, actor=self.admin, days=2, limit=1
        )
        step = plan.steps.get()
        self.assertEqual(step.status, AppointmentRescheduleStep.Status.VALID)

        result = plan_svc.create_confirmations_for_step(step, actor=self.admin)

        self.assertTrue(result.created)
        self.assertEqual(
            ConfirmationEmailDelivery.objects.filter(confirmation__in=result.created).count(),
            len(result.created),
        )

    def test_non_operator_cannot_create_confirmation_or_delivery(self):
        client = Client()
        client.force_login(User.objects.create_user("outbox-specialist", password="x"))

        response = client.post(
            reverse("appointment_send_confirmation", args=[self.appointment.pk]),
            {
                "target_type": AppointmentConfirmation.TargetType.REPRESENTATIVE,
                "subject": "Запрещено",
                "message": "Запрещенная отправка.",
            },
        )

        self.assertIn(response.status_code, {302, 403})
        self.assertFalse(AppointmentConfirmation.objects.filter(subject="Запрещено").exists())
        self.assertFalse(ConfirmationEmailDelivery.objects.exists())

    def test_status_command_only_reads_counts_and_once_reports_worker_counts(self):
        self.queue()
        output = StringIO()
        with (
            patch("operations.services.confirmation_email_outbox.EmailMessage.send") as send,
            patch(
                "operations.management.commands.process_confirmation_emails.process_due"
            ) as process,
        ):
            call_command("process_confirmation_emails", "--status", stdout=output)

        self.assertIn('"pending": 1', output.getvalue())
        process.assert_not_called()
        send.assert_not_called()

        output = StringIO()
        with (
            patch(
                "operations.management.commands.process_confirmation_emails.connection",
                SimpleNamespace(vendor="postgresql"),
            ),
            patch(
                "operations.management.commands.process_confirmation_emails.process_due",
                return_value={"processed": 1, "sent": 1, "not_sent": 0},
            ) as process,
        ):
            call_command("process_confirmation_emails", "--once", "--limit", "2", stdout=output)

        process.assert_called_once_with(limit=2)
        self.assertIn('"processed": 1', output.getvalue())


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RM_PUBLIC_BASE_URL="https://center.example.test",
    EMAIL_TIMEOUT=5,
)
class ConfirmationEmailOutboxPostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            "outbox-concurrent-admin", password="x", is_staff=True
        )
        parent = ParentGuardian.objects.create(
            last_name="Параллельный", first_name="Родитель", email="concurrent@example.local"
        )
        child = Child.objects.create(
            last_name="Параллельный", first_name="Ребенок", primary_parent=parent
        )
        staff = StaffMember.objects.create(full_name="Параллельный Специалист")
        service = Service.objects.create(name="Параллельная услуга", code="OUTBOX-CONCURRENT")
        starts_at = _local_dt(timezone.localdate() + timedelta(days=4), time(10, 0))
        appointment = Appointment.objects.create(
            child=child,
            staff_member=staff,
            service=service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            status=Appointment.Status.PROPOSED,
        )
        confirmation = AppointmentConfirmation.objects.create(
            appointment=appointment,
            target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
            representative=parent,
            email=parent.email,
            subject="Параллельное согласование",
            message="Подтвердите занятие.",
        )
        self.delivery = outbox_svc.queue_confirmation(confirmation)

    @skipUnless(
        connection.vendor == "postgresql", "Конкурентная отправка проверяется только на PostgreSQL."
    )
    def test_second_runner_cannot_send_while_first_holds_delivery_lock(self):
        claim = outbox_svc.claim_delivery(confirmation_id=self.delivery.confirmation_id)
        ConfirmationEmailDelivery.objects.filter(pk=claim.pk).update(
            locked_until=timezone.now() - timedelta(seconds=1)
        )
        started = Event()
        release = Event()
        outcomes = Queue()

        def blocked_send(_message, **kwargs):
            started.set()
            self.assertTrue(release.wait(timeout=10))
            return 1

        def deliver() -> None:
            close_old_connections()
            try:
                outcomes.put(outbox_svc.deliver_claim(claim.pk, claim.claim_token))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        with patch(
            "operations.services.confirmation_email_outbox.EmailMessage.send",
            autospec=True,
            side_effect=blocked_send,
        ) as send:
            first = Thread(target=deliver)
            first.start()
            self.assertTrue(started.wait(timeout=10))
            # The lease is stale, but the live SMTP sender owns the row lock.
            self.assertIsNone(
                outbox_svc.claim_delivery(confirmation_id=self.delivery.confirmation_id)
            )
            release.set()
            first.join(timeout=15)

        self.assertFalse(first.is_alive())
        outcome = outcomes.get_nowait()
        if isinstance(outcome, BaseException):
            raise outcome
        self.assertTrue(outcome)
        self.assertEqual(send.call_count, 1)

    @skipUnless(
        connection.vendor == "postgresql", "Конкурентная отправка проверяется только на PostgreSQL."
    )
    def test_operator_acceptance_commits_while_smtp_is_in_flight(self):
        confirmation = AppointmentConfirmation.objects.create(
            appointment=self.delivery.confirmation.appointment,
            target_type=AppointmentConfirmation.TargetType.SPECIALIST,
            email="concurrent-specialist@example.local",
            subject="Согласование специалиста",
            message="Подтвердите занятие.",
            sent_by=self.admin,
        )
        delivery = outbox_svc.queue_confirmation(confirmation)
        claim = outbox_svc.claim_delivery(confirmation_id=confirmation.pk)
        started = Event()
        release = Event()
        outcomes = Queue()

        def blocked_send(_message, **kwargs):
            started.set()
            self.assertTrue(release.wait(timeout=10))
            return 1

        def deliver() -> None:
            close_old_connections()
            try:
                outcomes.put(outbox_svc.deliver_claim(claim.pk, claim.claim_token))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        with patch(
            "operations.services.confirmation_email_outbox.EmailMessage.send",
            autospec=True,
            side_effect=blocked_send,
        ):
            worker = Thread(target=deliver)
            worker.start()
            try:
                self.assertTrue(started.wait(timeout=10))
                decision = schedule_decisions.resolve_manually(
                    self.delivery.confirmation.appointment,
                    staff_member=self.delivery.confirmation.appointment.staff_member,
                    action="confirm",
                    reason="Оператор принял расписание во время отправки.",
                    actor=self.admin,
                )
            finally:
                release.set()
                worker.join(timeout=15)

        self.assertFalse(worker.is_alive())
        outcome = outcomes.get_nowait()
        if isinstance(outcome, BaseException):
            raise outcome
        self.assertTrue(outcome)
        confirmation.refresh_from_db()
        delivery.refresh_from_db()
        self.assertEqual(confirmation.status, AppointmentConfirmation.Status.CONFIRMED)
        self.assertEqual(
            schedule_decisions.current_confirmation_decision(confirmation).pk,
            decision.pk,
        )
        self.assertEqual(delivery.status, ConfirmationEmailDelivery.Status.SENT)
        self.assertEqual(confirmation.delivery_status, AppointmentConfirmation.DeliveryStatus.SENT)
