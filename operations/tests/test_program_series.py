from __future__ import annotations

import importlib
from datetime import datetime, time, timedelta
from decimal import Decimal
from queue import Queue
from threading import Barrier, Thread
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentSeries,
    AppointmentSeriesMaterializationResult,
    AppointmentSeriesMaterializationRun,
    AppointmentSeriesMaterializationRunEvent,
    AppointmentSeriesOccurrence,
    AppointmentSeriesRetryTarget,
    AppointmentSeriesRevision,
    AppointmentSeriesRevisionParticipant,
    AppointmentSeriesRevisionStaffAssignment,
    AppointmentSeriesStaffAssignment,
    BalanceAccount,
    Child,
    FundingSource,
    LedgerEntry,
    ParentGuardian,
    ProgramBlock,
    Room,
    Service,
    StaffAvailability,
    StaffMember,
    TreatmentProgram,
)
from operations.services import (
    billing as billing_svc,
    program_series,
    program_wizard,
    series_revisions,
)

User = get_user_model()


def _local(day, clock):
    return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())


class GroupProgramSeriesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            "group-series-admin",
            password="x",
            is_staff=True,
            is_superuser=True,
        )
        cls.specialist_user = User.objects.create_user("group-series-specialist", password="x")
        cls.parent1 = ParentGuardian.objects.create(
            last_name="Первая",
            first_name="Мама",
            phone="+7 900 000-10-01",
        )
        cls.parent2 = ParentGuardian.objects.create(
            last_name="Вторая",
            first_name="Мама",
            phone="+7 900 000-10-02",
        )
        cls.child1 = Child.objects.create(
            last_name="Группа",
            first_name="Первый",
            primary_parent=cls.parent1,
        )
        cls.child2 = Child.objects.create(
            last_name="Группа",
            first_name="Второй",
            primary_parent=cls.parent2,
        )
        cls.service = Service.objects.create(
            name="Групповая логопедия",
            code="GROUP-SPEECH",
            category=Service.Category.SPEECH,
            default_duration_minutes=45,
            default_price=Decimal("1200"),
        )
        cls.staff1 = StaffMember.objects.create(
            user=cls.specialist_user,
            full_name="Основной специалист серии",
            specializations="Логопед",
        )
        cls.staff2 = StaffMember.objects.create(
            full_name="Ассистент серии",
            specializations="Логопед",
        )
        cls.room = Room.objects.create(
            name="Групповой кабинет серии",
            allow_group_sessions=True,
            limit_staff_count=True,
            max_staff_count=2,
            limit_recipient_count=True,
            max_recipient_count=4,
        )
        cls.funding = FundingSource.objects.create(
            name="Оплата групповой серии",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        cls.account1 = BalanceAccount.objects.create(
            child=cls.child1,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("10"),
        )
        cls.account2 = BalanceAccount.objects.create(
            child=cls.child2,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("10"),
        )
        cls.program1 = TreatmentProgram.objects.create(
            child=cls.child1,
            title="Программа первого",
            status=TreatmentProgram.Status.ACTIVE,
        )
        cls.program2 = TreatmentProgram.objects.create(
            child=cls.child2,
            title="Программа второго",
            status=TreatmentProgram.Status.ACTIVE,
        )
        cls.block1 = ProgramBlock.objects.create(
            program=cls.program1,
            number=1,
            title="Каскад первого",
            service=cls.service,
            staff_member=cls.staff1,
            planned_sessions=4,
            balance_account=cls.account1,
        )
        cls.block2 = ProgramBlock.objects.create(
            program=cls.program2,
            number=1,
            title="Каскад второго",
            service=cls.service,
            staff_member=cls.staff1,
            planned_sessions=4,
            balance_account=cls.account2,
        )

    def setUp(self):
        self.client.force_login(self.admin)
        self.start_date = timezone.localdate() + timedelta(days=5)
        self.end_date = self.start_date + timedelta(days=7)

    def preview(self, **overrides):
        params = {
            "blocks": [self.block1, self.block2],
            "staff_members": [self.staff1, self.staff2],
            "room": self.room,
            "title": "Постоянная группа",
            "start_date": self.start_date,
            "end_date": self.end_date,
            "weekdays": {self.start_date.weekday()},
            "start_time": time(10, 0),
            "duration_minutes": 45,
            "default_appointment_status": Appointment.Status.PROPOSED,
        }
        params.update(overrides)
        return program_series.preview_group_series(**params)

    def create_third_block(self):
        child = Child.objects.create(last_name="Группа", first_name="Третий")
        account = BalanceAccount.objects.create(
            child=child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("10"),
        )
        program = TreatmentProgram.objects.create(
            child=child,
            title="Программа третьего",
            status=TreatmentProgram.Status.ACTIVE,
        )
        block = ProgramBlock.objects.create(
            program=program,
            number=1,
            title="Каскад третьего",
            service=self.service,
            staff_member=self.staff1,
            planned_sessions=4,
            balance_account=account,
        )
        return child, account, block

    def flush_pending_revision_composition_constraints(self):
        if connection.vendor != "postgresql":
            return
        constraint_names = ", ".join(
            connection.ops.quote_name(name)
            for name in (
                "operations_appointmentseriesrevision_composition",
                "operations_appointmentseriesrevisionparticipant_composition",
                "operations_appointmentseriesrevisionstaffassignment_composition",
            )
        )
        with connection.cursor() as cursor:
            cursor.execute(f"SET CONSTRAINTS {constraint_names} IMMEDIATE")
            cursor.execute(f"SET CONSTRAINTS {constraint_names} DEFERRED")

    def form_payload(self, *, operation_key=None, action="preview"):
        return {
            "operation_key": str(operation_key or uuid4()),
            "title": "Постоянная группа",
            "additional_blocks": [str(self.block2.pk)],
            "staff_members": [str(self.staff1.pk), str(self.staff2.pk)],
            "primary_staff_member": str(self.staff1.pk),
            "room": str(self.room.pk),
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "weekdays": [str(self.start_date.weekday())],
            "start_time": "10:00",
            "duration_minutes": "45",
            "default_appointment_status": Appointment.Status.PROPOSED,
            "override_reason": "",
            "action": action,
        }

    def test_preview_skips_conflict_and_keeps_later_date(self):
        conflict_start = _local(self.start_date, time(10, 0))
        Appointment.objects.create(
            child=self.child2,
            staff_member=self.staff1,
            service=self.service,
            starts_at=conflict_start,
            ends_at=conflict_start + timedelta(minutes=45),
            status=Appointment.Status.CONFIRMED,
        )

        preview = self.preview(staff_members=[self.staff2])

        self.assertEqual(len(preview.dates), 2)
        self.assertFalse(preview.dates[0].ready)
        self.assertEqual(preview.dates[0].reason_code, "schedule_conflict")
        self.assertTrue(preview.dates[1].ready)

    def test_create_group_series_preserves_per_participant_program_data(self):
        conflict_start = _local(self.start_date, time(10, 0))
        other_staff = StaffMember.objects.create(full_name="Специалист конфликта")
        Appointment.objects.create(
            child=self.child2,
            staff_member=other_staff,
            service=self.service,
            starts_at=conflict_start,
            ends_at=conflict_start + timedelta(minutes=45),
            status=Appointment.Status.CONFIRMED,
        )
        preview = self.preview()

        result = program_series.create_group_series(
            preview,
            operation_key=uuid4(),
            actor=self.admin,
        )

        self.assertEqual(result.created_count, 1)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.series.session_type, Appointment.SessionType.GROUP)
        self.assertEqual(result.series.default_participants.count(), 2)
        self.assertEqual(result.series.default_staff_assignments.count(), 2)
        self.assertEqual(result.series.occurrences.count(), 2)
        appointment = Appointment.objects.get(series=result.series)
        self.assertEqual(appointment.participants.count(), 2)
        self.assertEqual(appointment.staff_assignments.count(), 2)
        by_child = {
            row.child_id: row
            for row in appointment.participants.select_related("program_block", "billing_account")
        }
        self.assertEqual(by_child[self.child1.pk].program_block_id, self.block1.pk)
        self.assertEqual(by_child[self.child2.pk].program_block_id, self.block2.pk)
        self.assertEqual(by_child[self.child1.pk].billing_account_id, self.account1.pk)
        self.assertEqual(by_child[self.child2.pk].billing_account_id, self.account2.pk)
        self.assertEqual(by_child[self.child1.pk].sequence_number, 1)
        self.assertEqual(by_child[self.child2.pk].sequence_number, 1)
        self.block1.refresh_from_db()
        self.block2.refresh_from_db()
        self.assertEqual(self.block1.status, ProgramBlock.Status.SCHEDULED)
        self.assertEqual(self.block2.status, ProgramBlock.Status.SCHEDULED)
        self.assertFalse(LedgerEntry.objects.exists())

    def test_initial_revision_run_and_results_are_dual_written_idempotently(self):
        operation_key = uuid4()
        preview = self.preview(end_date=self.start_date)

        first = program_series.create_group_series(
            preview,
            operation_key=operation_key,
            actor=self.admin,
        )
        repeated = program_series.create_group_series(
            preview,
            operation_key=operation_key,
            actor=self.admin,
        )

        series = first.series
        series.refresh_from_db()
        revision = series.current_revision
        self.assertIsNotNone(revision)
        self.assertEqual(revision.revision_number, 1)
        self.assertEqual(revision.event_type, AppointmentSeriesRevision.EventType.CREATED)
        self.assertEqual(
            revision.provenance_kind,
            AppointmentSeriesRevision.ProvenanceKind.NATIVE,
        )
        self.assertEqual(revision.participants.count(), 2)
        self.assertEqual(revision.staff_assignments.count(), 2)
        run = series.materialization_runs.get()
        self.assertEqual(run.mode, AppointmentSeriesMaterializationRun.Mode.INITIAL)
        self.assertEqual(run.operation_key, operation_key)
        self.assertEqual(run.expected_result_count, 1)
        occurrence = series.occurrences.get()
        result = run.results.get()
        self.assertEqual(result.compatibility_occurrence_id, occurrence.pk)
        self.assertEqual(
            result.scheduled_date,
            timezone.localtime(occurrence.scheduled_starts_at).date(),
        )
        self.assertEqual(
            result.provenance_kind,
            AppointmentSeriesMaterializationResult.ProvenanceKind.NATIVE,
        )
        event = run.events.get()
        self.assertEqual(
            event.event_type,
            AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED,
        )
        self.assertEqual(event.event_number, 1)
        self.assertEqual(event.result_count, 1)
        self.assertEqual(AppointmentSeriesRevision.objects.filter(series=series).count(), 1)
        self.assertEqual(
            AppointmentSeriesMaterializationRun.objects.filter(series=series).count(),
            1,
        )
        self.assertEqual(
            AppointmentSeriesMaterializationResult.objects.filter(series=series).count(),
            1,
        )
        self.assertEqual(repeated.created_count, 0)
        self.assertEqual(repeated.unchanged_count, 1)
        self.assertFalse(LedgerEntry.objects.exists())

    def test_occurrence_and_canonical_result_are_atomic_and_retryable(self):
        operation_key = uuid4()
        preview = self.preview(end_date=self.start_date)

        with patch(
            "operations.services.series_revisions.record_compatibility_result",
            side_effect=ValidationError("Имитированный сбой dual-write."),
        ), self.assertRaisesMessage(ValidationError, "dual-write"):
            program_series.create_group_series(
                preview,
                operation_key=operation_key,
                actor=self.admin,
            )

        series = AppointmentSeries.objects.get(operation_key=operation_key)
        self.assertTrue(series.current_revision_id)
        self.assertEqual(series.materialization_runs.count(), 1)
        self.assertFalse(series.materialization_runs.get().events.exists())
        self.assertFalse(series.occurrences.exists())
        self.assertFalse(series.materialization_results.exists())
        self.assertFalse(Appointment.objects.filter(series=series).exists())

        recovered = program_series.create_group_series(
            preview,
            operation_key=operation_key,
            actor=self.admin,
        )

        self.assertEqual(recovered.created_count, 1)
        self.assertEqual(series.occurrences.count(), 1)
        self.assertEqual(series.materialization_results.count(), 1)
        self.assertEqual(series.materialization_runs.get().events.count(), 1)

    def test_materializer_selects_primary_staff_by_role_not_row_order(self):
        series, _ = program_series._create_series_definition(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
        )
        series.default_staff_assignments.all().delete()
        AppointmentSeriesStaffAssignment.objects.create(
            series=series,
            staff_member=self.staff2,
            role=AppointmentSeriesStaffAssignment.Role.ASSISTANT,
        )
        AppointmentSeriesStaffAssignment.objects.create(
            series=series,
            staff_member=self.staff1,
            role=AppointmentSeriesStaffAssignment.Role.PRIMARY,
        )

        result = program_series.materialize_group_series(
            series,
            actor=self.admin,
        )

        appointment = Appointment.objects.get(series=series)
        self.assertEqual(result.created_count, 1)
        self.assertEqual(appointment.staff_member_id, self.staff1.pk)
        self.assertEqual(
            appointment.staff_assignments.get(
                role=AppointmentSeriesStaffAssignment.Role.PRIMARY
            ).staff_member_id,
            self.staff1.pk,
        )
        self.assertEqual(
            appointment.staff_assignments.get(
                role=AppointmentSeriesStaffAssignment.Role.ASSISTANT
            ).staff_member_id,
            self.staff2.pk,
        )

    def test_materializer_fails_closed_when_block_account_changes_after_revision(self):
        series, _ = program_series._create_series_definition(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
        )
        series_revisions.ensure_initial_revision(series, actor=self.admin)
        replacement = BalanceAccount.objects.create(
            child=self.child1,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("10"),
        )
        self.block1.balance_account = replacement
        self.block1.save(update_fields=["balance_account", "updated_at"])

        result = program_series.materialize_group_series(
            series,
            actor=self.admin,
        )

        occurrence = series.occurrences.get()
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(occurrence.outcome, AppointmentSeriesOccurrence.Outcome.SKIPPED)
        self.assertEqual(occurrence.reason_code, "funding_changed")
        self.assertFalse(Appointment.objects.filter(series=series).exists())

    def test_future_composition_revision_preserves_materialized_appointments(self):
        applied = program_series.create_group_series(
            self.preview(),
            operation_key=uuid4(),
            actor=self.admin,
        )
        series = applied.series
        series.refresh_from_db()
        self.flush_pending_revision_composition_constraints()
        previous = series.current_revision
        child3, account3, block3 = self.create_third_block()
        appointment_ids = list(
            Appointment.objects.filter(series=series).values_list("pk", flat=True)
        )
        participant_inputs = [
            series_revisions.SeriesParticipantInput(
                child_id=self.child1.pk,
                program_block_id=self.block1.pk,
                billing_account_id=self.account1.pk,
                position=1,
            ),
            series_revisions.SeriesParticipantInput(
                child_id=child3.pk,
                program_block_id=block3.pk,
                billing_account_id=account3.pk,
                position=2,
            ),
        ]
        staff_inputs = [
            series_revisions.SeriesStaffInput(
                staff_member_id=self.staff2.pk,
                role=AppointmentSeriesStaffAssignment.Role.ASSISTANT,
            ),
            series_revisions.SeriesStaffInput(
                staff_member_id=self.staff1.pk,
                role=AppointmentSeriesStaffAssignment.Role.PRIMARY,
            ),
        ]

        revision = series_revisions.revise_future_composition(
            series,
            expected_revision_id=previous.pk,
            effective_from=self.start_date + timedelta(days=1),
            participants=participant_inputs,
            staff_assignments=staff_inputs,
            actor=self.admin,
            reason="Изменение будущего состава группы.",
        )

        series.refresh_from_db()
        self.assertEqual(revision.revision_number, 2)
        self.assertEqual(
            revision.event_type,
            AppointmentSeriesRevision.EventType.FUTURE_COMPOSITION,
        )
        self.assertEqual(revision.supersedes_id, previous.pk)
        self.assertEqual(series.current_revision_id, revision.pk)
        self.assertEqual(
            set(series.default_participants.values_list("child_id", flat=True)),
            {self.child1.pk, child3.pk},
        )
        self.assertEqual(series.child_id, self.child1.pk)
        self.assertEqual(series.program_block_id, self.block1.pk)
        self.assertEqual(series.staff_member_id, self.staff1.pk)
        self.assertEqual(revision.participants.count(), 2)
        self.assertEqual(revision.staff_assignments.count(), 2)
        series_revisions.assert_current_projection(series, revision)

        self.assertEqual(
            list(
                Appointment.objects.filter(series=series)
                .order_by("pk")
                .values_list("pk", flat=True)
            ),
            appointment_ids,
        )
        for appointment in Appointment.objects.filter(series=series):
            self.assertEqual(
                set(appointment.participants.values_list("child_id", flat=True)),
                {self.child1.pk, self.child2.pk},
            )
            self.assertFalse(appointment.participants.filter(child=child3).exists())

        with self.assertRaisesMessage(
            series_revisions.SeriesRevisionMismatch,
            "уже изменен",
        ):
            series_revisions.revise_future_composition(
                series,
                expected_revision_id=previous.pk,
                effective_from=self.start_date + timedelta(days=2),
                participants=participant_inputs,
                staff_assignments=staff_inputs,
                actor=self.admin,
                reason="Устаревшая повторная команда.",
            )
        self.assertEqual(series.revisions.count(), 2)
        with self.assertRaisesMessage(ValidationError, "явного режима materialization"):
            program_series.materialize_group_series(series, actor=self.admin)

    def test_missing_only_materializes_absent_dates_and_replays_completed_run(self):
        series, _ = program_series._create_series_definition(
            self.preview(),
            operation_key=uuid4(),
        )
        series_revisions.ensure_initial_revision(series, actor=self.admin)
        self.flush_pending_revision_composition_constraints()
        operation_key = uuid4()

        applied = program_series.materialize_missing_series(
            series,
            operation_key=operation_key,
            actor=self.admin,
        )
        repeated = program_series.materialize_missing_series(
            series,
            operation_key=operation_key,
            actor=self.admin,
        )
        series.status = AppointmentSeries.Status.CANCELLED
        series.save(update_fields=["status", "updated_at"])
        replayed_after_status_change = program_series.materialize_missing_series(
            series,
            operation_key=operation_key,
            actor=self.admin,
        )

        self.assertEqual(applied.created_count, 2)
        self.assertEqual(applied.skipped_count, 0)
        self.assertEqual(applied.unchanged_count, 0)
        self.assertFalse(applied.reused_run)
        self.assertTrue(repeated.reused_run)
        self.assertTrue(replayed_after_status_change.reused_run)
        self.assertEqual(repeated.run.pk, applied.run.pk)
        self.assertEqual(replayed_after_status_change.run.pk, applied.run.pk)
        self.assertEqual(repeated.created_count, 2)
        self.assertEqual(applied.run.mode, AppointmentSeriesMaterializationRun.Mode.MISSING_ONLY)
        self.assertEqual(applied.run.expected_result_count, 2)
        self.assertEqual(
            list(applied.run.results.values_list("attempt_number", flat=True)),
            [1, 1],
        )
        self.assertEqual(
            list(applied.run.events.values_list("event_type", flat=True)),
            [AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED],
        )
        self.assertEqual(Appointment.objects.filter(series=series).count(), 2)
        self.assertEqual(series.occurrences.count(), 2)
        with self.assertRaisesMessage(ValidationError, "другой операции"):
            program_series.materialize_missing_series(
                series,
                operation_key=operation_key,
                actor=self.admin,
                reason="Другой смысл того же ключа запуска.",
            )

    def test_missing_only_records_cross_revision_history_as_unchanged(self):
        applied = program_series.create_group_series(
            self.preview(),
            operation_key=uuid4(),
            actor=self.admin,
        )
        series = applied.series
        series.refresh_from_db()
        self.flush_pending_revision_composition_constraints()
        previous = series.current_revision
        appointment_ids = list(
            Appointment.objects.filter(series=series)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        revision = series_revisions.revise_future_composition(
            series,
            expected_revision_id=previous.pk,
            effective_from=self.end_date,
            participants=[
                series_revisions.SeriesParticipantInput(
                    child_id=self.child1.pk,
                    program_block_id=self.block1.pk,
                    billing_account_id=self.account1.pk,
                    position=1,
                ),
                series_revisions.SeriesParticipantInput(
                    child_id=self.child2.pk,
                    program_block_id=self.block2.pk,
                    billing_account_id=self.account2.pk,
                    position=2,
                ),
            ],
            staff_assignments=[
                series_revisions.SeriesStaffInput(
                    staff_member_id=self.staff1.pk,
                    role=AppointmentSeriesStaffAssignment.Role.PRIMARY,
                ),
                series_revisions.SeriesStaffInput(
                    staff_member_id=self.staff2.pk,
                    role=AppointmentSeriesStaffAssignment.Role.ASSISTANT,
                ),
            ],
            actor=self.admin,
            reason="Новая редакция будущего состава для проверки истории.",
        )
        self.flush_pending_revision_composition_constraints()

        result = program_series.materialize_missing_series(
            series,
            operation_key=uuid4(),
            actor=self.admin,
        )

        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.skipped_count, 0)
        self.assertEqual(result.unchanged_count, 1)
        unchanged = result.run.results.select_related("supersedes").get()
        self.assertEqual(unchanged.revision_id, revision.pk)
        self.assertEqual(unchanged.attempt_number, 2)
        self.assertEqual(
            unchanged.outcome,
            AppointmentSeriesOccurrence.Outcome.UNCHANGED,
        )
        self.assertEqual(unchanged.reason_code, "existing_history")
        self.assertIsNotNone(unchanged.supersedes_id)
        self.assertEqual(unchanged.supersedes.revision_id, previous.pk)
        self.assertIsNone(unchanged.appointment_id)
        self.assertIsNone(unchanged.compatibility_occurrence_id)
        self.assertEqual(
            list(
                Appointment.objects.filter(series=series)
                .order_by("pk")
                .values_list("pk", flat=True)
            ),
            appointment_ids,
        )

    def test_missing_only_resumes_interrupted_run_without_duplicates(self):
        series, _ = program_series._create_series_definition(
            self.preview(),
            operation_key=uuid4(),
        )
        series_revisions.ensure_initial_revision(series, actor=self.admin)
        self.flush_pending_revision_composition_constraints()
        operation_key = uuid4()
        original = program_series._materialize_missing_date
        call_count = 0

        def fail_on_second_date(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("fault injection after first durable date")
            return original(*args, **kwargs)

        with patch(
            "operations.services.program_series._materialize_missing_date",
            side_effect=fail_on_second_date,
        ), self.assertRaisesMessage(RuntimeError, "fault injection"):
            program_series.materialize_missing_series(
                series,
                operation_key=operation_key,
                actor=self.admin,
            )

        run = AppointmentSeriesMaterializationRun.objects.get(
            operation_key=operation_key
        )
        self.assertEqual(run.results.count(), 1)
        interrupted = run.events.get()
        self.assertEqual(
            interrupted.event_type,
            AppointmentSeriesMaterializationRunEvent.EventType.INTERRUPTED,
        )
        self.assertEqual(interrupted.result_count, 1)
        self.assertEqual(interrupted.created_count, 1)

        series.refresh_from_db()
        previous_revision = series.current_revision
        next_revision = series_revisions.revise_future_composition(
            series,
            expected_revision_id=previous_revision.pk,
            effective_from=self.end_date,
            participants=[
                series_revisions.SeriesParticipantInput(
                    child_id=self.child1.pk,
                    program_block_id=self.block1.pk,
                    billing_account_id=self.account1.pk,
                    position=1,
                ),
                series_revisions.SeriesParticipantInput(
                    child_id=self.child2.pk,
                    program_block_id=self.block2.pk,
                    billing_account_id=self.account2.pk,
                    position=2,
                ),
            ],
            staff_assignments=[
                series_revisions.SeriesStaffInput(
                    staff_member_id=self.staff1.pk,
                    role=AppointmentSeriesStaffAssignment.Role.PRIMARY,
                ),
                series_revisions.SeriesStaffInput(
                    staff_member_id=self.staff2.pk,
                    role=AppointmentSeriesStaffAssignment.Role.ASSISTANT,
                ),
            ],
            actor=self.admin,
            reason="Новая редакция создана после принятия прерванного запуска.",
        )
        self.flush_pending_revision_composition_constraints()

        with self.assertRaisesMessage(
            ValidationError,
            "ранее принятый пересекающийся запуск",
        ):
            program_series.materialize_missing_series(
                series,
                operation_key=uuid4(),
                actor=self.admin,
            )

        recovered = program_series.materialize_missing_series(
            series,
            operation_key=operation_key,
            actor=self.admin,
        )

        self.assertTrue(recovered.reused_run)
        self.assertEqual(recovered.run.pk, run.pk)
        self.assertEqual(recovered.run.revision_id, previous_revision.pk)
        series.refresh_from_db()
        self.assertEqual(series.current_revision_id, next_revision.pk)
        self.assertEqual(recovered.created_count, 2)
        self.assertEqual(run.results.count(), 2)
        self.assertEqual(Appointment.objects.filter(series=series).count(), 2)
        self.assertEqual(
            list(run.events.order_by("event_number").values_list("event_type", flat=True)),
            [
                AppointmentSeriesMaterializationRunEvent.EventType.INTERRUPTED,
                AppointmentSeriesMaterializationRunEvent.EventType.RESUMED,
                AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED,
            ],
        )
        newer_run = program_series.materialize_missing_series(
            series,
            operation_key=uuid4(),
            actor=self.admin,
        )
        self.assertEqual(newer_run.run.revision_id, next_revision.pk)
        self.assertEqual(newer_run.unchanged_count, 1)
        self.assertEqual(Appointment.objects.filter(series=series).count(), 2)

    def test_interrupted_run_rejects_results_until_explicit_resume(self):
        applied = program_series.create_group_series(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
            actor=self.admin,
        )
        series = applied.series
        series.refresh_from_db()
        starts_at = _local(self.start_date, time(10, 0))
        run, _ = series_revisions.get_or_create_missing_run(
            series,
            series.current_revision,
            operation_key=uuid4(),
            actor=self.admin,
            date_from=self.start_date,
            date_to=self.start_date,
            expected_result_count=1,
        )
        series_revisions.interrupt_run(
            run,
            reason="Проверка запрета записи до явного возобновления.",
        )

        with self.assertRaisesMessage(ValidationError, "явно возобновить"):
            series_revisions.record_unchanged_result(
                run,
                scheduled_starts_at=starts_at,
            )

        previous = series.materialization_results.get(
            scheduled_starts_at=starts_at,
            attempt_number=1,
        )
        forbidden = AppointmentSeriesMaterializationResult(
            series=series,
            revision=series.current_revision,
            run=run,
            scheduled_starts_at=starts_at,
            scheduled_date=self.start_date,
            attempt_number=2,
            provenance_kind=(
                AppointmentSeriesMaterializationResult.ProvenanceKind.NATIVE
            ),
            outcome=AppointmentSeriesOccurrence.Outcome.UNCHANGED,
            reason_code="forbidden_while_interrupted",
            reason="Запись не должна пройти до возобновления.",
            supersedes=previous,
        )
        with self.assertRaisesMessage(ValidationError, "явно возобновить"):
            forbidden.save()

        self.assertFalse(run.results.exists())

    def test_missing_only_replay_survives_actor_role_promotion(self):
        operator = User.objects.create_user(
            "missing-role-change-operator",
            password="x",
            is_staff=True,
        )
        series, _ = program_series._create_series_definition(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
        )
        series_revisions.ensure_initial_revision(series, actor=operator)
        self.flush_pending_revision_composition_constraints()
        operation_key = uuid4()
        applied = program_series.materialize_missing_series(
            series,
            operation_key=operation_key,
            actor=operator,
        )
        operator.is_superuser = True
        operator.save(update_fields=["is_superuser"])

        repeated = program_series.materialize_missing_series(
            series,
            operation_key=operation_key,
            actor=operator,
        )

        self.assertTrue(repeated.reused_run)
        self.assertEqual(repeated.run.pk, applied.run.pk)
        self.assertEqual(
            applied.run.actor_role_snapshot,
            AppointmentSeriesMaterializationRun.ActorRole.ADMINISTRATOR,
        )

    def test_retry_target_freezes_effective_skipped_chain_and_is_immutable(self):
        conflict_start = _local(self.start_date, time(10, 0))
        Appointment.objects.create(
            child=self.child1,
            staff_member=self.staff1,
            service=self.service,
            starts_at=conflict_start,
            ends_at=conflict_start + timedelta(minutes=45),
            status=Appointment.Status.CONFIRMED,
        )
        initial = program_series.create_group_series(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
            actor=self.admin,
        )
        series = initial.series
        series.refresh_from_db()
        skipped = series.materialization_results.get(
            outcome=AppointmentSeriesOccurrence.Outcome.SKIPPED
        )
        missing = program_series.materialize_missing_series(
            series=series,
            date_from=self.start_date,
            date_to=self.start_date,
            operation_key=uuid4(),
            actor=self.admin,
        )
        chain_head = missing.run.results.get()
        self.assertEqual(
            chain_head.outcome,
            AppointmentSeriesOccurrence.Outcome.UNCHANGED,
        )

        with transaction.atomic():
            retry_run = AppointmentSeriesMaterializationRun.objects.create(
                series=series,
                revision=series.current_revision,
                operation_key=uuid4(),
                fingerprint="a" * 64,
                mode=AppointmentSeriesMaterializationRun.Mode.RETRY_SKIPPED,
                date_from=self.start_date,
                date_to=self.start_date,
                expected_result_count=1,
                actor=self.admin,
                actor_role_snapshot=(
                    AppointmentSeriesMaterializationRun.ActorRole.ADMINISTRATOR
                ),
                reason="Повтор после устранения конфликта расписания.",
            )
            target = AppointmentSeriesRetryTarget.objects.create(
                run=retry_run,
                scheduled_starts_at=chain_head.scheduled_starts_at,
                scheduled_date=chain_head.scheduled_date,
                chain_head_result=chain_head,
                effective_skipped_result=skipped,
            )

        target.scheduled_date += timedelta(days=1)
        with self.assertRaisesMessage(ValidationError, "нельзя изменять"):
            target.save()
        with self.assertRaisesMessage(ValidationError, "нельзя удалять"):
            target.delete()
        with self.assertRaises(ValidationError):
            AppointmentSeriesRetryTarget.objects.filter(pk=target.pk).update(
                scheduled_date=self.start_date + timedelta(days=1)
            )

        invalid = AppointmentSeriesRetryTarget(
            run=retry_run,
            scheduled_starts_at=chain_head.scheduled_starts_at,
            scheduled_date=chain_head.scheduled_date,
            chain_head_result=chain_head,
            effective_skipped_result=chain_head,
        )
        with self.assertRaisesMessage(
            ValidationError,
            "Эффективный исход текущей цепочки должен быть skipped",
        ):
            invalid.full_clean(validate_unique=False, validate_constraints=False)

        unrelated_run = AppointmentSeriesMaterializationRun.objects.create(
            series=series,
            revision=series.current_revision,
            operation_key=uuid4(),
            fingerprint="b" * 64,
            mode=AppointmentSeriesMaterializationRun.Mode.MISSING_ONLY,
            date_from=self.start_date,
            date_to=self.start_date,
            expected_result_count=1,
            actor=self.admin,
            actor_role_snapshot=(
                AppointmentSeriesMaterializationRun.ActorRole.ADMINISTRATOR
            ),
            reason="Проверка резервирования вершины цепочки.",
        )
        unsafe_result = AppointmentSeriesMaterializationResult(
            series=series,
            revision=series.current_revision,
            run=unrelated_run,
            scheduled_starts_at=chain_head.scheduled_starts_at,
            scheduled_date=chain_head.scheduled_date,
            attempt_number=chain_head.attempt_number + 1,
            provenance_kind=(
                AppointmentSeriesMaterializationResult.ProvenanceKind.NATIVE
            ),
            outcome=AppointmentSeriesOccurrence.Outcome.UNCHANGED,
            reason_code="unsafe_branch",
            reason="Чужой запуск не должен занять цель retry.",
            supersedes=chain_head,
        )
        with self.assertRaisesMessage(ValidationError, "зарезервирована"):
            unsafe_result.save()

        retry_result = AppointmentSeriesMaterializationResult.objects.create(
            series=series,
            revision=series.current_revision,
            run=retry_run,
            scheduled_starts_at=chain_head.scheduled_starts_at,
            scheduled_date=chain_head.scheduled_date,
            attempt_number=chain_head.attempt_number + 1,
            provenance_kind=(
                AppointmentSeriesMaterializationResult.ProvenanceKind.NATIVE
            ),
            outcome=AppointmentSeriesOccurrence.Outcome.SKIPPED,
            reason_code="still_conflicted",
            reason="Повтор выполнен, конфликт пока сохраняется.",
            supersedes=chain_head,
        )
        self.assertEqual(retry_result.supersedes_id, chain_head.pk)

    def test_future_group_revision_rejects_incomplete_composition_atomically(self):
        applied = program_series.create_group_series(
            self.preview(),
            operation_key=uuid4(),
            actor=self.admin,
        )
        series = applied.series
        series.refresh_from_db()
        self.flush_pending_revision_composition_constraints()
        previous_id = series.current_revision_id

        with self.assertRaisesMessage(ValidationError, "минимум двух"):
            series_revisions.revise_future_composition(
                series,
                expected_revision_id=previous_id,
                effective_from=self.start_date + timedelta(days=1),
                participants=[
                    series_revisions.SeriesParticipantInput(
                        child_id=self.child1.pk,
                        program_block_id=self.block1.pk,
                        billing_account_id=self.account1.pk,
                        position=1,
                    )
                ],
                staff_assignments=[
                    series_revisions.SeriesStaffInput(
                        staff_member_id=self.staff1.pk,
                        role=AppointmentSeriesStaffAssignment.Role.PRIMARY,
                    )
                ],
                actor=self.admin,
                reason="Неполный будущий состав.",
            )

        series.refresh_from_db()
        self.assertEqual(series.current_revision_id, previous_id)
        self.assertEqual(series.revisions.count(), 1)
        self.assertEqual(
            set(series.default_participants.values_list("child_id", flat=True)),
            {self.child1.pk, self.child2.pk},
        )

    def test_future_composition_revision_requires_operator(self):
        applied = program_series.create_group_series(
            self.preview(),
            operation_key=uuid4(),
            actor=self.admin,
        )
        series = applied.series
        series.refresh_from_db()
        self.flush_pending_revision_composition_constraints()
        previous_id = series.current_revision_id

        with self.assertRaises(PermissionDenied):
            series_revisions.revise_future_composition(
                series,
                expected_revision_id=previous_id,
                effective_from=self.start_date + timedelta(days=1),
                participants=[
                    series_revisions.SeriesParticipantInput(
                        child_id=self.child1.pk,
                        program_block_id=self.block1.pk,
                        billing_account_id=self.account1.pk,
                        position=1,
                    ),
                    series_revisions.SeriesParticipantInput(
                        child_id=self.child2.pk,
                        program_block_id=self.block2.pk,
                        billing_account_id=self.account2.pk,
                        position=2,
                    ),
                ],
                staff_assignments=[
                    series_revisions.SeriesStaffInput(
                        staff_member_id=self.staff1.pk,
                        role=AppointmentSeriesStaffAssignment.Role.PRIMARY,
                    ),
                    series_revisions.SeriesStaffInput(
                        staff_member_id=self.staff2.pk,
                        role=AppointmentSeriesStaffAssignment.Role.ASSISTANT,
                    ),
                ],
                actor=self.specialist_user,
                reason="Попытка изменить будущий состав специалистом.",
            )

        series.refresh_from_db()
        self.assertEqual(series.current_revision_id, previous_id)
        self.assertEqual(series.revisions.count(), 1)

    def test_future_composition_rejects_program_ending_before_series(self):
        applied = program_series.create_group_series(
            self.preview(),
            operation_key=uuid4(),
            actor=self.admin,
        )
        series = applied.series
        series.refresh_from_db()
        self.flush_pending_revision_composition_constraints()
        previous_id = series.current_revision_id
        child3, account3, block3 = self.create_third_block()
        block3.program.ends_on = self.end_date - timedelta(days=1)
        block3.program.save(update_fields=["ends_on", "updated_at"])

        with self.assertRaisesMessage(ValidationError, "заканчивается раньше"):
            series_revisions.revise_future_composition(
                series,
                expected_revision_id=previous_id,
                effective_from=self.start_date + timedelta(days=1),
                participants=[
                    series_revisions.SeriesParticipantInput(
                        child_id=self.child1.pk,
                        program_block_id=self.block1.pk,
                        billing_account_id=self.account1.pk,
                        position=1,
                    ),
                    series_revisions.SeriesParticipantInput(
                        child_id=child3.pk,
                        program_block_id=block3.pk,
                        billing_account_id=account3.pk,
                        position=2,
                    ),
                ],
                staff_assignments=[
                    series_revisions.SeriesStaffInput(
                        staff_member_id=self.staff1.pk,
                        role=AppointmentSeriesStaffAssignment.Role.PRIMARY,
                    )
                ],
                actor=self.admin,
                reason="Программа не покрывает будущий остаток серии.",
            )

        series.refresh_from_db()
        self.assertEqual(series.current_revision_id, previous_id)
        self.assertEqual(series.revisions.count(), 1)

    def test_future_composition_requires_override_for_staff_leave_status(self):
        applied = program_series.create_group_series(
            self.preview(),
            operation_key=uuid4(),
            actor=self.admin,
        )
        series = applied.series
        series.refresh_from_db()
        self.flush_pending_revision_composition_constraints()
        previous_id = series.current_revision_id
        self.staff2.status = StaffMember.Status.VACATION
        self.staff2.save(update_fields=["status", "updated_at"])
        participants = [
            series_revisions.SeriesParticipantInput(
                child_id=self.child1.pk,
                program_block_id=self.block1.pk,
                billing_account_id=self.account1.pk,
                position=1,
            ),
            series_revisions.SeriesParticipantInput(
                child_id=self.child2.pk,
                program_block_id=self.block2.pk,
                billing_account_id=self.account2.pk,
                position=2,
            ),
        ]

        with self.assertRaisesMessage(ValidationError, "явного разрешения"):
            series_revisions.revise_future_composition(
                series,
                expected_revision_id=previous_id,
                effective_from=self.start_date + timedelta(days=1),
                participants=participants,
                staff_assignments=[
                    series_revisions.SeriesStaffInput(
                        staff_member_id=self.staff1.pk,
                        role=AppointmentSeriesStaffAssignment.Role.PRIMARY,
                    ),
                    series_revisions.SeriesStaffInput(
                        staff_member_id=self.staff2.pk,
                        role=AppointmentSeriesStaffAssignment.Role.ASSISTANT,
                    ),
                ],
                actor=self.admin,
                reason="Специалист находится в отпуске.",
            )

        revision = series_revisions.revise_future_composition(
            series,
            expected_revision_id=previous_id,
            effective_from=self.start_date + timedelta(days=1),
            participants=participants,
            staff_assignments=[
                series_revisions.SeriesStaffInput(
                    staff_member_id=self.staff1.pk,
                    role=AppointmentSeriesStaffAssignment.Role.PRIMARY,
                ),
                series_revisions.SeriesStaffInput(
                    staff_member_id=self.staff2.pk,
                    role=AppointmentSeriesStaffAssignment.Role.ASSISTANT,
                    override_availability=True,
                    override_reason="Согласованный выход специалиста из отпуска.",
                ),
            ],
            actor=self.admin,
            reason="Согласован выход специалиста из отпуска.",
        )

        self.assertEqual(revision.revision_number, 2)
        self.assertTrue(
            revision.staff_assignments.get(staff_member=self.staff2).override_availability
        )

    def test_slot_conflict_honors_per_staff_availability_overrides(self):
        for staff_member in (self.staff1, self.staff2):
            StaffAvailability.objects.create(
                staff_member=staff_member,
                weekday=self.start_date.weekday(),
                starts_at=time(12, 0),
                ends_at=time(13, 0),
            )
        starts_at = _local(self.start_date, time(10, 0))
        ends_at = starts_at + timedelta(minutes=45)

        partially_overridden = program_series._slot_conflict(
            starts_at=starts_at,
            ends_at=ends_at,
            blocks=[self.block1, self.block2],
            staff_members=[self.staff1, self.staff2],
            room=self.room,
            allow_outside_availability=False,
            availability_override_staff_ids={self.staff1.pk},
        )
        fully_overridden = program_series._slot_conflict(
            starts_at=starts_at,
            ends_at=ends_at,
            blocks=[self.block1, self.block2],
            staff_members=[self.staff1, self.staff2],
            room=self.room,
            allow_outside_availability=False,
            availability_override_staff_ids={self.staff1.pk, self.staff2.pk},
        )

        self.assertEqual(partially_overridden[0], "staff_unavailable")
        self.assertIn(str(self.staff2), partially_overridden[1])
        self.assertEqual(fully_overridden[0], "")
        self.staff2.status = StaffMember.Status.INACTIVE
        inactive = program_series._slot_conflict(
            starts_at=starts_at,
            ends_at=ends_at,
            blocks=[self.block1, self.block2],
            staff_members=[self.staff1, self.staff2],
            room=self.room,
            allow_outside_availability=False,
            availability_override_staff_ids={self.staff1.pk, self.staff2.pk},
        )
        self.assertEqual(inactive[0], "staff_inactive")

    def test_group_materializer_locks_and_skips_inactive_staff(self):
        preview = self.preview(
            end_date=self.start_date,
            allow_outside_availability=True,
            override_reason="Согласованный выход специалистов вне графика.",
        )
        series, _ = program_series._create_series_definition(
            preview,
            operation_key=uuid4(),
        )
        self.staff2.status = StaffMember.Status.INACTIVE
        self.staff2.save(update_fields=["status", "updated_at"])

        result = program_series.materialize_group_series(series, actor=self.admin)

        occurrence = series.occurrences.get()
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(occurrence.outcome, AppointmentSeriesOccurrence.Outcome.SKIPPED)
        self.assertEqual(occurrence.reason_code, "staff_inactive")
        self.assertFalse(Appointment.objects.filter(series=series).exists())

    def test_individual_materializer_locks_and_skips_inactive_staff(self):
        series = AppointmentSeries.objects.create(
            operation_key=uuid4(),
            child=self.child1,
            service=self.service,
            staff_member=self.staff1,
            room=self.room,
            program_block=self.block1,
            title="Индивидуальная серия с деактивированным специалистом",
            start_date=self.start_date,
            end_date=self.start_date,
            days_of_week=program_series._days_of_week_value(
                {self.start_date.weekday()}
            ),
            time=time(10, 0),
            duration_minutes=45,
            session_type=Appointment.SessionType.INDIVIDUAL,
            materialization_mode=AppointmentSeries.MaterializationMode.CREATE_APPOINTMENTS,
            status=AppointmentSeries.Status.ACTIVE,
        )
        self.staff1.status = StaffMember.Status.INACTIVE
        self.staff1.save(update_fields=["status", "updated_at"])

        result = program_series.materialize_individual_series(series, actor=self.admin)

        occurrence = series.occurrences.get()
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(occurrence.outcome, AppointmentSeriesOccurrence.Outcome.SKIPPED)
        self.assertEqual(occurrence.reason_code, "staff_inactive")
        self.assertFalse(Appointment.objects.filter(series=series).exists())

    def test_materialization_rejects_mutated_root_projection(self):
        applied = program_series.create_group_series(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
            actor=self.admin,
        )
        appointment_count = Appointment.objects.count()
        occurrence_count = AppointmentSeriesOccurrence.objects.count()
        AppointmentSeries.objects.filter(pk=applied.series.pk).update(
            title="Несогласованная ручная правка"
        )
        applied.series.refresh_from_db()

        with self.assertRaisesMessage(
            series_revisions.SeriesRevisionMismatch,
            "не совпадает с immutable-редакцией",
        ):
            program_series.materialize_group_series(
                applied.series,
                actor=self.admin,
            )

        self.assertEqual(Appointment.objects.count(), appointment_count)
        self.assertEqual(AppointmentSeriesOccurrence.objects.count(), occurrence_count)

    @skipUnless(connection.vendor == "postgresql", "DB guards проверяются на PostgreSQL.")
    def test_revision_history_database_guards_block_mutation_and_pointer_clear(self):
        applied = program_series.create_group_series(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
            actor=self.admin,
        )
        series = applied.series
        series.refresh_from_db()
        revision = series.current_revision
        materialization_result = series.materialization_results.get()

        with self.assertRaisesMessage(
            DatabaseError, "appointment series history rows are immutable"
        ), transaction.atomic():
            AppointmentSeriesRevision._base_manager.filter(pk=revision.pk).update(
                reason="Bypass attempt"
            )
        with self.assertRaisesMessage(
            DatabaseError, "appointment series history rows are immutable"
        ), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM operations_appointmentseriesmaterializationresult WHERE id = %s",
                [materialization_result.pk],
            )
        with self.assertRaisesMessage(
            DatabaseError,
            "appointment series with revision history requires current revision",
        ), transaction.atomic():
            AppointmentSeries.objects.filter(pk=series.pk).update(current_revision=None)
        late_result = AppointmentSeriesMaterializationResult(
            series=series,
            revision=revision,
            run=series.materialization_runs.get(),
            scheduled_starts_at=materialization_result.scheduled_starts_at,
            scheduled_date=materialization_result.scheduled_date,
            attempt_number=1,
            provenance_kind=AppointmentSeriesMaterializationResult.ProvenanceKind.NATIVE,
            outcome=AppointmentSeriesOccurrence.Outcome.SKIPPED,
            reason_code="late_result",
            reason="Завершенный запуск не должен меняться.",
        )
        with self.assertRaisesMessage(
            DatabaseError, "completed series run does not accept results"
        ), transaction.atomic():
            AppointmentSeriesMaterializationResult._base_manager.bulk_create(
                [late_result]
            )

    def test_revision_history_queryset_mutations_are_blocked_on_all_databases(self):
        applied = program_series.create_group_series(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
            actor=self.admin,
        )
        revision = applied.series.revisions.get()
        materialization_result = applied.series.materialization_results.get()

        with self.assertRaisesMessage(ValidationError, "историю нельзя"):
            AppointmentSeriesRevision.objects.filter(pk=revision.pk).update(
                reason="Bypass attempt"
            )
        with self.assertRaisesMessage(ValidationError, "историю нельзя"):
            AppointmentSeriesMaterializationResult.objects.filter(
                pk=materialization_result.pk
            ).delete()

    @skipUnless(connection.vendor == "postgresql", "DB guards проверяются на PostgreSQL.")
    def test_revision_insert_requires_current_pointer_to_move_in_same_transaction(self):
        applied = program_series.create_group_series(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
            actor=self.admin,
        )
        series = applied.series
        series.refresh_from_db()
        previous = series.current_revision

        revision = AppointmentSeriesRevision(
            series=series,
            revision_number=2,
            event_type=AppointmentSeriesRevision.EventType.FUTURE_COMPOSITION,
            provenance_kind=AppointmentSeriesRevision.ProvenanceKind.NATIVE,
            effective_from=previous.effective_from,
            title=previous.title,
            service=previous.service,
            room=previous.room,
            start_date=previous.start_date,
            end_date=previous.end_date,
            days_of_week=previous.days_of_week,
            time=previous.time,
            duration_minutes=previous.duration_minutes,
            session_type=previous.session_type,
            materialization_mode=previous.materialization_mode,
            default_appointment_status=previous.default_appointment_status,
            allow_unpaid_reserve=previous.allow_unpaid_reserve,
            allow_outside_availability=previous.allow_outside_availability,
            override_reason=previous.override_reason,
            fingerprint="f" * 64,
            actor=self.admin,
            actor_role_snapshot=AppointmentSeriesRevision.ActorRole.ADMINISTRATOR,
            reason="Проверка обязательного перевода текущей редакции.",
            supersedes=previous,
            decided_at=timezone.now(),
        )

        with self.assertRaisesMessage(
            DatabaseError,
            "appointment series current revision does not match inserted revision",
        ), transaction.atomic():
            AppointmentSeriesRevision._base_manager.bulk_create([revision])
            AppointmentSeriesRevisionParticipant._base_manager.bulk_create(
                [
                    AppointmentSeriesRevisionParticipant(
                        revision=revision,
                        child_id=participant.child_id,
                        program_block_id=participant.program_block_id,
                        billing_account_id=participant.billing_account_id,
                        position=participant.position,
                    )
                    for participant in previous.participants.all()
                ]
            )
            AppointmentSeriesRevisionStaffAssignment._base_manager.bulk_create(
                [
                    AppointmentSeriesRevisionStaffAssignment(
                        revision=revision,
                        staff_member_id=assignment.staff_member_id,
                        role=assignment.role,
                        override_availability=assignment.override_availability,
                        override_reason=assignment.override_reason,
                    )
                    for assignment in previous.staff_assignments.all()
                ]
            )
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    @skipUnless(connection.vendor == "postgresql", "DB guards проверяются на PostgreSQL.")
    def test_run_database_guards_reject_cross_root_and_false_completion(self):
        first = program_series.create_group_series(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
            actor=self.admin,
        )
        second = program_series.create_group_series(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
            actor=self.admin,
        )
        first.series.refresh_from_db()
        second.series.refresh_from_db()

        invalid_run = AppointmentSeriesMaterializationRun(
            series=first.series,
            revision=second.series.current_revision,
            operation_key=uuid4(),
            fingerprint="a" * 64,
            mode=AppointmentSeriesMaterializationRun.Mode.MISSING_ONLY,
            date_from=self.start_date,
            date_to=self.start_date,
            expected_result_count=0,
            actor=self.admin,
            actor_role_snapshot=AppointmentSeriesMaterializationRun.ActorRole.ADMINISTRATOR,
        )
        with self.assertRaisesMessage(
            DatabaseError, "series run revision belongs to another series"
        ), transaction.atomic():
            AppointmentSeriesMaterializationRun._base_manager.bulk_create([invalid_run])

        run = AppointmentSeriesMaterializationRun.objects.create(
            series=first.series,
            revision=first.series.current_revision,
            operation_key=uuid4(),
            fingerprint="b" * 64,
            mode=AppointmentSeriesMaterializationRun.Mode.MISSING_ONLY,
            date_from=self.start_date,
            date_to=self.start_date,
            expected_result_count=1,
            actor=self.admin,
            actor_role_snapshot=AppointmentSeriesMaterializationRun.ActorRole.ADMINISTRATOR,
        )
        false_completion = AppointmentSeriesMaterializationRunEvent(
            run=run,
            event_number=1,
            event_type=AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED,
            result_count=0,
            created_count=0,
            joined_count=0,
            skipped_count=0,
            unchanged_count=0,
        )
        with self.assertRaisesMessage(
            DatabaseError, "series run completion counters do not match results"
        ), transaction.atomic():
            AppointmentSeriesMaterializationRunEvent._base_manager.bulk_create(
                [false_completion]
            )

    @skipUnless(connection.vendor == "postgresql", "DB guards проверяются на PostgreSQL.")
    def test_result_database_guard_rejects_participant_from_another_appointment(self):
        first = program_series.create_group_series(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
            actor=self.admin,
        )
        second = program_series.create_group_series(
            self.preview(end_date=self.start_date, start_time=time(12, 0)),
            operation_key=uuid4(),
            actor=self.admin,
        )
        first.series.refresh_from_db()
        first_appointment = Appointment.objects.get(series=first.series)
        foreign_participant = AppointmentParticipant.objects.filter(
            appointment__series=second.series
        ).first()
        self.assertIsNotNone(foreign_participant)
        run = AppointmentSeriesMaterializationRun.objects.create(
            series=first.series,
            revision=first.series.current_revision,
            operation_key=uuid4(),
            fingerprint="c" * 64,
            mode=AppointmentSeriesMaterializationRun.Mode.MISSING_ONLY,
            date_from=self.start_date,
            date_to=self.start_date,
            expected_result_count=1,
            actor=self.admin,
            actor_role_snapshot=AppointmentSeriesMaterializationRun.ActorRole.ADMINISTRATOR,
        )
        invalid_result = AppointmentSeriesMaterializationResult(
            series=first.series,
            revision=first.series.current_revision,
            run=run,
            scheduled_starts_at=first_appointment.starts_at,
            scheduled_date=timezone.localtime(first_appointment.starts_at).date(),
            attempt_number=1,
            provenance_kind=AppointmentSeriesMaterializationResult.ProvenanceKind.NATIVE,
            appointment=first_appointment,
            appointment_participant=foreign_participant,
            outcome=AppointmentSeriesOccurrence.Outcome.JOINED,
            reason_code="guard_test",
            reason="Участник другого занятия должен быть отклонен.",
        )

        with self.assertRaisesMessage(
            DatabaseError, "series result participant belongs to another appointment"
        ), transaction.atomic():
            AppointmentSeriesMaterializationResult._base_manager.bulk_create(
                [invalid_result]
            )

    @skipUnless(connection.vendor == "postgresql", "DB guards проверяются на PostgreSQL.")
    def test_result_database_guard_rejects_incorrect_local_date(self):
        applied = program_series.create_group_series(
            self.preview(),
            operation_key=uuid4(),
            actor=self.admin,
        )
        applied.series.refresh_from_db()
        revision = applied.series.current_revision
        first_result = applied.series.materialization_results.order_by(
            "scheduled_starts_at"
        ).first()
        self.assertIsNotNone(first_result)
        run = AppointmentSeriesMaterializationRun.objects.create(
            series=applied.series,
            revision=revision,
            operation_key=uuid4(),
            fingerprint="d" * 64,
            mode=AppointmentSeriesMaterializationRun.Mode.MISSING_ONLY,
            date_from=revision.start_date,
            date_to=revision.end_date,
            expected_result_count=1,
            actor=self.admin,
            actor_role_snapshot=AppointmentSeriesMaterializationRun.ActorRole.ADMINISTRATOR,
        )
        invalid_result = AppointmentSeriesMaterializationResult(
            series=applied.series,
            revision=revision,
            run=run,
            scheduled_starts_at=first_result.scheduled_starts_at,
            scheduled_date=revision.end_date,
            attempt_number=1,
            provenance_kind=AppointmentSeriesMaterializationResult.ProvenanceKind.NATIVE,
            outcome=AppointmentSeriesOccurrence.Outcome.SKIPPED,
            reason_code="invalid_local_date",
            reason="Локальная дата не должна подменяться.",
        )

        with self.assertRaisesMessage(
            DatabaseError,
            "series result local date does not match scheduled start",
        ), transaction.atomic():
            AppointmentSeriesMaterializationResult._base_manager.bulk_create(
                [invalid_result]
            )

    def test_series_service_requires_operator_before_writing(self):
        operation_key = uuid4()

        with self.assertRaises(PermissionDenied):
            program_series.create_group_series(
                self.preview(end_date=self.start_date),
                operation_key=operation_key,
                actor=self.specialist_user,
            )

        self.assertFalse(AppointmentSeries.objects.filter(operation_key=operation_key).exists())

    def test_series_history_protects_materialized_appointment(self):
        result = program_series.create_group_series(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
            actor=self.admin,
        )
        appointment = Appointment.objects.get(series=result.series)

        with self.assertRaises(ProtectedError):
            appointment.delete()

    def test_composition_preserves_selected_primary_participant_and_staff(self):
        preview = self.preview(
            blocks=[self.block2, self.block1],
            staff_members=[self.staff2, self.staff1],
            end_date=self.start_date,
        )

        result = program_series.create_group_series(
            preview,
            operation_key=uuid4(),
            actor=self.admin,
        )

        self.assertEqual(result.series.child_id, self.child2.pk)
        self.assertEqual(result.series.staff_member_id, self.staff2.pk)
        self.assertEqual(
            list(result.series.default_participants.values_list("child_id", flat=True)),
            [self.child2.pk, self.child1.pk],
        )
        self.assertEqual(
            list(
                result.series.default_staff_assignments.values_list(
                    "staff_member_id", flat=True
                )
            ),
            [self.staff2.pk, self.staff1.pk],
        )

    def test_preview_rejects_invalid_weekday_and_appointment_status(self):
        with self.assertRaises(ValidationError):
            self.preview(weekdays={7})
        with self.assertRaises(ValidationError):
            self.preview(default_appointment_status="unknown")

    def test_plan_limit_marks_remaining_dates_skipped(self):
        self.block1.planned_sessions = 1
        self.block1.save(update_fields=["planned_sessions", "updated_at"])

        preview = self.preview()

        self.assertEqual(preview.ready_count, 1)
        self.assertEqual(preview.dates[1].reason_code, "plan_limit")

    def test_individual_wizard_does_not_schedule_beyond_block_plan(self):
        self.block1.planned_sessions = 0
        self.block1.save(update_fields=["planned_sessions", "updated_at"])

        preview = program_wizard.suggest_program_block_slots(
            self.block1,
            date_from=self.start_date,
            date_to=self.start_date,
            weekdays={self.start_date.weekday()},
            time_from=time(10, 0),
            time_until=time(12, 0),
            duration_minutes=45,
            staff_member=self.staff1,
            room=self.room,
            requested_count=1,
        )

        self.assertEqual(preview.allowed_count, 0)
        self.assertEqual(preview.slots, [])
        self.assertTrue(preview.limited_by_plan)

    def test_individual_wizard_rechecks_plan_and_funding_during_apply(self):
        preview = program_wizard.suggest_program_block_slots(
            self.block1,
            date_from=self.start_date,
            date_to=self.start_date,
            weekdays={self.start_date.weekday()},
            time_from=time(10, 0),
            time_until=time(10, 45),
            duration_minutes=45,
            staff_member=self.staff1,
            room=self.room,
            requested_count=1,
        )
        self.account1.initial_amount = Decimal("0")
        self.account1.save(update_fields=["initial_amount", "updated_at"])

        with self.assertRaisesMessage(ValidationError, "Доступная оплата изменилась"):
            program_wizard.create_schedule_from_preview(preview)

        self.account1.initial_amount = Decimal("10")
        self.account1.save(update_fields=["initial_amount", "updated_at"])
        preview = program_wizard.suggest_program_block_slots(
            self.block1,
            date_from=self.start_date,
            date_to=self.start_date,
            weekdays={self.start_date.weekday()},
            time_from=time(10, 0),
            time_until=time(10, 45),
            duration_minutes=45,
            staff_member=self.staff1,
            room=self.room,
            requested_count=1,
        )
        self.block1.planned_sessions = 0
        self.block1.save(update_fields=["planned_sessions", "updated_at"])

        with self.assertRaisesMessage(ValidationError, "План каскада изменился"):
            program_wizard.create_schedule_from_preview(preview)

    def test_individual_wizard_rechecks_program_lifecycle_during_apply(self):
        preview = program_wizard.suggest_program_block_slots(
            self.block1,
            date_from=self.start_date,
            date_to=self.start_date,
            weekdays={self.start_date.weekday()},
            time_from=time(10, 0),
            time_until=time(10, 45),
            duration_minutes=45,
            staff_member=self.staff1,
            room=self.room,
            requested_count=1,
        )
        self.program1.status = TreatmentProgram.Status.PAUSED
        self.program1.save(update_fields=["status", "updated_at"])

        with self.assertRaisesMessage(ValidationError, "Программа изменилась"):
            program_wizard.create_schedule_from_preview(preview)

    def test_unpaid_override_requires_reserved_status_and_reason(self):
        self.account2.initial_amount = Decimal("0")
        self.account2.save(update_fields=["initial_amount", "updated_at"])

        preview = self.preview()
        self.assertEqual(preview.ready_count, 0)
        self.assertTrue(all(item.reason_code == "funding_limit" for item in preview.dates))
        with self.assertRaises(ValidationError):
            self.preview(allow_unpaid_reserve=True)
        override = self.preview(
            allow_unpaid_reserve=True,
            default_appointment_status=Appointment.Status.RESERVED,
            override_reason="Решение администратора о временной брони",
        )
        self.assertEqual(override.ready_count, 2)

    def test_repeated_operation_key_reuses_series_without_duplicates(self):
        key = uuid4()
        preview = self.preview()

        first = program_series.create_group_series(preview, operation_key=key, actor=self.admin)
        second = program_series.create_group_series(preview, operation_key=key, actor=self.admin)

        self.assertFalse(first.reused_series)
        self.assertTrue(second.reused_series)
        self.assertEqual(second.created_count, 0)
        self.assertEqual(second.unchanged_count, 2)
        self.assertEqual(AppointmentSeries.objects.filter(operation_key=key).count(), 1)
        self.assertEqual(Appointment.objects.filter(series=first.series).count(), 2)
        self.assertEqual(AppointmentSeriesOccurrence.objects.filter(series=first.series).count(), 2)

    def test_sequence_number_is_not_reused_after_cancellation(self):
        preview = self.preview(end_date=self.start_date)
        result = program_series.create_group_series(preview, operation_key=uuid4(), actor=self.admin)
        first = AppointmentParticipant.objects.get(
            appointment__series=result.series,
            child=self.child1,
        )
        first.appointment_status = Appointment.Status.CANCELLED
        first.save(update_fields=["appointment_status", "updated_at"])
        later_start = _local(self.start_date + timedelta(days=1), time(12, 0))
        later = Appointment.objects.create(
            child=self.child1,
            staff_member=self.staff1,
            service=self.service,
            starts_at=later_start,
            ends_at=later_start + timedelta(minutes=45),
            program_block=self.block1,
        )

        self.assertEqual(later.primary_participant.sequence_number, 2)

    def test_legacy_individual_series_uses_monotonic_sequence_allocator(self):
        cancelled_start = _local(self.start_date, time(9, 0))
        Appointment.objects.create(
            child=self.child1,
            staff_member=self.staff1,
            service=self.service,
            starts_at=cancelled_start,
            ends_at=cancelled_start + timedelta(minutes=45),
            status=Appointment.Status.CANCELLED,
            program_block=self.block1,
        )
        day = self.start_date + timedelta(days=1)
        weekday_label = next(
            label for label, value in AppointmentSeries.DAY_MAP.items() if value == day.weekday()
        )
        series = AppointmentSeries.objects.create(
            child=self.child1,
            service=self.service,
            staff_member=self.staff1,
            room=self.room,
            program_block=self.block1,
            title="Legacy individual series",
            start_date=day,
            end_date=day,
            days_of_week=weekday_label,
            time=time(9, 0),
            duration_minutes=45,
            status=AppointmentSeries.Status.ACTIVE,
        )

        self.assertEqual(series.materialize_series(actor=self.admin), 1)

        created = Appointment.objects.get(series=series)
        self.assertEqual(created.primary_participant.sequence_number, 2)

    def test_expand_gate_does_not_relabel_legacy_occurrence_as_native(self):
        day = self.start_date + timedelta(days=1)
        weekday_label = next(
            label for label, value in AppointmentSeries.DAY_MAP.items() if value == day.weekday()
        )
        series = AppointmentSeries.objects.create(
            child=self.child1,
            service=self.service,
            staff_member=self.staff1,
            room=self.room,
            program_block=self.block1,
            title="Legacy series awaiting backfill",
            start_date=day,
            end_date=day,
            days_of_week=weekday_label,
            time=time(9, 0),
            duration_minutes=45,
            status=AppointmentSeries.Status.ACTIVE,
        )
        AppointmentSeriesOccurrence.objects.create(
            series=series,
            scheduled_starts_at=_local(day, time(9, 0)),
            outcome=AppointmentSeriesOccurrence.Outcome.SKIPPED,
            reason_code="legacy_backfill",
            reason="Исторический результат до dual-write.",
        )

        with self.assertRaisesMessage(ValidationError, "legacy backfill"):
            series.materialize_series(actor=self.admin)

        series.refresh_from_db()
        self.assertIsNone(series.current_revision_id)
        self.assertFalse(series.default_participants.exists())
        self.assertFalse(series.default_staff_assignments.exists())
        self.assertFalse(series.materialization_results.exists())

    def test_normal_planning_fails_closed_without_usable_account(self):
        self.block2.balance_account = None
        self.block2.save(update_fields=["balance_account", "updated_at"])

        preview = self.preview()

        self.assertEqual(preview.ready_count, 0)
        self.assertTrue(
            all(item.reason_code == "funding_unavailable" for item in preview.dates)
        )
        unpaid = self.preview(
            allow_unpaid_reserve=True,
            default_appointment_status=Appointment.Status.RESERVED,
            override_reason="Администратор разрешил временную неоплаченную бронь",
        )
        self.assertEqual(unpaid.ready_count, 2)

    def test_funding_reservations_are_shared_across_blocks_of_one_account(self):
        self.account1.initial_amount = Decimal("1")
        self.account1.save(update_fields=["initial_amount", "updated_at"])
        another_program = TreatmentProgram.objects.create(
            child=self.child1,
            title="Другая программа того же счета",
            status=TreatmentProgram.Status.ACTIVE,
        )
        another_block = ProgramBlock.objects.create(
            program=another_program,
            number=1,
            title="Другой каскад того же счета",
            service=self.service,
            staff_member=self.staff1,
            planned_sessions=2,
            balance_account=self.account1,
        )
        starts_at = _local(self.start_date, time(9, 0))
        Appointment.objects.create(
            child=self.child1,
            staff_member=self.staff1,
            service=self.service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
            status=Appointment.Status.RESERVED,
            billing_account=self.account1,
            program_block=another_block,
        )

        self.assertEqual(program_wizard.funded_sessions_remaining(self.block1), 0)
        self.assertEqual(self.preview().ready_count, 0)

    def test_inactive_or_expired_account_requires_explicit_unpaid_reserve(self):
        self.account2.status = BalanceAccount.Status.PAUSED
        self.account2.save(update_fields=["status", "updated_at"])
        preview = self.preview()
        self.assertEqual(preview.ready_count, 0)
        self.assertTrue(
            all(item.reason_code == "funding_unavailable" for item in preview.dates)
        )

        self.account2.status = BalanceAccount.Status.ACTIVE
        self.account2.valid_until = self.start_date - timedelta(days=1)
        self.account2.save(update_fields=["status", "valid_until", "updated_at"])
        expired = self.preview()
        self.assertEqual(expired.ready_count, 0)
        self.assertTrue(
            all(item.reason_code == "funding_unavailable" for item in expired.dates)
        )

    def test_group_series_requires_active_program_and_respects_program_dates(self):
        self.program2.status = TreatmentProgram.Status.PAUSED
        self.program2.save(update_fields=["status", "updated_at"])
        with self.assertRaisesMessage(ValidationError, "активных программ"):
            self.preview()

        self.program2.status = TreatmentProgram.Status.ACTIVE
        self.program2.starts_on = self.start_date + timedelta(days=1)
        self.program2.save(update_fields=["status", "starts_on", "updated_at"])
        with self.assertRaisesMessage(ValidationError, "начинается раньше программы"):
            self.preview()

    def test_group_series_rechecks_program_lifecycle_during_apply(self):
        preview = self.preview(end_date=self.start_date)
        self.program2.status = TreatmentProgram.Status.PAUSED
        self.program2.save(update_fields=["status", "updated_at"])

        result = program_series.create_group_series(
            preview,
            operation_key=uuid4(),
            actor=self.admin,
        )

        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.skipped_count, 1)
        occurrence = result.series.occurrences.get()
        self.assertEqual(occurrence.reason_code, "program_unavailable")
        self.assertIsNone(occurrence.appointment_id)

    def test_occurrence_and_series_history_cannot_be_changed_or_deleted(self):
        result = program_series.create_group_series(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
            actor=self.admin,
        )
        occurrence = result.series.occurrences.get()
        occurrence.reason = "Попытка изменить историю"

        with self.assertRaisesMessage(ValidationError, "неизменяем"):
            occurrence.save()
        with self.assertRaisesMessage(ValidationError, "нельзя удалить"):
            occurrence.delete()
        with self.assertRaisesMessage(ValidationError, "Серию с историей нельзя удалить"):
            result.series.delete()

    @skipUnless(connection.vendor == "postgresql", "DB guard проверяется на PostgreSQL.")
    def test_occurrence_history_is_immutable_through_queryset_writes(self):
        result = program_series.create_group_series(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
            actor=self.admin,
        )
        occurrence = result.series.occurrences.get()

        with self.assertRaisesMessage(
            DatabaseError, "occurrences are immutable"
        ), transaction.atomic():
            AppointmentSeriesOccurrence.objects.filter(pk=occurrence.pk).update(
                reason="Bypass attempt"
            )
        with self.assertRaises(ProtectedError), transaction.atomic():
            AppointmentSeriesOccurrence.objects.filter(pk=occurrence.pk).delete()

    def test_group_series_view_preview_create_and_detail(self):
        key = uuid4()
        url = reverse("program_block_group_series_create", args=[self.block1.pk])

        preview_response = self.client.post(url, self.form_payload(operation_key=key))
        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(preview_response.context["preview"].ready_count, 2)
        self.assertContains(preview_response, "Предварительная проверка")
        self.assertContains(preview_response, self.child1.full_name)
        self.assertContains(preview_response, self.service.name)

        create_response = self.client.post(
            url,
            self.form_payload(operation_key=key, action="create"),
        )
        series = AppointmentSeries.objects.get(operation_key=key)
        self.assertRedirects(
            create_response,
            reverse("appointment_series_detail", args=[series.pk]),
        )
        detail = self.client.get(reverse("appointment_series_detail", args=[series.pk]))
        self.assertContains(detail, "Постоянная группа")
        self.assertContains(detail, self.child1.full_name)
        self.assertContains(detail, self.child2.full_name)
        self.assertContains(detail, self.staff2.full_name)
        self.assertContains(detail, "Открыть")

    def test_group_series_form_requires_primary_staff_in_selected_composition(self):
        payload = self.form_payload()
        payload["staff_members"] = [str(self.staff1.pk)]
        payload["primary_staff_member"] = str(self.staff2.pk)

        response = self.client.post(
            reverse("program_block_group_series_create", args=[self.block1.pk]),
            payload,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            "primary_staff_member",
            "Основной специалист должен входить в состав серии.",
        )

    def test_specialist_cannot_open_group_series_admin_views(self):
        self.client.logout()
        self.client.force_login(self.specialist_user)

        response = self.client.get(
            reverse("program_block_group_series_create", args=[self.block1.pk])
        )

        self.assertEqual(response.status_code, 403)


class GroupProgramJoinTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            "group-join-admin",
            password="x",
            is_staff=True,
            is_superuser=True,
        )
        cls.specialist_user = User.objects.create_user("group-join-specialist", password="x")
        cls.children = [
            Child.objects.create(last_name="Join", first_name=f"Child {index}")
            for index in range(1, 4)
        ]
        cls.service = Service.objects.create(
            name="Join group service",
            code="JOIN-GROUP-SERVICE",
            default_duration_minutes=45,
            default_price=Decimal("900"),
        )
        cls.other_service = Service.objects.create(
            name="Other join service",
            code="OTHER-JOIN-SERVICE",
            default_duration_minutes=45,
        )
        cls.staff = StaffMember.objects.create(
            user=cls.specialist_user,
            full_name="Join group specialist",
        )
        cls.room = Room.objects.create(
            name="Join group room",
            allow_group_sessions=True,
            limit_staff_count=True,
            max_staff_count=2,
            limit_recipient_count=True,
            max_recipient_count=4,
        )
        cls.other_room = Room.objects.create(
            name="Join conflict room",
            allow_group_sessions=True,
            max_staff_count=2,
            max_recipient_count=4,
        )
        cls.funding = FundingSource.objects.create(
            name="Join group funding",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        cls.account = BalanceAccount.objects.create(
            child=cls.children[2],
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("5"),
        )
        cls.program = TreatmentProgram.objects.create(
            child=cls.children[2],
            title="Join group program",
            status=TreatmentProgram.Status.ACTIVE,
        )
        cls.block = ProgramBlock.objects.create(
            program=cls.program,
            number=1,
            title="Join group block",
            service=cls.service,
            staff_member=cls.staff,
            planned_sessions=4,
            balance_account=cls.account,
        )

    def setUp(self):
        self.client.force_login(self.admin)
        self.day = timezone.localdate() + timedelta(days=12)

    def create_target(self, *, day=None, hour=10, service=None, room=None):
        starts_at = _local(day or self.day, time(hour, 0))
        appointment = Appointment.objects.create(
            child=self.children[0],
            staff_member=self.staff,
            service=service or self.service,
            room=room or self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
            session_type=Appointment.SessionType.GROUP,
            title="Existing join group",
            status=Appointment.Status.CONFIRMED,
        )
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=self.children[1],
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        return appointment

    def payload(self, appointment, *, operation_key=None, action="join"):
        return {
            "operation_key": str(operation_key or uuid4()),
            "date_from": self.day.isoformat(),
            "date_to": (self.day + timedelta(days=2)).isoformat(),
            "appointments": [str(appointment.pk)],
            "action": action,
        }

    def test_preview_and_join_preserve_participant_program_data_without_ledger(self):
        target = self.create_target()
        preview = program_series.preview_group_joins(
            block=self.block,
            date_from=self.day,
            date_to=self.day,
        )

        self.assertEqual(preview.ready_count, 1)
        self.assertEqual(preview.candidates[0].recipient_count_after, 3)
        result = program_series.join_program_block_to_groups(
            block=self.block,
            appointments=[target],
            operation_key=uuid4(),
            actor=self.admin,
        )

        self.assertEqual(result.joined_count, 1)
        self.assertEqual(result.skipped_count, 0)
        participant = target.participants.get(child=self.children[2])
        self.assertEqual(participant.program_block, self.block)
        self.assertEqual(participant.billing_account, self.account)
        self.assertEqual(participant.sequence_number, 1)
        self.assertEqual(participant.billing_decision, Appointment.BillingDecision.UNDECIDED)
        self.assertFalse(LedgerEntry.objects.filter(appointment_participant=participant).exists())
        self.assertEqual(
            result.series.materialization_mode,
            AppointmentSeries.MaterializationMode.JOIN_EXISTING,
        )
        self.assertEqual(len(result.series.operation_fingerprint), 64)
        self.assertEqual(result.series.default_participants.count(), 1)
        self.assertEqual(result.series.default_staff_assignments.count(), 0)
        occurrence = result.series.occurrences.get()
        self.assertEqual(occurrence.outcome, AppointmentSeriesOccurrence.Outcome.JOINED)
        self.assertEqual(occurrence.appointment, target)
        self.assertEqual(occurrence.appointment_participant, participant)
        result.series.refresh_from_db()
        revision = result.series.current_revision
        self.assertEqual(
            revision.provenance_kind,
            AppointmentSeriesRevision.ProvenanceKind.NATIVE,
        )
        self.assertEqual(revision.participants.count(), 1)
        self.assertEqual(revision.staff_assignments.count(), 0)
        run = result.series.materialization_runs.get()
        materialization_result = run.results.get()
        self.assertEqual(materialization_result.outcome, occurrence.outcome)
        self.assertEqual(
            materialization_result.compatibility_occurrence_id,
            occurrence.pk,
        )
        self.assertEqual(materialization_result.appointment_participant_id, participant.pk)
        self.assertEqual(run.events.get().result_count, 1)
        with self.assertRaises(ProtectedError):
            participant.delete()

        local_start = timezone.localtime(target.starts_at)
        edit_response = self.client.post(
            reverse("appointment_edit", args=[target.pk]),
            {
                "child": self.children[0].pk,
                "participants": [self.children[0].pk, self.children[1].pk],
                "service": self.service.pk,
                "staff_member": self.staff.pk,
                "staff_members": [self.staff.pk],
                "room": self.room.pk,
                "session_type": Appointment.SessionType.GROUP,
                "status": Appointment.Status.CONFIRMED,
                "date": local_start.date().isoformat(),
                "time": local_start.strftime("%H:%M"),
                "duration_minutes": "45",
            },
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, "История присоединения неизменяема")
        self.assertTrue(target.participants.filter(pk=participant.pk).exists())

    def test_repeat_is_idempotent_and_changed_payload_is_rejected(self):
        first = self.create_target()
        second = self.create_target(day=self.day + timedelta(days=1))
        operation_key = uuid4()

        initial = program_series.join_program_block_to_groups(
            block=self.block,
            appointments=[first],
            operation_key=operation_key,
            actor=self.admin,
        )
        repeated = program_series.join_program_block_to_groups(
            block=self.block,
            appointments=[first],
            operation_key=operation_key,
            actor=self.admin,
        )

        self.assertEqual(initial.joined_count, 1)
        self.assertTrue(repeated.reused_series)
        self.assertEqual(repeated.unchanged_count, 1)
        self.assertEqual(first.participants.filter(child=self.children[2]).count(), 1)
        with self.assertRaisesMessage(ValidationError, "другого набора"):
            program_series.join_program_block_to_groups(
                block=self.block,
                appointments=[second],
                operation_key=operation_key,
                actor=self.admin,
            )

    def test_preview_blocks_capacity_recipient_conflict_and_missing_funding(self):
        target = self.create_target()
        self.room.max_recipient_count = 2
        self.room.save(update_fields=["max_recipient_count", "updated_at"])

        capacity_preview = program_series.preview_group_joins(
            block=self.block,
            date_from=self.day,
            date_to=self.day,
        )
        self.assertEqual(capacity_preview.candidates[0].reason_code, "capacity")

        self.room.max_recipient_count = 4
        self.room.save(update_fields=["max_recipient_count", "updated_at"])
        conflict_report = program_series.scheduling.ConflictReport(
            child_conflict=target,
            staff_conflict=None,
            room_conflict=None,
        )
        with patch.object(
            program_series.scheduling,
            "find_overlaps",
            return_value=conflict_report,
        ):
            conflict_preview = program_series.preview_group_joins(
                block=self.block,
                date_from=self.day,
                date_to=self.day,
            )
        self.assertEqual(conflict_preview.candidates[0].reason_code, "recipient_conflict")

        self.account.initial_amount = Decimal("0")
        self.account.save(update_fields=["initial_amount", "updated_at"])
        funding_preview = program_series.preview_group_joins(
            block=self.block,
            date_from=self.day,
            date_to=self.day,
        )
        self.assertEqual(funding_preview.candidates[0].reason_code, "funding_limit")

    def test_join_fails_closed_when_block_account_changes_after_selection(self):
        target = self.create_target()
        series, _ = program_series._create_join_series_definition(
            self.block,
            (target,),
            operation_key=uuid4(),
        )
        revision, _ = series_revisions.ensure_initial_revision(
            series,
            actor=self.admin,
        )
        replacement = BalanceAccount.objects.create(
            child=self.children[2],
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("5"),
        )
        self.block.balance_account = replacement
        self.block.save(update_fields=["balance_account", "updated_at"])

        occurrence, created = program_series._join_one_appointment(
            series,
            revision,
            target,
            actor=self.admin,
        )

        self.assertTrue(created)
        self.assertEqual(occurrence.outcome, AppointmentSeriesOccurrence.Outcome.SKIPPED)
        self.assertEqual(occurrence.reason_code, "funding_changed")
        self.assertFalse(target.participants.filter(child=self.children[2]).exists())

    def test_operation_key_rejects_same_appointment_after_time_change(self):
        target = self.create_target()
        operation_key = uuid4()
        program_series._create_join_series_definition(
            self.block,
            (target,),
            operation_key=operation_key,
        )
        moved_start = target.starts_at + timedelta(hours=1)
        Appointment.objects.filter(pk=target.pk).update(
            starts_at=moved_start,
            ends_at=moved_start + timedelta(minutes=45),
        )
        target.refresh_from_db()

        with self.assertRaisesMessage(ValidationError, "другого набора"):
            program_series.join_program_block_to_groups(
                block=self.block,
                appointments=[target],
                operation_key=operation_key,
                actor=self.admin,
            )
        self.assertFalse(target.participants.filter(child=self.children[2]).exists())

    def test_join_view_search_apply_detail_and_permissions(self):
        target = self.create_target()
        operation_key = uuid4()
        url = reverse("program_block_group_join", args=[self.block.pk])

        search_response = self.client.post(
            url,
            self.payload(target, operation_key=operation_key, action="search"),
        )
        self.assertEqual(search_response.status_code, 200)
        self.assertContains(search_response, "Existing join group")
        self.assertContains(search_response, "готово")

        join_response = self.client.post(
            url,
            self.payload(target, operation_key=operation_key),
        )
        series = AppointmentSeries.objects.get(operation_key=operation_key)
        self.assertRedirects(
            join_response,
            reverse("appointment_series_detail", args=[series.pk]),
        )
        detail = self.client.get(reverse("appointment_series_detail", args=[series.pk]))
        self.assertContains(detail, "Присоединять к существующим")
        self.assertContains(detail, "Фактические данные")
        self.assertContains(detail, self.children[2].full_name)
        self.assertContains(detail, "Присоединено")

        repeat_response = self.client.post(
            url,
            self.payload(target, operation_key=operation_key),
            follow=True,
        )
        self.assertContains(
            repeat_response,
            "Повторный запрос распознан без создания дублей",
        )
        self.assertEqual(target.participants.filter(child=self.children[2]).count(), 1)

        self.client.logout()
        self.client.force_login(self.specialist_user)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_join_existing_mode_requires_group_and_available_funding(self):
        series = AppointmentSeries(
            child=self.children[2],
            service=self.service,
            staff_member=self.staff,
            room=self.room,
            program_block=self.block,
            title="Invalid join series",
            start_date=self.day,
            end_date=self.day,
            days_of_week="ПН",
            time=time(10, 0),
            duration_minutes=45,
            session_type=Appointment.SessionType.INDIVIDUAL,
            materialization_mode=AppointmentSeries.MaterializationMode.JOIN_EXISTING,
        )
        with self.assertRaisesMessage(ValidationError, "только к групповым"):
            series.full_clean()

    @skipUnless(
        connection.vendor == "postgresql",
        "DB-ограничение истории присоединения проверяется только на PostgreSQL.",
    )
    def test_database_rejects_joined_occurrence_without_participant(self):
        target = self.create_target()
        series, _ = program_series._create_join_series_definition(
            self.block,
            (target,),
            operation_key=uuid4(),
        )

        with self.assertRaises(DatabaseError), transaction.atomic():
            AppointmentSeriesOccurrence.objects.bulk_create(
                [
                    AppointmentSeriesOccurrence(
                        series=series,
                        scheduled_starts_at=target.starts_at,
                        appointment=target,
                        outcome=AppointmentSeriesOccurrence.Outcome.JOINED,
                    )
                ]
            )

        other_target = self.create_target(day=self.day + timedelta(days=1))
        unrelated_participant = other_target.primary_participant
        with self.assertRaises(DatabaseError), transaction.atomic():
            AppointmentSeriesOccurrence.objects.bulk_create(
                [
                    AppointmentSeriesOccurrence(
                        series=series,
                        scheduled_starts_at=target.starts_at,
                        appointment=target,
                        appointment_participant=unrelated_participant,
                        outcome=AppointmentSeriesOccurrence.Outcome.JOINED,
                    )
                ]
            )


class GroupProgramSeriesPostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            "group-series-concurrency-admin",
            password="x",
            is_staff=True,
            is_superuser=True,
        )
        self.children = [
            Child.objects.create(last_name="Concurrent", first_name=f"Child {index}")
            for index in range(1, 5)
        ]
        self.staff_members = [
            StaffMember.objects.create(full_name=f"Concurrent group specialist {index}")
            for index in range(1, 3)
        ]
        self.service = Service.objects.create(
            name="Concurrent group service",
            code="CONCURRENT-GROUP-SERIES",
            default_duration_minutes=30,
        )
        self.room = Room.objects.create(
            name="Concurrent group room",
            allow_group_sessions=True,
            max_staff_count=2,
            max_recipient_count=2,
        )
        funding = FundingSource.objects.create(
            name="Concurrent group funding",
            source_type=FundingSource.SourceType.PERSONAL,
            transfer_policy=FundingSource.TransferPolicy.WITHIN_CHILD,
        )
        self.blocks = []
        for position, child in enumerate(self.children, start=1):
            account = BalanceAccount.objects.create(
                child=child,
                funding_source=funding,
                unit=BalanceAccount.Unit.SESSIONS,
                service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
                service=self.service,
                initial_amount=Decimal("10"),
            )
            program = TreatmentProgram.objects.create(
                child=child,
                title=f"Concurrent program {position}",
                status=TreatmentProgram.Status.ACTIVE,
            )
            self.blocks.append(
                ProgramBlock.objects.create(
                    program=program,
                    number=1,
                    title=f"Concurrent block {position}",
                    service=self.service,
                    staff_member=self.staff_members[0 if position <= 2 else 1],
                    planned_sessions=5,
                    balance_account=account,
                )
            )
        self.day = timezone.localdate() + timedelta(days=20)

    def _create_join_target(self, *, hour=12):
        starts_at = _local(self.day, time(hour, 0))
        return Appointment.objects.create(
            child=self.children[0],
            staff_member=self.staff_members[0],
            service=self.service,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            session_type=Appointment.SessionType.GROUP,
            title="Concurrent existing group",
            status=Appointment.Status.CONFIRMED,
        )

    def _join_in_thread(self, block_id, appointment_id, operation_key, barrier, outcomes):
        close_old_connections()
        try:
            block = ProgramBlock.objects.select_related(
                "program__child",
                "service",
                "staff_member",
                "balance_account",
            ).get(pk=block_id)
            appointment = Appointment.objects.select_related(
                "child",
                "service",
                "staff_member",
                "room",
            ).get(pk=appointment_id)
            barrier.wait(timeout=10)
            result = program_series.join_program_block_to_groups(
                block=block,
                appointments=[appointment],
                operation_key=operation_key,
                actor=self.admin,
            )
        except BaseException as exc:
            outcomes.put(exc)
        else:
            outcomes.put(
                (result.joined_count, result.skipped_count, result.unchanged_count)
            )
        finally:
            connection.close()

    def _run_competing_joins(self, specs):
        barrier = Barrier(2)
        outcomes = Queue()
        threads = [
            Thread(
                target=self._join_in_thread,
                args=(*spec, barrier, outcomes),
            )
            for spec in specs
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        return [outcomes.get_nowait() for _ in range(2)]

    def _preview_from_database(self, block_ids=None, staff_id=None):
        block_ids = block_ids or [block.pk for block in self.blocks[:2]]
        staff_id = staff_id or self.staff_members[0].pk
        blocks = list(
            ProgramBlock.objects.select_related(
                "program__child",
                "service",
                "staff_member",
                "balance_account",
            )
            .filter(pk__in=block_ids)
            .order_by("pk")
        )
        return program_series.preview_group_series(
            blocks=blocks,
            staff_members=[StaffMember.objects.get(pk=staff_id)],
            room=Room.objects.get(pk=self.room.pk),
            title="Concurrent group",
            start_date=self.day,
            end_date=self.day,
            weekdays={self.day.weekday()},
            start_time=time(10, 0),
            duration_minutes=30,
        )

    def _create_unmaterialized_series(self):
        series, _ = program_series._create_series_definition(
            self._preview_from_database(),
            operation_key=uuid4(),
        )
        series_revisions.ensure_initial_revision(series, actor=self.admin)
        return series

    def _run_competing_missing(self, series_id, operation_keys):
        barrier = Barrier(2)
        outcomes = Queue()

        def run(operation_key):
            close_old_connections()
            try:
                series = AppointmentSeries.objects.get(pk=series_id)
                barrier.wait(timeout=10)
                result = program_series.materialize_missing_series(
                    series,
                    operation_key=operation_key,
                    actor=self.admin,
                )
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put(
                    (
                        result.run.pk,
                        result.created_count,
                        result.unchanged_count,
                        result.reused_run,
                    )
                )
            finally:
                connection.close()

        threads = [Thread(target=run, args=(key,)) for key in operation_keys]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        return [outcomes.get_nowait() for _ in range(2)]

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентное создание серий проверяется только на PostgreSQL.",
    )
    def test_competing_series_cannot_overbook_room_or_duplicate_sequences(self):
        barrier = Barrier(2)
        outcomes = Queue()

        groups = (
            ([block.pk for block in self.blocks[:2]], self.staff_members[0].pk),
            ([block.pk for block in self.blocks[2:]], self.staff_members[1].pk),
        )

        def run(block_ids, staff_id):
            close_old_connections()
            try:
                preview = self._preview_from_database(block_ids, staff_id)
                barrier.wait(timeout=10)
                result = program_series.create_group_series(
                    preview,
                    operation_key=uuid4(),
                    actor=self.admin,
                )
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put((result.created_count, result.skipped_count))
            finally:
                connection.close()

        threads = [Thread(target=run, args=group) for group in groups]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual(sorted(results), [(0, 1), (1, 0)])
        appointments = Appointment.objects.filter(
            room=self.room,
            starts_at=_local(self.day, time(10, 0)),
        )
        self.assertEqual(appointments.count(), 1)
        sequences = list(
            AppointmentParticipant.objects.filter(appointment__in=appointments)
            .order_by("program_block_id")
            .values_list("sequence_number", flat=True)
        )
        self.assertEqual(sequences, [1, 1])

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентная нумерация каскада проверяется только на PostgreSQL.",
    )
    def test_concurrent_appointments_receive_distinct_monotonic_sequence_numbers(self):
        barrier = Barrier(2)
        outcomes = Queue()
        block_id = self.blocks[0].pk
        child_id = self.children[0].pk
        staff_id = self.staff_members[0].pk
        service_id = self.service.pk

        def run(hour):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                starts_at = _local(self.day, time(hour, 0))
                appointment = Appointment.objects.create(
                    child_id=child_id,
                    staff_member_id=staff_id,
                    service_id=service_id,
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(minutes=30),
                    program_block_id=block_id,
                )
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put(appointment.primary_participant.sequence_number)
            finally:
                connection.close()

        threads = [Thread(target=run, args=(10,)), Thread(target=run, args=(11,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertTrue(all(isinstance(item, int) for item in results), results)
        self.assertEqual(sorted(results), [1, 2])

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентная идемпотентность серии проверяется только на PostgreSQL.",
    )
    def test_concurrent_same_operation_key_creates_one_series_and_one_occurrence(self):
        barrier = Barrier(2)
        outcomes = Queue()
        operation_key = uuid4()

        def run():
            close_old_connections()
            try:
                preview = self._preview_from_database()
                barrier.wait(timeout=10)
                result = program_series.create_group_series(
                    preview,
                    operation_key=operation_key,
                    actor=self.admin,
                )
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put((result.created_count, result.unchanged_count))
            finally:
                connection.close()

        threads = [Thread(target=run), Thread(target=run)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual(sorted(results), [(0, 1), (1, 0)])
        self.assertEqual(AppointmentSeries.objects.filter(operation_key=operation_key).count(), 1)
        series = AppointmentSeries.objects.get(operation_key=operation_key)
        self.assertEqual(series.occurrences.count(), 1)
        self.assertEqual(Appointment.objects.filter(series=series).count(), 1)

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентный missing_only проверяется только на PostgreSQL.",
    )
    def test_concurrent_missing_only_same_key_reuses_one_run(self):
        series = self._create_unmaterialized_series()
        operation_key = uuid4()

        results = self._run_competing_missing(
            series.pk,
            [operation_key, operation_key],
        )

        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual(len({item[0] for item in results}), 1)
        self.assertEqual(
            sorted((item[1], item[2], item[3]) for item in results),
            [(1, 0, False), (1, 0, True)],
        )
        self.assertEqual(series.materialization_runs.count(), 1)
        run = series.materialization_runs.get()
        self.assertEqual(run.results.count(), 1)
        self.assertEqual(run.events.count(), 1)
        self.assertEqual(series.occurrences.count(), 1)
        self.assertEqual(Appointment.objects.filter(series=series).count(), 1)

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентные missing_only runs проверяются только на PostgreSQL.",
    )
    def test_concurrent_missing_only_runs_form_one_attempt_chain(self):
        series = self._create_unmaterialized_series()

        results = self._run_competing_missing(
            series.pk,
            [uuid4(), uuid4()],
        )

        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual(
            sorted((item[1], item[2]) for item in results),
            [(0, 1), (1, 0)],
        )
        self.assertEqual(series.materialization_runs.count(), 2)
        self.assertEqual(series.occurrences.count(), 1)
        self.assertEqual(Appointment.objects.filter(series=series).count(), 1)
        self.assertEqual(
            list(
                series.materialization_results.order_by("attempt_number").values_list(
                    "attempt_number",
                    "outcome",
                )
            ),
            [
                (1, AppointmentSeriesOccurrence.Outcome.CREATED),
                (2, AppointmentSeriesOccurrence.Outcome.UNCHANGED),
            ],
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "Гонка interrupt и missing_only writer проверяется только на PostgreSQL.",
    )
    def test_interrupt_and_missing_writer_leave_consistent_event_counts(self):
        series = self._create_unmaterialized_series()
        series.refresh_from_db()
        operation_key = uuid4()
        run, _ = series_revisions.get_or_create_missing_run(
            series,
            series.current_revision,
            operation_key=operation_key,
            actor=self.admin,
            date_from=self.day,
            date_to=self.day,
            expected_result_count=1,
        )
        barrier = Barrier(2)
        outcomes = Queue()

        def write_result():
            close_old_connections()
            try:
                selected_series = AppointmentSeries.objects.get(pk=series.pk)
                selected_revision = AppointmentSeriesRevision.objects.get(
                    pk=run.revision_id
                )
                selected_run = AppointmentSeriesMaterializationRun.objects.get(
                    pk=run.pk
                )
                barrier.wait(timeout=10)
                program_series._materialize_missing_date(
                    selected_series,
                    selected_revision,
                    selected_run,
                    _local(self.day, time(10, 0)),
                    actor=self.admin,
                )
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put(("writer", "recorded"))
            finally:
                connection.close()

        def interrupt():
            close_old_connections()
            try:
                selected_run = AppointmentSeriesMaterializationRun.objects.get(
                    pk=run.pk
                )
                barrier.wait(timeout=10)
                event = series_revisions.interrupt_run(
                    selected_run,
                    reason="Конкурентная остановка активного missing_only writer.",
                )
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put(("interrupt", event.result_count))
            finally:
                connection.close()

        threads = [Thread(target=write_result), Thread(target=interrupt)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertTrue(
            all(isinstance(item, tuple | ValidationError) for item in results),
            results,
        )
        run.refresh_from_db()
        interrupted = run.events.get()
        self.assertEqual(
            interrupted.event_type,
            AppointmentSeriesMaterializationRunEvent.EventType.INTERRUPTED,
        )
        self.assertEqual(interrupted.result_count, run.results.count())
        self.assertEqual(
            Appointment.objects.filter(series=series).count(),
            run.results.filter(
                outcome=AppointmentSeriesOccurrence.Outcome.CREATED
            ).count(),
        )

        recovered = program_series.materialize_missing_series(
            series,
            operation_key=operation_key,
            actor=self.admin,
        )

        self.assertEqual(recovered.created_count, 1)
        self.assertEqual(run.results.count(), 1)
        self.assertEqual(Appointment.objects.filter(series=series).count(), 1)
        self.assertEqual(
            list(run.events.order_by("event_number").values_list("event_type", flat=True)),
            [
                AppointmentSeriesMaterializationRunEvent.EventType.INTERRUPTED,
                AppointmentSeriesMaterializationRunEvent.EventType.RESUMED,
                AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED,
            ],
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентное присоединение к последнему месту проверяется только на PostgreSQL.",
    )
    def test_competing_group_joins_cannot_take_the_same_last_room_place(self):
        target = self._create_join_target()

        results = self._run_competing_joins(
            [
                (self.blocks[1].pk, target.pk, uuid4()),
                (self.blocks[2].pk, target.pk, uuid4()),
            ]
        )

        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual(sorted(results), [(0, 1, 0), (1, 0, 0)])
        self.assertEqual(target.participants.count(), 2)
        self.assertEqual(
            AppointmentSeriesOccurrence.objects.filter(
                appointment=target,
                outcome=AppointmentSeriesOccurrence.Outcome.JOINED,
            ).count(),
            1,
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "Идемпотентность присоединения проверяется только на PostgreSQL.",
    )
    def test_concurrent_same_join_operation_key_creates_one_participant(self):
        target = self._create_join_target()
        operation_key = uuid4()

        results = self._run_competing_joins(
            [
                (self.blocks[1].pk, target.pk, operation_key),
                (self.blocks[1].pk, target.pk, operation_key),
            ]
        )

        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual(sorted(results), [(0, 0, 1), (1, 0, 0)])
        self.assertEqual(
            target.participants.filter(child=self.children[1]).count(),
            1,
        )
        series = AppointmentSeries.objects.get(operation_key=operation_key)
        self.assertEqual(series.occurrences.count(), 1)

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентный лимит оплаты присоединений проверяется только на PostgreSQL.",
    )
    def test_shared_last_funded_session_allows_only_one_join(self):
        first_target = self._create_join_target(hour=12)
        second_target = self._create_join_target(hour=13)
        block = self.blocks[1]
        block.balance_account.initial_amount = Decimal("1")
        block.balance_account.save(update_fields=["initial_amount", "updated_at"])

        results = self._run_competing_joins(
            [
                (block.pk, first_target.pk, uuid4()),
                (block.pk, second_target.pk, uuid4()),
            ]
        )

        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual(sorted(results), [(0, 1, 0), (1, 0, 0)])
        joined = AppointmentParticipant.objects.filter(
            program_block=block,
            child=self.children[1],
        )
        self.assertEqual(joined.count(), 1)
        self.assertEqual(joined.get().sequence_number, 1)

    @skipUnless(
        connection.vendor == "postgresql",
        "Порядок блокировок присоединения и переноса проверяется только на PostgreSQL.",
    )
    def test_join_and_block_transfer_complete_without_deadlock(self):
        target = self._create_join_target()
        block = self.blocks[1]
        source = BalanceAccount.objects.create(
            child=self.children[1],
            funding_source=block.balance_account.funding_source,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("2"),
        )
        barrier = Barrier(2)
        outcomes = Queue()

        def join_group():
            close_old_connections()
            try:
                selected_block = ProgramBlock.objects.select_related(
                    "program__child", "service", "balance_account"
                ).get(pk=block.pk)
                selected_target = Appointment.objects.select_related(
                    "child", "service", "staff_member", "room"
                ).get(pk=target.pk)
                barrier.wait(timeout=10)
                result = program_series.join_program_block_to_groups(
                    block=selected_block,
                    appointments=[selected_target],
                    operation_key=uuid4(),
                    actor=self.admin,
                )
                outcomes.put(("join", result.joined_count))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        def transfer_funds():
            close_old_connections()
            try:
                selected_block = ProgramBlock.objects.get(pk=block.pk)
                from_account = BalanceAccount.objects.get(pk=source.pk)
                to_account = BalanceAccount.objects.get(pk=block.balance_account_id)
                barrier.wait(timeout=10)
                transfer = billing_svc.record_balance_transfer(
                    from_account=from_account,
                    to_account=to_account,
                    amount=Decimal("1"),
                    reason="Concurrent transfer while joining a group.",
                    program_block=selected_block,
                    idempotency_key=uuid4(),
                )
                outcomes.put(("transfer", transfer.pk))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        threads = [Thread(target=join_group), Thread(target=transfer_funds)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual({item[0] for item in results}, {"join", "transfer"})
        self.assertEqual(target.participants.filter(child=self.children[1]).count(), 1)


class ProgramWizardPostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.child = Child.objects.create(last_name="Wizard", first_name="Concurrent")
        self.staff = StaffMember.objects.create(full_name="Wizard concurrent specialist")
        self.service = Service.objects.create(
            name="Wizard concurrent service",
            code="WIZARD-CONCURRENT",
            default_duration_minutes=30,
        )
        self.rooms = [
            Room.objects.create(name=f"Wizard concurrent room {index}")
            for index in range(1, 3)
        ]
        funding = FundingSource.objects.create(
            name="Wizard concurrent funding",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        self.account = BalanceAccount.objects.create(
            child=self.child,
            funding_source=funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("1"),
        )
        program = TreatmentProgram.objects.create(
            child=self.child,
            title="Wizard concurrent program",
            status=TreatmentProgram.Status.ACTIVE,
        )
        self.blocks = [
            ProgramBlock.objects.create(
                program=program,
                number=index,
                title=f"Wizard concurrent block {index}",
                service=self.service,
                staff_member=self.staff,
                planned_sessions=5,
                balance_account=self.account,
            )
            for index in range(1, 3)
        ]
        self.day = timezone.localdate() + timedelta(days=24)

    def _preview(self, block_id, room_id, hour):
        block = ProgramBlock.objects.select_related(
            "program__child", "service", "staff_member", "balance_account"
        ).get(pk=block_id)
        return program_wizard.suggest_program_block_slots(
            block,
            date_from=self.day,
            date_to=self.day,
            weekdays={self.day.weekday()},
            time_from=time(hour, 0),
            time_until=time(hour, 30),
            duration_minutes=30,
            staff_member=StaffMember.objects.get(pk=self.staff.pk),
            room=Room.objects.get(pk=room_id),
            requested_count=1,
        )

    def _run_competing_previews(self, specs):
        barrier = Barrier(2)
        outcomes = Queue()

        def run(block_id, room_id, hour):
            close_old_connections()
            try:
                preview = self._preview(block_id, room_id, hour)
                barrier.wait(timeout=10)
                result = program_wizard.create_schedule_from_preview(preview)
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put(len(result.appointments))
            finally:
                connection.close()

        threads = [Thread(target=run, args=spec) for spec in specs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        return [outcomes.get_nowait() for _ in range(2)]

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентный лимит общего счета проверяется только на PostgreSQL.",
    )
    def test_shared_account_cannot_reserve_two_blocks_concurrently(self):
        results = self._run_competing_previews(
            [
                (self.blocks[0].pk, self.rooms[0].pk, 10),
                (self.blocks[1].pk, self.rooms[1].pk, 11),
            ]
        )

        self.assertEqual(sum(item == 1 for item in results), 1, results)
        self.assertEqual(sum(isinstance(item, ValidationError) for item in results), 1, results)
        self.assertEqual(Appointment.objects.count(), 1)

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентный лимит плана проверяется только на PostgreSQL.",
    )
    def test_block_plan_cannot_be_exceeded_concurrently(self):
        self.account.initial_amount = Decimal("10")
        self.account.save(update_fields=["initial_amount", "updated_at"])
        block = self.blocks[0]
        block.planned_sessions = 1
        block.save(update_fields=["planned_sessions", "updated_at"])

        results = self._run_competing_previews(
            [
                (block.pk, self.rooms[0].pk, 10),
                (block.pk, self.rooms[1].pk, 11),
            ]
        )

        self.assertEqual(sum(item == 1 for item in results), 1, results)
        self.assertEqual(sum(isinstance(item, ValidationError) for item in results), 1, results)
        self.assertEqual(Appointment.objects.filter(program_block=block).count(), 1)


class GroupProgramSeriesMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("operations"))
        super().tearDown()

    def _create_native_series_history(self, apps, *, suffix):
        user_model = apps.get_model("auth", "User")
        child_model = apps.get_model("operations", "Child")
        staff_model = apps.get_model("operations", "StaffMember")
        service_model = apps.get_model("operations", "Service")
        room_model = apps.get_model("operations", "Room")
        series_model = apps.get_model("operations", "AppointmentSeries")
        revision_model = apps.get_model("operations", "AppointmentSeriesRevision")
        revision_participant_model = apps.get_model(
            "operations", "AppointmentSeriesRevisionParticipant"
        )
        revision_staff_model = apps.get_model(
            "operations", "AppointmentSeriesRevisionStaffAssignment"
        )
        run_model = apps.get_model(
            "operations", "AppointmentSeriesMaterializationRun"
        )

        actor = user_model.objects.create(
            username=f"series-migration-{suffix}",
            password="!",
            is_staff=True,
            is_superuser=True,
        )
        child = child_model.objects.create(
            last_name="Native",
            first_name=f"Series {suffix}",
        )
        staff = staff_model.objects.create(full_name=f"Native staff {suffix}")
        service = service_model.objects.create(
            name=f"Native service {suffix}",
            code=f"NATIVE-{suffix}",
        )
        room = room_model.objects.create(name=f"Native room {suffix}")
        day = timezone.localdate() + timedelta(days=40)
        series = series_model.objects.create(
            child=child,
            service=service,
            staff_member=staff,
            room=room,
            title=f"Native series {suffix}",
            start_date=day,
            end_date=day,
            days_of_week="ПН",
            time=time(10, 0),
            duration_minutes=30,
            session_type="individual",
            materialization_mode="create_appointments",
            default_appointment_status="proposed",
            status="active",
        )
        revision = revision_model.objects.create(
            series_id=series.pk,
            revision_number=1,
            event_type="created",
            provenance_kind="native",
            effective_from=day,
            title=series.title,
            service_id=service.pk,
            room_id=room.pk,
            start_date=day,
            end_date=day,
            days_of_week="ПН",
            time=time(10, 0),
            duration_minutes=30,
            session_type="individual",
            materialization_mode="create_appointments",
            default_appointment_status="proposed",
            allow_unpaid_reserve=False,
            allow_outside_availability=False,
            override_reason="",
            fingerprint="d" * 64,
            actor_id=actor.pk,
            actor_role_snapshot="director",
            reason="Native dual-write migration fixture.",
            decided_at=timezone.now(),
        )
        revision_participant_model.objects.create(
            revision_id=revision.pk,
            child_id=child.pk,
            position=1,
        )
        revision_staff_model.objects.create(
            revision_id=revision.pk,
            staff_member_id=staff.pk,
            role="primary",
        )
        series_model.objects.filter(pk=series.pk).update(
            current_revision_id=revision.pk
        )
        run = run_model.objects.create(
            series_id=series.pk,
            revision_id=revision.pk,
            operation_key=uuid4(),
            fingerprint="e" * 64,
            mode="initial",
            date_from=day,
            date_to=day,
            expected_result_count=0,
            actor_id=actor.pk,
            actor_role_snapshot="director",
            reason="Native dual-write migration fixture.",
        )
        return series, revision, run

    def _create_retry_target_history(self, apps, *, suffix, create_retry=True):
        series, revision, _ = self._create_native_series_history(
            apps,
            suffix=suffix,
        )
        run_model = apps.get_model(
            "operations", "AppointmentSeriesMaterializationRun"
        )
        result_model = apps.get_model(
            "operations", "AppointmentSeriesMaterializationResult"
        )
        target_model = apps.get_model("operations", "AppointmentSeriesRetryTarget")
        day = revision.effective_from
        starts_at = _local(day, time(10, 0))
        source_run = run_model.objects.create(
            series_id=series.pk,
            revision_id=revision.pk,
            operation_key=uuid4(),
            fingerprint="1" * 64,
            mode="missing_only",
            date_from=day,
            date_to=day,
            expected_result_count=1,
            actor_id=revision.actor_id,
            actor_role_snapshot="director",
            reason="Retry target source run.",
        )
        effective_skipped = result_model.objects.create(
            series_id=series.pk,
            revision_id=revision.pk,
            run_id=source_run.pk,
            scheduled_starts_at=starts_at,
            scheduled_date=day,
            attempt_number=1,
            provenance_kind="native",
            outcome="skipped",
            reason_code="fixture_skip",
            reason="Skipped result for frozen retry target.",
        )
        head_run = run_model.objects.create(
            series_id=series.pk,
            revision_id=revision.pk,
            operation_key=uuid4(),
            fingerprint="2" * 64,
            mode="missing_only",
            date_from=day,
            date_to=day,
            expected_result_count=1,
            actor_id=revision.actor_id,
            actor_role_snapshot="director",
            reason="Retry target chain head run.",
        )
        chain_head = result_model.objects.create(
            series_id=series.pk,
            revision_id=revision.pk,
            run_id=head_run.pk,
            scheduled_starts_at=starts_at,
            scheduled_date=day,
            attempt_number=2,
            provenance_kind="native",
            outcome="unchanged",
            reason_code="existing_result",
            reason="Transparent result before retry acceptance.",
            supersedes_id=effective_skipped.pk,
        )
        retry_run = None
        target = None
        if create_retry:
            retry_run = run_model.objects.create(
                series_id=series.pk,
                revision_id=revision.pk,
                operation_key=uuid4(),
                fingerprint="3" * 64,
                mode="retry_skipped",
                date_from=day,
                date_to=day,
                expected_result_count=1,
                actor_id=revision.actor_id,
                actor_role_snapshot="director",
                reason="Retry skipped date after conflict resolution.",
            )
            target = target_model.objects.create(
                run_id=retry_run.pk,
                scheduled_starts_at=starts_at,
                scheduled_date=day,
                chain_head_result_id=chain_head.pk,
                effective_skipped_result_id=effective_skipped.pk,
            )
        return (
            series,
            revision,
            retry_run,
            target,
            chain_head,
            effective_skipped,
        )

    def test_backfill_skips_complete_native_dual_write_state(self):
        target_before = [("operations", "0057_series_revision_expand")]
        target_after = [("operations", "0058_backfill_series_revisions")]
        executor = MigrationExecutor(connection)
        executor.migrate(target_before)
        apps = executor.loader.project_state(target_before).apps

        with transaction.atomic():
            series, revision, run = self._create_native_series_history(
                apps,
                suffix="mixed",
            )

        executor = MigrationExecutor(connection)
        executor.migrate(target_after)
        migrated_apps = executor.loader.project_state(target_after).apps
        revision_model = migrated_apps.get_model(
            "operations", "AppointmentSeriesRevision"
        )
        run_model = migrated_apps.get_model(
            "operations", "AppointmentSeriesMaterializationRun"
        )
        migrated_series = migrated_apps.get_model(
            "operations", "AppointmentSeries"
        ).objects.get(pk=series.pk)

        self.assertEqual(migrated_series.current_revision_id, revision.pk)
        self.assertEqual(revision_model.objects.filter(series_id=series.pk).count(), 1)
        self.assertEqual(run_model.objects.filter(series_id=series.pk).count(), 1)
        self.assertEqual(run_model.objects.get(pk=run.pk).mode, "initial")
        self.assertFalse(
            revision_model.objects.filter(
                series_id=series.pk,
                event_type="legacy_import",
            ).exists()
        )

    @skipUnless(connection.vendor == "postgresql", "Reverse guard проверяется на PostgreSQL.")
    def test_native_history_blocks_guard_removal_and_keeps_triggers_installed(self):
        target_with_guards = [("operations", "0059_series_revision_guards")]
        target_without_guards = [("operations", "0058_backfill_series_revisions")]
        executor = MigrationExecutor(connection)
        executor.migrate(target_with_guards)
        apps = executor.loader.project_state(target_with_guards).apps

        with transaction.atomic():
            _, revision, _ = self._create_native_series_history(
                apps,
                suffix="reverse",
            )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError,
            "Cannot remove series revision guards while native history exists",
        ):
            executor.migrate(target_without_guards)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'operations_appointmentseriesrevision_immutable'"
                ")"
            )
            self.assertTrue(cursor.fetchone()[0])
        with self.assertRaisesMessage(
            DatabaseError,
            "appointment series history rows are immutable",
        ), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operations_appointmentseriesrevision "
                "SET reason = %s WHERE id = %s",
                ["Bypass attempt", revision.pk],
            )

    @skipUnless(
        connection.vendor == "postgresql",
        "Cross-revision result guard проверяется на PostgreSQL.",
    )
    def test_cross_revision_attempt_is_forward_only_and_blocks_reverse(self):
        target_before = [("operations", "0059_series_revision_guards")]
        target_after = [
            ("operations", "0060_series_cross_revision_attempt_guards")
        ]
        executor = MigrationExecutor(connection)
        executor.migrate(target_after)
        apps = executor.loader.project_state(target_after).apps

        with transaction.atomic():
            series, first_revision, _ = self._create_native_series_history(
                apps,
                suffix="cross-revision",
            )

        run_model = apps.get_model(
            "operations", "AppointmentSeriesMaterializationRun"
        )
        result_model = apps.get_model(
            "operations", "AppointmentSeriesMaterializationResult"
        )
        revision_model = apps.get_model(
            "operations", "AppointmentSeriesRevision"
        )
        revision_participant_model = apps.get_model(
            "operations", "AppointmentSeriesRevisionParticipant"
        )
        revision_staff_model = apps.get_model(
            "operations", "AppointmentSeriesRevisionStaffAssignment"
        )
        series_model = apps.get_model("operations", "AppointmentSeries")
        day = first_revision.effective_from
        starts_at = _local(day, time(10, 0))
        first_run = run_model.objects.create(
            series_id=series.pk,
            revision_id=first_revision.pk,
            operation_key=uuid4(),
            fingerprint="f" * 64,
            mode="missing_only",
            date_from=day,
            date_to=day,
            expected_result_count=2,
            actor_id=first_revision.actor_id,
            actor_role_snapshot="director",
            reason="First revision result.",
        )
        first_result = result_model.objects.create(
            series_id=series.pk,
            revision_id=first_revision.pk,
            run_id=first_run.pk,
            scheduled_starts_at=starts_at,
            scheduled_date=day,
            attempt_number=1,
            provenance_kind="native",
            outcome="skipped",
            reason_code="fixture_skip",
            reason="Skipped before the future revision.",
        )
        first_participant = revision_participant_model.objects.get(
            revision_id=first_revision.pk
        )
        first_staff = revision_staff_model.objects.get(
            revision_id=first_revision.pk
        )
        with transaction.atomic():
            second_revision = revision_model.objects.create(
                series_id=series.pk,
                revision_number=2,
                event_type="future_composition",
                provenance_kind="native",
                effective_from=day,
                title=first_revision.title,
                service_id=first_revision.service_id,
                room_id=first_revision.room_id,
                start_date=first_revision.start_date,
                end_date=first_revision.end_date,
                days_of_week=first_revision.days_of_week,
                time=first_revision.time,
                duration_minutes=first_revision.duration_minutes,
                session_type=first_revision.session_type,
                materialization_mode=first_revision.materialization_mode,
                default_appointment_status=first_revision.default_appointment_status,
                allow_unpaid_reserve=False,
                allow_outside_availability=False,
                override_reason="",
                fingerprint="a" * 64,
                actor_id=first_revision.actor_id,
                actor_role_snapshot="director",
                reason="Future composition migration fixture.",
                supersedes_id=first_revision.pk,
                decided_at=timezone.now(),
            )
            revision_participant_model.objects.create(
                revision_id=second_revision.pk,
                child_id=first_participant.child_id,
                position=1,
            )
            revision_staff_model.objects.create(
                revision_id=second_revision.pk,
                staff_member_id=first_staff.staff_member_id,
                role="primary",
            )
            series_model.objects.filter(pk=series.pk).update(
                current_revision_id=second_revision.pk
            )

        second_run = run_model.objects.create(
            series_id=series.pk,
            revision_id=second_revision.pk,
            operation_key=uuid4(),
            fingerprint="b" * 64,
            mode="missing_only",
            date_from=day,
            date_to=day,
            expected_result_count=1,
            actor_id=first_revision.actor_id,
            actor_role_snapshot="director",
            reason="Cross-revision unchanged result.",
        )
        second_result = result_model.objects.create(
            series_id=series.pk,
            revision_id=second_revision.pk,
            run_id=second_run.pk,
            scheduled_starts_at=starts_at,
            scheduled_date=day,
            attempt_number=2,
            provenance_kind="native",
            outcome="unchanged",
            reason_code="existing_result",
            reason="The date already has immutable history.",
            supersedes_id=first_result.pk,
        )

        self.assertEqual(second_result.supersedes_id, first_result.pk)
        with self.assertRaisesMessage(
            DatabaseError,
            "invalid series result attempt chain",
        ), transaction.atomic():
            result_model.objects.create(
                series_id=series.pk,
                revision_id=first_revision.pk,
                run_id=first_run.pk,
                scheduled_starts_at=starts_at,
                scheduled_date=day,
                attempt_number=3,
                provenance_kind="native",
                outcome="unchanged",
                reason_code="reverse_revision_attempt",
                reason="A newer revision cannot supersede into an older one.",
                supersedes_id=second_result.pk,
            )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError,
            "Cannot restore same-revision attempt guards",
        ):
            executor.migrate(target_before)

    @skipUnless(
        connection.vendor == "postgresql",
        "Retry target migration preflight проверяется на PostgreSQL.",
    )
    def test_retry_target_expand_rejects_unfrozen_existing_retry_run(self):
        target_before = [
            ("operations", "0060_series_cross_revision_attempt_guards")
        ]
        target_after = [("operations", "0061_series_retry_target_expand")]
        executor = MigrationExecutor(connection)
        executor.migrate(target_before)
        apps = executor.loader.project_state(target_before).apps

        with transaction.atomic():
            series, revision, _ = self._create_native_series_history(
                apps,
                suffix="retry-preflight",
            )
            run_model = apps.get_model(
                "operations", "AppointmentSeriesMaterializationRun"
            )
            legacy_retry = run_model.objects.create(
                series_id=series.pk,
                revision_id=revision.pk,
                operation_key=uuid4(),
                fingerprint="6" * 64,
                mode="retry_skipped",
                date_from=revision.effective_from,
                date_to=revision.effective_from,
                expected_result_count=1,
                actor_id=revision.actor_id,
                actor_role_snapshot="director",
                reason="Unfrozen retry run must block the expand migration.",
            )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError,
            "Cannot install frozen retry targets while legacy retry_skipped runs exist",
        ):
            executor.migrate(target_after)

        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE operations_appointmentseriesmaterializationrun "
                "DISABLE TRIGGER operations_appointmentseriesmaterializationrun_immutable"
            )
            try:
                cursor.execute(
                    "DELETE FROM operations_appointmentseriesmaterializationrun "
                    "WHERE id = %s",
                    [legacy_retry.pk],
                )
            finally:
                cursor.execute(
                    "ALTER TABLE operations_appointmentseriesmaterializationrun "
                    "ENABLE TRIGGER "
                    "operations_appointmentseriesmaterializationrun_immutable"
                )

        executor = MigrationExecutor(connection)
        executor.migrate(target_after)

    @skipUnless(
        connection.vendor == "postgresql",
        "Deferred retry target count проверяется на PostgreSQL.",
    )
    def test_retry_run_without_frozen_targets_is_rejected_at_commit(self):
        target_after = [("operations", "0061_series_retry_target_expand")]
        executor = MigrationExecutor(connection)
        executor.migrate(target_after)
        apps = executor.loader.project_state(target_after).apps

        with transaction.atomic():
            series, revision, _ = self._create_native_series_history(
                apps,
                suffix="retry-count",
            )
        run_model = apps.get_model(
            "operations", "AppointmentSeriesMaterializationRun"
        )
        with self.assertRaisesMessage(
            DatabaseError,
            "series retry target count does not match run",
        ), transaction.atomic():
            run_model.objects.create(
                series_id=series.pk,
                revision_id=revision.pk,
                operation_key=uuid4(),
                fingerprint="7" * 64,
                mode="retry_skipped",
                date_from=revision.effective_from,
                date_to=revision.effective_from,
                expected_result_count=1,
                actor_id=revision.actor_id,
                actor_role_snapshot="director",
                reason="Retry run without a frozen target must fail.",
            )

    @skipUnless(
        connection.vendor == "postgresql",
        "Frozen retry target guards проверяются на PostgreSQL.",
    )
    def test_retry_target_guards_freeze_chain_and_block_unsafe_reverse(self):
        target_before = [
            ("operations", "0060_series_cross_revision_attempt_guards")
        ]
        target_after = [("operations", "0061_series_retry_target_expand")]
        executor = MigrationExecutor(connection)
        executor.migrate(target_after)
        apps = executor.loader.project_state(target_after).apps

        with transaction.atomic():
            series, revision, retry_run, target, chain_head, _ = (
                self._create_retry_target_history(apps, suffix="retry-target")
            )

        run_model = apps.get_model(
            "operations", "AppointmentSeriesMaterializationRun"
        )
        result_model = apps.get_model(
            "operations", "AppointmentSeriesMaterializationResult"
        )
        target_model = apps.get_model("operations", "AppointmentSeriesRetryTarget")
        day = revision.effective_from

        unrelated_run = run_model.objects.create(
            series_id=series.pk,
            revision_id=revision.pk,
            operation_key=uuid4(),
            fingerprint="4" * 64,
            mode="missing_only",
            date_from=day,
            date_to=day,
            expected_result_count=1,
            actor_id=revision.actor_id,
            actor_role_snapshot="director",
            reason="Must not consume a frozen retry target.",
        )
        with self.assertRaisesMessage(
            DatabaseError,
            "series result chain head is reserved for retry",
        ), transaction.atomic():
            result_model.objects.create(
                series_id=series.pk,
                revision_id=revision.pk,
                run_id=unrelated_run.pk,
                scheduled_starts_at=chain_head.scheduled_starts_at,
                scheduled_date=day,
                attempt_number=3,
                provenance_kind="native",
                outcome="unchanged",
                reason_code="unsafe_branch",
                reason="A missing run cannot consume the retry reservation.",
                supersedes_id=chain_head.pk,
            )

        retry_result = result_model.objects.create(
            series_id=series.pk,
            revision_id=revision.pk,
            run_id=retry_run.pk,
            scheduled_starts_at=chain_head.scheduled_starts_at,
            scheduled_date=day,
            attempt_number=3,
            provenance_kind="native",
            outcome="skipped",
            reason_code="still_conflicted",
            reason="The retry was attempted and skipped again.",
            supersedes_id=chain_head.pk,
        )
        self.assertEqual(retry_result.supersedes_id, chain_head.pk)

        with self.assertRaisesMessage(
            DatabaseError,
            "appointment series history rows are immutable",
        ), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operations_appointmentseriesretrytarget "
                "SET scheduled_date = scheduled_date + 1 WHERE id = %s",
                [target.pk],
            )

        invalid_run = run_model.objects.create(
            series_id=series.pk,
            revision_id=revision.pk,
            operation_key=uuid4(),
            fingerprint="5" * 64,
            mode="missing_only",
            date_from=day,
            date_to=day,
            expected_result_count=1,
            actor_id=revision.actor_id,
            actor_role_snapshot="director",
            reason="Invalid target run mode fixture.",
        )
        with self.assertRaisesMessage(
            DatabaseError,
            "invalid series retry target run",
        ), transaction.atomic():
            target_model.objects.create(
                run_id=invalid_run.pk,
                scheduled_starts_at=chain_head.scheduled_starts_at,
                scheduled_date=day,
                chain_head_result_id=retry_result.pk,
                effective_skipped_result_id=retry_result.pk,
            )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError,
            "Cannot remove series retry target schema while frozen targets exist",
        ):
            executor.migrate(target_before)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'operations_appointmentseriesretrytarget_immutable'"
                ")"
            )
            self.assertTrue(cursor.fetchone()[0])

    @skipUnless(
        connection.vendor == "postgresql",
        "Retry target/result race проверяется на PostgreSQL.",
    )
    def test_retry_target_and_foreign_successor_are_serialized(self):
        target_after = [("operations", "0061_series_retry_target_expand")]
        executor = MigrationExecutor(connection)
        executor.migrate(target_after)
        apps = executor.loader.project_state(target_after).apps

        with transaction.atomic():
            series, revision, _, _, chain_head, effective_skipped = (
                self._create_retry_target_history(
                    apps,
                    suffix="retry-race",
                    create_retry=False,
                )
            )
        run_model = apps.get_model(
            "operations", "AppointmentSeriesMaterializationRun"
        )
        result_model = apps.get_model(
            "operations", "AppointmentSeriesMaterializationResult"
        )
        target_model = apps.get_model("operations", "AppointmentSeriesRetryTarget")
        day = revision.effective_from
        unrelated_run = run_model.objects.create(
            series_id=series.pk,
            revision_id=revision.pk,
            operation_key=uuid4(),
            fingerprint="8" * 64,
            mode="missing_only",
            date_from=day,
            date_to=day,
            expected_result_count=1,
            actor_id=revision.actor_id,
            actor_role_snapshot="director",
            reason="Concurrent successor race fixture.",
        )
        barrier = Barrier(2)
        outcomes = Queue()

        def accept_retry_target():
            close_old_connections()
            try:
                with transaction.atomic():
                    retry_run = run_model.objects.create(
                        series_id=series.pk,
                        revision_id=revision.pk,
                        operation_key=uuid4(),
                        fingerprint="9" * 64,
                        mode="retry_skipped",
                        date_from=day,
                        date_to=day,
                        expected_result_count=1,
                        actor_id=revision.actor_id,
                        actor_role_snapshot="director",
                        reason="Concurrent frozen target acceptance.",
                    )
                    barrier.wait(timeout=10)
                    target = target_model.objects.create(
                        run_id=retry_run.pk,
                        scheduled_starts_at=chain_head.scheduled_starts_at,
                        scheduled_date=day,
                        chain_head_result_id=chain_head.pk,
                        effective_skipped_result_id=effective_skipped.pk,
                    )
                outcomes.put(("target", target.pk))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        def append_foreign_result():
            close_old_connections()
            try:
                with transaction.atomic():
                    barrier.wait(timeout=10)
                    result = result_model.objects.create(
                        series_id=series.pk,
                        revision_id=revision.pk,
                        run_id=unrelated_run.pk,
                        scheduled_starts_at=chain_head.scheduled_starts_at,
                        scheduled_date=day,
                        attempt_number=chain_head.attempt_number + 1,
                        provenance_kind="native",
                        outcome="unchanged",
                        reason_code="concurrent_successor",
                        reason="Concurrent result competing with retry acceptance.",
                        supersedes_id=chain_head.pk,
                    )
                outcomes.put(("result", result.pk))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        threads = [
            Thread(target=accept_retry_target),
            Thread(target=append_foreign_result),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertEqual(sum(isinstance(item, tuple) for item in results), 1, results)
        self.assertEqual(
            sum(isinstance(item, DatabaseError) for item in results),
            1,
            results,
        )
        successor_count = result_model.objects.filter(
            supersedes_id=chain_head.pk
        ).count()
        target_count = target_model.objects.filter(
            chain_head_result_id=chain_head.pk
        ).count()
        self.assertEqual(successor_count + target_count, 1)

    def test_series_revision_backfill_copies_history_with_explicit_provenance(self):
        actor = User.objects.create_superuser(
            username="legacy-revision-retry-admin",
            password="x",
        )
        target_before = [("operations", "0056_group_series_join_mode")]
        target_after = [("operations", "0058_backfill_series_revisions")]
        executor = MigrationExecutor(connection)
        executor.migrate(target_before)
        old_apps = executor.loader.project_state(target_before).apps

        child_model = old_apps.get_model("operations", "Child")
        staff_model = old_apps.get_model("operations", "StaffMember")
        service_model = old_apps.get_model("operations", "Service")
        room_model = old_apps.get_model("operations", "Room")
        program_model = old_apps.get_model("operations", "TreatmentProgram")
        block_model = old_apps.get_model("operations", "ProgramBlock")
        series_model = old_apps.get_model("operations", "AppointmentSeries")
        series_participant_model = old_apps.get_model(
            "operations", "AppointmentSeriesParticipant"
        )
        series_staff_model = old_apps.get_model(
            "operations", "AppointmentSeriesStaffAssignment"
        )
        appointment_model = old_apps.get_model("operations", "Appointment")
        participant_model = old_apps.get_model("operations", "AppointmentParticipant")
        assignment_model = old_apps.get_model("operations", "AppointmentStaffAssignment")
        occurrence_model = old_apps.get_model(
            "operations", "AppointmentSeriesOccurrence"
        )

        child = child_model.objects.create(last_name="Revision", first_name="Legacy")
        staff = staff_model.objects.create(full_name="Legacy revision staff")
        service = service_model.objects.create(
            name="Legacy revision service",
            code="LEGACY-REVISION",
        )
        room = room_model.objects.create(name="Legacy revision room")
        program = program_model.objects.create(
            child=child,
            title="Legacy revision program",
            status="active",
        )
        block = block_model.objects.create(
            program=program,
            number=1,
            title="Legacy revision block",
            service=service,
            staff_member=staff,
            planned_sessions=1,
        )
        day = timezone.localdate() + timedelta(days=40)
        weekday_label = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")[
            day.weekday()
        ]
        starts_at = _local(day, time(10, 0))
        ends_at = starts_at + timedelta(minutes=30)
        series = series_model.objects.create(
            child=child,
            service=service,
            staff_member=staff,
            room=room,
            program_block=block,
            title="Legacy revision series",
            start_date=day,
            end_date=day,
            days_of_week=weekday_label,
            time=time(10, 0),
            duration_minutes=30,
            session_type="individual",
            status="active",
        )
        series_participant_model.objects.create(
            series=series,
            child=child,
            program_block=block,
            position=1,
        )
        series_staff_model.objects.create(
            series=series,
            staff_member=staff,
            role="primary",
        )
        appointment = appointment_model.objects.create(
            child=child,
            service=service,
            staff_member=staff,
            room=room,
            starts_at=starts_at,
            ends_at=ends_at,
            status="confirmed",
            series=series,
            program_block=block,
            session_type="individual",
        )
        participant_model.objects.create(
            appointment=appointment,
            child=child,
            program_block=block,
            starts_at_snapshot=starts_at,
            ends_at_snapshot=ends_at,
            appointment_status="confirmed",
        )
        assignment_model.objects.create(
            appointment=appointment,
            staff_member=staff,
            role="primary",
            starts_at_snapshot=starts_at,
            ends_at_snapshot=ends_at,
            appointment_status="confirmed",
        )
        occurrence = occurrence_model.objects.create(
            series=series,
            scheduled_starts_at=starts_at,
            appointment=appointment,
            outcome="created",
            reason_code="legacy_backfill",
            reason="Existing immutable history.",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(target_after)
        apps = executor.loader.project_state(target_after).apps
        migrated_series = apps.get_model("operations", "AppointmentSeries").objects.get(
            pk=series.pk
        )
        revision_model = apps.get_model("operations", "AppointmentSeriesRevision")
        run_model = apps.get_model(
            "operations", "AppointmentSeriesMaterializationRun"
        )
        result_model = apps.get_model(
            "operations", "AppointmentSeriesMaterializationResult"
        )
        event_model = apps.get_model(
            "operations", "AppointmentSeriesMaterializationRunEvent"
        )
        migrated_occurrence = apps.get_model(
            "operations", "AppointmentSeriesOccurrence"
        ).objects.get(pk=occurrence.pk)

        revision = revision_model.objects.get(series_id=series.pk)
        self.assertEqual(migrated_series.current_revision_id, revision.pk)
        self.assertEqual(revision.revision_number, 1)
        self.assertEqual(revision.provenance_kind, "legacy_reconstructed")
        self.assertIsNone(revision.actor_id)
        self.assertEqual(revision.participants.count(), 1)
        self.assertEqual(revision.staff_assignments.count(), 1)
        run = run_model.objects.get(series_id=series.pk)
        self.assertEqual(run.mode, "legacy_import")
        self.assertEqual(run.expected_result_count, 1)
        result = result_model.objects.get(run_id=run.pk)
        self.assertEqual(result.compatibility_occurrence_id, occurrence.pk)
        self.assertEqual(result.provenance_kind, "legacy_reconstructed")
        self.assertEqual(result.appointment_id, appointment.pk)
        event = event_model.objects.get(run_id=run.pk)
        self.assertEqual(event.event_number, 1)
        self.assertEqual(event.result_count, 1)
        self.assertEqual(migrated_occurrence.appointment_id, appointment.pk)

        live_series = AppointmentSeries.objects.get(pk=series.pk)
        self.assertEqual(live_series.materialize_series(actor=actor), 0)
        self.assertEqual(live_series.materialization_runs.count(), 1)
        self.assertEqual(live_series.materialization_results.count(), 1)
        self.assertEqual(live_series.occurrences.count(), 1)

        result_model.objects.create(
            series_id=series.pk,
            revision_id=revision.pk,
            run_id=run.pk,
            scheduled_starts_at=starts_at + timedelta(days=1),
            scheduled_date=timezone.localtime(starts_at + timedelta(days=1)).date(),
            attempt_number=1,
            provenance_kind="native",
            outcome="skipped",
            reason_code="native_history",
            reason="New history blocks destructive reverse.",
        )
        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError,
            "Cannot reverse series revision backfill while new revision or run history exists",
        ):
            executor.migrate(target_before)

    def test_single_participant_legacy_group_replays_without_new_appointments(self):
        actor = User.objects.create_superuser(
            username="legacy-group-retry-admin",
            password="x",
        )
        target_before = [("operations", "0056_group_series_join_mode")]
        target_after = [("operations", "0058_backfill_series_revisions")]
        executor = MigrationExecutor(connection)
        executor.migrate(target_before)
        apps = executor.loader.project_state(target_before).apps

        child_model = apps.get_model("operations", "Child")
        staff_model = apps.get_model("operations", "StaffMember")
        service_model = apps.get_model("operations", "Service")
        room_model = apps.get_model("operations", "Room")
        series_model = apps.get_model("operations", "AppointmentSeries")
        participant_model = apps.get_model(
            "operations", "AppointmentSeriesParticipant"
        )
        staff_assignment_model = apps.get_model(
            "operations", "AppointmentSeriesStaffAssignment"
        )
        appointment_model = apps.get_model("operations", "Appointment")
        occurrence_model = apps.get_model(
            "operations", "AppointmentSeriesOccurrence"
        )

        child = child_model.objects.create(last_name="Legacy", first_name="Group")
        staff = staff_model.objects.create(full_name="Legacy group specialist")
        service = service_model.objects.create(
            name="Legacy group service",
            code="LEGACY-GROUP-REPLAY",
        )
        room = room_model.objects.create(name="Legacy group room")
        day = timezone.localdate() + timedelta(days=35)
        weekday_label = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")[
            day.weekday()
        ]
        starts_at = _local(day, time(10, 0))
        series = series_model.objects.create(
            operation_key=uuid4(),
            child=child,
            service=service,
            staff_member=staff,
            room=room,
            title="Legacy group awaiting recruitment",
            start_date=day,
            end_date=day,
            days_of_week=weekday_label,
            time=time(10, 0),
            duration_minutes=30,
            session_type="group",
            materialization_mode="create_appointments",
            default_appointment_status="confirmed",
            status="active",
        )
        participant_model.objects.create(
            series=series,
            child=child,
            position=1,
        )
        staff_assignment_model.objects.create(
            series=series,
            staff_member=staff,
            role="primary",
        )
        appointment = appointment_model.objects.create(
            child=child,
            service=service,
            staff_member=staff,
            room=room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            status="confirmed",
            series=series,
            session_type="group",
        )
        occurrence_model.objects.create(
            series=series,
            scheduled_starts_at=starts_at,
            appointment=appointment,
            outcome="created",
            reason_code="legacy_backfill",
            reason="Legacy group fact.",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(target_after)
        live_series = AppointmentSeries.objects.get(pk=series.pk)
        replay = program_series.materialize_group_series(
            live_series,
            actor=actor,
        )

        self.assertEqual(replay.created_count, 0)
        self.assertEqual(replay.unchanged_count, 1)
        self.assertEqual(live_series.materialization_runs.count(), 1)
        self.assertEqual(live_series.materialization_results.count(), 1)
        self.assertEqual(live_series.occurrences.count(), 1)
        self.assertEqual(Appointment.objects.filter(series_id=series.pk).count(), 1)

    @skipUnless(connection.vendor == "postgresql", "Миграционный backfill проверяется на PostgreSQL.")
    def test_legacy_series_is_backfilled_without_removing_legacy_fields(self):
        target_before = [("operations", "0054_donor_report_submission")]
        target_after = [("operations", "0055_group_program_series")]
        executor = MigrationExecutor(connection)
        executor.migrate(target_before)
        old_apps = executor.loader.project_state(target_before).apps

        child_old = old_apps.get_model("operations", "Child")
        staff_old = old_apps.get_model("operations", "StaffMember")
        service_old = old_apps.get_model("operations", "Service")
        room_old = old_apps.get_model("operations", "Room")
        program_old = old_apps.get_model("operations", "TreatmentProgram")
        block_old = old_apps.get_model("operations", "ProgramBlock")
        series_old = old_apps.get_model("operations", "AppointmentSeries")
        appointment_old = old_apps.get_model("operations", "Appointment")

        child = child_old.objects.create(last_name="Legacy", first_name="Series")
        staff = staff_old.objects.create(full_name="Legacy series staff")
        service = service_old.objects.create(name="Legacy series service", code="LEGACY-SERIES")
        room = room_old.objects.create(name="Legacy series room")
        program = program_old.objects.create(child=child, title="Legacy program")
        block = block_old.objects.create(
            program=program,
            number=1,
            title="Legacy block",
            service=service,
            planned_sessions=2,
        )
        day = timezone.localdate() + timedelta(days=30)
        weekday_label = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")[
            day.weekday()
        ]
        series = series_old.objects.create(
            child=child,
            service=service,
            staff_member=staff,
            room=room,
            program_block=block,
            title="Legacy series",
            start_date=day,
            end_date=day,
            days_of_week=weekday_label,
            time=time(10, 0),
            duration_minutes=30,
            status="active",
        )
        starts_at = _local(day, time(10, 0))
        appointment = appointment_old.objects.create(
            child=child,
            service=service,
            staff_member=staff,
            room=room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            series=series,
            program_block=block,
            sequence_number=1,
            session_type="group",
        )
        duplicate = appointment_old.objects.create(
            child=child,
            service=service,
            staff_member=staff,
            room=room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            series=series,
            program_block=block,
            sequence_number=2,
            status="cancelled",
            session_type="group",
        )
        migration = importlib.import_module("operations.migrations.0055_group_program_series")
        with self.assertRaisesMessage(RuntimeError, "Duplicate legacy series occurrence times"):
            migration.assert_legacy_series_occurrences_unique(old_apps, None)
        duplicate.delete()

        executor = MigrationExecutor(connection)
        executor.migrate(target_after)
        new_apps = executor.loader.project_state(target_after).apps
        series_new = new_apps.get_model("operations", "AppointmentSeries")
        membership_new = new_apps.get_model("operations", "AppointmentSeriesParticipant")
        staff_assignment_new = new_apps.get_model(
            "operations", "AppointmentSeriesStaffAssignment"
        )
        occurrence_new = new_apps.get_model("operations", "AppointmentSeriesOccurrence")

        migrated = series_new.objects.get(pk=series.pk)
        self.assertEqual(migrated.child_id, child.pk)
        self.assertEqual(migrated.staff_member_id, staff.pk)
        self.assertEqual(migrated.program_block_id, block.pk)
        self.assertEqual(migrated.session_type, "group")
        self.assertEqual(migrated.default_appointment_status, "confirmed")
        self.assertIsNotNone(migrated.operation_key)
        self.assertTrue(
            membership_new.objects.filter(
                series_id=series.pk,
                child_id=child.pk,
                program_block_id=block.pk,
            ).exists()
        )
        self.assertTrue(
            staff_assignment_new.objects.filter(
                series_id=series.pk,
                staff_member_id=staff.pk,
            ).exists()
        )
        self.assertTrue(
            occurrence_new.objects.filter(
                series_id=series.pk,
                appointment_id=appointment.pk,
                outcome="created",
                reason_code="legacy_backfill",
            ).exists()
        )

        occurrence_new.objects.create(
            series_id=series.pk,
            scheduled_starts_at=starts_at + timedelta(hours=1),
            outcome="skipped",
            reason_code="new_series_history",
            reason="New history must block a destructive reverse migration.",
        )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError,
            "Cannot reverse group program series migration while new series history exists",
        ):
            executor.migrate(target_before)

    @skipUnless(
        connection.vendor == "postgresql",
        "Защита отката истории присоединения проверяется на PostgreSQL.",
    )
    def test_join_series_history_blocks_reverse_migration(self):
        target_before = [("operations", "0055_group_program_series")]
        target_after = [("operations", "0056_group_series_join_mode")]
        executor = MigrationExecutor(connection)
        executor.migrate(target_before)

        executor = MigrationExecutor(connection)
        executor.migrate(target_after)
        apps = executor.loader.project_state(target_after).apps
        child_model = apps.get_model("operations", "Child")
        staff_model = apps.get_model("operations", "StaffMember")
        service_model = apps.get_model("operations", "Service")
        room_model = apps.get_model("operations", "Room")
        series_model = apps.get_model("operations", "AppointmentSeries")
        series_participant_model = apps.get_model(
            "operations", "AppointmentSeriesParticipant"
        )

        child = child_model.objects.create(last_name="Join", first_name="History")
        staff = staff_model.objects.create(full_name="Join migration staff")
        service = service_model.objects.create(
            name="Join migration service",
            code="JOIN-MIGRATION",
        )
        room = room_model.objects.create(name="Join migration room")
        day = timezone.localdate() + timedelta(days=30)
        series = series_model.objects.create(
            child=child,
            service=service,
            staff_member=staff,
            room=room,
            title="Join migration history",
            start_date=day,
            end_date=day,
            days_of_week="ПН",
            time=time(10, 0),
            duration_minutes=30,
            session_type="group",
            materialization_mode="join_existing",
            status="active",
        )
        series_participant_model.objects.create(
            series=series,
            child=child,
            position=1,
        )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError,
            "Cannot reverse group-series join mode while joined history exists",
        ):
            executor.migrate(target_before)
