from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from operations.models import (
    Appointment,
    BalanceAccount,
    Child,
    FundingSource,
    ProgramBlock,
    Room,
    Service,
    StaffMember,
    TreatmentProgram,
)
from operations.services import program_scheduling, program_wizard


def _local(day, clock):
    return timezone.make_aware(
        datetime.combine(day, clock), timezone.get_current_timezone()
    )


class ProgramSchedulingProjectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.child = Child.objects.create(last_name="Проекция", first_name="Каскада")
        cls.staff = StaffMember.objects.create(full_name="Специалист проекции")
        cls.service = Service.objects.create(
            name="Услуга проекции каскада",
            code="PROGRAM-PROJECTION",
            default_duration_minutes=30,
            default_price=Decimal("1000"),
        )
        cls.room = Room.objects.create(name="Кабинет проекции", capacity=1)
        funding = FundingSource.objects.create(
            name="Источник проекции",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        cls.account = BalanceAccount.objects.create(
            child=cls.child,
            funding_source=funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("5"),
        )
        cls.program = TreatmentProgram.objects.create(
            child=cls.child,
            title="Программа проекции каскада",
            status=TreatmentProgram.Status.ACTIVE,
        )
        cls.block = ProgramBlock.objects.create(
            program=cls.program,
            number=1,
            title="Каскад проекции",
            service=cls.service,
            staff_member=cls.staff,
            planned_sessions=5,
            balance_account=cls.account,
        )
        cls.starts_at = _local(timezone.localdate() + timedelta(days=14), time(10, 0))

    def _preview(self):
        slot = program_wizard.ScheduleSlot(
            starts_at=self.starts_at,
            ends_at=self.starts_at + timedelta(minutes=30),
            staff_member=self.staff,
            room=self.room,
            room_capacity=1,
            room_occupancy=0,
        )
        return program_wizard.SchedulePreview(
            block=self.block,
            requested_count=1,
            allowed_count=1,
            funded_remaining=5,
            slots=[slot],
        )

    def test_wizard_allocation_does_not_regress_an_in_progress_block(self):
        self.block.status = ProgramBlock.Status.IN_PROGRESS
        self.block.save(update_fields=["status", "updated_at"])

        result = program_wizard.create_schedule_from_preview(self._preview())

        self.block.refresh_from_db()
        self.assertEqual(len(result.appointments), 1)
        self.assertEqual(self.block.status, ProgramBlock.Status.IN_PROGRESS)
        self.assertTrue(
            Appointment.objects.filter(
                pk=result.appointments[0].pk,
                participants__program_block=self.block,
            ).exists()
        )

    def test_projection_failure_rolls_back_the_created_appointment_and_status(self):
        original_projection = program_scheduling.mark_program_blocks_scheduled

        def update_then_fail(block_ids):
            original_projection(block_ids)
            self.assertEqual(
                ProgramBlock.objects.values_list("status", flat=True).get(pk=self.block.pk),
                ProgramBlock.Status.SCHEDULED,
            )
            raise RuntimeError("synthetic projection failure")

        with patch.object(
            program_scheduling,
            "mark_program_blocks_scheduled",
            side_effect=update_then_fail,
        ), self.assertRaisesMessage(RuntimeError, "synthetic projection failure"):
            program_wizard.create_schedule_from_preview(self._preview())

        self.block.refresh_from_db()
        self.assertEqual(self.block.status, ProgramBlock.Status.PLANNED)
        self.assertFalse(Appointment.objects.filter(program_block=self.block).exists())
