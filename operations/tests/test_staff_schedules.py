from datetime import datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentStaffAssignment,
    Child,
    Room,
    Service,
    StaffMember,
    StaffScheduleChangeRequest,
    StaffScheduleRevision,
)
from operations.services import staff_schedules


def _local(day, clock):
    return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())


def _week(*, closed=()):
    return [
        {
            "weekday": weekday,
            "closed": weekday in closed,
            "windows": [] if weekday in closed else [{"start": "09:00", "end": "18:00"}],
        }
        for weekday in range(7)
    ]


class StaffScheduleFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("schedule-admin", password="x", is_staff=True)
        cls.director = User.objects.create_superuser("schedule-director", password="x")
        cls.specialist_user = User.objects.create_user("schedule-specialist", password="x")
        cls.other_user = User.objects.create_user("other-schedule-specialist", password="x")
        cls.staff = StaffMember.objects.create(user=cls.specialist_user, full_name="Специалист графика")
        cls.other_staff = StaffMember.objects.create(user=cls.other_user, full_name="Другой специалист")
        cls.child = Child.objects.create(last_name="График", first_name="Ребенок")
        cls.service = Service.objects.create(
            name="Услуга графика",
            code="STAFF-SCHEDULE",
            default_duration_minutes=30,
            default_price=Decimal("1000.00"),
        )
        cls.room = Room.objects.create(name="Кабинет графика")

    def create_request(self, *, actor=None, week=None, effective_from=None):
        return staff_schedules.create_request(
            staff_member=self.staff,
            effective_from=effective_from or timezone.localdate() + timedelta(days=3),
            week=week or _week(),
            reason="Постоянное изменение рабочего графика.",
            actor=actor or self.specialist_user,
            request_key=uuid4(),
        )

    def decide(self, request, *, actor=None, action="approve", key=None):
        return staff_schedules.decide_request(
            request,
            action=action,
            reason="Решение по постоянному графику принято.",
            actor=actor or self.admin,
            expected_decision_id=request.current_decision.pk if request.current_decision else None,
            expected_revision_id=staff_schedules.latest_revision_id(request.staff_member),
            request_key=key or uuid4(),
        )


