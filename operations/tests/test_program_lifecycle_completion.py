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
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentSeries,
    AppointmentStaffAssignment,
    BalanceAccount,
    Child,
    FundingSource,
    LedgerEntry,
    ProgramBlock,
    Room,
    Service,
    StaffMember,
    TreatmentProgram,
    TreatmentProgramLifecycleEvent,
)
from operations.services import program_block_lifecycle, program_lifecycle, program_scheduling
from operations.services.series_revisions import canonical_fingerprint

User = get_user_model()
_DEFAULT_ACCOUNT = object()


def _local(day, clock):
    return timezone.make_aware(
        datetime.combine(day, clock), timezone.get_current_timezone()
    )


class ProgramCompletionLifecycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.administrator = User.objects.create_user(
            "program-completion-administrator", password="x", is_staff=True
        )
        cls.director = User.objects.create_superuser(
            "program-completion-director", password="x"
        )
        cls.specialist = User.objects.create_user(
            "program-completion-specialist", password="x"
        )
        cls.child = Child.objects.create(last_name="Жизненный цикл", first_name="Финал")
        cls.other_child = Child.objects.create(
            last_name="Жизненный цикл", first_name="Другой"
        )
        cls.staff = StaffMember.objects.create(full_name="Специалист финала программы")
        cls.service = Service.objects.create(
            name="Услуга финала программы",
            code="PROGRAM-COMPLETION",
            default_duration_minutes=45,
            default_price=Decimal("1500"),
        )
        cls.other_service = Service.objects.create(
            name="Другая услуга финала программы",
            code="PROGRAM-COMPLETION-OTHER",
            default_duration_minutes=30,
            default_price=Decimal("900"),
        )
        cls.room = Room.objects.create(name="Кабинет финала программы")
        cls.funding = FundingSource.objects.create(
            name="Источник финала программы",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        cls.account = BalanceAccount.objects.create(
            child=cls.child,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("12"),
        )
        cls.other_child_account = BalanceAccount.objects.create(
            child=cls.other_child,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("12"),
        )
        cls.other_service_account = BalanceAccount.objects.create(
            child=cls.child,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.other_service,
            initial_amount=Decimal("12"),
        )

    def _program(self, *, status=TreatmentProgram.Status.DRAFT, suffix=None):
        return TreatmentProgram.objects.create(
            child=self.child,
            title=f"Программа {suffix or uuid4().hex[:8]}",
            status=status,
        )

    def _block(
        self,
        program,
        *,
        number=1,
        title=None,
        planned_sessions=4,
        service=None,
        account=_DEFAULT_ACCOUNT,
        status=ProgramBlock.Status.PLANNED,
    ):
        return ProgramBlock.objects.create(
            program=program,
            number=number,
            title=title or f"Каскад {number}",
            service=service or self.service,
            staff_member=self.staff,
            planned_sessions=planned_sessions,
            balance_account=self.account if account is _DEFAULT_ACCOUNT else account,
            status=status,
        )

    def _review(self, program):
        return program_lifecycle.get_program_lifecycle_review(program)

    def _activate(self, program, *, actor=None, key=None, expected=0, review=None):
        review = review or self._review(program)
        return program_lifecycle.activate_program(
            program,
            actor=actor or self.administrator,
            reason="Активировать проверенную программу.",
            operation_key=key or uuid4(),
            expected_event_id=expected,
            expected_review_fingerprint=review.fingerprint,
        )

    def _complete(self, program, *, actor=None, key=None, expected=0, review=None):
        review = review or self._review(program)
        return program_lifecycle.complete_program(
            program,
            actor=actor or self.director,
            reason="Завершить программу с сохранением истории.",
            operation_key=key or uuid4(),
            expected_event_id=expected,
            expected_review_fingerprint=review.fingerprint,
        )

    def _cancel(self, program, *, actor=None, key=None, expected=0, review=None):
        review = review or self._review(program)
        return program_lifecycle.cancel_program(
            program,
            actor=actor or self.administrator,
            reason="Отменить программу с сохранением фактов.",
            operation_key=key or uuid4(),
            expected_event_id=expected,
            expected_review_fingerprint=review.fingerprint,
        )

    def test_activation_requires_at_least_one_structurally_valid_nonterminal_block(self):
        program = self._program(suffix="структурная проверка")
        review = self._review(program)
        self.assertFalse(review.can_activate)
        self.assertTrue(review.activation_error)

        self._block(program, number=1, planned_sessions=0)
        self._block(program, number=2, account=None)
        self._block(program, number=3, account=self.other_child_account)
        self._block(program, number=4, account=self.other_service_account)
        self._block(
            program,
            number=5,
            status=ProgramBlock.Status.COMPLETED,
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            self._block(program, number=6, status="unknown-legacy-status")
        review = self._review(program)
        self.assertFalse(review.can_activate)
        self.assertEqual(len(review.blocks), 5)
        with self.assertRaises(ValidationError):
            self._activate(program, review=review)
        program.refresh_from_db()
        self.assertEqual(program.status, TreatmentProgram.Status.DRAFT)
        self.assertFalse(program.lifecycle_events.exists())

        valid = self._block(program, number=7, title="Валидный каскад")
        review = self._review(program)
        self.assertTrue(review.can_activate)
        self.assertEqual(review.activation_error, "")
        self.assertEqual(review.blocks[-1]["id"], valid.pk)
        self.assertEqual(review.blocks[-1]["service_id"], self.service.pk)
        self.assertEqual(review.blocks[-1]["balance_account_id"], self.account.pk)

    def test_activation_appends_a_review_snapshot_and_role_snapshot(self):
        program = self._program(suffix="активация")
        block = self._block(program)
        review = self._review(program)

        result = self._activate(program, review=review)

        self.assertEqual(result.program.status, TreatmentProgram.Status.ACTIVE)
        self.assertEqual(result.event.event_type, TreatmentProgramLifecycleEvent.EventType.ACTIVATED)
        self.assertEqual(result.event.status_from, TreatmentProgram.Status.DRAFT)
        self.assertEqual(result.event.status_to, TreatmentProgram.Status.ACTIVE)
        self.assertEqual(
            result.event.actor_role_snapshot,
            TreatmentProgramLifecycleEvent.ActorRole.ADMINISTRATOR,
        )
        self.assertEqual(result.event.event_number, 1)
        self.assertIsNone(result.event.supersedes_id)
        self.assertEqual(result.event.context_snapshot, review.snapshot)
        self.assertEqual(
            canonical_fingerprint(result.event.context_snapshot), review.fingerprint
        )

        block.title = "Название изменено после решения"
        block.save(update_fields=["title", "updated_at"])
        result.event.refresh_from_db()
        self.assertEqual(result.event.context_snapshot["blocks"], review.blocks)

    def test_activation_rejects_an_inverted_program_period(self):
        program = TreatmentProgram.objects.create(
            child=self.child,
            title="Программа с неверным периодом",
            status=TreatmentProgram.Status.DRAFT,
            starts_on=timezone.localdate() + timedelta(days=2),
            ends_on=timezone.localdate() + timedelta(days=1),
        )
        self._block(program)
        review = self._review(program)
        self.assertFalse(review.can_activate)
        self.assertTrue(review.activation_error)
        with self.assertRaises(ValidationError):
            self._activate(program, review=review)
        program.refresh_from_db()
        self.assertEqual(program.status, TreatmentProgram.Status.DRAFT)
        self.assertFalse(program.lifecycle_events.exists())

    def test_completion_roles_depend_on_unfinished_blocks_and_accept_paused_state(self):
        complete_program = self._program(
            status=TreatmentProgram.Status.ACTIVE, suffix="выполненная"
        )
        complete_block = self._block(
            complete_program, status=ProgramBlock.Status.COMPLETED
        )
        review = self._review(complete_program)
        self.assertEqual(review.unfinished_blocks, [])
        completed = self._complete(
            complete_program, actor=self.administrator, review=review
        )
        self.assertEqual(completed.program.status, TreatmentProgram.Status.COMPLETED)
        self.assertEqual(
            completed.event.actor_role_snapshot,
            TreatmentProgramLifecycleEvent.ActorRole.ADMINISTRATOR,
        )
        self.assertEqual(completed.event.context_snapshot["blocks"][0]["id"], complete_block.pk)

        early_program = self._program(
            status=TreatmentProgram.Status.PAUSED, suffix="досрочная"
        )
        unfinished = self._block(early_program)
        early_review = self._review(early_program)
        self.assertEqual([row["id"] for row in early_review.unfinished_blocks], [unfinished.pk])
        with self.assertRaises(PermissionDenied):
            self._complete(
                early_program, actor=self.administrator, review=early_review
            )
        self.assertFalse(early_program.lifecycle_events.exists())

        completed_early = self._complete(
            early_program, actor=self.director, review=early_review
        )
        self.assertEqual(completed_early.program.status, TreatmentProgram.Status.COMPLETED)
        self.assertEqual(
            completed_early.event.status_from, TreatmentProgram.Status.PAUSED
        )

    def test_cancel_accepts_draft_active_and_paused_but_terminal_states_are_final(self):
        for status in (
            TreatmentProgram.Status.DRAFT,
            TreatmentProgram.Status.ACTIVE,
            TreatmentProgram.Status.PAUSED,
        ):
            with self.subTest(status=status):
                program = self._program(status=status, suffix=f"отмена {status}")
                cancelled = self._cancel(program)
                self.assertEqual(cancelled.program.status, TreatmentProgram.Status.CANCELLED)
                self.assertEqual(cancelled.event.status_from, status)
                self.assertEqual(
                    cancelled.event.event_type,
                    TreatmentProgramLifecycleEvent.EventType.CANCELLED,
                )
                with self.assertRaises(program_lifecycle.ProgramLifecycleMismatch):
                    self._cancel(
                        cancelled.program,
                        actor=self.director,
                        expected=cancelled.event.pk,
                    )

        completed = self._program(
            status=TreatmentProgram.Status.COMPLETED, suffix="уже завершенная"
        )
        with self.assertRaises(program_lifecycle.ProgramLifecycleMismatch):
            self._cancel(completed, actor=self.director)

    def test_specialist_is_denied_and_director_decision_has_priority(self):
        denied = self._program(suffix="специалист")
        self._block(denied)
        review = self._review(denied)
        with self.assertRaises(PermissionDenied):
            self._activate(denied, actor=self.specialist, review=review)

        program = self._program(suffix="приоритет руководителя")
        self._block(program)
        activated = self._activate(program, actor=self.director)
        with self.assertRaises(PermissionDenied):
            self._cancel(
                activated.program,
                actor=self.administrator,
                expected=activated.event.pk,
            )
        activated.program.refresh_from_db()
        self.assertEqual(activated.program.status, TreatmentProgram.Status.ACTIVE)
        self.assertEqual(activated.program.lifecycle_events.count(), 1)

    def test_replay_uses_stored_review_before_later_state_and_rejects_changed_payload(self):
        program = self._program(suffix="повтор")
        self._block(program)
        activation_review = self._review(program)
        activation_key = uuid4()
        activated = self._activate(
            program,
            actor=self.administrator,
            key=activation_key,
            review=activation_review,
        )
        paused = program_lifecycle.pause_program(
            activated.program,
            actor=self.director,
            reason="Руководитель остановил программу после активации.",
            operation_key=uuid4(),
            expected_event_id=activated.event.pk,
        )

        replay = self._activate(
            paused.program,
            actor=self.administrator,
            key=activation_key,
            expected=0,
            review=activation_review,
        )
        self.assertTrue(replay.reused_event)
        self.assertEqual(replay.event.pk, activated.event.pk)
        self.assertEqual(replay.program.status, TreatmentProgram.Status.PAUSED)
        self.assertEqual(program.lifecycle_events.count(), 2)

        changed_review = self._review(paused.program)
        with self.assertRaises(program_lifecycle.ProgramLifecycleMismatch):
            self._activate(
                paused.program,
                actor=self.administrator,
                key=activation_key,
                expected=0,
                review=changed_review,
            )
        self.assertEqual(program.lifecycle_events.count(), 2)

    def test_stale_event_and_review_fingerprints_are_atomic(self):
        stale_event = self._program(suffix="устаревшее событие")
        self._block(stale_event)
        review = self._review(stale_event)
        with self.assertRaises(program_lifecycle.ProgramLifecycleMismatch):
            self._activate(stale_event, expected=999999, review=review)
        stale_event.refresh_from_db()
        self.assertEqual(stale_event.status, TreatmentProgram.Status.DRAFT)
        self.assertFalse(stale_event.lifecycle_events.exists())

        stale_review = self._program(suffix="устаревший обзор")
        block = self._block(stale_review)
        review = self._review(stale_review)
        block.title = "Каскад изменился после открытия формы"
        block.save(update_fields=["title", "updated_at"])
        with self.assertRaises(program_lifecycle.ProgramLifecycleMismatch):
            self._activate(stale_review, review=review)
        stale_review.refresh_from_db()
        self.assertEqual(stale_review.status, TreatmentProgram.Status.DRAFT)
        self.assertFalse(stale_review.lifecycle_events.exists())

    def test_cancel_preserves_schedule_series_and_financial_facts(self):
        program = self._program(status=TreatmentProgram.Status.ACTIVE, suffix="факты")
        block = self._block(program)
        starts_at = _local(timezone.localdate() + timedelta(days=7), time(10, 0))
        appointment = Appointment.objects.create(
            child=self.child,
            staff_member=self.staff,
            service=self.service,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
            status=Appointment.Status.PROPOSED,
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=self.account,
            program_block=block,
        )
        participant = appointment.participants.get(child=self.child)
        participant.price_snapshot = Decimal("1500")
        participant.save(update_fields=["price_snapshot", "updated_at"])
        LedgerEntry.objects.create(
            account=self.account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
            appointment=appointment,
            appointment_participant=participant,
            price_snapshot=Decimal("1500"),
            created_by=self.director,
            reason="Финансовый факт до отмены программы.",
        )
        AppointmentSeries.objects.create(
            child=self.child,
            service=self.service,
            staff_member=self.staff,
            room=self.room,
            program_block=block,
            title="Серия до отмены программы",
            start_date=starts_at.date(),
            end_date=starts_at.date() + timedelta(days=14),
            days_of_week="ПН",
            time=time(10, 0),
            duration_minutes=45,
            status=AppointmentSeries.Status.ACTIVE,
        )
        before = self._program_facts(program)

        cancelled = self._cancel(program)

        self.assertEqual(cancelled.program.status, TreatmentProgram.Status.CANCELLED)
        self.assertEqual(self._program_facts(program), before)

    def test_terminal_program_rejects_new_or_reparented_blocks_but_allows_existing_updates(self):
        terminal = self._program(
            status=TreatmentProgram.Status.CANCELLED, suffix="закрытая"
        )
        with self.assertRaises(ValidationError):
            self._block(terminal)

        active = self._program(
            status=TreatmentProgram.Status.ACTIVE, suffix="для переноса"
        )
        block = self._block(active)
        block.program = terminal
        with self.assertRaises(ValidationError):
            block.save(update_fields=["program", "updated_at"])
        block.refresh_from_db()
        self.assertEqual(block.program_id, active.pk)

        completed = self._complete(active, actor=self.director)
        block.title = "Уточненный итог каскада после закрытия программы"
        block.save(update_fields=["title", "updated_at"])
        program_block_lifecycle.complete_block(
            block, actor=self.director, reason="Явное завершение каскада после программы.",
            operation_key=uuid4(), expected_event_id=0,
            expected_review_fingerprint=program_block_lifecycle.get_program_block_lifecycle_review(block).fingerprint,
        )
        block.refresh_from_db()
        self.assertEqual(block.status, ProgramBlock.Status.COMPLETED)
        self.assertEqual(completed.program.status, TreatmentProgram.Status.COMPLETED)

        old_parent = self._program(
            status=TreatmentProgram.Status.ACTIVE, suffix="старый родитель"
        )
        current_parent = self._program(
            status=TreatmentProgram.Status.ACTIVE, suffix="текущий родитель"
        )
        stale_instance = self._block(old_parent)
        reparented = ProgramBlock.objects.get(pk=stale_instance.pk)
        reparented.program = current_parent
        reparented.save(update_fields=["program", "updated_at"])
        self._cancel(old_parent, actor=self.director)

        stale_instance.notes = "Изменение через экземпляр со старым program_id."
        stale_instance.save(update_fields=["notes", "updated_at"])
        stale_instance.refresh_from_db()
        self.assertEqual(stale_instance.program_id, current_parent.pk)
        self.assertEqual(
            stale_instance.notes, "Изменение через экземпляр со старым program_id."
        )

    def _program_facts(self, program):
        appointment_ids = Appointment.objects.filter(
            participants__program_block__program=program
        ).values("pk")
        return {
            "blocks": list(ProgramBlock.objects.filter(program=program).order_by("pk").values()),
            "appointments": list(
                Appointment.objects.filter(pk__in=appointment_ids).order_by("pk").values()
            ),
            "participants": list(
                AppointmentParticipant.objects.filter(appointment_id__in=appointment_ids)
                .order_by("pk")
                .values()
            ),
            "staff": list(
                AppointmentStaffAssignment.objects.filter(appointment_id__in=appointment_ids)
                .order_by("pk")
                .values()
            ),
            "series": list(
                AppointmentSeries.objects.filter(program_block__program=program)
                .order_by("pk")
                .values()
            ),
            "ledger": list(
                LedgerEntry.objects.filter(appointment_id__in=appointment_ids)
                .order_by("pk")
                .values()
            ),
        }


@skipUnless(connection.vendor == "postgresql", "DB guards проверяются на PostgreSQL.")
class ProgramCompletionPostgreSQLTests(TransactionTestCase):
    def setUp(self):
        self.director = User.objects.create_superuser(
            f"program-completion-pg-{uuid4().hex[:8]}", password="x"
        )
        self.child = Child.objects.create(last_name="PG", first_name="Completion")
        self.program = TreatmentProgram.objects.create(
            child=self.child,
            title="PG completion program",
            status=TreatmentProgram.Status.ACTIVE,
        )

    def test_raw_sql_cannot_bypass_terminal_lifecycle_or_reopen_terminal_program(self):
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operations_treatmentprogram SET status = %s WHERE id = %s",
                [TreatmentProgram.Status.CANCELLED, self.program.pk],
            )
        self.program.refresh_from_db()
        self.assertEqual(self.program.status, TreatmentProgram.Status.ACTIVE)

        review = program_lifecycle.get_program_lifecycle_review(self.program)
        cancelled = program_lifecycle.cancel_program(
            self.program,
            actor=self.director,
            reason="Корректная отмена перед raw SQL обходом.",
            operation_key=uuid4(),
            expected_event_id=0,
            expected_review_fingerprint=review.fingerprint,
        )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operations_treatmentprogram SET status = %s WHERE id = %s",
                [TreatmentProgram.Status.ACTIVE, self.program.pk],
            )
        cancelled.program.refresh_from_db()
        self.assertEqual(cancelled.program.status, TreatmentProgram.Status.CANCELLED)

    def test_raw_sql_rejects_invalid_terminal_transition_even_with_valid_fingerprint(self):
        probe = TreatmentProgramLifecycleEvent(
            program=self.program,
            event_type=TreatmentProgramLifecycleEvent.EventType.COMPLETED,
            event_number=1,
            status_from=TreatmentProgram.Status.ACTIVE,
            status_to=TreatmentProgram.Status.COMPLETED,
            actor=self.director,
            actor_role_snapshot=TreatmentProgramLifecycleEvent.ActorRole.ADMINISTRATOR,
            reason="Raw SQL подменяет роль руководителя.",
            operation_key=uuid4(),
            context_snapshot={},
            fingerprint="",
        )
        probe.fingerprint = canonical_fingerprint(probe.fingerprint_payload())
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO operations_treatmentprogramlifecycleevent (
                    created_at, updated_at, operation_key, fingerprint, event_type,
                    event_number, status_from, status_to, actor_role_snapshot,
                    reason, occurred_at, actor_id, program_id, supersedes_id,
                    context_snapshot
                ) VALUES (
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s, %s, %s, 1, %s, %s,
                    %s, %s, CURRENT_TIMESTAMP, %s, %s, NULL, %s
                )
                """,
                [
                    str(probe.operation_key),
                    probe.fingerprint,
                    probe.event_type,
                    probe.status_from,
                    probe.status_to,
                    probe.actor_role_snapshot,
                    probe.reason,
                    self.director.pk,
                    self.program.pk,
                    "{}",
                ],
            )
        self.assertFalse(self.program.lifecycle_events.exists())

    def test_raw_sql_rejects_a_stale_review_snapshot(self):
        service = Service.objects.create(
            name="PG review service",
            code=f"PG-REVIEW-{uuid4().hex[:8]}",
            default_duration_minutes=30,
        )
        funding = FundingSource.objects.create(
            name=f"PG review funding {uuid4().hex[:8]}",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        account = BalanceAccount.objects.create(
            child=self.child,
            funding_source=funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=service,
            initial_amount=Decimal("3"),
        )
        block = ProgramBlock.objects.create(
            program=self.program,
            number=1,
            title="PG review block",
            service=service,
            planned_sessions=3,
            balance_account=account,
        )
        stale_snapshot = program_lifecycle.get_program_lifecycle_review(
            self.program
        ).snapshot
        block.title = "PG review block changed"
        block.save(update_fields=["title", "updated_at"])
        probe = TreatmentProgramLifecycleEvent(
            program=self.program,
            event_type=TreatmentProgramLifecycleEvent.EventType.CANCELLED,
            event_number=1,
            status_from=TreatmentProgram.Status.ACTIVE,
            status_to=TreatmentProgram.Status.CANCELLED,
            actor=self.director,
            actor_role_snapshot=TreatmentProgramLifecycleEvent.ActorRole.DIRECTOR,
            reason="Raw SQL использует устаревший снимок каскада.",
            operation_key=uuid4(),
            context_snapshot=stale_snapshot,
            fingerprint="",
        )
        probe.fingerprint = canonical_fingerprint(probe.fingerprint_payload())

        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO operations_treatmentprogramlifecycleevent (
                    created_at, updated_at, operation_key, fingerprint, event_type,
                    event_number, status_from, status_to, actor_role_snapshot,
                    reason, occurred_at, actor_id, program_id, supersedes_id,
                    context_snapshot
                ) VALUES (
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s, %s, %s, 1, %s, %s,
                    %s, %s, CURRENT_TIMESTAMP, %s, %s, NULL, %s
                )
                """,
                [
                    str(probe.operation_key),
                    probe.fingerprint,
                    probe.event_type,
                    probe.status_from,
                    probe.status_to,
                    probe.actor_role_snapshot,
                    probe.reason,
                    self.director.pk,
                    self.program.pk,
                    json.dumps(stale_snapshot),
                ],
            )
        self.program.refresh_from_db()
        self.assertEqual(self.program.status, TreatmentProgram.Status.ACTIVE)
        self.assertFalse(self.program.lifecycle_events.exists())

    def test_raw_sql_rejects_a_64_hex_fingerprint_that_does_not_match_the_snapshot(self):
        snapshot = program_lifecycle.get_program_lifecycle_review(self.program).snapshot
        probe = TreatmentProgramLifecycleEvent(
            program=self.program,
            event_type=TreatmentProgramLifecycleEvent.EventType.CANCELLED,
            event_number=1,
            status_from=TreatmentProgram.Status.ACTIVE,
            status_to=TreatmentProgram.Status.CANCELLED,
            actor=self.director,
            actor_role_snapshot=TreatmentProgramLifecycleEvent.ActorRole.DIRECTOR,
            reason="Raw SQL использует ложный шестнадцатеричный отпечаток.",
            operation_key=uuid4(),
            context_snapshot=snapshot,
            fingerprint="c" * 64,
        )
        self.assertNotEqual(
            probe.fingerprint, canonical_fingerprint(probe.fingerprint_payload())
        )

        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO operations_treatmentprogramlifecycleevent (
                    created_at, updated_at, operation_key, fingerprint, event_type,
                    event_number, status_from, status_to, actor_role_snapshot,
                    reason, occurred_at, actor_id, program_id, supersedes_id,
                    context_snapshot
                ) VALUES (
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s, %s, %s, 1, %s, %s,
                    %s, %s, CURRENT_TIMESTAMP, %s, %s, NULL, %s
                )
                """,
                [
                    str(probe.operation_key),
                    probe.fingerprint,
                    probe.event_type,
                    probe.status_from,
                    probe.status_to,
                    probe.actor_role_snapshot,
                    probe.reason,
                    self.director.pk,
                    self.program.pk,
                    json.dumps(snapshot),
                ],
            )
        self.program.refresh_from_db()
        self.assertEqual(self.program.status, TreatmentProgram.Status.ACTIVE)
        self.assertFalse(self.program.lifecycle_events.exists())

    def test_raw_sql_block_guard_rejects_insert_and_reparent_but_allows_updates(self):
        service = Service.objects.create(
            name="PG terminal block service",
            code=f"PG-TERMINAL-{uuid4().hex[:8]}",
            default_duration_minutes=30,
        )
        existing = ProgramBlock.objects.create(
            program=self.program,
            number=1,
            title="Existing terminal block",
            service=service,
            planned_sessions=2,
        )
        review = program_lifecycle.get_program_lifecycle_review(self.program)
        terminal = program_lifecycle.cancel_program(
            self.program,
            actor=self.director,
            reason="Закрыть программу перед проверкой SQL каскадов.",
            operation_key=uuid4(),
            expected_event_id=0,
            expected_review_fingerprint=review.fingerprint,
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operations_programblock SET title = %s WHERE id = %s",
                ["Allowed update after closure", existing.pk],
            )
        existing.refresh_from_db()
        self.assertEqual(existing.title, "Allowed update after closure")

        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO operations_programblock (
                    created_at, updated_at, number, title, planned_sessions,
                    status, color, notes, program_id, service_id,
                    staff_member_id, balance_account_id
                ) VALUES (
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 2, %s, 1,
                    'planned', '#b71b55', '', %s, %s, NULL, NULL
                )
                """,
                ["Forbidden insert after closure", terminal.program.pk, service.pk],
            )

        other_program = TreatmentProgram.objects.create(
            child=self.child,
            title="PG reparent source",
            status=TreatmentProgram.Status.ACTIVE,
        )
        moving = ProgramBlock.objects.create(
            program=other_program,
            number=1,
            title="Moving block",
            service=service,
            planned_sessions=1,
        )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operations_programblock SET program_id = %s WHERE id = %s",
                [terminal.program.pk, moving.pk],
            )
        moving.refresh_from_db()
        self.assertEqual(moving.program_id, other_program.pk)

    def test_program_cancellation_serializes_with_a_concurrent_new_block(self):
        service = Service.objects.create(
            name="PG concurrent block service",
            code=f"PG-CONCURRENT-BLOCK-{uuid4().hex[:8]}",
            default_duration_minutes=30,
        )
        review = program_lifecycle.get_program_lifecycle_review(self.program)
        cancellation_holds_root = Event()
        release_cancellation = Event()
        outcomes = Queue()
        application_name = f"program-completion-block-race-{uuid4().hex}"

        def cancellation_worker():
            close_old_connections()
            try:
                with transaction.atomic():
                    result = program_lifecycle.cancel_program(
                        TreatmentProgram.objects.get(pk=self.program.pk),
                        actor=User.objects.get(pk=self.director.pk),
                        reason="Отмена выигрывает гонку с новым каскадом.",
                        operation_key=uuid4(),
                        expected_event_id=0,
                        expected_review_fingerprint=review.fingerprint,
                    )
                    cancellation_holds_root.set()
                    if not release_cancellation.wait(timeout=10):
                        raise TimeoutError("Block writer did not wait on the program root.")
                outcomes.put(("cancelled", result.event.pk))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        def block_worker():
            close_old_connections()
            try:
                if not cancellation_holds_root.wait(timeout=10):
                    raise TimeoutError("Cancellation did not acquire the program root.")
                with connection.cursor() as cursor:
                    cursor.execute("SET application_name = %s", [application_name])
                try:
                    ProgramBlock.objects.create(
                        program_id=self.program.pk,
                        number=1,
                        title="Конкурентный каскад",
                        service_id=service.pk,
                        planned_sessions=1,
                    )
                except ValidationError as exc:
                    outcomes.put(("block_rejected", exc))
                else:
                    outcomes.put(("block_created", None))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        cancellation_thread = Thread(target=cancellation_worker)
        block_thread = Thread(target=block_worker)
        cancellation_thread.start()
        self.assertTrue(cancellation_holds_root.wait(timeout=10))
        block_thread.start()

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
        self.assertTrue(blocked, "Block writer never waited on the program root lock.")
        release_cancellation.set()
        cancellation_thread.join(timeout=15)
        block_thread.join(timeout=15)
        self.assertFalse(cancellation_thread.is_alive())
        self.assertFalse(block_thread.is_alive())
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertFalse(any(isinstance(item, BaseException) for item in results), results)
        self.assertEqual({item[0] for item in results}, {"cancelled", "block_rejected"})
        self.program.refresh_from_db()
        self.assertEqual(self.program.status, TreatmentProgram.Status.CANCELLED)
        self.assertFalse(self.program.blocks.exists())

    def test_admin_inline_post_waits_for_block_before_root_and_avoids_deadlock(self):
        service = Service.objects.create(
            name="PG admin inline service",
            code=f"PG-ADMIN-INLINE-{uuid4().hex[:8]}",
            default_duration_minutes=30,
        )
        block = ProgramBlock.objects.create(
            program=self.program,
            number=1,
            title="Каскад до admin POST",
            service=service,
            planned_sessions=2,
        )
        block_is_locked = Event()
        let_writer_take_root = Event()
        outcomes = Queue()
        admin_application_name = f"program-admin-inline-race-{uuid4().hex}"

        def scheduling_order_writer():
            close_old_connections()
            try:
                with transaction.atomic():
                    locked = ProgramBlock.objects.select_for_update(of=("self",)).get(
                        pk=block.pk
                    )
                    block_is_locked.set()
                    if not let_writer_take_root.wait(timeout=10):
                        raise TimeoutError("Admin writer did not wait on the cascade lock.")
                    program_scheduling.lock_program_blocks([locked.pk])
                    locked.notes = "Запись планировщика после блокировки каскада и программы."
                    locked.save(update_fields=["notes", "updated_at"])
                outcomes.put(("schedule_writer", None))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        def admin_writer():
            close_old_connections()
            try:
                if not block_is_locked.wait(timeout=10):
                    raise TimeoutError("Scheduling writer did not lock the cascade.")
                with connection.cursor() as cursor:
                    cursor.execute("SET application_name = %s", [admin_application_name])
                client = Client()
                client.force_login(User.objects.get(pk=self.director.pk))
                response = client.post(
                    reverse(
                        "admin:operations_treatmentprogram_change",
                        args=[self.program.pk],
                    ),
                    {
                        "child": str(self.child.pk),
                        "title": "Программа после admin POST",
                        "consultation": "",
                        "status": TreatmentProgram.Status.ACTIVE,
                        "starts_on": "",
                        "ends_on": "",
                        "color": "#1267f2",
                        "notes": "Метаданные программы сохранены администратором.",
                        "blocks-TOTAL_FORMS": "1",
                        "blocks-INITIAL_FORMS": "1",
                        "blocks-MIN_NUM_FORMS": "0",
                        "blocks-MAX_NUM_FORMS": "1000",
                        "blocks-0-id": str(block.pk),
                        "blocks-0-program": str(self.program.pk),
                        "blocks-0-number": "1",
                        "blocks-0-title": "Каскад после admin POST",
                        "blocks-0-service": str(service.pk),
                        "blocks-0-staff_member": "",
                        "blocks-0-planned_sessions": "2",
                        "blocks-0-balance_account": "",
                        "blocks-0-status": ProgramBlock.Status.PLANNED,
                        "blocks-0-color": "#b71b55",
                        "_save": "Сохранить",
                    },
                )
                outcomes.put(("admin", response.status_code))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        scheduling_thread = Thread(target=scheduling_order_writer)
        admin_thread = Thread(target=admin_writer)
        scheduling_thread.start()
        self.assertTrue(block_is_locked.wait(timeout=10))
        admin_thread.start()

        deadline = monotonic() + 10
        admin_waited_on_block = False
        while monotonic() < deadline and not admin_waited_on_block:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT cardinality(pg_blocking_pids(pid)) > 0
                    FROM pg_stat_activity
                    WHERE application_name = %s
                    """,
                    [admin_application_name],
                )
                row = cursor.fetchone()
            admin_waited_on_block = bool(row and row[0])
        self.assertTrue(
            admin_waited_on_block,
            "Admin POST never waited on the cascade before taking the program root.",
        )
        let_writer_take_root.set()
        scheduling_thread.join(timeout=15)
        admin_thread.join(timeout=15)
        self.assertFalse(scheduling_thread.is_alive())
        self.assertFalse(admin_thread.is_alive())
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertFalse(any(isinstance(item, BaseException) for item in results), results)
        self.assertIn(("schedule_writer", None), results)
        self.assertIn(("admin", 302), results)

        self.program.refresh_from_db()
        block.refresh_from_db()
        self.assertEqual(self.program.title, "Программа после admin POST")
        self.assertEqual(block.title, "Каскад после admin POST")
        self.assertEqual(
            block.notes,
            "Запись планировщика после блокировки каскада и программы.",
        )


class ProgramCompletionMigrationTests(TransactionTestCase):
    migrate_from = [("operations", "0067_program_pause_resume")]
    migrate_to = [("operations", "0068_program_activation_completion")]

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("operations"))
        super().tearDown()

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def test_0068_forward_and_reverse_preserve_existing_0067_history(self):
        old_apps = self._migrate(self.migrate_from)
        suffix = uuid4().hex[:8]
        user = old_apps.get_model("auth", "User").objects.create(
            username=f"program-completion-migration-{suffix}",
            is_staff=True,
            is_superuser=True,
        )
        child = old_apps.get_model("operations", "Child").objects.create(
            last_name="Migration", first_name=suffix
        )
        program = old_apps.get_model("operations", "TreatmentProgram").objects.create(
            child_id=child.pk,
            title="Program with accepted 0067 history",
            status="active",
        )
        event_model = old_apps.get_model("operations", "TreatmentProgramLifecycleEvent")
        with transaction.atomic():
            event = event_model.objects.create(
                program_id=program.pk,
                operation_key=uuid4(),
                fingerprint="a" * 64,
                event_type="paused",
                event_number=1,
                status_from="active",
                status_to="paused",
                actor_id=user.pk,
                actor_role_snapshot="director",
                reason="Existing pause history survives expansion.",
            )
            old_apps.get_model("operations", "TreatmentProgram").objects.filter(
                pk=program.pk
            ).update(status="paused")
        original_fingerprint = event.fingerprint

        new_apps = self._migrate(self.migrate_to)
        expanded = new_apps.get_model(
            "operations", "TreatmentProgramLifecycleEvent"
        ).objects.get(pk=event.pk)
        self.assertEqual(expanded.fingerprint, original_fingerprint)
        self.assertEqual(expanded.event_type, "paused")
        self.assertEqual(expanded.context_snapshot, {})

        old_apps = self._migrate(self.migrate_from)
        restored = old_apps.get_model(
            "operations", "TreatmentProgramLifecycleEvent"
        ).objects.get(pk=event.pk)
        self.assertEqual(restored.fingerprint, original_fingerprint)
        self.assertEqual(restored.event_type, "paused")

        self._migrate(self.migrate_to)

    def test_0068_reverse_refuses_to_discard_new_review_history(self):
        apps = self._migrate(self.migrate_to)
        suffix = uuid4().hex[:8]
        user = apps.get_model("auth", "User").objects.create(
            username=f"program-completion-new-history-{suffix}",
            is_staff=True,
            is_superuser=True,
        )
        child = apps.get_model("operations", "Child").objects.create(
            last_name="Migration", first_name=f"New {suffix}"
        )
        program_model = apps.get_model("operations", "TreatmentProgram")
        event_model = apps.get_model("operations", "TreatmentProgramLifecycleEvent")
        program = program_model.objects.create(
            child_id=child.pk,
            title="Program with 0068 review history",
            status="draft",
        )
        snapshot = {
            "program_id": program.pk,
            "child_id": child.pk,
            "status": "draft",
            "starts_on": None,
            "ends_on": None,
            "blocks": [],
        }
        reason = "New review history blocks schema reversal."
        fingerprint = canonical_fingerprint(
            {
                "program_id": program.pk,
                "event_type": "cancelled",
                "event_number": 1,
                "status_from": "draft",
                "status_to": "cancelled",
                "actor_id": user.pk,
                "actor_role_snapshot": "director",
                "reason": reason,
                "supersedes_id": None,
                "context_snapshot": snapshot,
            }
        )
        with transaction.atomic():
            event = event_model.objects.create(
                program_id=program.pk,
                operation_key=uuid4(),
                fingerprint=fingerprint,
                event_type="cancelled",
                event_number=1,
                status_from="draft",
                status_to="cancelled",
                actor_id=user.pk,
                actor_role_snapshot="director",
                reason=reason,
                context_snapshot=snapshot,
            )
            program_model.objects.filter(pk=program.pk).update(status="cancelled")

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError, "Cannot reverse program completion migration"
        ):
            executor.migrate(self.migrate_from)
        self.assertEqual(event_model.objects.filter(pk=event.pk).count(), 1)

        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE operations_treatmentprogramlifecycleevent "
                    "DISABLE TRIGGER USER"
                )
                cursor.execute(
                    "ALTER TABLE operations_treatmentprogram "
                    "DISABLE TRIGGER program_status_guard"
                )
                try:
                    cursor.execute(
                        "DELETE FROM operations_treatmentprogramlifecycleevent "
                        "WHERE id = %s",
                        [event.pk],
                    )
                    cursor.execute(
                        "UPDATE operations_treatmentprogram SET status = 'draft' "
                        "WHERE id = %s",
                        [program.pk],
                    )
                finally:
                    cursor.execute(
                        "ALTER TABLE operations_treatmentprogramlifecycleevent "
                        "ENABLE TRIGGER USER"
                    )
                    cursor.execute(
                        "ALTER TABLE operations_treatmentprogram "
                        "ENABLE TRIGGER program_status_guard"
                    )
        else:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM operations_treatmentprogramlifecycleevent WHERE id = %s",
                    [event.pk],
                )
                cursor.execute(
                    "UPDATE operations_treatmentprogram SET status = 'draft' WHERE id = %s",
                    [program.pk],
                )
        self._migrate(self.migrate_from)
        self._migrate(self.migrate_to)
