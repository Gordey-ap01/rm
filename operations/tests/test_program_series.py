from __future__ import annotations

import importlib
from datetime import datetime, time, timedelta
from decimal import Decimal
from queue import Queue
from threading import Barrier, Thread
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentSeries,
    AppointmentSeriesOccurrence,
    BalanceAccount,
    Child,
    FundingSource,
    LedgerEntry,
    ParentGuardian,
    ProgramBlock,
    Room,
    Service,
    StaffMember,
    TreatmentProgram,
)
from operations.services import billing as billing_svc, program_series, program_wizard

User = get_user_model()


def _local(day, clock):
    return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())


class GroupProgramSeriesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            "group-series-admin",
            password="x",
            is_staff=True,
            is_superuser=True,
        )
        cls.specialist_user = User.objects.create_user("group-series-specialist", password="x")
        cls.parent1 = ParentGuardian.objects.create(
            last_name="Первая",
            first_name="Мама",
            phone="+7 900 000-10-01",
        )
        cls.parent2 = ParentGuardian.objects.create(
            last_name="Вторая",
            first_name="Мама",
            phone="+7 900 000-10-02",
        )
        cls.child1 = Child.objects.create(
            last_name="Группа",
            first_name="Первый",
            primary_parent=cls.parent1,
        )
        cls.child2 = Child.objects.create(
            last_name="Группа",
            first_name="Второй",
            primary_parent=cls.parent2,
        )
        cls.service = Service.objects.create(
            name="Групповая логопедия",
            code="GROUP-SPEECH",
            category=Service.Category.SPEECH,
            default_duration_minutes=45,
            default_price=Decimal("1200"),
        )
        cls.staff1 = StaffMember.objects.create(
            user=cls.specialist_user,
            full_name="Основной специалист серии",
            specializations="Логопед",
        )
        cls.staff2 = StaffMember.objects.create(
            full_name="Ассистент серии",
            specializations="Логопед",
        )
        cls.room = Room.objects.create(
            name="Групповой кабинет серии",
            allow_group_sessions=True,
            limit_staff_count=True,
            max_staff_count=2,
            limit_recipient_count=True,
            max_recipient_count=4,
        )
        cls.funding = FundingSource.objects.create(
            name="Оплата групповой серии",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        cls.account1 = BalanceAccount.objects.create(
            child=cls.child1,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("10"),
        )
        cls.account2 = BalanceAccount.objects.create(
            child=cls.child2,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("10"),
        )
        cls.program1 = TreatmentProgram.objects.create(
            child=cls.child1,
            title="Программа первого",
            status=TreatmentProgram.Status.ACTIVE,
        )
        cls.program2 = TreatmentProgram.objects.create(
            child=cls.child2,
            title="Программа второго",
            status=TreatmentProgram.Status.ACTIVE,
        )
        cls.block1 = ProgramBlock.objects.create(
            program=cls.program1,
            number=1,
            title="Каскад первого",
            service=cls.service,
            staff_member=cls.staff1,
            planned_sessions=4,
            balance_account=cls.account1,
        )
        cls.block2 = ProgramBlock.objects.create(
            program=cls.program2,
            number=1,
            title="Каскад второго",
            service=cls.service,
            staff_member=cls.staff1,
            planned_sessions=4,
            balance_account=cls.account2,
        )

    def setUp(self):
        self.client.force_login(self.admin)
        self.start_date = timezone.localdate() + timedelta(days=5)
        self.end_date = self.start_date + timedelta(days=7)

    def preview(self, **overrides):
        params = {
            "blocks": [self.block1, self.block2],
            "staff_members": [self.staff1, self.staff2],
            "room": self.room,
            "title": "Постоянная группа",
            "start_date": self.start_date,
            "end_date": self.end_date,
            "weekdays": {self.start_date.weekday()},
            "start_time": time(10, 0),
            "duration_minutes": 45,
            "default_appointment_status": Appointment.Status.PROPOSED,
        }
        params.update(overrides)
        return program_series.preview_group_series(**params)

    def form_payload(self, *, operation_key=None, action="preview"):
        return {
            "operation_key": str(operation_key or uuid4()),
            "title": "Постоянная группа",
            "additional_blocks": [str(self.block2.pk)],
            "staff_members": [str(self.staff1.pk), str(self.staff2.pk)],
            "primary_staff_member": str(self.staff1.pk),
            "room": str(self.room.pk),
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "weekdays": [str(self.start_date.weekday())],
            "start_time": "10:00",
            "duration_minutes": "45",
            "default_appointment_status": Appointment.Status.PROPOSED,
            "override_reason": "",
            "action": action,
        }

    def test_preview_skips_conflict_and_keeps_later_date(self):
        conflict_start = _local(self.start_date, time(10, 0))
        Appointment.objects.create(
            child=self.child2,
            staff_member=self.staff1,
            service=self.service,
            starts_at=conflict_start,
            ends_at=conflict_start + timedelta(minutes=45),
            status=Appointment.Status.CONFIRMED,
        )

        preview = self.preview(staff_members=[self.staff2])

        self.assertEqual(len(preview.dates), 2)
        self.assertFalse(preview.dates[0].ready)
        self.assertEqual(preview.dates[0].reason_code, "schedule_conflict")
        self.assertTrue(preview.dates[1].ready)

    def test_create_group_series_preserves_per_participant_program_data(self):
        conflict_start = _local(self.start_date, time(10, 0))
        other_staff = StaffMember.objects.create(full_name="Специалист конфликта")
        Appointment.objects.create(
            child=self.child2,
            staff_member=other_staff,
            service=self.service,
            starts_at=conflict_start,
            ends_at=conflict_start + timedelta(minutes=45),
            status=Appointment.Status.CONFIRMED,
        )
        preview = self.preview()

        result = program_series.create_group_series(
            preview,
            operation_key=uuid4(),
            actor=self.admin,
        )

        self.assertEqual(result.created_count, 1)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.series.session_type, Appointment.SessionType.GROUP)
        self.assertEqual(result.series.default_participants.count(), 2)
        self.assertEqual(result.series.default_staff_assignments.count(), 2)
        self.assertEqual(result.series.occurrences.count(), 2)
        appointment = Appointment.objects.get(series=result.series)
        self.assertEqual(appointment.participants.count(), 2)
        self.assertEqual(appointment.staff_assignments.count(), 2)
        by_child = {
            row.child_id: row
            for row in appointment.participants.select_related("program_block", "billing_account")
        }
        self.assertEqual(by_child[self.child1.pk].program_block_id, self.block1.pk)
        self.assertEqual(by_child[self.child2.pk].program_block_id, self.block2.pk)
        self.assertEqual(by_child[self.child1.pk].billing_account_id, self.account1.pk)
        self.assertEqual(by_child[self.child2.pk].billing_account_id, self.account2.pk)
        self.assertEqual(by_child[self.child1.pk].sequence_number, 1)
        self.assertEqual(by_child[self.child2.pk].sequence_number, 1)
        self.block1.refresh_from_db()
        self.block2.refresh_from_db()
        self.assertEqual(self.block1.status, ProgramBlock.Status.SCHEDULED)
        self.assertEqual(self.block2.status, ProgramBlock.Status.SCHEDULED)
        self.assertFalse(LedgerEntry.objects.exists())

    def test_composition_preserves_selected_primary_participant_and_staff(self):
        preview = self.preview(
            blocks=[self.block2, self.block1],
            staff_members=[self.staff2, self.staff1],
            end_date=self.start_date,
        )

        result = program_series.create_group_series(preview, operation_key=uuid4())

        self.assertEqual(result.series.child_id, self.child2.pk)
        self.assertEqual(result.series.staff_member_id, self.staff2.pk)
        self.assertEqual(
            list(result.series.default_participants.values_list("child_id", flat=True)),
            [self.child2.pk, self.child1.pk],
        )
        self.assertEqual(
            list(
                result.series.default_staff_assignments.values_list(
                    "staff_member_id", flat=True
                )
            ),
            [self.staff2.pk, self.staff1.pk],
        )

    def test_preview_rejects_invalid_weekday_and_appointment_status(self):
        with self.assertRaises(ValidationError):
            self.preview(weekdays={7})
        with self.assertRaises(ValidationError):
            self.preview(default_appointment_status="unknown")

    def test_plan_limit_marks_remaining_dates_skipped(self):
        self.block1.planned_sessions = 1
        self.block1.save(update_fields=["planned_sessions", "updated_at"])

        preview = self.preview()

        self.assertEqual(preview.ready_count, 1)
        self.assertEqual(preview.dates[1].reason_code, "plan_limit")

    def test_individual_wizard_does_not_schedule_beyond_block_plan(self):
        self.block1.planned_sessions = 0
        self.block1.save(update_fields=["planned_sessions", "updated_at"])

        preview = program_wizard.suggest_program_block_slots(
            self.block1,
            date_from=self.start_date,
            date_to=self.start_date,
            weekdays={self.start_date.weekday()},
            time_from=time(10, 0),
            time_until=time(12, 0),
            duration_minutes=45,
            staff_member=self.staff1,
            room=self.room,
            requested_count=1,
        )

        self.assertEqual(preview.allowed_count, 0)
        self.assertEqual(preview.slots, [])
        self.assertTrue(preview.limited_by_plan)

    def test_individual_wizard_rechecks_plan_and_funding_during_apply(self):
        preview = program_wizard.suggest_program_block_slots(
            self.block1,
            date_from=self.start_date,
            date_to=self.start_date,
            weekdays={self.start_date.weekday()},
            time_from=time(10, 0),
            time_until=time(10, 45),
            duration_minutes=45,
            staff_member=self.staff1,
            room=self.room,
            requested_count=1,
        )
        self.account1.initial_amount = Decimal("0")
        self.account1.save(update_fields=["initial_amount", "updated_at"])

        with self.assertRaisesMessage(ValidationError, "Доступная оплата изменилась"):
            program_wizard.create_schedule_from_preview(preview)

        self.account1.initial_amount = Decimal("10")
        self.account1.save(update_fields=["initial_amount", "updated_at"])
        preview = program_wizard.suggest_program_block_slots(
            self.block1,
            date_from=self.start_date,
            date_to=self.start_date,
            weekdays={self.start_date.weekday()},
            time_from=time(10, 0),
            time_until=time(10, 45),
            duration_minutes=45,
            staff_member=self.staff1,
            room=self.room,
            requested_count=1,
        )
        self.block1.planned_sessions = 0
        self.block1.save(update_fields=["planned_sessions", "updated_at"])

        with self.assertRaisesMessage(ValidationError, "План каскада изменился"):
            program_wizard.create_schedule_from_preview(preview)

    def test_individual_wizard_rechecks_program_lifecycle_during_apply(self):
        preview = program_wizard.suggest_program_block_slots(
            self.block1,
            date_from=self.start_date,
            date_to=self.start_date,
            weekdays={self.start_date.weekday()},
            time_from=time(10, 0),
            time_until=time(10, 45),
            duration_minutes=45,
            staff_member=self.staff1,
            room=self.room,
            requested_count=1,
        )
        self.program1.status = TreatmentProgram.Status.PAUSED
        self.program1.save(update_fields=["status", "updated_at"])

        with self.assertRaisesMessage(ValidationError, "Программа изменилась"):
            program_wizard.create_schedule_from_preview(preview)

    def test_unpaid_override_requires_reserved_status_and_reason(self):
        self.account2.initial_amount = Decimal("0")
        self.account2.save(update_fields=["initial_amount", "updated_at"])

        preview = self.preview()
        self.assertEqual(preview.ready_count, 0)
        self.assertTrue(all(item.reason_code == "funding_limit" for item in preview.dates))
        with self.assertRaises(ValidationError):
            self.preview(allow_unpaid_reserve=True)
        override = self.preview(
            allow_unpaid_reserve=True,
            default_appointment_status=Appointment.Status.RESERVED,
            override_reason="Решение администратора о временной брони",
        )
        self.assertEqual(override.ready_count, 2)

    def test_repeated_operation_key_reuses_series_without_duplicates(self):
        key = uuid4()
        preview = self.preview()

        first = program_series.create_group_series(preview, operation_key=key, actor=self.admin)
        second = program_series.create_group_series(preview, operation_key=key, actor=self.admin)

        self.assertFalse(first.reused_series)
        self.assertTrue(second.reused_series)
        self.assertEqual(second.created_count, 0)
        self.assertEqual(second.unchanged_count, 2)
        self.assertEqual(AppointmentSeries.objects.filter(operation_key=key).count(), 1)
        self.assertEqual(Appointment.objects.filter(series=first.series).count(), 2)
        self.assertEqual(AppointmentSeriesOccurrence.objects.filter(series=first.series).count(), 2)

    def test_sequence_number_is_not_reused_after_cancellation(self):
        preview = self.preview(end_date=self.start_date)
        result = program_series.create_group_series(preview, operation_key=uuid4(), actor=self.admin)
        first = AppointmentParticipant.objects.get(
            appointment__series=result.series,
            child=self.child1,
        )
        first.appointment_status = Appointment.Status.CANCELLED
        first.save(update_fields=["appointment_status", "updated_at"])
        later_start = _local(self.start_date + timedelta(days=1), time(12, 0))
        later = Appointment.objects.create(
            child=self.child1,
            staff_member=self.staff1,
            service=self.service,
            starts_at=later_start,
            ends_at=later_start + timedelta(minutes=45),
            program_block=self.block1,
        )

        self.assertEqual(later.primary_participant.sequence_number, 2)

    def test_legacy_individual_series_uses_monotonic_sequence_allocator(self):
        cancelled_start = _local(self.start_date, time(9, 0))
        Appointment.objects.create(
            child=self.child1,
            staff_member=self.staff1,
            service=self.service,
            starts_at=cancelled_start,
            ends_at=cancelled_start + timedelta(minutes=45),
            status=Appointment.Status.CANCELLED,
            program_block=self.block1,
        )
        day = self.start_date + timedelta(days=1)
        weekday_label = next(
            label for label, value in AppointmentSeries.DAY_MAP.items() if value == day.weekday()
        )
        series = AppointmentSeries.objects.create(
            child=self.child1,
            service=self.service,
            staff_member=self.staff1,
            room=self.room,
            program_block=self.block1,
            title="Legacy individual series",
            start_date=day,
            end_date=day,
            days_of_week=weekday_label,
            time=time(9, 0),
            duration_minutes=45,
            status=AppointmentSeries.Status.ACTIVE,
        )

        self.assertEqual(series.materialize_series(), 1)

        created = Appointment.objects.get(series=series)
        self.assertEqual(created.primary_participant.sequence_number, 2)

    def test_normal_planning_fails_closed_without_usable_account(self):
        self.block2.balance_account = None
        self.block2.save(update_fields=["balance_account", "updated_at"])

        preview = self.preview()

        self.assertEqual(preview.ready_count, 0)
        self.assertTrue(
            all(item.reason_code == "funding_unavailable" for item in preview.dates)
        )
        unpaid = self.preview(
            allow_unpaid_reserve=True,
            default_appointment_status=Appointment.Status.RESERVED,
            override_reason="Администратор разрешил временную неоплаченную бронь",
        )
        self.assertEqual(unpaid.ready_count, 2)

    def test_funding_reservations_are_shared_across_blocks_of_one_account(self):
        self.account1.initial_amount = Decimal("1")
        self.account1.save(update_fields=["initial_amount", "updated_at"])
        another_program = TreatmentProgram.objects.create(
            child=self.child1,
            title="Другая программа того же счета",
            status=TreatmentProgram.Status.ACTIVE,
        )
        another_block = ProgramBlock.objects.create(
            program=another_program,
            number=1,
            title="Другой каскад того же счета",
            service=self.service,
            staff_member=self.staff1,
            planned_sessions=2,
            balance_account=self.account1,
        )
        starts_at = _local(self.start_date, time(9, 0))
        Appointment.objects.create(
            child=self.child1,
            staff_member=self.staff1,
            service=self.service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
            status=Appointment.Status.RESERVED,
            billing_account=self.account1,
            program_block=another_block,
        )

        self.assertEqual(program_wizard.funded_sessions_remaining(self.block1), 0)
        self.assertEqual(self.preview().ready_count, 0)

    def test_inactive_or_expired_account_requires_explicit_unpaid_reserve(self):
        self.account2.status = BalanceAccount.Status.PAUSED
        self.account2.save(update_fields=["status", "updated_at"])
        preview = self.preview()
        self.assertEqual(preview.ready_count, 0)
        self.assertTrue(
            all(item.reason_code == "funding_unavailable" for item in preview.dates)
        )

        self.account2.status = BalanceAccount.Status.ACTIVE
        self.account2.valid_until = self.start_date - timedelta(days=1)
        self.account2.save(update_fields=["status", "valid_until", "updated_at"])
        expired = self.preview()
        self.assertEqual(expired.ready_count, 0)
        self.assertTrue(
            all(item.reason_code == "funding_unavailable" for item in expired.dates)
        )

    def test_group_series_requires_active_program_and_respects_program_dates(self):
        self.program2.status = TreatmentProgram.Status.PAUSED
        self.program2.save(update_fields=["status", "updated_at"])
        with self.assertRaisesMessage(ValidationError, "активных программ"):
            self.preview()

        self.program2.status = TreatmentProgram.Status.ACTIVE
        self.program2.starts_on = self.start_date + timedelta(days=1)
        self.program2.save(update_fields=["status", "starts_on", "updated_at"])
        with self.assertRaisesMessage(ValidationError, "начинается раньше программы"):
            self.preview()

    def test_group_series_rechecks_program_lifecycle_during_apply(self):
        preview = self.preview(end_date=self.start_date)
        self.program2.status = TreatmentProgram.Status.PAUSED
        self.program2.save(update_fields=["status", "updated_at"])

        result = program_series.create_group_series(
            preview,
            operation_key=uuid4(),
        )

        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.skipped_count, 1)
        occurrence = result.series.occurrences.get()
        self.assertEqual(occurrence.reason_code, "program_unavailable")
        self.assertIsNone(occurrence.appointment_id)

    def test_occurrence_and_series_history_cannot_be_changed_or_deleted(self):
        result = program_series.create_group_series(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
        )
        occurrence = result.series.occurrences.get()
        occurrence.reason = "Попытка изменить историю"

        with self.assertRaisesMessage(ValidationError, "неизменяем"):
            occurrence.save()
        with self.assertRaisesMessage(ValidationError, "нельзя удалить"):
            occurrence.delete()
        with self.assertRaisesMessage(ValidationError, "Серию с историей нельзя удалить"):
            result.series.delete()

    @skipUnless(connection.vendor == "postgresql", "DB guard проверяется на PostgreSQL.")
    def test_occurrence_history_is_immutable_through_queryset_writes(self):
        result = program_series.create_group_series(
            self.preview(end_date=self.start_date),
            operation_key=uuid4(),
        )
        occurrence = result.series.occurrences.get()

        with self.assertRaisesMessage(
            DatabaseError, "occurrences are immutable"
        ), transaction.atomic():
            AppointmentSeriesOccurrence.objects.filter(pk=occurrence.pk).update(
                reason="Bypass attempt"
            )
        with self.assertRaisesMessage(
            DatabaseError, "occurrences are immutable"
        ), transaction.atomic():
            AppointmentSeriesOccurrence.objects.filter(pk=occurrence.pk).delete()

    def test_group_series_view_preview_create_and_detail(self):
        key = uuid4()
        url = reverse("program_block_group_series_create", args=[self.block1.pk])

        preview_response = self.client.post(url, self.form_payload(operation_key=key))
        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(preview_response.context["preview"].ready_count, 2)
        self.assertContains(preview_response, "Предварительная проверка")
        self.assertContains(preview_response, self.child1.full_name)
        self.assertContains(preview_response, self.service.name)

        create_response = self.client.post(
            url,
            self.form_payload(operation_key=key, action="create"),
        )
        series = AppointmentSeries.objects.get(operation_key=key)
        self.assertRedirects(
            create_response,
            reverse("appointment_series_detail", args=[series.pk]),
        )
        detail = self.client.get(reverse("appointment_series_detail", args=[series.pk]))
        self.assertContains(detail, "Постоянная группа")
        self.assertContains(detail, self.child1.full_name)
        self.assertContains(detail, self.child2.full_name)
        self.assertContains(detail, self.staff2.full_name)
        self.assertContains(detail, "Открыть")

    def test_group_series_form_requires_primary_staff_in_selected_composition(self):
        payload = self.form_payload()
        payload["staff_members"] = [str(self.staff1.pk)]
        payload["primary_staff_member"] = str(self.staff2.pk)

        response = self.client.post(
            reverse("program_block_group_series_create", args=[self.block1.pk]),
            payload,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            "primary_staff_member",
            "Основной специалист должен входить в состав серии.",
        )

    def test_specialist_cannot_open_group_series_admin_views(self):
        self.client.logout()
        self.client.force_login(self.specialist_user)

        response = self.client.get(
            reverse("program_block_group_series_create", args=[self.block1.pk])
        )

        self.assertEqual(response.status_code, 403)


class GroupProgramJoinTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            "group-join-admin",
            password="x",
            is_staff=True,
            is_superuser=True,
        )
        cls.specialist_user = User.objects.create_user("group-join-specialist", password="x")
        cls.children = [
            Child.objects.create(last_name="Join", first_name=f"Child {index}")
            for index in range(1, 4)
        ]
        cls.service = Service.objects.create(
            name="Join group service",
            code="JOIN-GROUP-SERVICE",
            default_duration_minutes=45,
            default_price=Decimal("900"),
        )
        cls.other_service = Service.objects.create(
            name="Other join service",
            code="OTHER-JOIN-SERVICE",
            default_duration_minutes=45,
        )
        cls.staff = StaffMember.objects.create(
            user=cls.specialist_user,
            full_name="Join group specialist",
        )
        cls.room = Room.objects.create(
            name="Join group room",
            allow_group_sessions=True,
            limit_staff_count=True,
            max_staff_count=2,
            limit_recipient_count=True,
            max_recipient_count=4,
        )
        cls.other_room = Room.objects.create(
            name="Join conflict room",
            allow_group_sessions=True,
            max_staff_count=2,
            max_recipient_count=4,
        )
        cls.funding = FundingSource.objects.create(
            name="Join group funding",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        cls.account = BalanceAccount.objects.create(
            child=cls.children[2],
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("5"),
        )
        cls.program = TreatmentProgram.objects.create(
            child=cls.children[2],
            title="Join group program",
            status=TreatmentProgram.Status.ACTIVE,
        )
        cls.block = ProgramBlock.objects.create(
            program=cls.program,
            number=1,
            title="Join group block",
            service=cls.service,
            staff_member=cls.staff,
            planned_sessions=4,
            balance_account=cls.account,
        )

    def setUp(self):
        self.client.force_login(self.admin)
        self.day = timezone.localdate() + timedelta(days=12)

    def create_target(self, *, day=None, hour=10, service=None, room=None):
        starts_at = _local(day or self.day, time(hour, 0))
        appointment = Appointment.objects.create(
            child=self.children[0],
            staff_member=self.staff,
            service=service or self.service,
            room=room or self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
            session_type=Appointment.SessionType.GROUP,
            title="Existing join group",
            status=Appointment.Status.CONFIRMED,
        )
        AppointmentParticipant.objects.create(
            appointment=appointment,
            child=self.children[1],
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        return appointment

    def payload(self, appointment, *, operation_key=None, action="join"):
        return {
            "operation_key": str(operation_key or uuid4()),
            "date_from": self.day.isoformat(),
            "date_to": (self.day + timedelta(days=2)).isoformat(),
            "appointments": [str(appointment.pk)],
            "action": action,
        }

    def test_preview_and_join_preserve_participant_program_data_without_ledger(self):
        target = self.create_target()
        preview = program_series.preview_group_joins(
            block=self.block,
            date_from=self.day,
            date_to=self.day,
        )

        self.assertEqual(preview.ready_count, 1)
        self.assertEqual(preview.candidates[0].recipient_count_after, 3)
        result = program_series.join_program_block_to_groups(
            block=self.block,
            appointments=[target],
            operation_key=uuid4(),
            actor=self.admin,
        )

        self.assertEqual(result.joined_count, 1)
        self.assertEqual(result.skipped_count, 0)
        participant = target.participants.get(child=self.children[2])
        self.assertEqual(participant.program_block, self.block)
        self.assertEqual(participant.billing_account, self.account)
        self.assertEqual(participant.sequence_number, 1)
        self.assertEqual(participant.billing_decision, Appointment.BillingDecision.UNDECIDED)
        self.assertFalse(LedgerEntry.objects.filter(appointment_participant=participant).exists())
        self.assertEqual(
            result.series.materialization_mode,
            AppointmentSeries.MaterializationMode.JOIN_EXISTING,
        )
        self.assertEqual(len(result.series.operation_fingerprint), 64)
        self.assertEqual(result.series.default_participants.count(), 1)
        self.assertEqual(result.series.default_staff_assignments.count(), 0)
        occurrence = result.series.occurrences.get()
        self.assertEqual(occurrence.outcome, AppointmentSeriesOccurrence.Outcome.JOINED)
        self.assertEqual(occurrence.appointment, target)
        self.assertEqual(occurrence.appointment_participant, participant)
        with self.assertRaises(ProtectedError):
            participant.delete()

        local_start = timezone.localtime(target.starts_at)
        edit_response = self.client.post(
            reverse("appointment_edit", args=[target.pk]),
            {
                "child": self.children[0].pk,
                "participants": [self.children[0].pk, self.children[1].pk],
                "service": self.service.pk,
                "staff_member": self.staff.pk,
                "staff_members": [self.staff.pk],
                "room": self.room.pk,
                "session_type": Appointment.SessionType.GROUP,
                "status": Appointment.Status.CONFIRMED,
                "date": local_start.date().isoformat(),
                "time": local_start.strftime("%H:%M"),
                "duration_minutes": "45",
            },
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, "История присоединения неизменяема")
        self.assertTrue(target.participants.filter(pk=participant.pk).exists())

    def test_repeat_is_idempotent_and_changed_payload_is_rejected(self):
        first = self.create_target()
        second = self.create_target(day=self.day + timedelta(days=1))
        operation_key = uuid4()

        initial = program_series.join_program_block_to_groups(
            block=self.block,
            appointments=[first],
            operation_key=operation_key,
        )
        repeated = program_series.join_program_block_to_groups(
            block=self.block,
            appointments=[first],
            operation_key=operation_key,
        )

        self.assertEqual(initial.joined_count, 1)
        self.assertTrue(repeated.reused_series)
        self.assertEqual(repeated.unchanged_count, 1)
        self.assertEqual(first.participants.filter(child=self.children[2]).count(), 1)
        with self.assertRaisesMessage(ValidationError, "другого набора"):
            program_series.join_program_block_to_groups(
                block=self.block,
                appointments=[second],
                operation_key=operation_key,
            )

    def test_preview_blocks_capacity_recipient_conflict_and_missing_funding(self):
        target = self.create_target()
        self.room.max_recipient_count = 2
        self.room.save(update_fields=["max_recipient_count", "updated_at"])

        capacity_preview = program_series.preview_group_joins(
            block=self.block,
            date_from=self.day,
            date_to=self.day,
        )
        self.assertEqual(capacity_preview.candidates[0].reason_code, "capacity")

        self.room.max_recipient_count = 4
        self.room.save(update_fields=["max_recipient_count", "updated_at"])
        conflict_report = program_series.scheduling.ConflictReport(
            child_conflict=target,
            staff_conflict=None,
            room_conflict=None,
        )
        with patch.object(
            program_series.scheduling,
            "find_overlaps",
            return_value=conflict_report,
        ):
            conflict_preview = program_series.preview_group_joins(
                block=self.block,
                date_from=self.day,
                date_to=self.day,
            )
        self.assertEqual(conflict_preview.candidates[0].reason_code, "recipient_conflict")

        self.account.initial_amount = Decimal("0")
        self.account.save(update_fields=["initial_amount", "updated_at"])
        funding_preview = program_series.preview_group_joins(
            block=self.block,
            date_from=self.day,
            date_to=self.day,
        )
        self.assertEqual(funding_preview.candidates[0].reason_code, "funding_limit")

    def test_join_fails_closed_when_block_account_changes_after_selection(self):
        target = self.create_target()
        series, _ = program_series._create_join_series_definition(
            self.block,
            (target,),
            operation_key=uuid4(),
        )
        replacement = BalanceAccount.objects.create(
            child=self.children[2],
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("5"),
        )
        self.block.balance_account = replacement
        self.block.save(update_fields=["balance_account", "updated_at"])

        occurrence, created = program_series._join_one_appointment(
            series,
            target,
            actor=self.admin,
        )

        self.assertTrue(created)
        self.assertEqual(occurrence.outcome, AppointmentSeriesOccurrence.Outcome.SKIPPED)
        self.assertEqual(occurrence.reason_code, "funding_changed")
        self.assertFalse(target.participants.filter(child=self.children[2]).exists())

    def test_operation_key_rejects_same_appointment_after_time_change(self):
        target = self.create_target()
        operation_key = uuid4()
        program_series._create_join_series_definition(
            self.block,
            (target,),
            operation_key=operation_key,
        )
        moved_start = target.starts_at + timedelta(hours=1)
        Appointment.objects.filter(pk=target.pk).update(
            starts_at=moved_start,
            ends_at=moved_start + timedelta(minutes=45),
        )
        target.refresh_from_db()

        with self.assertRaisesMessage(ValidationError, "другого набора"):
            program_series.join_program_block_to_groups(
                block=self.block,
                appointments=[target],
                operation_key=operation_key,
            )
        self.assertFalse(target.participants.filter(child=self.children[2]).exists())

    def test_join_view_search_apply_detail_and_permissions(self):
        target = self.create_target()
        operation_key = uuid4()
        url = reverse("program_block_group_join", args=[self.block.pk])

        search_response = self.client.post(
            url,
            self.payload(target, operation_key=operation_key, action="search"),
        )
        self.assertEqual(search_response.status_code, 200)
        self.assertContains(search_response, "Existing join group")
        self.assertContains(search_response, "готово")

        join_response = self.client.post(
            url,
            self.payload(target, operation_key=operation_key),
        )
        series = AppointmentSeries.objects.get(operation_key=operation_key)
        self.assertRedirects(
            join_response,
            reverse("appointment_series_detail", args=[series.pk]),
        )
        detail = self.client.get(reverse("appointment_series_detail", args=[series.pk]))
        self.assertContains(detail, "Присоединять к существующим")
        self.assertContains(detail, "Фактические данные")
        self.assertContains(detail, self.children[2].full_name)
        self.assertContains(detail, "Присоединено")

        repeat_response = self.client.post(
            url,
            self.payload(target, operation_key=operation_key),
            follow=True,
        )
        self.assertContains(
            repeat_response,
            "Повторный запрос распознан без создания дублей",
        )
        self.assertEqual(target.participants.filter(child=self.children[2]).count(), 1)

        self.client.logout()
        self.client.force_login(self.specialist_user)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_join_existing_mode_requires_group_and_available_funding(self):
        series = AppointmentSeries(
            child=self.children[2],
            service=self.service,
            staff_member=self.staff,
            room=self.room,
            program_block=self.block,
            title="Invalid join series",
            start_date=self.day,
            end_date=self.day,
            days_of_week="ПН",
            time=time(10, 0),
            duration_minutes=45,
            session_type=Appointment.SessionType.INDIVIDUAL,
            materialization_mode=AppointmentSeries.MaterializationMode.JOIN_EXISTING,
        )
        with self.assertRaisesMessage(ValidationError, "только к групповым"):
            series.full_clean()

    @skipUnless(
        connection.vendor == "postgresql",
        "DB-ограничение истории присоединения проверяется только на PostgreSQL.",
    )
    def test_database_rejects_joined_occurrence_without_participant(self):
        target = self.create_target()
        series, _ = program_series._create_join_series_definition(
            self.block,
            (target,),
            operation_key=uuid4(),
        )

        with self.assertRaises(DatabaseError), transaction.atomic():
            AppointmentSeriesOccurrence.objects.bulk_create(
                [
                    AppointmentSeriesOccurrence(
                        series=series,
                        scheduled_starts_at=target.starts_at,
                        appointment=target,
                        outcome=AppointmentSeriesOccurrence.Outcome.JOINED,
                    )
                ]
            )

        other_target = self.create_target(day=self.day + timedelta(days=1))
        unrelated_participant = other_target.primary_participant
        with self.assertRaises(DatabaseError), transaction.atomic():
            AppointmentSeriesOccurrence.objects.bulk_create(
                [
                    AppointmentSeriesOccurrence(
                        series=series,
                        scheduled_starts_at=target.starts_at,
                        appointment=target,
                        appointment_participant=unrelated_participant,
                        outcome=AppointmentSeriesOccurrence.Outcome.JOINED,
                    )
                ]
            )


class GroupProgramSeriesPostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.children = [
            Child.objects.create(last_name="Concurrent", first_name=f"Child {index}")
            for index in range(1, 5)
        ]
        self.staff_members = [
            StaffMember.objects.create(full_name=f"Concurrent group specialist {index}")
            for index in range(1, 3)
        ]
        self.service = Service.objects.create(
            name="Concurrent group service",
            code="CONCURRENT-GROUP-SERIES",
            default_duration_minutes=30,
        )
        self.room = Room.objects.create(
            name="Concurrent group room",
            allow_group_sessions=True,
            max_staff_count=2,
            max_recipient_count=2,
        )
        funding = FundingSource.objects.create(
            name="Concurrent group funding",
            source_type=FundingSource.SourceType.PERSONAL,
            transfer_policy=FundingSource.TransferPolicy.WITHIN_CHILD,
        )
        self.blocks = []
        for position, child in enumerate(self.children, start=1):
            account = BalanceAccount.objects.create(
                child=child,
                funding_source=funding,
                unit=BalanceAccount.Unit.SESSIONS,
                service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
                service=self.service,
                initial_amount=Decimal("10"),
            )
            program = TreatmentProgram.objects.create(
                child=child,
                title=f"Concurrent program {position}",
                status=TreatmentProgram.Status.ACTIVE,
            )
            self.blocks.append(
                ProgramBlock.objects.create(
                    program=program,
                    number=1,
                    title=f"Concurrent block {position}",
                    service=self.service,
                    staff_member=self.staff_members[0 if position <= 2 else 1],
                    planned_sessions=5,
                    balance_account=account,
                )
            )
        self.day = timezone.localdate() + timedelta(days=20)

    def _create_join_target(self, *, hour=12):
        starts_at = _local(self.day, time(hour, 0))
        return Appointment.objects.create(
            child=self.children[0],
            staff_member=self.staff_members[0],
            service=self.service,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            session_type=Appointment.SessionType.GROUP,
            title="Concurrent existing group",
            status=Appointment.Status.CONFIRMED,
        )

    def _join_in_thread(self, block_id, appointment_id, operation_key, barrier, outcomes):
        close_old_connections()
        try:
            block = ProgramBlock.objects.select_related(
                "program__child",
                "service",
                "staff_member",
                "balance_account",
            ).get(pk=block_id)
            appointment = Appointment.objects.select_related(
                "child",
                "service",
                "staff_member",
                "room",
            ).get(pk=appointment_id)
            barrier.wait(timeout=10)
            result = program_series.join_program_block_to_groups(
                block=block,
                appointments=[appointment],
                operation_key=operation_key,
            )
        except BaseException as exc:
            outcomes.put(exc)
        else:
            outcomes.put(
                (result.joined_count, result.skipped_count, result.unchanged_count)
            )
        finally:
            connection.close()

    def _run_competing_joins(self, specs):
        barrier = Barrier(2)
        outcomes = Queue()
        threads = [
            Thread(
                target=self._join_in_thread,
                args=(*spec, barrier, outcomes),
            )
            for spec in specs
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        return [outcomes.get_nowait() for _ in range(2)]

    def _preview_from_database(self, block_ids=None, staff_id=None):
        block_ids = block_ids or [block.pk for block in self.blocks[:2]]
        staff_id = staff_id or self.staff_members[0].pk
        blocks = list(
            ProgramBlock.objects.select_related(
                "program__child",
                "service",
                "staff_member",
                "balance_account",
            )
            .filter(pk__in=block_ids)
            .order_by("pk")
        )
        return program_series.preview_group_series(
            blocks=blocks,
            staff_members=[StaffMember.objects.get(pk=staff_id)],
            room=Room.objects.get(pk=self.room.pk),
            title="Concurrent group",
            start_date=self.day,
            end_date=self.day,
            weekdays={self.day.weekday()},
            start_time=time(10, 0),
            duration_minutes=30,
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентное создание серий проверяется только на PostgreSQL.",
    )
    def test_competing_series_cannot_overbook_room_or_duplicate_sequences(self):
        barrier = Barrier(2)
        outcomes = Queue()

        groups = (
            ([block.pk for block in self.blocks[:2]], self.staff_members[0].pk),
            ([block.pk for block in self.blocks[2:]], self.staff_members[1].pk),
        )

        def run(block_ids, staff_id):
            close_old_connections()
            try:
                preview = self._preview_from_database(block_ids, staff_id)
                barrier.wait(timeout=10)
                result = program_series.create_group_series(
                    preview,
                    operation_key=uuid4(),
                )
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put((result.created_count, result.skipped_count))
            finally:
                connection.close()

        threads = [Thread(target=run, args=group) for group in groups]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual(sorted(results), [(0, 1), (1, 0)])
        appointments = Appointment.objects.filter(
            room=self.room,
            starts_at=_local(self.day, time(10, 0)),
        )
        self.assertEqual(appointments.count(), 1)
        sequences = list(
            AppointmentParticipant.objects.filter(appointment__in=appointments)
            .order_by("program_block_id")
            .values_list("sequence_number", flat=True)
        )
        self.assertEqual(sequences, [1, 1])

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентная нумерация каскада проверяется только на PostgreSQL.",
    )
    def test_concurrent_appointments_receive_distinct_monotonic_sequence_numbers(self):
        barrier = Barrier(2)
        outcomes = Queue()
        block_id = self.blocks[0].pk
        child_id = self.children[0].pk
        staff_id = self.staff_members[0].pk
        service_id = self.service.pk

        def run(hour):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                starts_at = _local(self.day, time(hour, 0))
                appointment = Appointment.objects.create(
                    child_id=child_id,
                    staff_member_id=staff_id,
                    service_id=service_id,
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(minutes=30),
                    program_block_id=block_id,
                )
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put(appointment.primary_participant.sequence_number)
            finally:
                connection.close()

        threads = [Thread(target=run, args=(10,)), Thread(target=run, args=(11,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertTrue(all(isinstance(item, int) for item in results), results)
        self.assertEqual(sorted(results), [1, 2])

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентная идемпотентность серии проверяется только на PostgreSQL.",
    )
    def test_concurrent_same_operation_key_creates_one_series_and_one_occurrence(self):
        barrier = Barrier(2)
        outcomes = Queue()
        operation_key = uuid4()

        def run():
            close_old_connections()
            try:
                preview = self._preview_from_database()
                barrier.wait(timeout=10)
                result = program_series.create_group_series(
                    preview,
                    operation_key=operation_key,
                )
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put((result.created_count, result.unchanged_count))
            finally:
                connection.close()

        threads = [Thread(target=run), Thread(target=run)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual(sorted(results), [(0, 1), (1, 0)])
        self.assertEqual(AppointmentSeries.objects.filter(operation_key=operation_key).count(), 1)
        series = AppointmentSeries.objects.get(operation_key=operation_key)
        self.assertEqual(series.occurrences.count(), 1)
        self.assertEqual(Appointment.objects.filter(series=series).count(), 1)

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентное присоединение к последнему месту проверяется только на PostgreSQL.",
    )
    def test_competing_group_joins_cannot_take_the_same_last_room_place(self):
        target = self._create_join_target()

        results = self._run_competing_joins(
            [
                (self.blocks[1].pk, target.pk, uuid4()),
                (self.blocks[2].pk, target.pk, uuid4()),
            ]
        )

        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual(sorted(results), [(0, 1, 0), (1, 0, 0)])
        self.assertEqual(target.participants.count(), 2)
        self.assertEqual(
            AppointmentSeriesOccurrence.objects.filter(
                appointment=target,
                outcome=AppointmentSeriesOccurrence.Outcome.JOINED,
            ).count(),
            1,
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "Идемпотентность присоединения проверяется только на PostgreSQL.",
    )
    def test_concurrent_same_join_operation_key_creates_one_participant(self):
        target = self._create_join_target()
        operation_key = uuid4()

        results = self._run_competing_joins(
            [
                (self.blocks[1].pk, target.pk, operation_key),
                (self.blocks[1].pk, target.pk, operation_key),
            ]
        )

        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual(sorted(results), [(0, 0, 1), (1, 0, 0)])
        self.assertEqual(
            target.participants.filter(child=self.children[1]).count(),
            1,
        )
        series = AppointmentSeries.objects.get(operation_key=operation_key)
        self.assertEqual(series.occurrences.count(), 1)

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентный лимит оплаты присоединений проверяется только на PostgreSQL.",
    )
    def test_shared_last_funded_session_allows_only_one_join(self):
        first_target = self._create_join_target(hour=12)
        second_target = self._create_join_target(hour=13)
        block = self.blocks[1]
        block.balance_account.initial_amount = Decimal("1")
        block.balance_account.save(update_fields=["initial_amount", "updated_at"])

        results = self._run_competing_joins(
            [
                (block.pk, first_target.pk, uuid4()),
                (block.pk, second_target.pk, uuid4()),
            ]
        )

        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual(sorted(results), [(0, 1, 0), (1, 0, 0)])
        joined = AppointmentParticipant.objects.filter(
            program_block=block,
            child=self.children[1],
        )
        self.assertEqual(joined.count(), 1)
        self.assertEqual(joined.get().sequence_number, 1)

    @skipUnless(
        connection.vendor == "postgresql",
        "Порядок блокировок присоединения и переноса проверяется только на PostgreSQL.",
    )
    def test_join_and_block_transfer_complete_without_deadlock(self):
        target = self._create_join_target()
        block = self.blocks[1]
        source = BalanceAccount.objects.create(
            child=self.children[1],
            funding_source=block.balance_account.funding_source,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("2"),
        )
        barrier = Barrier(2)
        outcomes = Queue()

        def join_group():
            close_old_connections()
            try:
                selected_block = ProgramBlock.objects.select_related(
                    "program__child", "service", "balance_account"
                ).get(pk=block.pk)
                selected_target = Appointment.objects.select_related(
                    "child", "service", "staff_member", "room"
                ).get(pk=target.pk)
                barrier.wait(timeout=10)
                result = program_series.join_program_block_to_groups(
                    block=selected_block,
                    appointments=[selected_target],
                    operation_key=uuid4(),
                )
                outcomes.put(("join", result.joined_count))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        def transfer_funds():
            close_old_connections()
            try:
                selected_block = ProgramBlock.objects.get(pk=block.pk)
                from_account = BalanceAccount.objects.get(pk=source.pk)
                to_account = BalanceAccount.objects.get(pk=block.balance_account_id)
                barrier.wait(timeout=10)
                transfer = billing_svc.record_balance_transfer(
                    from_account=from_account,
                    to_account=to_account,
                    amount=Decimal("1"),
                    reason="Concurrent transfer while joining a group.",
                    program_block=selected_block,
                    idempotency_key=uuid4(),
                )
                outcomes.put(("transfer", transfer.pk))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        threads = [Thread(target=join_group), Thread(target=transfer_funds)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertTrue(all(isinstance(item, tuple) for item in results), results)
        self.assertEqual({item[0] for item in results}, {"join", "transfer"})
        self.assertEqual(target.participants.filter(child=self.children[1]).count(), 1)


class ProgramWizardPostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.child = Child.objects.create(last_name="Wizard", first_name="Concurrent")
        self.staff = StaffMember.objects.create(full_name="Wizard concurrent specialist")
        self.service = Service.objects.create(
            name="Wizard concurrent service",
            code="WIZARD-CONCURRENT",
            default_duration_minutes=30,
        )
        self.rooms = [
            Room.objects.create(name=f"Wizard concurrent room {index}")
            for index in range(1, 3)
        ]
        funding = FundingSource.objects.create(
            name="Wizard concurrent funding",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        self.account = BalanceAccount.objects.create(
            child=self.child,
            funding_source=funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("1"),
        )
        program = TreatmentProgram.objects.create(
            child=self.child,
            title="Wizard concurrent program",
            status=TreatmentProgram.Status.ACTIVE,
        )
        self.blocks = [
            ProgramBlock.objects.create(
                program=program,
                number=index,
                title=f"Wizard concurrent block {index}",
                service=self.service,
                staff_member=self.staff,
                planned_sessions=5,
                balance_account=self.account,
            )
            for index in range(1, 3)
        ]
        self.day = timezone.localdate() + timedelta(days=24)

    def _preview(self, block_id, room_id, hour):
        block = ProgramBlock.objects.select_related(
            "program__child", "service", "staff_member", "balance_account"
        ).get(pk=block_id)
        return program_wizard.suggest_program_block_slots(
            block,
            date_from=self.day,
            date_to=self.day,
            weekdays={self.day.weekday()},
            time_from=time(hour, 0),
            time_until=time(hour, 30),
            duration_minutes=30,
            staff_member=StaffMember.objects.get(pk=self.staff.pk),
            room=Room.objects.get(pk=room_id),
            requested_count=1,
        )

    def _run_competing_previews(self, specs):
        barrier = Barrier(2)
        outcomes = Queue()

        def run(block_id, room_id, hour):
            close_old_connections()
            try:
                preview = self._preview(block_id, room_id, hour)
                barrier.wait(timeout=10)
                result = program_wizard.create_schedule_from_preview(preview)
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put(len(result.appointments))
            finally:
                connection.close()

        threads = [Thread(target=run, args=spec) for spec in specs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        return [outcomes.get_nowait() for _ in range(2)]

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентный лимит общего счета проверяется только на PostgreSQL.",
    )
    def test_shared_account_cannot_reserve_two_blocks_concurrently(self):
        results = self._run_competing_previews(
            [
                (self.blocks[0].pk, self.rooms[0].pk, 10),
                (self.blocks[1].pk, self.rooms[1].pk, 11),
            ]
        )

        self.assertEqual(sum(item == 1 for item in results), 1, results)
        self.assertEqual(sum(isinstance(item, ValidationError) for item in results), 1, results)
        self.assertEqual(Appointment.objects.count(), 1)

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентный лимит плана проверяется только на PostgreSQL.",
    )
    def test_block_plan_cannot_be_exceeded_concurrently(self):
        self.account.initial_amount = Decimal("10")
        self.account.save(update_fields=["initial_amount", "updated_at"])
        block = self.blocks[0]
        block.planned_sessions = 1
        block.save(update_fields=["planned_sessions", "updated_at"])

        results = self._run_competing_previews(
            [
                (block.pk, self.rooms[0].pk, 10),
                (block.pk, self.rooms[1].pk, 11),
            ]
        )

        self.assertEqual(sum(item == 1 for item in results), 1, results)
        self.assertEqual(sum(isinstance(item, ValidationError) for item in results), 1, results)
        self.assertEqual(Appointment.objects.filter(program_block=block).count(), 1)


class GroupProgramSeriesMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("operations"))
        super().tearDown()

    @skipUnless(connection.vendor == "postgresql", "Миграционный backfill проверяется на PostgreSQL.")
    def test_legacy_series_is_backfilled_without_removing_legacy_fields(self):
        target_before = [("operations", "0054_donor_report_submission")]
        target_after = [("operations", "0055_group_program_series")]
        executor = MigrationExecutor(connection)
        executor.migrate(target_before)
        old_apps = executor.loader.project_state(target_before).apps

        child_old = old_apps.get_model("operations", "Child")
        staff_old = old_apps.get_model("operations", "StaffMember")
        service_old = old_apps.get_model("operations", "Service")
        room_old = old_apps.get_model("operations", "Room")
        program_old = old_apps.get_model("operations", "TreatmentProgram")
        block_old = old_apps.get_model("operations", "ProgramBlock")
        series_old = old_apps.get_model("operations", "AppointmentSeries")
        appointment_old = old_apps.get_model("operations", "Appointment")

        child = child_old.objects.create(last_name="Legacy", first_name="Series")
        staff = staff_old.objects.create(full_name="Legacy series staff")
        service = service_old.objects.create(name="Legacy series service", code="LEGACY-SERIES")
        room = room_old.objects.create(name="Legacy series room")
        program = program_old.objects.create(child=child, title="Legacy program")
        block = block_old.objects.create(
            program=program,
            number=1,
            title="Legacy block",
            service=service,
            planned_sessions=2,
        )
        day = timezone.localdate() + timedelta(days=30)
        series = series_old.objects.create(
            child=child,
            service=service,
            staff_member=staff,
            room=room,
            program_block=block,
            title="Legacy series",
            start_date=day,
            end_date=day,
            days_of_week="ПН",
            time=time(10, 0),
            duration_minutes=30,
            status="active",
        )
        starts_at = _local(day, time(10, 0))
        appointment = appointment_old.objects.create(
            child=child,
            service=service,
            staff_member=staff,
            room=room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            series=series,
            program_block=block,
            sequence_number=1,
            session_type="group",
        )
        duplicate = appointment_old.objects.create(
            child=child,
            service=service,
            staff_member=staff,
            room=room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            series=series,
            program_block=block,
            sequence_number=2,
            status="cancelled",
            session_type="group",
        )
        migration = importlib.import_module("operations.migrations.0055_group_program_series")
        with self.assertRaisesMessage(RuntimeError, "Duplicate legacy series occurrence times"):
            migration.assert_legacy_series_occurrences_unique(old_apps, None)
        duplicate.delete()

        executor = MigrationExecutor(connection)
        executor.migrate(target_after)
        new_apps = executor.loader.project_state(target_after).apps
        series_new = new_apps.get_model("operations", "AppointmentSeries")
        membership_new = new_apps.get_model("operations", "AppointmentSeriesParticipant")
        staff_assignment_new = new_apps.get_model(
            "operations", "AppointmentSeriesStaffAssignment"
        )
        occurrence_new = new_apps.get_model("operations", "AppointmentSeriesOccurrence")

        migrated = series_new.objects.get(pk=series.pk)
        self.assertEqual(migrated.child_id, child.pk)
        self.assertEqual(migrated.staff_member_id, staff.pk)
        self.assertEqual(migrated.program_block_id, block.pk)
        self.assertEqual(migrated.session_type, "group")
        self.assertEqual(migrated.default_appointment_status, "confirmed")
        self.assertIsNotNone(migrated.operation_key)
        self.assertTrue(
            membership_new.objects.filter(
                series_id=series.pk,
                child_id=child.pk,
                program_block_id=block.pk,
            ).exists()
        )
        self.assertTrue(
            staff_assignment_new.objects.filter(
                series_id=series.pk,
                staff_member_id=staff.pk,
            ).exists()
        )
        self.assertTrue(
            occurrence_new.objects.filter(
                series_id=series.pk,
                appointment_id=appointment.pk,
                outcome="created",
                reason_code="legacy_backfill",
            ).exists()
        )
        occurrence_new.objects.create(
            series_id=series.pk,
            scheduled_starts_at=starts_at + timedelta(days=1),
            outcome="skipped",
            reason_code="new_series_history",
            reason="New history must block a destructive reverse migration.",
        )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError,
            "Cannot reverse group program series migration while new series history exists",
        ):
            executor.migrate(target_before)

    @skipUnless(
        connection.vendor == "postgresql",
        "Защита отката истории присоединения проверяется на PostgreSQL.",
    )
    def test_join_series_history_blocks_reverse_migration(self):
        target_before = [("operations", "0055_group_program_series")]
        target_after = [("operations", "0056_group_series_join_mode")]
        executor = MigrationExecutor(connection)
        executor.migrate(target_before)

        executor = MigrationExecutor(connection)
        executor.migrate(target_after)
        apps = executor.loader.project_state(target_after).apps
        child_model = apps.get_model("operations", "Child")
        staff_model = apps.get_model("operations", "StaffMember")
        service_model = apps.get_model("operations", "Service")
        room_model = apps.get_model("operations", "Room")
        series_model = apps.get_model("operations", "AppointmentSeries")

        child = child_model.objects.create(last_name="Join", first_name="History")
        staff = staff_model.objects.create(full_name="Join migration staff")
        service = service_model.objects.create(
            name="Join migration service",
            code="JOIN-MIGRATION",
        )
        room = room_model.objects.create(name="Join migration room")
        day = timezone.localdate() + timedelta(days=30)
        series_model.objects.create(
            child=child,
            service=service,
            staff_member=staff,
            room=room,
            title="Join migration history",
            start_date=day,
            end_date=day,
            days_of_week="ПН",
            time=time(10, 0),
            duration_minutes=30,
            session_type="group",
            materialization_mode="join_existing",
            status="active",
        )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError,
            "Cannot reverse group-series join mode while joined history exists",
        ):
            executor.migrate(target_before)
