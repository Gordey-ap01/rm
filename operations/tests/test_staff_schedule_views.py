from datetime import time, timedelta
from uuid import uuid4

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from operations.models import StaffAvailability, StaffMember, StaffScheduleChangeRequest
from operations.services import staff_schedules


class StaffScheduleViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("graph-admin", is_staff=True)
        cls.director = User.objects.create_superuser("graph-director", password="test")
        cls.user = User.objects.create_user("graph-specialist")
        cls.other_user = User.objects.create_user("graph-other")
        cls.staff = StaffMember.objects.create(user=cls.user, full_name="Первый график", can_use_mobile=True)
        cls.other = StaffMember.objects.create(user=cls.other_user, full_name="Чужой график", can_use_mobile=True)

    def payload(self, staff=None):
        values = {
            "staff_member": (staff or self.staff).pk,
            "effective_from": (timezone.localdate() + timedelta(days=3)).isoformat(),
            "reason": "Согласовать постоянный график",
            "request_key": str(uuid4()),
        }
        for day in range(7):
            values[f"day_{day}_start"] = ["08:00", "14:00"]
            values[f"day_{day}_end"] = ["12:00", "19:00"]
        return values

    def create(self, actor=None, staff=None):
        self.client.force_login(actor or self.user)
        response = self.client.post(reverse("staff_schedule_create"), self.payload(staff))
        self.assertEqual(response.status_code, 302)
        return StaffScheduleChangeRequest.objects.latest("pk")

    def decision_payload(self, item, action="approve"):
        current = item.current_decision
        return {
            "action": action,
            "reason": "Согласовано с рабочими потребностями центра",
            "request_key": str(uuid4()),
            "expected_decision_id": current.pk if current else "",
            "expected_revision_id": staff_schedules.latest_revision_id(item.staff_member) or "",
        }

    def decide(self, item, actor=None, action="approve"):
        self.client.force_login(actor or self.admin)
        return self.client.post(reverse("staff_schedule_decide", args=[item.pk]), self.decision_payload(item, action))

    def test_role_flow_and_weekly_display(self):
        item = self.create()
        response = self.client.get(reverse("staff_schedule_detail", args=[item.pk]))
        for value in ["Понедельник", "Воскресенье", "08:00", "19:00"]:
            self.assertContains(response, value)
        self.assertEqual(self.decide(item).status_code, 302)
        self.assertContains(self.client.get(reverse("work_queue")), "Изменения рабочего графика · 1")
        self.assertEqual(self.decide(item, self.director, "confirm").status_code, 302)
        item.refresh_from_db()
        self.assertFalse(item.current_decision.requires_director_review)
        self.assertContains(self.client.get(reverse("work_queue")), "Изменения рабочего графика · 0")
        self.assertEqual(self.decide(item, self.admin, "reject").status_code, 403)

    def test_specialist_scope_and_operator_on_behalf(self):
        item = self.create(staff=self.other)
        self.assertEqual(item.staff_member, self.staff)
        operator_item = self.create(self.admin, self.other)
        self.assertEqual(operator_item.created_by, self.admin)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("staff_schedule_detail", args=[operator_item.pk])).status_code, 403)
        response = self.client.get(reverse("staff_schedule_list"), {"staff_id": self.other.pk})
        self.assertNotContains(response, "Чужой график")
        self.assertEqual(self.decide(item, self.user).status_code, 403)

    def test_mobile_access_and_post_only_csrf(self):
        item = self.create()
        self.client.force_login(self.admin)
        endpoint = reverse("staff_schedule_decide", args=[item.pk])
        self.assertEqual(self.client.get(endpoint).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(csrf.post(endpoint, self.decision_payload(item)).status_code, 403)
        self.staff.can_use_mobile = False
        self.staff.save(update_fields=["can_use_mobile"])
        self.client.force_login(self.user)
        for path in [reverse("staff_schedule_list"), reverse("staff_schedule_create"), reverse("staff_schedule_detail", args=[item.pk])]:
            self.assertEqual(self.client.get(path).status_code, 403)

    def test_stale_submit_preserves_reason_and_does_not_change_decision(self):
        item = self.create()
        stale = self.decision_payload(item, "reject")
        stale["reason"] = "Моё объяснение из устаревшей формы"
        self.assertEqual(self.decide(item).status_code, 302)
        decision_id = item.current_decision.pk
        response = self.client.post(reverse("staff_schedule_decide", args=[item.pk]), stale)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, stale["reason"], status_code=409)
        self.assertEqual(item.current_decision.pk, decision_id)

    def test_invalid_intervals_preserve_form_without_request(self):
        self.client.force_login(self.user)
        data = self.payload()
        data["day_0_end"] = ["07:00", "19:00"]
        response = self.client.post(reverse("staff_schedule_create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "08:00")
        self.assertFalse(StaffScheduleChangeRequest.objects.exists())

    def test_managed_schedule_blocks_legacy_changes(self):
        window = StaffAvailability.objects.create(staff_member=self.staff, weekday=0, starts_at=time(9), ends_at=time(18))
        item = self.create()
        self.assertEqual(self.decide(item).status_code, 302)
        self.client.force_login(self.user)
        self.client.post(reverse("staff_availability_toggle", args=[window.pk]))
        window.refresh_from_db()
        self.assertTrue(window.is_active)
        count = StaffAvailability.objects.count()
        self.client.post(reverse("staff_availability_create"), {"weekday": 1, "starts_at": "09:00", "ends_at": "18:00"})
        self.assertEqual(StaffAvailability.objects.count(), count)
        response = self.client.get(reverse("specialist_home"))
        self.assertContains(response, "Запросить изменение графика")
        self.assertContains(response, "Заявки на график · 1")
        self.assertNotContains(response, "Добавить время работы")

    def test_invalid_hidden_decision_field_shows_error_and_no_write(self):
        item = self.create()
        self.client.force_login(self.admin)
        data = self.decision_payload(item)
        data["request_key"] = "broken-key"
        response = self.client.post(reverse("staff_schedule_decide", args=[item.pk]), data)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "UUID", status_code=400)
        self.assertIsNone(item.current_decision)

    def test_external_next_cannot_redirect_after_decision(self):
        item = self.create()
        self.client.force_login(self.admin)
        data = self.decision_payload(item)
        data["next"] = "https://example.invalid/"
        response = self.client.post(reverse("staff_schedule_decide", args=[item.pk]), data)
        self.assertRedirects(response, reverse("staff_schedule_detail", args=[item.pk]))
