from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from queue import Queue
from threading import Event, Thread
from time import monotonic
from unittest import skipUnless
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentParticipant,
    BalanceAccount,
    Child,
    FundingSource,
    LedgerEntry,
    ProgramBlock,
    ProgramBlockLifecycleEvent,
    Service,
    StaffMember,
    TreatmentProgram,
)
from operations.services import (
    appointments as appointment_svc,
    program_block_lifecycle,
    program_lifecycle,
)
from operations.services.series_revisions import canonical_fingerprint

User = get_user_model()


def _local(day, clock):
    return timezone.make_aware(
        datetime.combine(day, clock), timezone.get_current_timezone()
    )


class ProgramBlockLifecycleFixture:
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            "block-lifecycle-admin", password="x", is_staff=True
        )
        self.director = User.objects.create_superuser(
            "block-lifecycle-director", password="x"
        )
        self.specialist = User.objects.create_user(
            "block-lifecycle-specialist", password="x"
        )
        self.child = Child.objects.create(last_name="Каскад", first_name="Получатель")
        self.staff = StaffMember.objects.create(full_name="Специалист каскада")
        self.service = Service.objects.create(
            name="Услуга каскада",
            code="PROGRAM-BLOCK-LIFECYCLE",
            default_duration_minutes=45,
            default_price=Decimal("1000"),
        )
        funding = FundingSource.objects.create(
            name="Источник каскада", source_type=FundingSource.SourceType.PERSONAL
        )
        self.account = BalanceAccount.objects.create(
            child=self.child,
            funding_source=funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("20"),
        )
        self._appointment_hour = 9

    def _block(self, *, planned=2, status=ProgramBlock.Status.PLANNED):
        program = TreatmentProgram.objects.create(
            child=self.child,
            title="Программа каскада",
            status=TreatmentProgram.Status.ACTIVE,
        )
        return ProgramBlock.objects.create(
            program=program,
            number=1,
            title="Каскад для решения",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=planned,
            balance_account=self.account,
            status=status,
        )

    def _appointment(
        self,
        block: ProgramBlock | None,
        *,
        status=Appointment.Status.CONFIRMED,
        attendance=Appointment.AttendanceStatus.UNKNOWN,
    ):
        starts_at = _local(
            timezone.localdate() + timedelta(days=10), time(self._appointment_hour, 0)
        )
        self._appointment_hour += 1
        return Appointment.objects.create(
            child=self.child,
            staff_member=self.staff,
            service=self.service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
            status=status,
            attendance_status=attendance,
            program_block=block,
            billing_account=self.account,
        )

    def _review(self, block):
        return program_block_lifecycle.get_program_block_lifecycle_review(block)

    def _complete(self, block, *, actor, reason="Завершение каскада по рассмотренным фактам.", key=None, review=None):
        review = review or self._review(block)
        return program_block_lifecycle.complete_block(
            block,
            actor=actor,
            reason=reason,
            operation_key=key or uuid4(),
            expected_review_fingerprint=review.fingerprint,
            expected_event_id=0,
        )

    def _cancel(self, block, *, actor, reason="Отмена каскада по рассмотренным фактам.", key=None, review=None):
        review = review or self._review(block)
        return program_block_lifecycle.cancel_block(
            block,
            actor=actor,
            reason=reason,
            operation_key=key or uuid4(),
            expected_review_fingerprint=review.fingerprint,
            expected_event_id=0,
        )


