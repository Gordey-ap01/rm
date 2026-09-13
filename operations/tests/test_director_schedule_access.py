from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from operations.models import StaffMember

User = get_user_model()

CALENDAR_API_URLS = (
    "/api/appointments/",
    "/api/staff/",
    "/api/services/",
    "/api/rooms/",
    "/api/unavailability/",
)


class DirectorScheduleAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        director_group = Group.objects.create(name="Руководители")
        administrator_group = Group.objects.create(name="Администраторы")

        cls.director = User.objects.create_user("group-director", password="x")
        cls.director.groups.add(director_group)
        cls.group_administrator = User.objects.create_user(
            "group-administrator",
            password="x",
        )
        cls.group_administrator.groups.add(administrator_group)
        cls.specialist_user = User.objects.create_user("linked-specialist", password="x")
        StaffMember.objects.create(
            user=cls.specialist_user,
            full_name="Специалист без операторского доступа",
        )

    def assert_calendar_api_access(self, user, expected_status):
        self.client.force_login(user)
        for url in CALENDAR_API_URLS:
            with self.subTest(username=user.username, url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, expected_status)
                if expected_status == 200:
                    self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
                    self.assertIsInstance(response.json(), list)

    def page_html(self, user, view_name: str, **query) -> str:
        self.client.force_login(user)
        response = self.client.get(reverse(view_name), query)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def operator_nav(self, user) -> str:
        page = self.page_html(user, "dashboard")
        return page[page.index("<nav") : page.index("</nav>")]

    def test_group_director_can_read_all_calendar_json_endpoints(self):
        self.assertFalse(self.director.is_staff)

        self.assert_calendar_api_access(self.director, 200)
        staff_ids = {item["id"] for item in self.client.get("/api/staff/").json()}
        self.assertIn(self.specialist_user.staff_profile.pk, staff_ids)

    def test_group_administrator_keeps_calendar_json_access(self):
        self.assertFalse(self.group_administrator.is_staff)

        self.assert_calendar_api_access(self.group_administrator, 200)

    def test_linked_specialist_is_forbidden_from_calendar_json_endpoints(self):
        self.assert_calendar_api_access(self.specialist_user, 403)

    def test_anonymous_user_still_requires_authentication(self):
        for url in CALENDAR_API_URLS:
            with self.subTest(url=url):
                self.client.logout()
                self.assertEqual(self.client.get(url).status_code, 401)

    def test_group_operators_see_operator_nav_without_django_admin(self):
        for user in (self.director, self.group_administrator):
            with self.subTest(username=user.username):
                nav = self.operator_nav(user)
                self.assertIn(f'href="{reverse("schedule")}"', nav)
                self.assertIn(f'href="{reverse("work_queue")}"', nav)
                self.assertIn('type="search"', nav)
                self.assertNotIn('href="/admin/"', nav)

    def test_specialist_does_not_see_operator_nav_or_search(self):
        page = self.page_html(self.specialist_user, "specialist_home")
        nav = page[page.index("<nav") : page.index("</nav>")]

        self.assertNotIn(f'href="{reverse("schedule")}"', nav)
        self.assertNotIn(f'href="{reverse("work_queue")}"', nav)
        self.assertNotIn('type="search"', nav)
        self.assertNotIn('href="/admin/"', nav)
        self.assertNotIn("Составить расписание на месяц", page)

    def test_group_director_sees_month_schedule_link_for_specialist(self):
        staff = self.specialist_user.staff_profile

        page = self.page_html(
            self.director,
            "specialist_home",
            staff_id=staff.pk,
        )

        self.assertIn("Составить расписание на месяц", page)
        self.assertIn(f'href="{reverse("schedule")}?staff_id={staff.pk}"', page)
