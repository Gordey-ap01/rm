"""Integration coverage for date-effective staff schedules in availability flows."""

from __future__ import annotations

from datetime import time, timedelta
from uuid import uuid4

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentStaffAssignment,
    Child,
    ParentGuardian,
    Room,
    Service,
    StaffMember,
    TimeOffRequest,
)
from operations.schedule_validation import (
    build_local_datetime,
    iter_available_slot_times,
    staff_unavailability_reason,
)
from operations.services import appointments, rescheduling_plans, scheduling, staff_schedules
from operations.views.scheduling_helpers import suggested_transfer_slots


class StaffScheduleAvailabilityTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("schedule-admin", "admin@example.test", "x")
        self.staff = StaffMember.objects.create(full_name="Анна", status=StaffMember.Status.ACTIVE)
        self.assistant = StaffMember.objects.create(full_name="Борис", status=StaffMember.Status.ACTIVE)
        self.day = timezone.localdate() + timedelta(days=14)
        self.parent = ParentGuardian.objects.create(
            last_name="Иванов", first_name="Иван", phone="+79990000001"
        )
        self.child = Child.objects.create(
            last_name="Иванов", first_name="Петя", primary_parent=self.parent
        )
        self.room = Room.objects.create(name="Кабинет")
        self.service = Service.objects.create(
            name="Логопед", code="SCHEDULE-LOG", default_duration_minutes=30
        )

    def _week(self, *, weekday, closed=False, windows=None):
        windows = windows if windows is not None else [{"start": "09:00", "end": "18:00"}]
        return [
            {
                "weekday": number,
                "closed": closed if number == weekday else False,
                "windows": [] if number == weekday and closed else windows,
            }
            for number in range(7)
        ]

    def _approve_schedule(self, staff, week, *, effective_from=None):
        request = staff_schedules.create_request(
            staff_member=staff,
            effective_from=effective_from or self.day,
            week=week,
            reason="Согласованное изменение постоянного графика.",
            actor=self.admin,
            request_key=uuid4(),
        )
        return staff_schedules.decide_request(
            request,
            action="approve",
            reason="Руководитель согласовал изменение графика.",
            actor=self.admin,
            expected_decision_id=None,
            expected_revision_id=None,
            request_key=uuid4(),
        )

    def test_legacy_fallback_then_explicit_closed_day_at_effective_boundary(self):
        starts_at = build_local_datetime(self.day, time(10, 0))
        ends_at = starts_at + timedelta(minutes=30)
        self.assertEqual(staff_unavailability_reason(self.staff, starts_at, ends_at), "")

        self._approve_schedule(
            self.staff,
            self._week(weekday=self.day.weekday(), closed=True),
        )

        before = build_local_datetime(self.day - timedelta(days=1), time(8, 30))
        self.assertIn(
            "базового рабочего окна",
            staff_unavailability_reason(self.staff, before, before + timedelta(minutes=30)),
        )
        self.assertIn("рабочего графика", staff_unavailability_reason(self.staff, starts_at, ends_at))

    def test_multiple_windows_and_time_off_take_priority_in_both_helpers(self):
        self._approve_schedule(
            self.staff,
            self._week(
                weekday=self.day.weekday(),
                windows=[{"start": "08:00", "end": "10:00"}, {"start": "16:00", "end": "19:00"}],
            ),
        )
        morning = build_local_datetime(self.day, time(8, 30))
        gap = build_local_datetime(self.day, time(12, 0))
        self.assertEqual(staff_unavailability_reason(self.staff, morning, morning + timedelta(minutes=30)), "")
        self.assertIn("рабочего графика", staff_unavailability_reason(self.staff, gap, gap + timedelta(minutes=30)))

        TimeOffRequest.objects.create(
            staff_member=self.staff,
            request_type=TimeOffRequest.RequestType.VACATION,
            starts_on=self.day,
            ends_on=self.day,
            status=TimeOffRequest.Status.APPROVED,
        )
        reason = staff_unavailability_reason(self.staff, morning, morning + timedelta(minutes=30))
        self.assertIn("отпуск", reason)
        self.assertEqual(
            scheduling.is_within_availability(self.staff, morning, morning + timedelta(minutes=30)),
            reason,
        )

    def test_free_slot_search_respects_all_assigned_staff_and_explicit_bounds(self):
        self._approve_schedule(
            self.staff,
            self._week(weekday=self.day.weekday(), windows=[{"start": "08:00", "end": "19:00"}]),
        )
        self._approve_schedule(
            self.assistant,
            self._week(weekday=self.day.weekday(), windows=[{"start": "10:00", "end": "11:00"}]),
        )

        shared = scheduling.find_free_slots(
            self.day, 30, staff_members=[self.staff, self.assistant]
        )
        self.assertEqual([slot.time() for slot in shared], [time(10, 0), time(10, 30)])

        unrestricted = scheduling.find_free_slots(self.day, 30, staff_member=self.staff)
        bounded = scheduling.find_free_slots(
            self.day, 30, staff_member=self.staff, start_hour=9, end_hour=18
        )
        self.assertIn(time(8, 0), [slot.time() for slot in unrestricted])
        self.assertIn(time(18, 30), [slot.time() for slot in unrestricted])
        self.assertNotIn(time(8, 0), [slot.time() for slot in bounded])
        self.assertNotIn(time(18, 0), [slot.time() for slot in bounded])
        self.assertNotIn(time(18, 30), [slot.time() for slot in bounded])

    def test_slot_generation_rejects_non_positive_duration_and_step(self):
        with self.assertRaises(ValueError):
            list(iter_available_slot_times(self.day, 0))
        with self.assertRaises(ValueError):
            list(iter_available_slot_times(self.day, 30, slot_step_minutes=0))

    def test_transfer_and_plan_offer_early_late_slots_without_mutating_existing_appointment(self):
        appointment = appointments.create_appointment(
            child=self.child,
            staff_member=self.staff,
            service=self.service,
            starts_at=build_local_datetime(self.day, time(10, 0)),
            ends_at=build_local_datetime(self.day, time(10, 30)),
            room=self.room,
        )
        original_starts_at = appointment.starts_at
        self._approve_schedule(
            self.staff,
            self._week(weekday=self.day.weekday(), windows=[{"start": "08:00", "end": "19:00"}]),
        )

        transfer_times = {slot["time"] for slot in suggested_transfer_slots(appointment, days=1, limit=40)}
        plan = rescheduling_plans.create_plan_for_appointment(
            appointment, actor=self.admin, days=1, limit=40
        )
        plan_times = {
            timezone.localtime(step.proposed_starts_at).time().replace(tzinfo=None)
            for step in plan.steps.all()
        }

        self.assertIn("08:00", transfer_times)
        self.assertIn("18:30", transfer_times)
        self.assertIn(time(8, 0), plan_times)
        self.assertIn(time(18, 30), plan_times)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)
        self.assertEqual(appointment.starts_at, original_starts_at)

    def test_existing_group_assignment_requires_every_staff_window(self):
        appointment = appointments.create_appointment(
            child=self.child,
            staff_member=self.staff,
            service=self.service,
            starts_at=build_local_datetime(self.day, time(10, 0)),
            ends_at=build_local_datetime(self.day, time(10, 30)),
            room=self.room,
        )
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=self.assistant,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        self._approve_schedule(
            self.staff,
            self._week(weekday=self.day.weekday(), windows=[{"start": "08:00", "end": "19:00"}]),
        )
        self._approve_schedule(
            self.assistant,
            self._week(weekday=self.day.weekday(), windows=[{"start": "10:00", "end": "11:00"}]),
        )

        slots = suggested_transfer_slots(appointment, days=1, limit=40)
        self.assertEqual({slot["time"] for slot in slots}, {"10:30"})
