"""Тесты django-auditlog: запись LogEntry при create/update/delete."""

from __future__ import annotations

from auditlog.models import LogEntry
from django.contrib.auth.models import User
from django.test import TestCase

from operations.models import Child, FundingSource, ParentGuardian, StaffMember


class AuditLogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("admin", password="x", is_staff=True)

    def test_create_logs_action(self):
        parent = ParentGuardian.objects.create(
            last_name="Сидоров", first_name="С", phone="+7 999"
        )
        self.assertEqual(
            LogEntry.objects.filter(action=LogEntry.Action.CREATE, object_pk=str(parent.pk)).count(),
            1,
        )

    def test_update_logs_change_diff(self):
        parent = ParentGuardian.objects.create(
            last_name="Сидоров", first_name="С", phone="+7 999"
        )
        parent.phone = "+7 111"
        parent.save()
        updates = LogEntry.objects.filter(
            action=LogEntry.Action.UPDATE, object_pk=str(parent.pk)
        )
        self.assertEqual(updates.count(), 1)
        diff = updates.first().changes_dict
        self.assertIn("phone", diff)

    def test_delete_records_action(self):
        parent = ParentGuardian.objects.create(
            last_name="Сидоров", first_name="С", phone="+7 999"
        )
        pk = parent.pk
        parent.delete()
        self.assertTrue(
            LogEntry.objects.filter(
                action=LogEntry.Action.DELETE, object_pk=str(pk)
            ).exists()
        )

    def test_tracked_models_registered(self):
        from auditlog.registry import auditlog

        for model in (Child, StaffMember, FundingSource):
            self.assertIn(model, auditlog._registry)
