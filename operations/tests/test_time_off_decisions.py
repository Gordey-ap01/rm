from datetime import timedelta
from queue import Queue
from threading import Barrier, Thread
from unittest import skipUnless

from django.contrib.auth.models import Group, User
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
    def test_personal_header_keeps_approved_request_visible_through_director_review(self):
        request = self.create_request(request_type=TimeOffRequest.RequestType.SICK, days=3)
        time_off_svc.resolve_manually(
            request, action="approve", reason="Больничный согласован администратором.", actor=self.admin,
        )
        self.client.force_login(self.specialist_user)
        response = self.client.get(reverse("specialist_home"))
        summary = response.context["personal_time_off_summary"]
        self.assertEqual((summary["total"], summary["approved"], summary["review"]), (1, 1, 1))
        self.assertContains(response, "Мои заявки · 1")
        self.assertContains(response, "Согласовано: 1")
        self.assertContains(response, "На контроле руководителя: 1")
        self.assertContains(response, f'href="{reverse("specialist_home")}#staff-time-off"')

        time_off_svc.resolve_manually(
            request, action="approve", reason="Руководитель подтвердил больничный.", actor=self.director,
        )
        response = self.client.get(reverse("specialist_home"))
        summary = response.context["personal_time_off_summary"]
        self.assertEqual((summary["total"], summary["approved"], summary["awaiting_final"]), (1, 1, 0))
        self.assertContains(response, "Мои заявки · 1")
        self.assertContains(response, "Руководитель подтвердил больничный.")
        self.assertNotContains(response, "На контроле руководителя:")

    def test_personal_header_never_uses_selected_or_another_staff_profile(self):
        self.create_request()
        other_user = User.objects.create_user("other-requests-specialist", password="x")
        other_staff = StaffMember.objects.create(user=other_user, full_name="Другой специалист")
        self.client.force_login(other_user)
        response = self.client.get(reverse("specialist_home"), {"staff_id": self.staff.pk})
        self.assertEqual(response.context["personal_time_off_summary"]["total"], 0)
        self.assertEqual(response.context["staff"], other_staff)
        self.assertContains(response, "Пока не отправлены")
        self.assertNotContains(response, "Тестовая заявка специалиста.")

        for operator in (self.admin, self.director):
            self.client.force_login(operator)
            response = self.client.get(reverse("specialist_home"), {"staff_id": self.staff.pk})
            self.assertIsNone(response.context["personal_time_off_summary"])
            self.assertNotContains(response, 'class="personal-requests-link"')

        other_user.is_staff = True
        other_user.save(update_fields=["is_staff"])
        self.client.force_login(other_user)
        response = self.client.get(reverse("specialist_home"), {"staff_id": self.staff.pk})
        self.assertEqual(response.context["personal_time_off_summary"]["total"], 0)
        self.assertEqual(response.context["time_off_summary"]["total"], 1)

        other_staff.can_use_mobile = False
        other_staff.save(update_fields=["can_use_mobile"])
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, 'class="personal-requests-link"')
        self.client.logout()
        response = self.client.get(reverse("login"))
        self.assertNotContains(response, 'class="personal-requests-link"')

    def test_request_counts_include_history_beyond_first_page(self):
        oldest = self.create_request()
        for _ in range(11):
            item = self.create_request()
            item.status = TimeOffRequest.Status.CANCELLED
            item.save(update_fields=["status"])
        self.client.force_login(self.specialist_user)
        response = self.client.get(reverse("specialist_home"))
        summary = response.context["personal_time_off_summary"]
        self.assertEqual((summary["total"], summary["pending"], summary["cancelled"]), (12, 1, 11))
        self.assertEqual(len(response.context["time_off_requests"]), 10)
        self.assertContains(response, "Предыдущие заявки")
        response = self.client.get(reverse("specialist_home"), {"requests_page": 2})
        self.assertIn(oldest.pk, [item.pk for item in response.context["time_off_requests"]])
        self.assertContains(response, "Ожидает решения")

    def test_multiday_sick_leave_separates_admin_revision_from_director_review(self):
        request = self.create_request(request_type=TimeOffRequest.RequestType.SICK, days=3)
        endpoint = reverse("time_off_request_decide", args=[request.pk])
        self.client.force_login(self.admin)
        self.client.post(endpoint, {"action": "approve", "reason": "Согласовано по обращению специалиста."})

        for page in ("work_queue", "tomorrow"):
            with self.subTest(page=page):
                response = self.client.get(reverse(page))
                self.assertContains(response, "Текущий статус: Согласовано")
                self.assertContains(response, "Причина специалиста: Тестовая заявка специалиста.")
                self.assertContains(response, "Согласовано по обращению специалиста.")
                self.assertContains(response, "Повторно согласовывать заявку не нужно.")
                self.assertContains(response, '<details class="time-off-decision-change">')
                self.assertNotContains(response, '<details class="time-off-decision-change" open')
                self.assertContains(response, "Основание решения администратора")

        first = request.decision_history.get(is_current=True)
        self.client.post(endpoint, {"action": "reject", "reason": "Уточнены даты отсутствия специалиста."})
        first.refresh_from_db()
        second = request.decision_history.get(is_current=True)
        self.assertFalse(first.is_current)
        self.assertEqual(second.supersedes_id, first.pk)
        self.assertEqual(first.note, "Согласовано по обращению специалиста.")

        # The pilot director has group authority without Django staff/superuser flags.
        director = User.objects.create_user("group-only-leave-director", password="x")
        director.groups.add(Group.objects.get_or_create(name="Руководители")[0])
        self.client.force_login(director)
        for page in ("work_queue", "tomorrow"):
            with self.subTest(director_page=page):
                response = self.client.get(reverse(page))
                self.assertContains(response, "Подтвердить отказ")
                self.assertContains(response, "Основание решения руководителя")
                self.assertNotContains(response, '<details class="time-off-decision-change">')

        self.client.post(endpoint, {"action": "approve", "reason": "Руководитель проверил и согласовал отсутствие."})
        request.refresh_from_db()
        final = request.decision_history.get(is_current=True)
        self.assertEqual(request.status, TimeOffRequest.Status.APPROVED)
        self.assertEqual(final.actor_id, director.pk)
        self.assertFalse(final.awaits_director_review)
        self.assertFalse(time_off_svc.attention_queryset().filter(pk=request.pk).exists())
        self.client.force_login(self.admin)
        response = self.client.post(endpoint, {"action": "reject", "reason": "Попытка изменить итог руководителя."}, follow=True)
        self.assertContains(response, "Решение руководителя может изменить только руководитель.")
        self.assertEqual(request.decision_history.count(), 3)

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
        self.assertContains(response, "Подтвердить согласование")
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
