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
    BalanceAccount,
    Child,
    FundingSource,
    ProgramBlock,
    Room,
    Service,
    StaffMember,
    TreatmentProgram,
)
from operations.services import program_series, series_lifecycle
from operations.services.program_progress import get_program_block_progress

User = get_user_model()


def _local(day, clock):
    return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())


class ProgramBlockProgressTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("progress-admin", password="x")
        cls.child = Child.objects.create(last_name="Прогресс", first_name="Первый")
        cls.other_child = Child.objects.create(last_name="Прогресс", first_name="Второй")
        cls.staff = StaffMember.objects.create(full_name="Специалист прогресса")
        cls.service = Service.objects.create(
            name="Услуга прогресса",
            code="PROGRAM-PROGRESS",
            default_duration_minutes=45,
            default_price=Decimal("1000"),
        )
        funding = FundingSource.objects.create(
            name="Источник прогресса",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        cls.account = BalanceAccount.objects.create(
            child=cls.child,
            funding_source=funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("20"),
        )
        cls.other_account = BalanceAccount.objects.create(
            child=cls.other_child,
            funding_source=funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("20"),
        )
        cls.room = Room.objects.create(
            name="Кабинет прогресса",
            allow_group_sessions=True,
            limit_staff_count=True,
            max_staff_count=2,
            limit_recipient_count=True,
            max_recipient_count=4,
        )

    def _blocks(self, *, planned_first=3, planned_second=3):
        program = TreatmentProgram.objects.create(
            child=self.child,
            title="Программа расчета прогресса",
            status=TreatmentProgram.Status.ACTIVE,
        )
        first = ProgramBlock.objects.create(
            program=program,
            number=1,
            title="Первый каскад",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=planned_first,
            balance_account=self.account,
        )
        other_program = TreatmentProgram.objects.create(
            child=self.other_child,
            title="Программа второго получателя",
            status=TreatmentProgram.Status.ACTIVE,
        )
        second = ProgramBlock.objects.create(
            program=other_program,
            number=1,
            title="Второй каскад",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=planned_second,
            balance_account=self.other_account,
        )
        return first, second

    def _appointment(
        self,
        block,
        *,
        child=None,
        status=Appointment.Status.COMPLETED,
        attendance=Appointment.AttendanceStatus.ATTENDED,
        billing_decision=Appointment.BillingDecision.UNDECIDED,
        hour=9,
    ):
        starts_at = _local(timezone.localdate() + timedelta(days=10), time(hour, 0))
        return Appointment.objects.create(
            child=child or self.child,
            staff_member=self.staff,
            service=self.service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
            status=status,
            attendance_status=attendance,
            billing_decision=billing_decision,
            billing_account=self.account if (child or self.child) == self.child else None,
            program_block=block,
        )

    def _legacy_only(self, appointment):
        appointment.participants.all().delete()
        self.assertFalse(appointment.participants.exists())

    def test_group_participants_count_for_their_own_blocks_without_primary_double_count(self):
        first, second = self._blocks(planned_first=1, planned_second=1)
        appointment = self._appointment(first)
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=self.other_child,
            attendance_status=Appointment.AttendanceStatus.ATTENDED,
            billing_account=None,
            program_block=second,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=Appointment.Status.COMPLETED,
        )

        progress = get_program_block_progress([first.pk, second])

        self.assertEqual(progress[first.pk].completed, 1)
        self.assertEqual(progress[second.pk].completed, 1)
        self.assertEqual(progress[first.pk].allocated, 1)
        self.assertEqual(progress[first.pk].remaining, 0)
        self.assertEqual(progress[first.pk].activity_status, ProgramBlock.Status.IN_PROGRESS)
        self.assertEqual(progress[first.pk].activity_status_label, "Идёт")

    def test_missed_charged_participant_uses_capacity_but_does_not_complete_plan(self):
        first, _ = self._blocks(planned_first=1)
        appointment = self._appointment(first)
        participant = appointment.participants.get(child=self.child)
        AppointmentParticipant.objects.filter(pk=participant.pk).update(
            attendance_status=Appointment.AttendanceStatus.MISSED,
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=self.account,
        )

        progress = get_program_block_progress([first])[first.pk]

        self.assertEqual(progress.completed, 0)
        self.assertEqual(progress.allocated, 1)
        self.assertEqual(progress.missed, 1)
        self.assertEqual(progress.charged, 1)
        self.assertEqual(progress.remaining, 1)
        self.assertEqual(progress.activity_status, ProgramBlock.Status.SCHEDULED)

    def test_cancelled_and_rescheduled_sources_do_not_allocate_or_complete(self):
        first, _ = self._blocks()
        self._appointment(
            first,
            status=Appointment.Status.CANCELLED,
            attendance=Appointment.AttendanceStatus.ATTENDED,
            hour=9,
        )
        self._appointment(
            first,
            status=Appointment.Status.RESCHEDULED,
            attendance=Appointment.AttendanceStatus.ATTENDED,
            hour=10,
        )

        progress = get_program_block_progress([first])[first.pk]

        self.assertEqual(progress.completed, 0)
        self.assertEqual(progress.allocated, 0)
        self.assertEqual(progress.missed, 0)

    def test_legacy_fallback_needs_no_participants_even_if_only_other_block_is_withdrawn(self):
        first, second = self._blocks()
        counted_legacy = self._appointment(
            first,
            hour=9,
            billing_decision=Appointment.BillingDecision.CHARGE,
        )
        self._legacy_only(counted_legacy)

        suppressed_legacy = self._appointment(first, hour=10)
        self._legacy_only(suppressed_legacy)
        AppointmentParticipant.objects.create(
            appointment=suppressed_legacy,
            child=self.other_child,
            attendance_status=Appointment.AttendanceStatus.MISSED,
            program_block=second,
            starts_at_snapshot=suppressed_legacy.starts_at,
            ends_at_snapshot=suppressed_legacy.ends_at,
            appointment_status=Appointment.Status.CANCELLED,
        )

        progress = get_program_block_progress([first, second])

        self.assertEqual(progress[first.pk].completed, 1)
        self.assertEqual(progress[first.pk].allocated, 1)
        self.assertEqual(progress[first.pk].charged, 1)
        self.assertEqual(progress[second.pk].completed, 0)
        self.assertEqual(progress[second.pk].allocated, 0)

    def test_terminal_stored_statuses_win_over_later_factual_participation(self):
        completed_block, cancelled_block = self._blocks()
        completed_appointment = self._appointment(
            completed_block,
            status=Appointment.Status.CONFIRMED,
            attendance=Appointment.AttendanceStatus.UNKNOWN,
            hour=9,
        )
        cancelled_appointment = self._appointment(
            cancelled_block,
            child=self.other_child,
            status=Appointment.Status.CONFIRMED,
            attendance=Appointment.AttendanceStatus.UNKNOWN,
            hour=10,
        )
        ProgramBlock.objects.filter(pk=completed_block.pk).update(
            status=ProgramBlock.Status.COMPLETED
        )
        ProgramBlock.objects.filter(pk=cancelled_block.pk).update(
            status=ProgramBlock.Status.CANCELLED
        )
        for appointment in (completed_appointment, cancelled_appointment):
            Appointment.objects.filter(pk=appointment.pk).update(
                status=Appointment.Status.COMPLETED,
                attendance_status=Appointment.AttendanceStatus.ATTENDED,
            )
            AppointmentParticipant.objects.filter(appointment=appointment).update(
                appointment_status=Appointment.Status.COMPLETED,
                attendance_status=Appointment.AttendanceStatus.ATTENDED,
            )

        progress = get_program_block_progress([completed_block, cancelled_block])

        self.assertEqual(progress[completed_block.pk].activity_status, ProgramBlock.Status.COMPLETED)
        self.assertEqual(progress[cancelled_block.pk].activity_status, ProgramBlock.Status.CANCELLED)

    def test_no_show_only_is_scheduled_activity_without_completed_progress(self):
        first, _ = self._blocks()
        self._appointment(
            first,
            status=Appointment.Status.NO_SHOW,
            attendance=Appointment.AttendanceStatus.MISSED,
        )

        progress = get_program_block_progress([first])[first.pk]

        self.assertEqual(progress.completed, 0)
        self.assertEqual(progress.allocated, 1)
        self.assertEqual(progress.activity_status, ProgramBlock.Status.SCHEDULED)

    def test_real_withdrawal_result_excludes_the_participant_from_allocation(self):
        first, second = self._blocks()
        start_date = timezone.localdate() + timedelta(days=10)
        preview = program_series.preview_group_series(
            blocks=[first, second],
            staff_members=[self.staff],
            room=self.room,
            title="Группа для проверки снятия участия",
            start_date=start_date,
            end_date=start_date,
            weekdays={start_date.weekday()},
            start_time=time(11, 0),
            duration_minutes=45,
            default_appointment_status=Appointment.Status.PROPOSED,
        )
        materialized = program_series.create_group_series(
            preview,
            operation_key=uuid4(),
            actor=self.admin,
        )
        appointment = materialized.series.appointments.get()
        third_child = Child.objects.create(last_name="Прогресс", first_name="Третий")
        third_account = BalanceAccount.objects.create(
            child=third_child,
            funding_source=self.account.funding_source,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("20"),
        )
        third_program = TreatmentProgram.objects.create(
            child=third_child,
            title="Программа третьего получателя",
            status=TreatmentProgram.Status.ACTIVE,
        )
        third_block = ProgramBlock.objects.create(
            program=third_program,
            number=1,
            title="Третий каскад",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=1,
            balance_account=third_account,
        )
        joined = program_series.join_program_block_to_groups(
            block=third_block,
            appointments=[appointment],
            operation_key=uuid4(),
            actor=self.admin,
        )
        participant = appointment.participants.get(child=third_child)

        series_lifecycle.withdraw_future_joined_participations(
            joined.series,
            operation_key=uuid4(),
            actor=self.admin,
            reason="Снимаем единственное присоединенное участие для расчета прогресса.",
        )

        participant.refresh_from_db()
        self.assertTrue(participant.series_withdrawal_results.filter(outcome="cancelled").exists())
        progress = get_program_block_progress([third_block])[third_block.pk]
        self.assertEqual(progress.allocated, 0)
        self.assertEqual(progress.completed, 0)

    def test_bulk_lookup_returns_zero_progress_and_never_negative_remaining(self):
        first, second = self._blocks(planned_first=1, planned_second=2)
        self._appointment(first, hour=9)
        self._appointment(first, hour=10)

        with self.assertNumQueries(3):
            progress = get_program_block_progress([first, second.pk])

        self.assertEqual(progress[first.pk].completed, 2)
        self.assertEqual(progress[first.pk].remaining, 0)
        self.assertEqual(progress[second.pk].completed, 0)
        self.assertEqual(progress[second.pk].remaining, 2)
        self.assertEqual(progress[second.pk].activity_status, ProgramBlock.Status.PLANNED)
