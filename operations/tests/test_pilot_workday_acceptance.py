from __future__ import annotations

from datetime import time, timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentAttendanceDecision,
    AppointmentConfirmation,
    AppointmentScheduleDecision,
    BalanceAccount,
    Child,
    FundingSource,
    LedgerEntry,
    ProgramBlock,
    ProgramBlockLifecycleEvent,
    Room,
    Service,
    StaffMember,
    TreatmentProgram,
)
from operations.services import billing as billing_svc, program_series
from operations.services.program_block_lifecycle import (
    complete_block,
    get_program_block_lifecycle_review,
)
from operations.services.program_lifecycle_overview import (
    BLOCK_READY,
    block_attention_queryset,
)
from operations.services.program_progress import get_program_block_progress

User = get_user_model()


class PilotWorkdayAcceptanceTests(TestCase):
    """One group workday through the public operator and lifecycle paths."""

    @classmethod
    def setUpTestData(cls):
        cls.director = User.objects.create_superuser("pilot-workday-director", password="x")
        cls.administrator = User.objects.create_user(
            "pilot-workday-administrator", password="x", is_staff=True
        )
        cls.specialist_user = User.objects.create_user("pilot-workday-specialist", password="x")
        cls.primary_staff = StaffMember.objects.create(
            user=cls.specialist_user,
            full_name="Специалист пилотного дня",
        )
        cls.assistant_staff = StaffMember.objects.create(full_name="Ассистент пилотного дня")
        cls.service = Service.objects.create(
            name="Групповая услуга пилотного дня",
            code="PILOT-WORKDAY-GROUP",
            default_duration_minutes=45,
            default_price=Decimal("1200.00"),
        )
        cls.room = Room.objects.create(
            name="Кабинет пилотного дня",
            allow_group_sessions=True,
            limit_staff_count=True,
            max_staff_count=2,
            limit_recipient_count=True,
            max_recipient_count=4,
        )
        cls.funding = FundingSource.objects.create(
            name="Оплата пилотного дня",
            source_type=FundingSource.SourceType.PERSONAL,
        )

    def setUp(self):
        self.work_day = timezone.localdate() + timedelta(days=5)

    def _program_block(self, suffix: str) -> tuple[ProgramBlock, BalanceAccount]:
        child = Child.objects.create(last_name="Пилот", first_name=suffix)
        account = BalanceAccount.objects.create(
            child=child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("2.00"),
        )
        program = TreatmentProgram.objects.create(
            child=child,
            title=f"Программа {suffix}",
            status=TreatmentProgram.Status.ACTIVE,
        )
        block = ProgramBlock.objects.create(
            program=program,
            number=1,
            title=f"Каскад {suffix}",
            service=self.service,
            staff_member=self.primary_staff,
            planned_sessions=1,
            balance_account=account,
        )
        return block, account

    def _group_workday(self):
        first_block, first_account = self._program_block("Первый")
        second_block, second_account = self._program_block("Второй")
        preview = program_series.preview_group_series(
            blocks=[first_block, second_block],
            staff_members=[self.primary_staff, self.assistant_staff],
            room=self.room,
            title="Пилотная группа",
            start_date=self.work_day,
            end_date=self.work_day,
            weekdays={self.work_day.weekday()},
            start_time=time(10, 0),
            duration_minutes=45,
            default_appointment_status=Appointment.Status.PROPOSED,
        )
        result = program_series.create_group_series(
            preview,
            operation_key=uuid4(),
            actor=self.administrator,
        )
        return (
            result.series.appointments.get(),
            (first_block, second_block),
            (first_account, second_account),
        )

    def _schedule_post(self, appointment: Appointment, staff: StaffMember, action="confirm"):
        return self.client.post(
            reverse("appointment_schedule_decide", args=[appointment.pk]),
            {
                "staff_member": str(staff.pk),
                "action": action,
                "reason": "Расписание проверено оператором пилотного дня.",
                "operation_key": str(uuid4()),
                "expected_schedule": (
                    f"{appointment.starts_at.isoformat()}|{appointment.ends_at.isoformat()}"
                ),
            },
        )

    def _attendance_post(self, appointment: Appointment, *, action: str, status: str):
        participant_statuses = {
            f"participant_status_{participant.pk}": status
            for participant in appointment.participants.order_by("pk")
        }
        return self.client.post(
            reverse("appointment_attendance_decide", args=[appointment.pk]),
            {
                "action": action,
                "reason": "Факт группы проверен оператором пилотного дня.",
                "note": "Пилотный рабочий день.",
                "operation_key": str(uuid4()),
                **participant_statuses,
            },
        )

    def _financial_snapshot(self, appointment, accounts):
        account_balances = []
        for account in accounts:
            account.refresh_from_db()
            account_balances.append((account.pk, account.current_balance))
        appointment.refresh_from_db()
        return {
            "appointment": (
                appointment.billing_decision,
                appointment.billing_account_id,
            ),
            "participants": list(
                appointment.participants.order_by("pk").values_list(
                    "pk", "billing_decision", "billing_account_id", "price_snapshot"
                )
            ),
            "ledger": list(
                LedgerEntry.objects.filter(appointment=appointment)
                .order_by("pk")
                .values_list(
                    "pk",
                    "account_id",
                    "appointment_participant_id",
                    "entry_type",
                    "amount",
                    "price_snapshot",
                )
            ),
            "balances": account_balances,
        }

    def test_administrator_runs_group_day_and_closes_blocks_without_changing_finance(self):
        appointment, blocks, accounts = self._group_workday()
        self.client.force_login(self.administrator)

        for staff in (self.primary_staff, self.assistant_staff):
            self.assertEqual(self._schedule_post(appointment, staff).status_code, 302)
        self.assertEqual(
            AppointmentScheduleDecision.objects.filter(appointment=appointment).count(),
            2,
        )
        self.assertFalse(AppointmentConfirmation.objects.filter(appointment=appointment).exists())

        response = self._attendance_post(
            appointment,
            action="completed",
            status=Appointment.AttendanceStatus.ATTENDED,
        )
        self.assertEqual(response.status_code, 302)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.COMPLETED)
        self.assertIsNone(appointment.specialist_marked_at)
        attendance_decision = appointment.attendance_decisions.get()
        self.assertEqual(attendance_decision.actor, self.administrator)
        self.assertEqual(attendance_decision.actor_role_snapshot, "administrator")
        self.assertTrue(attendance_decision.reason.strip())
        self.assertFalse(appointment.participants.exclude(marked_by_staff_at__isnull=True).exists())

        progress = get_program_block_progress(blocks)
        for block in blocks:
            self.assertEqual(progress[block.pk].planned, 1)
            self.assertEqual(progress[block.pk].completed, 1)
            self.assertEqual(progress[block.pk].remaining, 0)
            self.assertEqual(progress[block.pk].activity_status, ProgramBlock.Status.IN_PROGRESS)
        self.assertEqual(
            set(block_attention_queryset(BLOCK_READY).values_list("pk", flat=True)),
            {block.pk for block in blocks},
        )

        accounts_by_child = {account.child_id: account for account in accounts}
        for participant in appointment.participants.order_by("pk"):
            billing_svc.apply_decision(
                appointment,
                participant=participant,
                decision=Appointment.BillingDecision.CHARGE,
                account=accounts_by_child[participant.child_id],
                amount=Decimal("-1.00"),
                reason="Отдельное списание участника пилотной группы.",
                actor=self.administrator,
            )

        progress = get_program_block_progress(blocks)
        self.assertEqual([progress[block.pk].charged for block in blocks], [1, 1])
        financial_before_completion = self._financial_snapshot(appointment, accounts)
        self.assertEqual(len(financial_before_completion["ledger"]), 2)
        self.assertEqual(
            [balance for _, balance in financial_before_completion["balances"]],
            [Decimal("1.00"), Decimal("1.00")],
        )

        for participant in appointment.participants.order_by("pk"):
            billing_svc.apply_decision(
                appointment,
                participant=participant,
                decision=Appointment.BillingDecision.CHARGE,
                account=accounts_by_child[participant.child_id],
                amount=Decimal("-1.00"),
                reason="Отдельное списание участника пилотной группы.",
                actor=self.administrator,
            )
        self.assertEqual(
            self._financial_snapshot(appointment, accounts),
            financial_before_completion,
        )

        for block in blocks:
            review = get_program_block_lifecycle_review(block)
            self.assertTrue(review.can_complete_normally)
            self.assertEqual(review.completed, 1)
            self.assertEqual(review.charged, 1)
            complete_block(
                block,
                actor=self.administrator,
                reason="План каскада выполнен в пилотный рабочий день.",
                operation_key=uuid4(),
                expected_review_fingerprint=review.fingerprint,
                expected_event_id=0,
            )

        self.assertEqual(
            set(
                ProgramBlock.objects.filter(pk__in=[block.pk for block in blocks]).values_list(
                    "status", flat=True
                )
            ),
            {ProgramBlock.Status.COMPLETED},
        )
        lifecycle_events = list(ProgramBlockLifecycleEvent.objects.order_by("block_id"))
        self.assertEqual(len(lifecycle_events), 2)
        self.assertEqual(
            {event.actor_id for event in lifecycle_events},
            {self.administrator.pk},
        )
        self.assertEqual(
            {event.actor_role_snapshot for event in lifecycle_events},
            {"administrator"},
        )
        self.assertTrue(all(event.reason.strip() for event in lifecycle_events))
        self.assertEqual(
            self._financial_snapshot(appointment, accounts),
            financial_before_completion,
        )
        self.assertFalse(
            block_attention_queryset(BLOCK_READY)
            .filter(pk__in=[block.pk for block in blocks])
            .exists()
        )

        correction = self._attendance_post(
            appointment,
            action="not_completed",
            status=Appointment.AttendanceStatus.MISSED,
        )
        self.assertEqual(correction.status_code, 400)
        self.assertContains(correction, "финансовая корректировка", status_code=400)
        self.assertEqual(
            self._financial_snapshot(appointment, accounts),
            financial_before_completion,
        )
        self.assertEqual(
            AppointmentAttendanceDecision.objects.filter(appointment=appointment).count(),
            1,
        )

    def test_director_decisions_cannot_be_rewritten_by_administrator(self):
        appointment, blocks, _accounts = self._group_workday()
        participant_ids = list(appointment.participants.values_list("pk", flat=True))

        self.client.force_login(self.administrator)
        self.assertEqual(
            self._schedule_post(appointment, self.primary_staff).status_code,
            302,
        )
        self.client.force_login(self.director)
        self.assertEqual(
            self._schedule_post(appointment, self.primary_staff).status_code,
            302,
        )
        self.client.force_login(self.administrator)
        rejected_schedule = self._schedule_post(appointment, self.primary_staff, action="decline")
        self.assertEqual(rejected_schedule.status_code, 400)

        schedule_decisions = list(
            AppointmentScheduleDecision.objects.filter(appointment=appointment).order_by(
                "decision_number"
            )
        )
        self.assertEqual(len(schedule_decisions), 2)
        self.assertEqual(schedule_decisions[-1].actor, self.director)
        self.assertEqual(schedule_decisions[-1].actor_role_snapshot, "director")
        self.assertEqual(schedule_decisions[-1].supersedes, schedule_decisions[0])
        self.assertEqual(schedule_decisions[-1].decision, "confirmed")

        self.assertEqual(
            self._attendance_post(
                appointment,
                action="completed",
                status=Appointment.AttendanceStatus.ATTENDED,
            ).status_code,
            302,
        )
        self.client.force_login(self.director)
        self.assertEqual(
            self._attendance_post(
                appointment,
                action="completed",
                status=Appointment.AttendanceStatus.ATTENDED,
            ).status_code,
            302,
        )
        self.client.force_login(self.administrator)
        rejected_attendance = self._attendance_post(
            appointment,
            action="not_completed",
            status=Appointment.AttendanceStatus.MISSED,
        )
        self.assertEqual(rejected_attendance.status_code, 400)

        attendance_decisions = list(
            AppointmentAttendanceDecision.objects.filter(appointment=appointment).order_by(
                "decision_number"
            )
        )
        self.assertEqual(len(attendance_decisions), 2)
        self.assertEqual(attendance_decisions[-1].actor, self.director)
        self.assertEqual(attendance_decisions[-1].actor_role_snapshot, "director")
        self.assertEqual(attendance_decisions[-1].supersedes, attendance_decisions[0])
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.COMPLETED)
        self.assertEqual(
            set(
                appointment.participants.filter(pk__in=participant_ids).values_list(
                    "attendance_status", flat=True
                )
            ),
            {Appointment.AttendanceStatus.ATTENDED},
        )
        progress = get_program_block_progress(blocks)
        self.assertEqual({progress[block.pk].completed for block in blocks}, {1})
        self.assertFalse(LedgerEntry.objects.filter(appointment=appointment).exists())