class StaffScheduleServiceTests(StaffScheduleFixture):
    def test_specialist_creates_only_own_request_and_replay_returns_same_row(self):
        key = uuid4()
        kwargs = {
            "staff_member": self.staff,
            "effective_from": timezone.localdate() + timedelta(days=3),
            "week": _week(),
            "reason": "Постоянное изменение рабочего графика.",
            "actor": self.specialist_user,
            "request_key": key,
        }
        first = staff_schedules.create_request(**kwargs)
        second = staff_schedules.create_request(**kwargs)

        self.assertEqual(first.pk, second.pk)
        with self.assertRaises(PermissionDenied):
            staff_schedules.create_request(
                **{**kwargs, "staff_member": self.other_staff, "request_key": uuid4()}
            )
        self.staff.can_use_mobile = False
        self.staff.save(update_fields=["can_use_mobile", "updated_at"])
        with self.assertRaises(PermissionDenied):
            staff_schedules.create_request(**{**kwargs, "request_key": uuid4()})

    def test_create_replay_is_returned_even_after_its_effective_date_has_passed(self):
        key = uuid4()
        request = staff_schedules.create_request(
            staff_member=self.staff,
            effective_from=timezone.localdate() + timedelta(days=2),
            week=_week(),
            reason="Постоянное изменение рабочего графика.",
            actor=self.specialist_user,
            request_key=key,
        )
        request.effective_from = timezone.localdate()
        request.save(update_fields=["effective_from", "updated_at"])

        replay = staff_schedules.create_request(
            staff_member=self.staff,
            effective_from=timezone.localdate() + timedelta(days=2),
            week=_week(),
            reason="Постоянное изменение рабочего графика.",
            actor=self.specialist_user,
            request_key=key,
        )
        self.assertEqual(replay.pk, request.pk)

    def test_week_requires_seven_explicit_days_and_non_overlapping_same_day_windows(self):
        with self.assertRaisesMessage(ValidationError, "семь дней"):
            self.create_request(week=_week()[:-1])
        overlap = _week()
        overlap[0]["windows"] = [
            {"start": "09:00", "end": "12:00"},
            {"start": "11:00", "end": "13:00"},
        ]
        with self.assertRaisesMessage(ValidationError, "не должны пересекаться"):
            self.create_request(week=overlap)
        with self.assertRaisesMessage(ValidationError, "не раньше завтрашнего"):
            self.create_request(effective_from=timezone.localdate())

    def test_admin_approval_is_effective_and_director_can_confirm_without_new_revision(self):
        request = self.create_request(week=_week(closed={2}))
        approval = self.decide(request)
        request.refresh_from_db()

        self.assertEqual(request.status, StaffScheduleChangeRequest.Status.APPROVED)
        self.assertTrue(approval.requires_director_review)
        self.assertEqual(staff_schedules.effective_windows(self.staff, request.effective_from), [
            (time(9), time(18))
        ])
        self.assertEqual(staff_schedules.effective_windows(self.staff, request.effective_from + timedelta(days=(2 - request.effective_from.weekday()) % 7)), [])
        approval_token = staff_schedules.latest_revision_id(self.staff)
        confirmation = self.decide(request, actor=self.director, action="confirm")

        self.assertFalse(confirmation.requires_director_review)
        self.assertNotEqual(staff_schedules.latest_revision_id(self.staff), approval_token)
        self.assertEqual(staff_schedules.latest_revision_id(self.staff), confirmation.pk)
        self.assertEqual(request.revisions.count(), 1)

    def test_stale_decision_and_replay_are_rejected_or_returned_deterministically(self):
        request = self.create_request()
        key = uuid4()
        decision = self.decide(request, key=key)
        replay = staff_schedules.decide_request(
            request,
            action="approve",
            reason="Решение по постоянному графику принято.",
            actor=self.admin,
            expected_decision_id=None,
            expected_revision_id=None,
            request_key=key,
        )
        self.assertEqual(replay.pk, decision.pk)
        with self.assertRaisesMessage(ValidationError, "другое решение"):
            staff_schedules.decide_request(
                request,
                action="reject",
                reason="Нужна повторная проверка графика.",
                actor=self.admin,
                expected_decision_id=None,
                expected_revision_id=None,
                request_key=uuid4(),
            )

    def test_director_decision_cannot_be_overridden_by_administrator(self):
        request = self.create_request()
        self.decide(request, actor=self.director)
        with self.assertRaises(PermissionDenied):
            self.decide(request, action="reject", actor=self.admin)

    def test_second_request_for_same_effective_date_fails_without_database_error(self):
        day = timezone.localdate() + timedelta(days=3)
        first = self.create_request(effective_from=day)
        self.decide(first)
        second = self.create_request(effective_from=day)

        with self.assertRaisesMessage(ValidationError, "уже утверждена другая версия"):
            self.decide(second)
        second.refresh_from_db()
        self.assertEqual(second.status, StaffScheduleChangeRequest.Status.PENDING)
        self.assertFalse(second.decisions.exists())

    def test_late_rejection_preserves_history_and_restores_legacy_from_tomorrow(self):
        request = self.create_request(effective_from=timezone.localdate() + timedelta(days=2))
        approval = self.decide(request)
        revision = request.revisions.get()
        revision.effective_from = timezone.localdate()
        revision.save(update_fields=["effective_from", "updated_at"])
        request.refresh_from_db()
        rejection = self.decide(request, actor=self.director, action="reject")

        revision.refresh_from_db()
        self.assertTrue(revision.is_current)
        successor = StaffScheduleRevision.objects.get(decision=rejection)
        self.assertEqual(successor.effective_from, timezone.localdate() + timedelta(days=1))
        self.assertFalse(successor.uses_legacy)
        self.assertEqual(
            staff_schedules.effective_windows(self.staff, successor.effective_from),
            [(time(9), time(18))],
        )
        self.assertEqual(staff_schedules.latest_revision_id(self.staff), rejection.pk)
        approval.refresh_from_db()
        self.assertFalse(approval.is_current)

    def test_repeated_terminal_action_is_a_controlled_validation_error(self):
        request = self.create_request()
        self.decide(request)
        with self.assertRaisesMessage(ValidationError, "уже согласована"):
            self.decide(request, actor=self.director)

        rejected = self.create_request()
        self.decide(rejected, action="reject")
        with self.assertRaisesMessage(ValidationError, "уже отклонена"):
            self.decide(rejected, action="reject", actor=self.director)

    def test_future_rejection_invalidates_another_request_stale_version_token(self):
        first = self.create_request(effective_from=timezone.localdate() + timedelta(days=3))
        self.decide(first)
        token_after_approval = staff_schedules.latest_revision_id(self.staff)
        second = self.create_request(effective_from=timezone.localdate() + timedelta(days=4))
        self.decide(first, action="reject", actor=self.director)

        with self.assertRaisesMessage(ValidationError, "График уже изменился"):
            staff_schedules.decide_request(
                second,
                action="approve",
                reason="Согласование второй заявки после отмены первой.",
                actor=self.admin,
                expected_decision_id=None,
                expected_revision_id=token_after_approval,
                request_key=uuid4(),
            )

    def test_request_with_audit_history_cannot_be_deleted(self):
        request = self.create_request()
        self.decide(request)

        with self.assertRaises(ProtectedError):
            request.delete()

    def test_late_override_cannot_replace_a_newer_version_effective_tomorrow(self):
        first = self.create_request(effective_from=timezone.localdate() + timedelta(days=2))
        self.decide(first)
        first_revision = first.revisions.get()
        first_revision.effective_from = timezone.localdate()
        first_revision.save(update_fields=["effective_from", "updated_at"])
        second = self.create_request(effective_from=timezone.localdate() + timedelta(days=1))
        self.decide(second)

        with self.assertRaisesMessage(ValidationError, "более новая утверждённая версия"):
            self.decide(first, actor=self.director, action="reject")
        self.assertEqual(first.revisions.filter(is_current=True).count(), 1)
        self.assertEqual(second.revisions.filter(is_current=True).count(), 1)

    def test_impact_includes_only_assignment_outside_proposed_week(self):
        day = timezone.localdate() + timedelta(days=3)
        request = self.create_request(
            effective_from=day,
            week=_week(closed={day.weekday()}),
        )
        appointment = Appointment.objects.create(
            child=self.child,
            staff_member=self.other_staff,
            service=self.service,
            room=self.room,
            starts_at=_local(day, time(10)),
            ends_at=_local(day, time(10, 30)),
        )
        AppointmentStaffAssignment.objects.create(
            appointment=appointment,
            staff_member=self.staff,
            role=AppointmentStaffAssignment.Role.ASSISTANT,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
        )

        self.assertEqual(staff_schedules.impact_rows(request), [appointment])

    def test_impact_omits_an_assignment_that_fits_the_proposed_window(self):
        day = timezone.localdate() + timedelta(days=3)
        request = self.create_request(effective_from=day)
        appointment = Appointment.objects.create(
            child=self.child,
            staff_member=self.staff,
            service=self.service,
            room=self.room,
            starts_at=_local(day, time(10)),
            ends_at=_local(day, time(10, 30)),
        )

        self.assertNotIn(appointment, staff_schedules.impact_rows(request))
