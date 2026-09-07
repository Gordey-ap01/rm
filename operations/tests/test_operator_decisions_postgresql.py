from datetime import datetime, time, timedelta
from decimal import Decimal
from queue import Queue
from threading import Barrier, Thread
from unittest import skipUnless
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone
from psycopg.types.json import Json

from operations.models import (
    Appointment,
    AppointmentAttendanceDecision,
    AppointmentParticipant,
    AppointmentScheduleDecision,
    Child,
    Room,
    Service,
    StaffMember,
)
from operations.services import appointments as appointment_svc, schedule_decisions


@skipUnless(connection.vendor == "postgresql", "PostgreSQL guards/concurrency only.")
class OperatorDecisionPostgreSQLTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.admin = User.objects.create_user("pg-operator-admin", password="x", is_staff=True)
        self.director = User.objects.create_superuser("pg-operator-director", password="x")
        self.child = Child.objects.create(last_name="PG", first_name="Решения")
        self.staff = StaffMember.objects.create(full_name="PG специалист")
        self.service = Service.objects.create(
            name="PG услуга",
            code="PG-OPERATOR-DECISIONS",
            default_duration_minutes=30,
            default_price=Decimal("1000.00"),
        )
        self.room = Room.objects.create(name="PG кабинет")

    def appointment(self):
        starts_at = timezone.make_aware(
            datetime.combine(
                timezone.localdate() + timedelta(days=20),
                time(10, 0),
            ),
            timezone.get_current_timezone(),
        )
        appointment = Appointment.objects.create(
            child=self.child,
            staff_member=self.staff,
            service=self.service,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            status=Appointment.Status.CONFIRMED,
        )
        return appointment

    def _raw_insert(self, model, instance, **overrides):
        fields = [field for field in model._meta.local_concrete_fields if not field.primary_key]
        columns = ", ".join(connection.ops.quote_name(field.column) for field in fields)
        values = []
        for field in fields:
            value = overrides.get(field.attname, getattr(instance, field.attname))
            if field.get_internal_type() == "JSONField":
                value = Json(value)
            values.append(value)
        placeholders = ", ".join(["%s"] * len(fields))
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {model._meta.db_table} ({columns}) VALUES ({placeholders})",
                values,
            )

    def test_concurrent_admin_and_director_leave_director_as_effective_attendance_decision(self):
        appointment = self.appointment()
        barrier = Barrier(2)
        outcomes = Queue()

        def decide(actor_id, action, reason):
            close_old_connections()
            try:
                actor = User.objects.get(pk=actor_id)
                current = Appointment.objects.get(pk=appointment.pk)
                barrier.wait(timeout=15)
                record = appointment_svc.record_attendance(
                    current,
                    action=action,
                    actor=actor,
                    reason=reason,
                    operation_key=uuid4(),
                )
                outcomes.put((actor.username, record.pk))
            except PermissionDenied:
                outcomes.put(("permission_denied", None))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        threads = [
            Thread(
                target=decide,
                args=(self.admin.pk, "completed", "Администратор отметил проведение."),
            ),
            Thread(
                target=decide,
                args=(self.director.pk, "not_completed", "Руководитель переопределил факт."),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get(timeout=5) for _ in threads]
        self.assertTrue(all(not isinstance(result, BaseException) for result in results), results)
        appointment.refresh_from_db()
        latest = appointment.attendance_decisions.order_by("-decision_number").first()
        self.assertEqual(latest.actor_id, self.director.pk)
        self.assertEqual(latest.actor_role_snapshot, "director")
        self.assertEqual(appointment.status, Appointment.Status.NO_SHOW)

    def test_repeated_attendance_operation_key_creates_one_event(self):
        appointment = self.appointment()
        key = uuid4()
        kwargs = {
            "action": "completed",
            "actor": self.admin,
            "reason": "Повторяемая PG операция.",
            "operation_key": key,
        }
        first = appointment_svc.record_attendance(appointment, **kwargs)
        second = appointment_svc.record_attendance(appointment, **kwargs)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            AppointmentAttendanceDecision.objects.filter(operation_key=key).count(),
            1,
        )

    def test_raw_sql_insert_rejects_false_attendance_role_snapshot(self):
        appointment = self.appointment()
        first = appointment_svc.record_attendance(
            appointment,
            action="completed",
            actor=self.admin,
            reason="Первое решение для raw role guard.",
            operation_key=uuid4(),
        )
        raw = AppointmentAttendanceDecision.objects.get(pk=first.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._raw_insert(
                AppointmentAttendanceDecision,
                raw,
                actor_id=self.admin.pk,
                actor_role_snapshot="director",
                operation_key=uuid4(),
                fingerprint="raw-role-mismatch",
                decision_number=2,
                supersedes_id=first.pk,
            )

    def test_raw_sql_insert_rejects_false_schedule_role_and_unassigned_staff(self):
        appointment = self.appointment()
        first = schedule_decisions.resolve_manually(
            appointment,
            staff_member=self.staff,
            action="confirm",
            reason="Первое решение для raw schedule guards.",
            actor=self.admin,
            operation_key=uuid4(),
        )
        raw = AppointmentScheduleDecision.objects.get(pk=first.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._raw_insert(
                AppointmentScheduleDecision,
                raw,
                actor_id=self.admin.pk,
                actor_role_snapshot="director",
                operation_key=uuid4(),
                fingerprint="raw-role-mismatch",
                decision_number=2,
                supersedes_id=first.pk,
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._raw_insert(
                AppointmentScheduleDecision,
                raw,
                staff_member_id=StaffMember.objects.create(full_name="PG не назначен").pk,
                actor_id=self.admin.pk,
                actor_role_snapshot="administrator",
                operation_key=uuid4(),
                fingerprint="raw-assignment-mismatch",
                decision_number=1,
                supersedes_id=None,
            )

    def test_raw_sql_cannot_update_or_delete_decision_history(self):
        appointment = self.appointment()
        schedule_decisions.resolve_manually(
            appointment,
            staff_member=self.staff,
            action="confirm",
            reason="История расписания для PG guard.",
            actor=self.admin,
            operation_key=uuid4(),
        )
        schedule_id = appointment.schedule_decisions.get().pk
        appointment_svc.record_attendance(
            appointment,
            action="completed",
            actor=self.admin,
            reason="История проведения для PG guard.",
            operation_key=uuid4(),
        )
        attendance_id = appointment.attendance_decisions.get().pk

        for table, row_id in (
            (AppointmentAttendanceDecision._meta.db_table, attendance_id),
            (AppointmentScheduleDecision._meta.db_table, schedule_id),
        ):
            with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {table} SET reason = %s WHERE id = %s",
                    ["raw update", row_id],
                )
            with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {table} WHERE id = %s", [row_id])

    def test_raw_sql_cannot_bypass_appointment_or_participant_projection_guards(self):
        appointment = self.appointment()
        participant = appointment.participants.get(child=self.child)
        appointment_svc.record_attendance(
            appointment,
            action="completed",
            actor=self.admin,
            reason="Проверка projection guard.",
            operation_key=uuid4(),
        )

        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {Appointment._meta.db_table} SET status = %s WHERE id = %s",
                [Appointment.Status.NO_SHOW, appointment.pk],
            )
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {AppointmentParticipant._meta.db_table} SET attendance_status = %s WHERE id = %s",
                [Appointment.AttendanceStatus.MISSED, participant.pk],
            )

    def test_empty_0065_reverse_and_forward_preserve_legacy_appointment(self):
        appointment = self.appointment()
        target_before = [("operations", "0064_series_withdraw_results_expand")]
        target_after = [("operations", "0065_appointment_operator_decisions")]
        executor = MigrationExecutor(connection)

        executor.migrate(target_before)
        historical_appointment = executor.loader.project_state(target_before).apps.get_model(
            "operations", "Appointment"
        )
        preserved = historical_appointment.objects.get(pk=appointment.pk)
        self.assertEqual(preserved.status, Appointment.Status.CONFIRMED)

        executor = MigrationExecutor(connection)
        executor.migrate(target_after)
        live = Appointment.objects.get(pk=appointment.pk)
        self.assertEqual(live.status, Appointment.Status.CONFIRMED)
        self.assertFalse(AppointmentAttendanceDecision.objects.filter(appointment=live).exists())

    def test_nonempty_0065_reverse_is_rejected_and_history_projection_remain(self):
        appointment = self.appointment()
        appointment_svc.record_attendance(
            appointment,
            action="completed",
            actor=self.admin,
            reason="История должна заблокировать reverse миграции.",
            operation_key=uuid4(),
        )
        decision_id = appointment.attendance_decisions.get().pk
        target_before = [("operations", "0064_series_withdraw_results_expand")]
        executor = MigrationExecutor(connection)

        with self.assertRaisesMessage(
            RuntimeError,
            "Cannot reverse 0065 after operator decisions exist",
        ):
            executor.migrate(target_before)

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.COMPLETED)
        self.assertTrue(
            AppointmentAttendanceDecision.objects.filter(pk=decision_id).exists()
        )