class ProgramBlockLifecycleTests(ProgramBlockLifecycleFixture, TestCase):
    def test_admin_completes_only_after_participant_first_plan_is_met(self):
        block = self._block(planned=1)
        appointment = self._appointment(
            block,
            status=Appointment.Status.COMPLETED,
            attendance=Appointment.AttendanceStatus.ATTENDED,
        )

        review = self._review(block)

        self.assertEqual(review.planned, 1)
        self.assertEqual(review.completed, 1)
        self.assertEqual(review.remaining, 0)
        self.assertEqual(review.allocated, 1)
        self.assertEqual(review.missed, 0)
        self.assertEqual(review.charged, 0)
        self.assertTrue(review.can_complete_normally)
        self.assertEqual(review.snapshot["facts"][0]["kind"], "participant")

        result = self._complete(block, actor=self.admin, review=review)

        self.assertFalse(result.reused_event)
        self.assertEqual(result.block.status, ProgramBlock.Status.COMPLETED)
        self.assertEqual(result.event.status_from, ProgramBlock.Status.PLANNED)
        self.assertEqual(result.event.context_snapshot, review.snapshot)
        self.assertEqual(appointment.participants.count(), 1)

    def test_authority_threshold_and_director_early_completion(self):
        block = self._block(planned=2)
        review = self._review(block)

        with self.assertRaises(PermissionDenied):
            self._complete(block, actor=self.specialist, review=review)
        with self.assertRaises(PermissionDenied):
            self._complete(block, actor=self.admin, review=review)

        result = self._complete(block, actor=self.director, review=review)

        self.assertEqual(result.block.status, ProgramBlock.Status.COMPLETED)
        self.assertEqual(
            result.event.actor_role_snapshot,
            ProgramBlockLifecycleEvent.ActorRole.DIRECTOR,
        )

    def test_cancel_is_available_to_both_operator_roles_and_preserves_facts(self):
        block = self._block(planned=3)
        appointment = self._appointment(block)
        participant = appointment.participants.get()
        ledger = LedgerEntry.objects.create(
            account=self.account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1000"),
            appointment=appointment,
            appointment_participant=participant,
            price_snapshot=Decimal("1000"),
            reason="Существующий финансовый факт.",
        )
        before = {
            "appointment": (appointment.status, appointment.attendance_status),
            "participant": (participant.appointment_status, participant.attendance_status),
            "ledger": (ledger.pk, ledger.amount, ledger.appointment_id, ledger.appointment_participant_id),
        }

        result = self._cancel(block, actor=self.admin)

        appointment.refresh_from_db()
        participant.refresh_from_db()
        ledger.refresh_from_db()
        self.assertEqual(result.block.status, ProgramBlock.Status.CANCELLED)
        self.assertEqual((appointment.status, appointment.attendance_status), before["appointment"])
        self.assertEqual((participant.appointment_status, participant.attendance_status), before["participant"])
        self.assertEqual(
            (ledger.pk, ledger.amount, ledger.appointment_id, ledger.appointment_participant_id),
            before["ledger"],
        )

        director_block = self._block()
        director_result = self._cancel(director_block, actor=self.director)
        self.assertEqual(director_result.block.status, ProgramBlock.Status.CANCELLED)

    def test_replay_precedes_terminal_state_and_rejects_payload_mismatch(self):
        block = self._block()
        review = self._review(block)
        key = uuid4()
        first = self._cancel(block, actor=self.admin, key=key, review=review)
        replay = self._cancel(block, actor=self.admin, key=key, review=review)

        self.assertFalse(first.reused_event)
        self.assertTrue(replay.reused_event)
        self.assertEqual(ProgramBlockLifecycleEvent.objects.count(), 1)
        with self.assertRaises(program_block_lifecycle.ProgramBlockLifecycleMismatch):
            self._cancel(
                block,
                actor=self.admin,
                key=key,
                review=review,
                reason="Другой смысл того же ключа операции.",
            )
        with self.assertRaises(program_block_lifecycle.ProgramBlockLifecycleMismatch):
            self._cancel(block, actor=self.admin, review=review)

    def test_factual_change_stales_the_review_snapshot(self):
        block = self._block()
        review = self._review(block)
        self._appointment(block)

        with self.assertRaises(program_block_lifecycle.ProgramBlockLifecycleMismatch):
            self._cancel(block, actor=self.admin, review=review)

        self.assertFalse(block.lifecycle_events.exists())

    def test_event_history_and_terminal_status_are_immutable(self):
        block = self._block(planned=1)
        self._appointment(
            block,
            status=Appointment.Status.COMPLETED,
            attendance=Appointment.AttendanceStatus.ATTENDED,
        )
        event = self._complete(block, actor=self.admin).event

        event.reason = "Переписать принятую историю нельзя."
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()
        block.status = ProgramBlock.Status.PLANNED
        with self.assertRaises(ValidationError):
            block.save(update_fields=["status", "updated_at"])

    def test_block_can_close_after_parent_terminal_decision_without_reopening_parent(self):
        block = self._block()
        program_review = program_lifecycle.get_program_lifecycle_review(block.program)
        program_lifecycle.cancel_program(
            block.program,
            actor=self.admin,
            reason="Программа закрыта независимо от каскада.",
            operation_key=uuid4(),
            expected_review_fingerprint=program_review.fingerprint,
            expected_event_id=0,
        )

        result = self._cancel(block, actor=self.admin)

        block.program.refresh_from_db()
        self.assertEqual(result.block.status, ProgramBlock.Status.CANCELLED)
        self.assertEqual(block.program.status, TreatmentProgram.Status.CANCELLED)


