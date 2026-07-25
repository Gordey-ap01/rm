from datetime import datetime, time, timedelta
from queue import Queue
from threading import Barrier, Thread
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.http import QueryDict
from django.test import TransactionTestCase
from django.utils import timezone

from operations.forms import AppointmentForm
from operations.models import Appointment, Child, Room, Service, StaffMember
from operations.schedule_writes import lock_schedule_write
from operations.services import appointments as appointment_svc


class RoomCapacityPostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.children = [
            Child.objects.create(last_name="Capacity", first_name=f"Child {index}")
            for index in range(1, 4)
        ]
        self.staff = [
            StaffMember.objects.create(full_name=f"Capacity Staff {index}")
            for index in range(1, 4)
        ]
        self.service = Service.objects.create(
            name="Capacity service",
            code="CAPACITY",
            default_duration_minutes=30,
        )
        self.room = Room.objects.create(
            name="Concurrent capacity room",
            capacity=2,
            limit_staff_count=True,
            max_staff_count=2,
            limit_recipient_count=True,
            max_recipient_count=2,
            allow_group_sessions=True,
        )
        self.day = timezone.localdate() + timedelta(days=20)
        self.starts_at = timezone.make_aware(
            datetime.combine(self.day, time(10, 0)),
            timezone.get_current_timezone(),
        )
        self.ends_at = self.starts_at + timedelta(minutes=30)
        Appointment.objects.create(
            child=self.children[0],
            staff_member=self.staff[0],
            service=self.service,
            room=self.room,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
        )

    def _form_data(self, child: Child, staff: StaffMember) -> QueryDict:
        data = QueryDict(mutable=True)
        data.update(
            {
                "child": str(child.pk),
                "service": str(self.service.pk),
                "staff_member": str(staff.pk),
                "room": str(self.room.pk),
                "status": Appointment.Status.CONFIRMED,
                "date": self.day.isoformat(),
                "time": "10:00",
                "duration_minutes": "30",
                "session_type": Appointment.SessionType.INDIVIDUAL,
                "admin_note": "",
            }
        )
        data.setlist("participants", [str(child.pk)])
        data.setlist("staff_members", [str(staff.pk)])
        return data

    def _run_competing_writers(self, writer) -> Queue:
        barrier = Barrier(2)
        outcomes = Queue()

        def run(child_id: int, staff_id: int) -> None:
            close_old_connections()
            try:
                child = Child.objects.get(pk=child_id)
                staff = StaffMember.objects.get(pk=staff_id)
                writer(child, staff, barrier)
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put("saved")
            finally:
                connection.close()

        threads = [
            Thread(target=run, args=(self.children[1].pk, self.staff[1].pk)),
            Thread(target=run, args=(self.children[2].pk, self.staff[2].pk)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        return outcomes

    def _assert_last_place_is_serialized(self, outcomes: Queue) -> None:
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertEqual(results.count("saved"), 1)
        errors = [item for item in results if item != "saved"]
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValidationError)
        self.assertIn("Ограничение кабинета", str(errors[0]))

        active = Appointment.objects.filter(
            room=self.room,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            status=Appointment.Status.CONFIRMED,
        )
        self.assertEqual(active.count(), 2)

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентная вместимость проверяется только на PostgreSQL.",
    )
    def test_concurrent_form_saves_cannot_take_the_same_last_place(self):
        def writer(child: Child, staff: StaffMember, barrier: Barrier) -> None:
            form = AppointmentForm(self._form_data(child, staff))
            self.assertTrue(form.is_valid(), form.errors)
            barrier.wait(timeout=10)
            form.save()

        outcomes = self._run_competing_writers(writer)

        self._assert_last_place_is_serialized(outcomes)

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентная вместимость проверяется только на PostgreSQL.",
    )
    def test_concurrent_service_creates_cannot_take_the_same_last_place(self):
        def writer(child: Child, staff: StaffMember, barrier: Barrier) -> None:
            barrier.wait(timeout=10)
            appointment_svc.create_appointment(
                child=child,
                staff_member=staff,
                service=self.service,
                room=Room.objects.get(pk=self.room.pk),
                starts_at=self.starts_at,
                ends_at=self.ends_at,
            )

        outcomes = self._run_competing_writers(writer)

        self._assert_last_place_is_serialized(outcomes)

    @skipUnless(
        connection.vendor == "postgresql",
        "Совместимость блокировок проверяется только на PostgreSQL.",
    )
    def test_locking_appointment_without_room_does_not_lock_nullable_join(self):
        appointment = Appointment.objects.create(
            child=self.children[1],
            staff_member=self.staff[1],
            service=self.service,
            starts_at=self.starts_at + timedelta(hours=1),
            ends_at=self.ends_at + timedelta(hours=1),
        )

        with lock_schedule_write(appointment_id=appointment.pk) as locked:
            self.assertEqual(locked.appointment.pk, appointment.pk)
            self.assertEqual(locked.rooms_by_id, {})
