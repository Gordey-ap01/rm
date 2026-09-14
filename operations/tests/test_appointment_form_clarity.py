from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from operations.forms import AppointmentForm
from operations.models import Appointment, Child, ParentGuardian, Room, Service, StaffMember


class AppointmentFormClarityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("form-clarity-admin", password="x")
        parent = ParentGuardian.objects.create(last_name="Учебный", first_name="Родитель", phone="+79990000000")
        cls.child = Child.objects.create(last_name="Первый", first_name="Получатель", primary_parent=parent)
        cls.other_child = Child.objects.create(last_name="Второй", first_name="Получатель", primary_parent=parent)
        cls.staff = StaffMember.objects.create(full_name="Первый специалист")
        cls.other_staff = StaffMember.objects.create(full_name="Второй специалист")
        cls.service = Service.objects.create(name="Учебное занятие", code="FORM-CLARITY", category=Service.Category.SPEECH)
        cls.room = Room.objects.create(name="Кабинет", allow_group_sessions=True, capacity=4, max_recipient_count=3, max_staff_count=2)

    def setUp(self):
        self.client.force_login(self.admin)

    def payload(self, **updates):
        data = {
            "participant_selection": "lists", "session_type": "individual",
            "participants": [str(self.child.pk)], "staff_members": [str(self.staff.pk)],
            "child": "", "staff_member": "", "service": self.service.pk, "room": self.room.pk,
            "date": (timezone.localdate() + timedelta(days=5)).isoformat(), "time": "10:00",
            "duration_minutes": "45", "status": "proposed", "admin_note": "Проверка понятной формы",
        }
        data.update(updates)
        return data

    def test_create_opens_saved_card_and_conflict_preserves_selections(self):
        response = self.client.post(reverse("appointment_create"), self.payload())
        appointment = Appointment.objects.get()
        self.assertRedirects(response, reverse("appointment_detail", args=[appointment.pk]))
        self.assertEqual(appointment.duration_minutes, 45)
        self.assertEqual(appointment.child, self.child)
        self.assertEqual(appointment.staff_member, self.staff)
        self.assertEqual(appointment.status, Appointment.Status.PROPOSED)
        self.assertIsNone(appointment.billing_account)
        duplicate = self.client.post(reverse("appointment_create"), self.payload())
        self.assertEqual(duplicate.status_code, 200)
        self.assertContains(duplicate, "Конфликт расписания")
        self.assertEqual(duplicate.context["form"]["participants"].value(), [str(self.child.pk)])
        self.assertEqual(Appointment.objects.count(), 1)

    def test_hidden_primary_does_not_restore_unchecked_people_on_edit(self):
        form = AppointmentForm(self.payload(), actor=self.admin)
        self.assertTrue(form.is_valid(), form.errors)
        appointment = form.save()
        form = AppointmentForm(self.payload(
            child=str(self.child.pk), staff_member=str(self.staff.pk),
            participants=[str(self.other_child.pk)], staff_members=[str(self.other_staff.pk)],
        ), instance=appointment, actor=self.admin)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        appointment.refresh_from_db()
        self.assertEqual(appointment.child, self.other_child)
        self.assertEqual(appointment.staff_member, self.other_staff)
        self.assertEqual(list(appointment.participants.values_list("child_id", flat=True)), [self.other_child.pk])
        self.assertEqual(list(appointment.staff_assignments.values_list("staff_member_id", flat=True)), [self.other_staff.pk])

    def test_unchecking_everyone_requires_selection_even_with_hidden_values(self):
        form = AppointmentForm(self.payload(
            child=str(self.child.pk), staff_member=str(self.staff.pk), participants=[], staff_members=[],
        ), actor=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn("participants", form.errors)
        self.assertIn("staff_members", form.errors)

    def test_joint_composition_still_uses_group_validation(self):
        form = AppointmentForm(self.payload(
            participants=[str(self.child.pk), str(self.other_child.pk)],
            staff_members=[str(self.staff.pk), str(self.other_staff.pk)],
        ), actor=self.admin)
        self.assertTrue(form.is_valid(), form.errors)
        appointment = form.save()
        self.assertEqual(appointment.session_type, Appointment.SessionType.GROUP)
        self.assertEqual(appointment.participants.count(), 2)
        self.assertEqual(appointment.staff_assignments.count(), 2)
