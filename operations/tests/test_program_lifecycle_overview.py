from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentSeries,
    BalanceAccount,
    Child,
    FundingSource,
    ProgramBlock,
    Room,
    Service,
    StaffMember,
    TreatmentProgram,
)
from operations.services import program_lifecycle, program_series, series_lifecycle
from operations.services.program_lifecycle_overview import (
    BLOCK_READY,
    PARENT_CLOSED,
    PROGRAM_PAUSED,
    PROGRAM_READY,
    block_attention_queryset,
    get_lifecycle_overview_counts,
    program_attention_queryset,
)

User = get_user_model()


def _local(day, clock):
    return timezone.make_aware(
        datetime.combine(day, clock), timezone.get_current_timezone()
    )


class ProgramLifecycleOverviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.director = User.objects.create_superuser("overview-director", password="x")
        cls.admin = User.objects.create_user("overview-admin", password="x", is_staff=True)
        cls.staff = StaffMember.objects.create(full_name="Специалист обзора")
        cls.service = Service.objects.create(
            name="Услуга обзора",
            code="LIFECYCLE-OVERVIEW",
            default_duration_minutes=45,
            default_price=Decimal("1000"),
        )
        cls.funding = FundingSource.objects.create(
            name="Источник обзора", source_type=FundingSource.SourceType.PERSONAL
        )
        cls.room = Room.objects.create(
            name="Кабинет обзора",
            allow_group_sessions=True,
            limit_staff_count=True,
            max_staff_count=2,
            limit_recipient_count=True,
            max_recipient_count=5,
        )

    def _child_account(self, suffix):
        child = Child.objects.create(last_name="Обзор", first_name=suffix)
        account = BalanceAccount.objects.create(
            child=child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("20"),
        )
        return child, account

    def _program(self, suffix, *, status=TreatmentProgram.Status.ACTIVE):
        child, account = self._child_account(suffix)
        program = TreatmentProgram.objects.create(
            child=child,
            title=f"Программа {suffix}",
            status=status,
        )
        return program, account

    def _block(self, suffix, *, planned=1, status=ProgramBlock.Status.PLANNED):
        program, account = self._program(suffix)
        block = ProgramBlock.objects.create(
            program=program,
            number=1,
            title=f"Каскад {suffix}",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=planned,
            balance_account=account,
            status=status,
        )
        return block, account

    def _appointment(
        self,
        block,
        *,
        hour,
        status=Appointment.Status.COMPLETED,
        attendance=Appointment.AttendanceStatus.ATTENDED,
        billing_decision=Appointment.BillingDecision.UNDECIDED,
    ):
        starts_at = _local(timezone.localdate() + timedelta(days=10), time(hour, 0))
        return Appointment.objects.create(
            child=block.program.child,
            staff_member=self.staff,
            service=self.service,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
            status=status,
            attendance_status=attendance,
            billing_decision=billing_decision,
            billing_account=(
                block.balance_account
                if billing_decision == Appointment.BillingDecision.CHARGE
                else None
            ),
            program_block=block,
        )

    def _cancel_parent(self, block):
        review = program_lifecycle.get_program_lifecycle_review(block.program)
        program_lifecycle.cancel_program(
            block.program,
            actor=self.director,
            reason="Закрываем программу для проверки глобального обзора.",
            operation_key=uuid4(),
            expected_review_fingerprint=review.fingerprint,
            expected_event_id=0,
        )

    def test_block_attention_is_participant_first_with_group_no_show_charge_and_legacy(self):
        ready, _ = self._block("А-готов", planned=1)
        no_show, no_show_account = self._block("Б-неявка", planned=1)
        group = self._appointment(ready, hour=9)
        AppointmentParticipant.objects.create(
            appointment=group,
            child=no_show.program.child,
            program_block=no_show,
            starts_at_snapshot=group.starts_at,
            ends_at_snapshot=group.ends_at,
            appointment_status=Appointment.Status.NO_SHOW,
            attendance_status=Appointment.AttendanceStatus.MISSED,
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=no_show_account,
        )
        self._cancel_parent(no_show)

        legacy, _ = self._block("В-legacy", planned=1)
        legacy_appointment = self._appointment(legacy, hour=10)
        legacy_appointment.participants.all().delete()

        rows = {row.pk: row for row in block_attention_queryset()}

        self.assertEqual(rows[ready.pk].completed_count, 1)
        self.assertEqual(rows[no_show.pk].completed_count, 0)
        self.assertEqual(rows[legacy.pk].completed_count, 1)
        self.assertEqual(
            list(block_attention_queryset(BLOCK_READY).values_list("pk", flat=True)),
            [ready.pk, legacy.pk],
        )
        self.assertEqual(
            list(block_attention_queryset(PARENT_CLOSED).values_list("pk", flat=True)),
            [no_show.pk],
        )

    def test_withdrawn_participant_does_not_make_its_block_ready(self):
        first, _ = self._block("Г-группа-1")
        second, _ = self._block("Д-группа-2")
        start_date = timezone.localdate() + timedelta(days=10)
        preview = program_series.preview_group_series(
            blocks=[first, second],
            staff_members=[self.staff],
            room=self.room,
            title="Группа обзора",
            start_date=start_date,
            end_date=start_date,
            weekdays={start_date.weekday()},
            start_time=time(11, 0),
            duration_minutes=45,
            default_appointment_status=Appointment.Status.PROPOSED,
        )
        created = program_series.create_group_series(
            preview, operation_key=uuid4(), actor=self.director
        )
        appointment = created.series.appointments.get()
        third, _ = self._block("Е-снятый")
        joined = program_series.join_program_block_to_groups(
            block=third,
            appointments=[appointment],
            operation_key=uuid4(),
            actor=self.director,
        )
        participant = appointment.participants.get(child=third.program.child)
        series_lifecycle.withdraw_future_joined_participations(
            joined.series,
            operation_key=uuid4(),
            actor=self.director,
            reason="Снимаем участие для проверки глобального обзора.",
        )
        self.assertTrue(
            participant.series_withdrawal_results.filter(outcome="cancelled").exists()
        )
        self._cancel_parent(third)

        row = block_attention_queryset(PARENT_CLOSED).get(pk=third.pk)
        self.assertEqual(row.completed_count, 0)
        self.assertFalse(block_attention_queryset(BLOCK_READY).filter(pk=third.pk).exists())

    def test_program_focus_includes_empty_active_program_and_paused_programs(self):
        empty, _ = self._program("Ж-пустая")
        paused_ready, _ = self._program("З-пауза-готова", status=TreatmentProgram.Status.PAUSED)
        paused_open, account = self._program(
            "И-пауза-в-работе", status=TreatmentProgram.Status.PAUSED
        )
        ProgramBlock.objects.create(
            program=paused_open,
            number=1,
            title="Незавершенный каскад",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=1,
            balance_account=account,
        )

        self.assertEqual(
            set(program_attention_queryset(PROGRAM_READY).values_list("pk", flat=True)),
            {empty.pk, paused_ready.pk},
        )
        self.assertEqual(
            set(program_attention_queryset(PROGRAM_PAUSED).values_list("pk", flat=True)),
            {paused_ready.pk, paused_open.pk},
        )

        review = program_lifecycle.get_program_lifecycle_review(empty)
        result = program_lifecycle.complete_program(
            empty,
            actor=self.admin,
            reason="Пустая активная программа соответствует правилам завершения.",
            operation_key=uuid4(),
            expected_review_fingerprint=review.fingerprint,
            expected_event_id=0,
        )
        self.assertEqual(result.program.status, TreatmentProgram.Status.COMPLETED)

    def test_counts_are_bounded_and_totals_deduplicate_overlapping_focuses(self):
        overlap, _ = self._block("К-пересечение", planned=1)
        self._appointment(overlap, hour=12)
        self._cancel_parent(overlap)
        self._program(
            "Л-пауза-пересечение", status=TreatmentProgram.Status.PAUSED
        )
        active_ready, _ = self._program("М-активная-готова")
        series = AppointmentSeries.objects.create(
            child=active_ready.child,
            service=self.service,
            staff_member=self.staff,
            room=self.room,
            title="Остановленная серия обзора",
            start_date=timezone.localdate() + timedelta(days=1),
            end_date=timezone.localdate() + timedelta(days=1),
            days_of_week="ПН",
            time=time(13, 0),
            duration_minutes=45,
            status=AppointmentSeries.Status.ACTIVE,
        )
        series_lifecycle.stop_materialization(
            series,
            operation_key=uuid4(),
            actor=self.director,
            reason="Останавливаем серию для информационного счетчика обзора.",
            expected_event_id=0,
        )

        with self.assertNumQueries(3):
            counts = get_lifecycle_overview_counts()

        self.assertEqual(counts.block_ready, 1)
        self.assertEqual(counts.parent_closed, 1)
        self.assertEqual(counts.program_ready, 2)
        self.assertEqual(counts.program_paused, 1)
        self.assertEqual(counts.stopped_series, 1)
        self.assertEqual(counts.total, 3)

    def test_querysets_are_one_query_and_reject_unknown_focuses(self):
        block, _ = self._block("Н-стабильный", planned=1)
        self._appointment(block, hour=14)
        with self.assertNumQueries(1):
            rows = list(block_attention_queryset())
        self.assertEqual([row.pk for row in rows], [block.pk])
        with self.assertNumQueries(1):
            programs = list(program_attention_queryset())
        self.assertEqual(programs, [])

        with self.assertRaises(ValueError):
            block_attention_queryset("unknown")
        with self.assertRaises(ValueError):
            program_attention_queryset("unknown")
