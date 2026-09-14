from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from operations.forms import AppointmentForm
from operations.models import Child, ParentGuardian, Room, Service, StaffMember


class AppointmentPeopleSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("people-search-admin", password="x")
        cls.specialist_user = User.objects.create_user("people-search-specialist", password="x")
        cls.parent = ParentGuardian.objects.create(last_name="Поиск", first_name="Родитель")
        cls.children = [
            Child.objects.create(
                last_name="Каталог",
                first_name=f"Получатель {index:02d}",
                primary_parent=cls.parent,
            )
            for index in range(12)
        ]
        cls.far_child = Child.objects.create(
            last_name="Дальний",
            first_name="Получатель",
            primary_parent=cls.parent,
        )
        cls.staff = [
            StaffMember.objects.create(full_name=f"Специалист {index:02d}")
            for index in range(11)
        ]
        cls.far_staff = StaffMember.objects.create(full_name="Дальний специалист")
        cls.inactive_staff = StaffMember.objects.create(
            full_name="Дальний неактивный",
            status=StaffMember.Status.INACTIVE,
        )
        cls.service = Service.objects.create(
            name="Поисковое занятие",
            code="PEOPLE-SEARCH",
            category=Service.Category.SPEECH,
        )
        cls.room = Room.objects.create(name="Кабинет поиска")

    def setUp(self):
        self.client.force_login(self.admin)

    def search(self, **params):
        return self.client.get(reverse("appointment_people_search"), params)

    def payload(self, **updates):
        data = {
            "participant_selection": "lists",
            "session_type": "individual",
            "participants": [str(self.far_child.pk)],
            "staff_members": [str(self.far_staff.pk)],
            "child": "",
            "staff_member": "",
            "service": str(self.service.pk),
            "room": str(self.room.pk),
            "date": (timezone.localdate() + timedelta(days=5)).isoformat(),
            "time": "10:00",
            "duration_minutes": "30",
            "status": "proposed",
        }
        data.update(updates)
        return data

    def test_participant_search_is_capped_paginated_and_not_cached(self):
        first_page = self.search(kind="participants", q="Каталог", page="1")

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page["Cache-Control"], "private, no-store")
        self.assertEqual(first_page.json()["page"], 1)
        self.assertEqual(len(first_page.json()["results"]), 10)
        self.assertTrue(first_page.json()["has_more"])
        self.assertEqual(
            [item["id"] for item in first_page.json()["results"]],
            [str(child.pk) for child in self.children[:10]],
        )
        self.assertEqual(
            self.search(kind="participants", q="Каталог", page="-3").json()["page"],
            1,
        )

        second_page = self.search(kind="participants", q="Каталог", page="2")

        self.assertEqual(second_page.json()["page"], 2)
        self.assertEqual(
            [item["id"] for item in second_page.json()["results"]],
            [str(child.pk) for child in self.children[10:]],
        )
        self.assertFalse(second_page.json()["has_more"])

    def test_searches_tokens_and_returns_only_active_staff(self):
        participants = self.search(kind="participants", q="Дальний Получатель").json()
        staff = self.search(kind="staff_members", q="Дальний").json()

        self.assertEqual(
            participants["results"],
            [{"id": str(self.far_child.pk), "label": str(self.far_child)}],
        )
        self.assertEqual(
            staff["results"],
            [{"id": str(self.far_staff.pk), "label": str(self.far_staff)}],
        )
        self.assertNotIn(str(self.inactive_staff.pk), [item["id"] for item in staff["results"]])

    def test_rejects_unknown_kind_and_non_operators_cannot_search(self):
        self.assertEqual(self.search(kind="unknown").status_code, 400)

        self.client.logout()
        self.client.force_login(self.specialist_user)
        response = self.search(kind="participants")

        self.assertEqual(response.status_code, 302)

    def test_widget_keeps_far_initial_and_bound_choices_but_not_invalid_ids(self):
        initial_form = AppointmentForm(
            initial={"participants": [self.far_child.pk], "staff_members": [self.far_staff.pk]}
        )
        bound_form = AppointmentForm(self.payload())
        invalid_form = AppointmentForm(self.payload(participants=["999999"]))

        self.assertIn(
            (str(self.far_child.pk), str(self.far_child)),
            initial_form.fields["participants"].widget.choices,
        )
        self.assertIn(
            (str(self.far_staff.pk), str(self.far_staff)),
            initial_form.fields["staff_members"].widget.choices,
        )
        self.assertIn(
            (str(self.far_child.pk), str(self.far_child)),
            bound_form.fields["participants"].widget.choices,
        )
        self.assertIn(
            (str(self.far_staff.pk), str(self.far_staff)),
            bound_form.fields["staff_members"].widget.choices,
        )
        self.assertTrue(
            bound_form.fields["participants"].queryset.filter(pk=self.children[0].pk).exists()
        )
        self.assertEqual(list(invalid_form.fields["participants"].widget.choices), [])
        self.assertFalse(invalid_form.is_valid())
        self.assertIn("participants", invalid_form.errors)
