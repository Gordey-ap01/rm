from datetime import timedelta
from queue import Queue
from threading import Barrier, Thread
from unittest import skipUnless

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.db.models.deletion import ProtectedError, RestrictedError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    StaffMember,
    TimeOffRequest,
    TimeOffRequestDecision,
)
from operations.services import time_off_decisions as time_off_svc


class TimeOffDecisionFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            "time-off-admin",
            password="x",
            is_staff=True,
        )
        cls.director = User.objects.create_superuser(
            "time-off-director",
            password="x",
        )
        cls.specialist_user = User.objects.create_user(
            "time-off-specialist",
            password="x",
        )
        cls.staff = StaffMember.objects.create(
            user=cls.specialist_user,
            full_name="Специалист по отсутствиям",
        )

    def create_request(
        self,
        *,
        request_type=TimeOffRequest.RequestType.DAY_OFF,
        days: int = 1,
    ) -> TimeOffRequest:
        starts_on = timezone.localdate() + timedelta(days=10)
        return TimeOffRequest.objects.create(
            staff_member=self.staff,
            request_type=request_type,
            starts_on=starts_on,
            ends_on=starts_on + timedelta(days=days - 1),
            reason="Тестовая заявка специалиста.",
        )


class TimeOffDecisionServiceTests(TimeOffDecisionFixture):
    def test_director_priority_classification(self):
        cases = [
            (TimeOffRequest.RequestType.DAY_OFF, 1, False),
            (TimeOffRequest.RequestType.SICK, 1, False),
            (TimeOffRequest.RequestType.OTHER, 1, False),
            (TimeOffRequest.RequestType.VACATION, 1, True),
            (TimeOffRequest.RequestType.SCHEDULE_CHANGE, 1, True),
            (TimeOffRequest.RequestType.DAY_OFF, 2, True),
        ]

        for request_type, days, expected in cases:
            with self.subTest(request_type=request_type, days=days):
                request = self.create_request(
                    request_type=request_type,
                    days=days,
                )
                self.assertEqual(request.director_priority_required, expected)

    def test_administrator_operational_decision_is_effective_and_complete(self):
        request = self.create_request()

        record = time_off_svc.resolve_manually(
            request,
            action="approve",
            reason="Однодневный отгул согласован администратором.",
            actor=self.admin,
        )

        request.refresh_from_db()
        self.assertEqual(request.status, TimeOffRequest.Status.APPROVED)
        self.assertFalse(record.director_priority)
        self.assertFalse(record.awaits_director_review)
        self.assertFalse(
            time_off_svc.attention_queryset().filter(pk=request.pk).exists()
        )

    def test_administrator_vacation_decision_remains_for_director_review(self):
        request = self.create_request(
            request_type=TimeOffRequest.RequestType.VACATION,
            days=5,
        )

        record = time_off_svc.resolve_manually(
            request,
            action="approve",
            reason="Отпуск оперативно внесен администратором.",
            actor=self.admin,
        )

        request.refresh_from_db()
        self.assertEqual(request.status, TimeOffRequest.Status.APPROVED)
        self.assertTrue(record.director_priority)
        self.assertTrue(record.awaits_director_review)
        self.assertTrue(
            time_off_svc.attention_queryset().filter(pk=request.pk).exists()
        )

    def test_director_can_confirm_or_override_administrator(self):
        request = self.create_request(
            request_type=TimeOffRequest.RequestType.VACATION,
            days=5,
        )
        first = time_off_svc.resolve_manually(
            request,
            action="approve",
            reason="Предварительное решение администратора.",
            actor=self.admin,
        )

        second = time_off_svc.resolve_manually(
            request,
            action="reject",
            reason="Руководитель отклонил отпуск после проверки.",
            actor=self.director,
        )

        first.refresh_from_db()
        request.refresh_from_db()
        self.assertFalse(first.is_current)
        self.assertTrue(second.is_current)
        self.assertEqual(second.supersedes, first)
        self.assertEqual(
            second.source,
            TimeOffRequestDecision.Source.DIRECTOR_MANUAL,
        )
        self.assertFalse(second.awaits_director_review)
        self.assertEqual(request.status, TimeOffRequest.Status.REJECTED)
        self.assertFalse(
            time_off_svc.attention_queryset().filter(pk=request.pk).exists()
        )

    def test_administrator_cannot_override_director(self):
        request = self.create_request()
        time_off_svc.resolve_manually(
            request,
            action="reject",
            reason="Окончательное решение руководителя.",
            actor=self.director,
        )

        with self.assertRaises(PermissionDenied):
            time_off_svc.resolve_manually(
                request,
                action="approve",
                reason="Попытка администратора изменить решение.",
                actor=self.admin,
            )

        self.assertEqual(request.decision_history.count(), 1)

    def test_administrator_cannot_override_legacy_director_summary(self):
        request = self.create_request()
        request.status = TimeOffRequest.Status.APPROVED
        request.decided_by = self.director
        request.decided_at = timezone.now()
        request.save(
            update_fields=["status", "decided_by", "decided_at", "updated_at"]
        )

        with self.assertRaises(PermissionDenied):
            time_off_svc.resolve_manually(
                request,
                action="reject",
                reason="Попытка изменить старое решение руководителя.",
                actor=self.admin,
            )

    def test_reason_is_required(self):
        request = self.create_request()

        with self.assertRaisesMessage(ValueError, "не короче 5"):
            time_off_svc.resolve_manually(
                request,
                action="approve",
                reason="нет",
                actor=self.admin,
            )

    def test_source_must_match_actor_role_snapshot(self):
        request = self.create_request()

        with self.assertRaises(ValidationError):
            TimeOffRequestDecision.objects.create(
                time_off_request=request,
                decision=TimeOffRequestDecision.Decision.APPROVED,
                source=TimeOffRequestDecision.Source.ADMINISTRATOR_MANUAL,
                actor=self.admin,
                actor_role_snapshot=TimeOffRequestDecision.ActorRole.DIRECTOR,
                note="Некорректная роль источника.",
                director_priority=False,
            )

    def test_manual_decision_protects_actor_identity(self):
        request = self.create_request()
        time_off_svc.resolve_manually(
            request,
            action="approve",
            reason="Автор решения должен остаться в журнале.",
            actor=self.admin,
        )

        with self.assertRaises(ProtectedError):
            self.admin.delete()

    def test_previous_decision_is_restricted_but_parent_cascades_history(self):
        request = self.create_request(
            request_type=TimeOffRequest.RequestType.VACATION,
            days=5,
        )
        first = time_off_svc.resolve_manually(
            request,
            action="approve",
            reason="Первое решение для проверки удаления.",
            actor=self.admin,
        )
        time_off_svc.resolve_manually(
            request,
            action="reject",
            reason="Второе решение для проверки удаления.",
            actor=self.director,
        )

        with self.assertRaises(RestrictedError):
            first.delete()

        request_id = request.pk
        request.delete()
        self.assertFalse(
            TimeOffRequestDecision.objects.filter(
                time_off_request_id=request_id
            ).exists()
        )