@skipUnless(connection.vendor == "postgresql", "PostgreSQL guards проверяются на PostgreSQL.")
class ProgramBlockLifecyclePostgreSQLTests(ProgramBlockLifecycleFixture, TransactionTestCase):
    reset_sequences = True

    def test_raw_status_and_forged_underplan_event_are_rejected(self):
        block = self._block(planned=2)
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operations_programblock SET status = %s WHERE id = %s",
                [ProgramBlock.Status.CANCELLED, block.pk],
            )

        review = self._review(block)
        probe = ProgramBlockLifecycleEvent(
            block=block,
            operation_key=uuid4(),
            event_type=ProgramBlockLifecycleEvent.EventType.COMPLETED,
            status_from=ProgramBlock.Status.PLANNED,
            status_to=ProgramBlock.Status.COMPLETED,
            actor=self.admin,
            actor_role_snapshot=ProgramBlockLifecycleEvent.ActorRole.ADMINISTRATOR,
            reason="Администратор пытается завершить недовыполненный каскад.",
            context_snapshot=review.snapshot,
            fingerprint="",
        )
        probe.fingerprint = canonical_fingerprint(probe.fingerprint_payload())
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO operations_programblocklifecycleevent (
                    created_at, updated_at, operation_key, fingerprint, event_type,
                    status_from, status_to, actor_role_snapshot, reason,
                    context_snapshot, occurred_at, actor_id, block_id
                ) VALUES (
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s, %s, %s, %s, %s,
                    %s, %s, %s::jsonb, CURRENT_TIMESTAMP, %s, %s
                )
                """,
                [
                    str(probe.operation_key), probe.fingerprint, probe.event_type,
                    probe.status_from, probe.status_to, probe.actor_role_snapshot,
                    probe.reason, json.dumps(probe.context_snapshot, ensure_ascii=False),
                    self.admin.pk, block.pk,
                ],
            )
        self.assertFalse(block.lifecycle_events.exists())

    def test_terminal_allocation_guard_allows_only_late_facts_and_legacy_backfill(self):
        legacy_block = self._block()
        legacy = self._appointment(legacy_block)
        legacy.participants.all().delete()
        self._cancel(legacy_block, actor=self.admin)

        appointment_svc.record_attendance(
            legacy,
            action="completed",
            actor=self.admin,
            reason="Поздняя фактическая отметка legacy-занятия.",
            operation_key=uuid4(),
        )
        self.assertEqual(legacy.participants.count(), 1)

        existing_block = self._block()
        existing = self._appointment(existing_block)
        self._cancel(existing_block, actor=self.admin)
        appointment_svc.record_attendance(
            existing,
            action="completed",
            actor=self.admin,
            reason="Поздняя фактическая отметка существующего участия.",
            operation_key=uuid4(),
        )
        existing.refresh_from_db()
        self.assertEqual(existing.status, Appointment.Status.COMPLETED)

        unallocated = self._appointment(None)
        participant = unallocated.participants.get()
        with self.assertRaises(DatabaseError), transaction.atomic():
            Appointment.objects.filter(pk=unallocated.pk).update(program_block=existing_block)
        with self.assertRaises(DatabaseError), transaction.atomic():
            AppointmentParticipant.objects.filter(pk=participant.pk).update(
                program_block=existing_block
            )

    def test_terminal_participant_cannot_be_reparented_without_changing_its_block(self):
        block = self._block()
        source = self._appointment(block)
        participant = source.participants.get()
        target = self._appointment(None)
        target.participants.all().delete()
        self._cancel(block, actor=self.admin)

        participant.appointment = target
        with self.assertRaises(ValidationError):
            participant.save(update_fields=["appointment", "updated_at"])
        participant.refresh_from_db()
        self.assertEqual(participant.appointment_id, source.pk)

        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operations_appointmentparticipant SET appointment_id = %s WHERE id = %s",
                [target.pk, participant.pk],
            )
        participant.refresh_from_db()
        self.assertEqual(participant.appointment_id, source.pk)

    def test_close_serializes_a_direct_new_allocation(self):
        block = self._block()
        unallocated = self._appointment(None)
        close_written = Event()
        release_close = Event()
        allocation_started = Event()
        outcomes = Queue()
        application_name = f"block-lifecycle-allocation-{uuid4().hex}"

        def close_worker():
            close_old_connections()
            try:
                with transaction.atomic():
                    current = ProgramBlock.objects.get(pk=block.pk)
                    self._cancel(current, actor=User.objects.get(pk=self.admin.pk))
                    close_written.set()
                    if not release_close.wait(timeout=10):
                        raise TimeoutError("Allocation writer did not reach the database guard.")
                outcomes.put(("closed", None))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        def allocation_worker():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET application_name = %s", [application_name])
                allocation_started.set()
                with self.assertRaises(DatabaseError), transaction.atomic():
                    Appointment.objects.filter(pk=unallocated.pk).update(program_block_id=block.pk)
                outcomes.put(("allocation_rejected", None))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        closer = Thread(target=close_worker)
        writer = Thread(target=allocation_worker)
        closer.start()
        self.assertTrue(close_written.wait(timeout=10))
        writer.start()
        self.assertTrue(allocation_started.wait(timeout=10))
        deadline = monotonic() + 10
        blocked = False
        while monotonic() < deadline and not blocked:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT cardinality(pg_blocking_pids(pid)) > 0
                    FROM pg_stat_activity
                    WHERE application_name = %s
                    """,
                    [application_name],
                )
                row = cursor.fetchone()
            blocked = bool(row and row[0])
        self.assertTrue(blocked, "Allocation writer never waited on the block lock.")
        release_close.set()
        closer.join(timeout=15)
        writer.join(timeout=15)
        self.assertFalse(closer.is_alive())
        self.assertFalse(writer.is_alive())
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertFalse(any(isinstance(item, BaseException) for item in results), results)
        self.assertEqual({item[0] for item in results}, {"closed", "allocation_rejected"})
