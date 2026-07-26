"""Grant plan revision and concurrency contracts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO
from queue import Queue
from threading import Barrier, Thread
from unittest import skipUnless

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import (
    DatabaseError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase

from operations.models import (
    FundingServiceQuota,
    FundingServiceQuotaRevision,
    FundingSource,
    FundingStaffAllocation,
    Service,
    StaffMember,
)
from operations.services import grant_plans as grant_plans_svc


class GrantPlanServiceTests(TestCase):
    def setUp(self):
        self.director = User.objects.create_superuser("grant-plan-director", password="x")
        self.admin = User.objects.create_user(
            "grant-plan-admin",
            password="x",
            is_staff=True,
        )
        self.funding = FundingSource.objects.create(
            name="Грант редакций",
            source_type=FundingSource.SourceType.GRANT,
        )
        self.service = Service.objects.create(
            name="Логопедическое занятие гранта",
            code="GRANT-REV",
        )
        self.staff = StaffMember.objects.create(full_name="Специалист редакций")
        self.period_start = date(2026, 1, 1)
        self.period_end = date(2026, 12, 31)

    def _create_quota(self, *, planned_sessions: int = 10) -> FundingServiceQuota:
        return grant_plans_svc.create_service_quota(
            funding_source=self.funding,
            service=self.service,
            planned_sessions=planned_sessions,
            starts_on=self.period_start,
            ends_on=self.period_end,
            note="Годовой план",
            actor=self.director,
            reason="Утвержден годовой план.",
        )

    def _create_allocation(
        self,
        quota: FundingServiceQuota,
        *,
        allocated_sessions: int = 6,
    ) -> FundingStaffAllocation:
        return grant_plans_svc.create_staff_allocation(
            service_quota=quota,
            funding_source=None,
            service=None,
            staff_member=self.staff,
            allocated_sessions=allocated_sessions,
            session_pay_amount=Decimal("500.00"),
            starts_on=self.period_start,
            ends_on=self.period_end,
            note="Распределение руководителя",
            actor=self.director,
            reason="Назначена квота специалисту.",
        )

    def test_create_quota_records_director_revision(self):
        quota = self._create_quota()

        revision = quota.revisions.get()
        self.assertEqual(revision.revision_number, 1)
        self.assertEqual(revision.event_type, FundingServiceQuotaRevision.EventType.CREATED)
        self.assertEqual(revision.actor, self.director)
        self.assertEqual(
            revision.actor_role_snapshot,
            FundingServiceQuotaRevision.ActorRole.DIRECTOR,
        )
        self.assertEqual(revision.reason, "Утвержден годовой план.")
        self.assertEqual(quota.current_revision, revision)

    def test_administrator_cannot_create_quota(self):
        with self.assertRaises(PermissionDenied):
            grant_plans_svc.create_service_quota(
                funding_source=self.funding,
                service=self.service,
                planned_sessions=10,
                starts_on=self.period_start,
                ends_on=self.period_end,
                note="",
                actor=self.admin,
                reason="Попытка изменить план.",
            )
        self.assertFalse(FundingServiceQuota.objects.exists())

    def test_revise_quota_preserves_previous_values(self):
        quota = self._create_quota()

        revised = grant_plans_svc.revise_service_quota(
            quota,
            planned_sessions=12,
            starts_on=self.period_start,
            ends_on=self.period_end,
            note="Уточненный план",
            actor=self.director,
            reason="Добавлены два занятия.",
            expected_revision_id=quota.current_revision_id,
        )

        revisions = list(revised.revisions.order_by("revision_number"))
        self.assertEqual(revised.pk, quota.pk)
        self.assertEqual(revised.planned_sessions, 12)
        self.assertEqual([item.planned_sessions for item in revisions], [10, 12])
        self.assertEqual(revisions[1].supersedes, revisions[0])
        self.assertEqual(revised.current_revision, revisions[1])

    def test_stale_revision_token_cannot_overwrite_newer_change(self):
        quota = self._create_quota()
        stale_revision_id = quota.current_revision_id
        first = grant_plans_svc.revise_service_quota(
            quota,
            planned_sessions=11,
            starts_on=self.period_start,
            ends_on=self.period_end,
            note="Первая редакция",
            actor=self.director,
            reason="Первая редакция плана.",
            expected_revision_id=stale_revision_id,
        )

        with self.assertRaises(ValidationError) as caught:
            grant_plans_svc.revise_service_quota(
                quota,
                planned_sessions=12,
                starts_on=self.period_start,
                ends_on=self.period_end,
                note="Устаревшая форма",
                actor=self.director,
                reason="Вторая редакция из старой формы.",
                expected_revision_id=stale_revision_id,
            )

        self.assertIn("expected_revision_id", caught.exception.message_dict)
        first.refresh_from_db()
        self.assertEqual(first.planned_sessions, 11)
        self.assertEqual(first.revisions.count(), 2)

    def test_direct_projection_mutation_is_blocked(self):
        quota = self._create_quota()
        quota.planned_sessions = 99

        with self.assertRaisesMessage(ValidationError, "нельзя изменять напрямую"):
            quota.save()
        quota.refresh_from_db()
        self.assertEqual(quota.planned_sessions, 10)

    def test_revision_business_fields_are_immutable_and_revision_is_protected(self):
        quota = self._create_quota()
        revision = quota.revisions.get()
        revision.planned_sessions = 99

        with self.assertRaisesMessage(ValidationError, "нельзя изменять"):
            revision.save()
        revision.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, "нельзя изменять"):
            revision.save()
        with self.assertRaisesMessage(ValidationError, "нельзя удалить"):
            revision.delete()
        with self.assertRaises(ProtectedError):
            quota.delete()

    def test_quota_cannot_be_reduced_below_allocated_total(self):
        quota = self._create_quota()
        self._create_allocation(quota, allocated_sessions=6)

        with self.assertRaises(ValidationError) as caught:
            grant_plans_svc.revise_service_quota(
                quota,
                planned_sessions=5,
                starts_on=self.period_start,
                ends_on=self.period_end,
                note="Слишком мало",
                actor=self.director,
                reason="Попытка уменьшения плана.",
                expected_revision_id=quota.current_revision_id,
            )

        self.assertIn("planned_sessions", caught.exception.message_dict)
        quota.refresh_from_db()
        self.assertEqual(quota.planned_sessions, 10)
        self.assertEqual(quota.revisions.count(), 1)

    def test_staff_revision_preserves_identity_and_history(self):
        quota = self._create_quota()
        allocation = self._create_allocation(quota)

        revised = grant_plans_svc.revise_staff_allocation(
            allocation,
            allocated_sessions=7,
            session_pay_amount=Decimal("550.00"),
            starts_on=self.period_start,
            ends_on=self.period_end,
            note="Ставка уточнена",
            actor=self.director,
            reason="Утверждена новая ставка.",
            expected_revision_id=allocation.current_revision_id,
        )

        revisions = list(revised.revisions.order_by("revision_number"))
        self.assertEqual(revised.staff_member, self.staff)
        self.assertEqual([item.allocated_sessions for item in revisions], [6, 7])
        self.assertEqual(
            [item.session_pay_amount for item in revisions],
            [Decimal("500.00"), Decimal("550.00")],
        )
        self.assertEqual(revised.current_revision, revisions[1])

    def test_overlapping_staff_allocation_is_rejected(self):
        quota = self._create_quota(planned_sessions=20)
        self._create_allocation(quota, allocated_sessions=6)

        with self.assertRaises(ValidationError) as caught:
            grant_plans_svc.create_staff_allocation(
                service_quota=None,
                funding_source=self.funding,
                service=self.service,
                staff_member=self.staff,
                allocated_sessions=4,
                session_pay_amount=Decimal("600.00"),
                starts_on=date(2026, 6, 1),
                ends_on=date(2026, 7, 1),
                note="",
                actor=self.director,
                reason="Параллельное прямое распределение.",
            )

        self.assertIn("starts_on", caught.exception.message_dict)

    def test_quota_closes_only_after_allocations_and_keeps_rows(self):
        quota = self._create_quota()
        allocation = self._create_allocation(quota)

        with self.assertRaisesMessage(ValidationError, "Сначала закройте"):
            grant_plans_svc.close_service_quota(
                quota,
                close_on=date(2026, 6, 30),
                actor=self.director,
                reason="Проект завершен досрочно.",
                expected_revision_id=quota.current_revision_id,
            )

        grant_plans_svc.close_staff_allocation(
            allocation,
            close_on=date(2026, 6, 30),
            actor=self.director,
            reason="Работа специалиста завершена.",
            expected_revision_id=allocation.current_revision_id,
        )
        grant_plans_svc.close_service_quota(
            quota,
            close_on=date(2026, 6, 30),
            actor=self.director,
            reason="Проект завершен досрочно.",
            expected_revision_id=quota.current_revision_id,
        )

        quota.refresh_from_db()
        allocation.refresh_from_db()
        self.assertEqual(quota.lifecycle_status, FundingServiceQuota.LifecycleStatus.CLOSED)
        self.assertEqual(
            allocation.lifecycle_status,
            FundingStaffAllocation.LifecycleStatus.CLOSED,
        )
        self.assertEqual(quota.revisions.count(), 2)
        self.assertEqual(allocation.revisions.count(), 2)
        self.assertTrue(FundingServiceQuota.objects.filter(pk=quota.pk).exists())
        self.assertTrue(FundingStaffAllocation.objects.filter(pk=allocation.pk).exists())

    def test_archived_source_rejects_revision(self):
        quota = self._create_quota()
        self.funding.archive()

        with self.assertRaisesMessage(ValidationError, "только для чтения"):
            grant_plans_svc.revise_service_quota(
                quota,
                planned_sessions=11,
                starts_on=self.period_start,
                ends_on=self.period_end,
                note="",
                actor=self.director,
                reason="Попытка изменить архив.",
                expected_revision_id=quota.current_revision_id,
            )

    def test_integrity_command_accepts_service_created_plan(self):
        quota = self._create_quota()
        self._create_allocation(quota)
        output = StringIO()

        call_command("check_grant_plan_integrity", strict=True, stdout=output)

        self.assertIn("Findings: 0", output.getvalue())

    def test_integrity_command_rejects_root_without_revision(self):
        FundingServiceQuota.objects.create(
            funding_source=self.funding,
            service=self.service,
            planned_sessions=3,
            starts_on=self.period_start,
            ends_on=self.period_end,
        )

        with self.assertRaises(CommandError):
            call_command(
                "check_grant_plan_integrity",
                strict=True,
                stdout=StringIO(),
                stderr=StringIO(),
            )


class GrantPlanPostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.director = User.objects.create_superuser(
            "grant-plan-pg-director",
            password="x",
        )
        self.funding = FundingSource.objects.create(
            name="Конкурентный грант",
            source_type=FundingSource.SourceType.GRANT,
        )
        self.service = Service.objects.create(
            name="Конкурентная услуга гранта",
            code="GRANT-PG",
        )
        self.staff_a = StaffMember.objects.create(full_name="Конкурентный специалист А")
        self.staff_b = StaffMember.objects.create(full_name="Конкурентный специалист Б")
        self.quota = grant_plans_svc.create_service_quota(
            funding_source=self.funding,
            service=self.service,
            planned_sessions=10,
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
            note="",
            actor=self.director,
            reason="Утвержден конкурентный план.",
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентное распределение квоты проверяется только на PostgreSQL.",
    )
    def test_concurrent_allocations_cannot_exceed_quota(self):
        barrier = Barrier(2)
        outcomes = Queue()

        def allocate(staff_id: int) -> None:
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                allocation = grant_plans_svc.create_staff_allocation(
                    service_quota=FundingServiceQuota.objects.get(pk=self.quota.pk),
                    funding_source=None,
                    service=None,
                    staff_member=StaffMember.objects.get(pk=staff_id),
                    allocated_sessions=6,
                    session_pay_amount=Decimal("500.00"),
                    starts_on=date(2026, 1, 1),
                    ends_on=date(2026, 12, 31),
                    note="",
                    actor=User.objects.get(pk=self.director.pk),
                    reason="Параллельное распределение квоты.",
                )
                outcomes.put(allocation.pk)
            except ValidationError:
                outcomes.put("rejected")
            finally:
                connections.close_all()

        threads = [
            Thread(target=allocate, args=(self.staff_a.pk,)),
            Thread(target=allocate, args=(self.staff_b.pk,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertEqual(sum(isinstance(result, int) for result in results), 1, results)
        self.assertEqual(results.count("rejected"), 1, results)
        self.assertEqual(
            sum(
                FundingStaffAllocation.objects.filter(service_quota=self.quota).values_list(
                    "allocated_sessions",
                    flat=True,
                )
            ),
            6,
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентные редакции проверяются только на PostgreSQL.",
    )
    def test_concurrent_revisions_form_one_chain(self):
        barrier = Barrier(2)
        outcomes = Queue()

        def revise(planned_sessions: int) -> None:
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                grant_plans_svc.revise_service_quota(
                    FundingServiceQuota.objects.get(pk=self.quota.pk),
                    planned_sessions=planned_sessions,
                    starts_on=date(2026, 1, 1),
                    ends_on=date(2026, 12, 31),
                    note=f"План {planned_sessions}",
                    actor=User.objects.get(pk=self.director.pk),
                    reason=f"Параллельная редакция плана {planned_sessions}.",
                    expected_revision_id=self.quota.current_revision_id,
                )
                outcomes.put("saved")
            except ValidationError:
                outcomes.put("conflict")
            finally:
                connections.close_all()

        threads = [Thread(target=revise, args=(11,)), Thread(target=revise, args=(12,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertEqual(results.count("saved"), 1, results)
        self.assertEqual(results.count("conflict"), 1, results)
        revisions = list(
            FundingServiceQuotaRevision.objects.filter(service_quota=self.quota).order_by(
                "revision_number"
            )
        )
        self.assertEqual([item.revision_number for item in revisions], [1, 2])
        self.assertEqual(revisions[1].supersedes, revisions[0])
        self.quota.refresh_from_db()
        self.assertEqual(self.quota.current_revision, revisions[1])

    @skipUnless(
        connection.vendor == "postgresql",
        "DB-защита неизменяемой истории проверяется только на PostgreSQL.",
    )
    def test_postgresql_blocks_revision_queryset_update(self):
        revision = self.quota.current_revision

        with self.assertRaises(DatabaseError), transaction.atomic():
            FundingServiceQuotaRevision.objects.filter(pk=revision.pk).update(
                reason="Попытка обойти доменный сервис."
            )

        revision.refresh_from_db()
        self.assertEqual(revision.reason, "Утвержден конкурентный план.")
