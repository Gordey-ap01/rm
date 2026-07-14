"""Тесты django-auditlog: запись LogEntry при create/update/delete."""

from __future__ import annotations

from auditlog.models import LogEntry
from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentRescheduleChain,
    AppointmentRescheduleStepDependency,
    AppointmentStaffAssignment,
    Child,
    FinancialIntegrityCheckRun,
    FinancialIntegrityFinding,
    FinancialIntegrityFindingEvent,
    FundingSource,
    GrantRecipientAllocation,
    LedgerEntry,
    ParentGuardian,
    PayrollAccrual,
    StaffCompensationRule,
    StaffMember,
)


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

        for model in (
            Appointment,
            AppointmentParticipant,
            AppointmentRescheduleChain,
            AppointmentRescheduleStepDependency,
            AppointmentStaffAssignment,
            Child,
            FundingSource,
            FinancialIntegrityCheckRun,
            FinancialIntegrityFinding,
            FinancialIntegrityFindingEvent,
            GrantRecipientAllocation,
            LedgerEntry,
            PayrollAccrual,
            StaffCompensationRule,
            StaffMember,
        ):
            self.assertIn(model, auditlog._registry)

    def test_financial_integrity_models_registered_in_admin(self):
        self.assertIn(FinancialIntegrityCheckRun, admin.site._registry)
        self.assertIn(FinancialIntegrityFinding, admin.site._registry)
        self.assertIn(FinancialIntegrityFindingEvent, admin.site._registry)

    def test_financial_integrity_finding_update_is_logged(self):
        now = timezone.now()
        finding = FinancialIntegrityFinding.objects.create(
            issue_key="auditlog-financial-integrity-finding",
            code="auditlog_test_issue",
            severity=FinancialIntegrityFinding.Severity.WARNING,
            status=FinancialIntegrityFinding.Status.OPEN,
            first_seen_at=now,
            last_seen_at=now,
            message="Auditlog test issue.",
        )

        finding.status = FinancialIntegrityFinding.Status.ACKNOWLEDGED
        finding.triage_note = "Accepted for review."
        finding.triaged_by = self.user
        finding.triaged_at = now
        finding.save()

        content_type = ContentType.objects.get_for_model(FinancialIntegrityFinding)
        update = LogEntry.objects.filter(
            action=LogEntry.Action.UPDATE,
            content_type=content_type,
            object_pk=str(finding.pk),
        ).latest("timestamp")

        self.assertIn("status", update.changes_dict)
        self.assertIn("triage_note", update.changes_dict)