class TimeOffDecisionViewTests(TimeOffDecisionFixture):
    def test_administrator_decision_creates_history(self):
        request = self.create_request()
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("time_off_request_decide", args=[request.pk]),
            {
                "action": "approve",
                "reason": "Решение принято администратором.",
                "next": f"{reverse('work_queue')}#queue-time-off",
            },
        )

        self.assertEqual(
            response.url,
            f"{reverse('work_queue')}#queue-time-off",
        )
        request.refresh_from_db()
        self.assertEqual(request.status, TimeOffRequest.Status.APPROVED)
        self.assertEqual(request.decision_history.count(), 1)

    def test_priority_decision_stays_in_work_queue_for_director(self):
        request = self.create_request(
            request_type=TimeOffRequest.RequestType.VACATION,
            days=5,
        )
        time_off_svc.resolve_manually(
            request,
            action="approve",
            reason="Отпуск предварительно согласован.",
            actor=self.admin,
        )
        self.client.force_login(self.director)

        response = self.client.get(reverse("work_queue"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "требуется контроль руководителя")
        self.assertContains(
            response,
            reverse("time_off_request_decide", args=[request.pk]),
        )

    def test_tomorrow_page_shows_reason_input_for_review(self):
        request = self.create_request(
            request_type=TimeOffRequest.RequestType.SCHEDULE_CHANGE,
        )
        time_off_svc.resolve_manually(
            request,
            action="approve",
            reason="Изменение внесено до ответа руководителя.",
            actor=self.admin,
        )
        self.client.force_login(self.director)

        response = self.client.get(reverse("tomorrow"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Основание решения")
        self.assertContains(response, "требуется контроль руководителя")

    def test_specialist_sees_effective_status_and_pending_review(self):
        request = self.create_request(
            request_type=TimeOffRequest.RequestType.VACATION,
            days=5,
        )
        time_off_svc.resolve_manually(
            request,
            action="approve",
            reason="Отпуск внесен в расписание.",
            actor=self.admin,
        )
        self.client.force_login(self.specialist_user)

        response = self.client.get(reverse("specialist_home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ожидает контроля руководителя")
        self.assertContains(response, "Отпуск внесен в расписание.")

    def test_non_operator_cannot_resolve_request(self):
        request = self.create_request()
        self.client.force_login(self.specialist_user)

        response = self.client.post(
            reverse("time_off_request_decide", args=[request.pk]),
            {
                "action": "approve",
                "reason": "Попытка специалиста решить заявку.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)
        request.refresh_from_db()
        self.assertEqual(request.status, TimeOffRequest.Status.PENDING)

    def test_decision_endpoint_is_post_only(self):
        request = self.create_request()
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse("time_off_request_decide", args=[request.pk])
        )

        self.assertEqual(response.status_code, 405)


class TimeOffDecisionPostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            "time-off-pg-admin",
            password="x",
            is_staff=True,
        )
        staff = StaffMember.objects.create(full_name="PG Специалист отсутствий")
        starts_on = timezone.localdate() + timedelta(days=10)
        self.request = TimeOffRequest.objects.create(
            staff_member=staff,
            request_type=TimeOffRequest.RequestType.VACATION,
            starts_on=starts_on,
            ends_on=starts_on + timedelta(days=4),
            reason="Конкурентная заявка.",
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентная блокировка проверяется только на PostgreSQL.",
    )
    def test_concurrent_decisions_leave_one_current_record(self):
        barrier = Barrier(2)
        errors = Queue()

        def decide(action: str, reason: str) -> None:
            close_old_connections()
            try:
                actor = User.objects.get(pk=self.admin.pk)
                request = TimeOffRequest.objects.get(pk=self.request.pk)
                barrier.wait(timeout=10)
                time_off_svc.resolve_manually(
                    request,
                    action=action,
                    reason=reason,
                    actor=actor,
                )
            except BaseException as exc:
                errors.put(exc)
            finally:
                connection.close()

        threads = [
            Thread(
                target=decide,
                args=("approve", "Первое конкурентное решение администратора."),
            ),
            Thread(
                target=decide,
                args=("reject", "Второе конкурентное решение администратора."),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        if not errors.empty():
            raise errors.get()

        decisions = self.request.decision_history.order_by("created_at")
        self.assertEqual(decisions.count(), 2)
        self.assertEqual(decisions.filter(is_current=True).count(), 1)
        self.assertIsNotNone(decisions.get(is_current=True).supersedes_id)
