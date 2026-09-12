from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
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
from operations.services.program_block_lifecycle import (
    ProgramBlockLifecycleMismatch,
    cancel_block,
    complete_block,
    get_program_block_lifecycle_review,
)
from operations.services.program_lifecycle import (
    cancel_program,
    get_program_lifecycle_review,
)

User = get_user_model()


def _local(day, clock):
    return timezone.make_aware(
        datetime.combine(day, clock), timezone.get_current_timezone()
    )


class ProgramBlockReviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.director = User.objects.create_superuser("block-review-director", password="x")
        cls.admin = User.objects.create_user(
            "block-review-admin", password="x", is_staff=True
        )
        cls.child = Child.objects.create(last_name="Каскад", first_name="Первый")
        cls.other_child = Child.objects.create(last_name="Каскад", first_name="Второй")
        cls.staff = StaffMember.objects.create(full_name="Специалист каскада")
        cls.service = Service.objects.create(
            name="Услуга каскада",
            code="BLOCK-REVIEW",
            default_duration_minutes=45,
            default_price=Decimal("1000"),
        )
        cls.funding = FundingSource.objects.create(
            name="Источник каскада",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        cls.account = BalanceAccount.objects.create(
            child=cls.child,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("20"),
        )
        cls.other_account = BalanceAccount.objects.create(
            child=cls.other_child,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("20"),
        )
        cls.room = Room.objects.create(
            name="Кабинет каскада",
            allow_group_sessions=True,
            limit_staff_count=True,
            max_staff_count=2,
            limit_recipient_count=True,
            max_recipient_count=4,
        )

    def _block(self, *, child=None, account=None, planned=3, number=1):
        child = child or self.child
        account = account or self.account
        program = TreatmentProgram.objects.create(
            child=child,
            title=f"Программа {child.pk}-{number}",
            status=TreatmentProgram.Status.ACTIVE,
        )
        return ProgramBlock.objects.create(
            program=program,
            number=number,
            title=f"Каскад {child.pk}-{number}",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=planned,
            balance_account=account,
        )

    def _appointment(
        self,
        block,
        *,
        child=None,
        account=None,
        status=Appointment.Status.COMPLETED,
        attendance=Appointment.AttendanceStatus.ATTENDED,
        billing_decision=Appointment.BillingDecision.UNDECIDED,
        hour=9,
    ):
        child = child or block.program.child
        account = account or block.balance_account
        starts_at = _local(timezone.localdate() + timedelta(days=10), time(hour, 0))
        return Appointment.objects.create(
            child=child,
            staff_member=self.staff,
            service=self.service,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
            status=status,
            attendance_status=attendance,
            billing_decision=billing_decision,
            billing_account=account if billing_decision == Appointment.BillingDecision.CHARGE else None,
            program_block=block,
        )

    def test_participant_rows_are_authoritative_and_legacy_is_only_a_fallback(self):
        block = self._block(planned=2)
        participant_appointment = self._appointment(block, hour=9)
        legacy_appointment = self._appointment(block, hour=10)
        legacy_appointment.participants.all().delete()

        review = get_program_block_lifecycle_review(block)

        self.assertEqual(
            [(fact["kind"], fact["appointment_id"]) for fact in review.snapshot["facts"]],
            [
                ("legacy", legacy_appointment.pk),
                ("participant", participant_appointment.pk),
            ],
        )
        self.assertEqual(review.completed, 2)
        self.assertEqual(review.allocated, 2)
        self.assertEqual(review.remaining, 0)
        self.assertTrue(review.can_complete_normally)

    def test_no_show_is_missed_and_allocated_while_charge_is_independent(self):
        block = self._block(planned=1)
        appointment = self._appointment(
            block,
            status=Appointment.Status.NO_SHOW,
            attendance=Appointment.AttendanceStatus.MISSED,
            billing_decision=Appointment.BillingDecision.CHARGE,
        )
        participant = appointment.participants.get(child=self.child)
        AppointmentParticipant.objects.filter(pk=participant.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=self.account,
        )

        review = get_program_block_lifecycle_review(block)

        self.assertEqual(review.completed, 0)
        self.assertEqual(review.allocated, 1)
        self.assertEqual(review.missed, 1)
        self.assertEqual(review.charged, 1)
        self.assertEqual(review.remaining, 1)
        self.assertEqual(review.activity_status_label, ProgramBlock.Status.SCHEDULED.label)

    def test_review_is_one_query_and_has_the_cross_database_canonical_shape(self):
        block = self._block(planned=1)
        appointment = self._appointment(block)
        participant = appointment.participants.get(child=self.child)

        with self.assertNumQueries(1):
            review = get_program_block_lifecycle_review(block)

        expected = {
            "block": {
                "id": block.pk,
                "program_id": block.program_id,
                "child_id": self.child.pk,
                "program_status": TreatmentProgram.Status.ACTIVE,
                "number": block.number,
                "title": block.title,
                "status": ProgramBlock.Status.PLANNED,
                "planned_sessions": 1,
                "service_id": self.service.pk,
                "balance_account_id": self.account.pk,
            },
            "facts": [
                {
                    "kind": "participant",
                    "appointment_id": appointment.pk,
                    "participant_id": participant.pk,
                    "appointment_status": Appointment.Status.COMPLETED,
                    "participant_status": Appointment.Status.COMPLETED,
                    "attendance_status": Appointment.AttendanceStatus.ATTENDED,
                    "withdrawn": False,
                    "billing_decision": Appointment.BillingDecision.UNDECIDED,
                    "billing_account_id": None,
                }
            ],
        }
        self.assertEqual(review.snapshot, expected)
        self.assertEqual(
            json.dumps(review.snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    def test_withdrawn_participant_does_not_complete_or_allocate_but_keeps_charge(self):
        first = self._block(planned=1)
        second = self._block(
            child=self.other_child,
            account=self.other_account,
            planned=1,
        )
        start_date = timezone.localdate() + timedelta(days=10)
        preview = program_series.preview_group_series(
            blocks=[first, second],
            staff_members=[self.staff],
            room=self.room,
            title="Группа для снимка снятого участия",
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
            actor=self.director,
        )
        appointment = materialized.series.appointments.get()

        third_child = Child.objects.create(last_name="Каскад", first_name="Третий")
        third_account = BalanceAccount.objects.create(
            child=third_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("20"),
        )
        third = self._block(child=third_child, account=third_account, planned=1)
        joined = program_series.join_program_block_to_groups(
            block=third,
            appointments=[appointment],
            operation_key=uuid4(),
            actor=self.director,
        )
        participant = appointment.participants.get(child=third_child)
        AppointmentParticipant.objects.filter(pk=participant.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=third_account,
        )

        series_lifecycle.withdraw_future_joined_participations(
            joined.series,
            operation_key=uuid4(),
            actor=self.director,
            reason="Снимаем участие для проверки терминального снимка каскада.",
        )

        review = get_program_block_lifecycle_review(third)
        self.assertTrue(review.snapshot["facts"][0]["withdrawn"])
        self.assertEqual(review.completed, 0)
        self.assertEqual(review.allocated, 0)
        self.assertEqual(review.charged, 1)

    def test_administrator_needs_completed_plan_and_replay_precedes_terminal_state(self):
        block = self._block(planned=1)
        self._appointment(block)
        review = get_program_block_lifecycle_review(block)
        operation_key = uuid4()

        result = complete_block(
            block,
            actor=self.admin,
            reason="План каскада полностью выполнен.",
            operation_key=operation_key,
            expected_review_fingerprint=review.fingerprint,
            expected_event_id=0,
        )
        replay = complete_block(
            block,
            actor=self.admin,
            reason="План каскада полностью выполнен.",
            operation_key=operation_key,
            expected_review_fingerprint=review.fingerprint,
            expected_event_id=0,
        )

        self.assertEqual(result.block.status, ProgramBlock.Status.COMPLETED)
        self.assertFalse(result.reused_event)
        self.assertTrue(replay.reused_event)
        self.assertEqual(replay.event.pk, result.event.pk)

    def test_only_director_can_complete_below_plan(self):
        block = self._block(planned=2)
        review = get_program_block_lifecycle_review(block)
        with self.assertRaises(PermissionDenied):
            complete_block(
                block,
                actor=self.admin,
                reason="План пока не выполнен полностью.",
                operation_key=uuid4(),
                expected_review_fingerprint=review.fingerprint,
                expected_event_id=0,
            )

        result = complete_block(
            block,
            actor=self.director,
            reason="Руководитель завершает каскад досрочно.",
            operation_key=uuid4(),
            expected_review_fingerprint=review.fingerprint,
            expected_event_id=0,
        )
        self.assertEqual(result.block.status, ProgramBlock.Status.COMPLETED)

    def test_operation_key_reused_for_another_block_is_a_lifecycle_mismatch(self):
        first = self._block(planned=1)
        second = self._block(planned=1)
        first_review = get_program_block_lifecycle_review(first)
        second_review = get_program_block_lifecycle_review(second)
        operation_key = uuid4()
        complete_block(
            first,
            actor=self.director,
            reason="Руководитель закрывает первый каскад.",
            operation_key=operation_key,
            expected_review_fingerprint=first_review.fingerprint,
            expected_event_id=0,
        )

        with self.assertRaises(ProgramBlockLifecycleMismatch):
            complete_block(
                second,
                actor=self.director,
                reason="Руководитель закрывает второй каскад.",
                operation_key=operation_key,
                expected_review_fingerprint=second_review.fingerprint,
                expected_event_id=0,
            )

    def test_existing_block_can_close_after_its_parent_program_is_closed(self):
        block = self._block(planned=1)
        program_review = get_program_lifecycle_review(block.program)
        cancel_program(
            block.program,
            actor=self.director,
            reason="Руководитель закрывает программу до каскада.",
            operation_key=uuid4(),
            expected_review_fingerprint=program_review.fingerprint,
            expected_event_id=0,
        )
        review = get_program_block_lifecycle_review(block)
        self.assertEqual(
            review.snapshot["block"]["program_status"],
            TreatmentProgram.Status.CANCELLED,
        )

        result = cancel_block(
            block,
            actor=self.admin,
            reason="Администратор закрывает оставшийся каскад.",
            operation_key=uuid4(),
            expected_review_fingerprint=review.fingerprint,
            expected_event_id=0,
        )

        block.program.refresh_from_db()
        self.assertEqual(result.block.status, ProgramBlock.Status.CANCELLED)
        self.assertEqual(block.program.status, TreatmentProgram.Status.CANCELLED)
        self.assertTrue(get_program_block_lifecycle_review(result.block).is_terminal)
