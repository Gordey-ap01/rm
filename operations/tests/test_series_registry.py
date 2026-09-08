from __future__ import annotations

from datetime import date, time, timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentSeries,
    AppointmentSeriesParticipant,
    BalanceAccount,
    Child,
    FundingSource,
    ProgramBlock,
    Room,
    Service,
    StaffMember,
    TreatmentProgram,
)
from operations.services import program_series, series_revisions

User = get_user_model()


class SeriesRegistryTests(TestCase):
    """Acceptance coverage for the read-only appointment-series registry."""

    @classmethod
    def setUpTestData(cls):
        cls.administrator = User.objects.create_user(
            "series-registry-administrator",
            password="x",
            is_staff=True,
        )
        cls.director = User.objects.create_superuser("series-registry-director", password="x")
        cls.specialist_user = User.objects.create_user("series-registry-specialist", password="x")
        cls.specialist = StaffMember.objects.create(
            user=cls.specialist_user,
            full_name="Специалист реестра",
        )

        cls.service = Service.objects.create(
            name="Логопедия для реестра",
            code="SERIES-REGISTRY-SPEECH",
            category=Service.Category.SPEECH,
            default_duration_minutes=45,
            default_price=Decimal("1200"),
        )
        cls.room = Room.objects.create(
            name="Кабинет реестра",
            room_type=Room.RoomType.GROUP,
            allow_group_sessions=True,
            limit_staff_count=True,
            max_staff_count=2,
            limit_recipient_count=True,
            max_recipient_count=4,
        )
        cls.funding = FundingSource.objects.create(
            name="Личные средства реестра",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        cls.children = [
            Child.objects.create(last_name="Реестр", first_name=name)
            for name in ("Первый", "Второй", "Третий")
        ]
        cls.blocks = [cls._make_block(child, index + 1) for index, child in enumerate(cls.children)]

        cls.period_start = timezone.localdate() + timedelta(days=20)
        cls.period_end = cls.period_start + timedelta(days=7)
        cls.created_series = cls._make_created_series()
        cls.historical_revision = cls.created_series.current_revision
        cls._flush_pending_revision_constraints()

        # The second revision removes child two from the current projection. The
        # old revision remains searchable by both recipient and program.
        cls.current_revision = series_revisions.revise_future_composition(
            cls.created_series,
            expected_revision_id=cls.historical_revision.pk,
            effective_from=cls.period_start + timedelta(days=1),
            participants=[
                series_revisions.SeriesParticipantInput(
                    child_id=cls.children[0].pk,
                    program_block_id=cls.blocks[0].pk,
                    billing_account_id=cls.blocks[0].balance_account_id,
                    position=1,
                ),
                series_revisions.SeriesParticipantInput(
                    child_id=cls.children[2].pk,
                    program_block_id=cls.blocks[2].pk,
                    billing_account_id=cls.blocks[2].balance_account_id,
                    position=2,
                ),
            ],
            staff_assignments=[
                series_revisions.SeriesStaffInput(
                    staff_member_id=cls.specialist.pk,
                    role="primary",
                ),
            ],
            actor=cls.administrator,
            reason="Обновление состава реестра.",
        )
        cls.created_series.refresh_from_db()

        cls.legacy_series = cls._make_legacy_series(
            title="Legacy серия",
            child=cls.children[0],
            block=cls.blocks[0],
            start_date=cls.period_start - timedelta(days=3),
            end_date=cls.period_start - timedelta(days=1),
        )
        cls.join_series = cls._make_join_series()

        # Make enough legacy roots to exercise the 25-row paginator without
        # creating appointments or any unnecessary materialization history.
        cls.pagination_series = [
            cls._make_legacy_series(
                title=f"Pagination {index:02d}",
                child=cls.children[0],
                block=cls.blocks[0],
                start_date=cls.period_start + timedelta(days=30 + index),
                end_date=cls.period_start + timedelta(days=30 + index),
            )
            for index in range(26)
        ]

    @classmethod
    def _make_block(cls, child: Child, number: int) -> ProgramBlock:
        account = BalanceAccount.objects.create(
            child=child,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("20"),
        )
        program = TreatmentProgram.objects.create(
            child=child,
            title=f"Программа реестра {number}",
            status=TreatmentProgram.Status.ACTIVE,
        )
        return ProgramBlock.objects.create(
            program=program,
            number=1,
            title=f"Каскад реестра {number}",
            service=cls.service,
            staff_member=cls.specialist,
            planned_sessions=20,
            balance_account=account,
        )

    @classmethod
    def _make_created_series(cls) -> AppointmentSeries:
        preview = program_series.preview_group_series(
            blocks=cls.blocks[:2],
            staff_members=[cls.specialist],
            room=cls.room,
            title="Редактируемая серия",
            start_date=cls.period_start,
            end_date=cls.period_end,
            weekdays={cls.period_start.weekday()},
            start_time=time(10, 0),
            duration_minutes=45,
            default_appointment_status=Appointment.Status.PROPOSED,
        )
        series, reused = program_series._create_series_definition(
            preview,
            operation_key=uuid4(),
        )
        assert not reused
        series_revisions.ensure_initial_revision(series, actor=cls.administrator)
        return AppointmentSeries.objects.get(pk=series.pk)

    @classmethod
    def _make_legacy_series(
        cls,
        *,
        title: str,
        child: Child,
        block: ProgramBlock,
        start_date: date,
        end_date: date,
    ) -> AppointmentSeries:
        return AppointmentSeries.objects.create(
            child=child,
            service=cls.service,
            staff_member=cls.specialist,
            room=cls.room,
            program_block=block,
            title=title,
            start_date=start_date,
            end_date=end_date,
            days_of_week="ПН",
            time=time(9, 0),
            duration_minutes=45,
            session_type=Appointment.SessionType.INDIVIDUAL,
            materialization_mode=AppointmentSeries.MaterializationMode.CREATE_APPOINTMENTS,
            default_appointment_status=Appointment.Status.PROPOSED,
            status=AppointmentSeries.Status.ACTIVE,
        )

    @classmethod
    def _make_join_series(cls) -> AppointmentSeries:
        series = AppointmentSeries.objects.create(
            child=cls.children[2],
            service=cls.service,
            staff_member=cls.specialist,
            room=cls.room,
            program_block=cls.blocks[2],
            title="Join серия",
            start_date=cls.period_start,
            end_date=cls.period_end,
            days_of_week="ПН",
            time=time(11, 0),
            duration_minutes=45,
            session_type=Appointment.SessionType.GROUP,
            materialization_mode=AppointmentSeries.MaterializationMode.JOIN_EXISTING,
            default_appointment_status=Appointment.Status.PROPOSED,
            status=AppointmentSeries.Status.ACTIVE,
        )
        AppointmentSeriesParticipant.objects.create(
            series=series,
            child=cls.children[2],
            program_block=cls.blocks[2],
            billing_account=cls.blocks[2].balance_account,
            position=1,
        )
        return series

    @classmethod
    def _flush_pending_revision_constraints(cls):
        if connection.vendor != "postgresql":
            return
        names = ", ".join(
            connection.ops.quote_name(name)
            for name in (
                "operations_appointmentseriesrevision_composition",
                "operations_appointmentseriesrevisionparticipant_composition",
                "operations_appointmentseriesrevisionstaffassignment_composition",
            )
        )
        with connection.cursor() as cursor:
            cursor.execute(f"SET CONSTRAINTS {names} IMMEDIATE")
            cursor.execute(f"SET CONSTRAINTS {names} DEFERRED")

    def setUp(self):
        self.client.force_login(self.administrator)

    def get_registry(self, **params):
        return self.client.get(reverse("appointment_series_list"), params)

    def row_series(self, response):
        return [row["series"] for row in response.context["registry_rows"]]

    def test_registry_requires_management_role(self):
        self.client.logout()
        anonymous = self.client.get(reverse("appointment_series_list"))
        self.assertEqual(anonymous.status_code, 302)

        self.client.force_login(self.specialist_user)
        self.assertEqual(self.client.get(reverse("appointment_series_list")).status_code, 403)

        self.client.force_login(self.administrator)
        self.assertEqual(self.client.get(reverse("appointment_series_list")).status_code, 200)

        self.client.force_login(self.director)
        self.assertEqual(self.client.get(reverse("appointment_series_list")).status_code, 200)
        self.assertEqual(self.client.post(reverse("appointment_series_list")).status_code, 405)

    def test_invalid_filters_are_reported_and_fail_closed(self):
        invalid_queries = (
            {"recipient": "999999"},
            {"program": "999999"},
            {"status": "not-a-status"},
            {"date_from": "not-a-date"},
            {"date_to": "2026-99-99"},
            {
                "date_from": self.period_end.isoformat(),
                "date_to": self.period_start.isoformat(),
            },
            {
                "recipient": self.children[0].pk,
                "program": self.blocks[1].program_id,
            },
        )
        for query in invalid_queries:
            with self.subTest(query=query):
                response = self.get_registry(**query)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context["filters_valid"])
                self.assertTrue(response.context["filter_form"].errors)
                self.assertEqual(list(response.context["page_obj"].object_list), [])
                self.assertEqual(response.context["registry_rows"], [])

    def test_combined_recipient_and_program_filters_use_one_membership(self):
        response = self.get_registry(
            recipient=self.children[1].pk,
            program=self.blocks[1].program_id,
        )
        self.assertTrue(response.context["filters_valid"])
        self.assertEqual([series.pk for series in self.row_series(response)], [self.created_series.pk])
        self.assertEqual(response.context["selected_recipient"].pk, self.children[1].pk)
        self.assertEqual(response.context["selected_program"].pk, self.blocks[1].program_id)

        mismatch = self.get_registry(
            recipient=self.children[1].pk,
            program=self.blocks[0].program_id,
        )
        self.assertFalse(mismatch.context["filters_valid"])
        self.assertEqual(mismatch.context["registry_rows"], [])

    def test_date_filter_is_inclusive_on_both_series_boundaries(self):
        for query in (
            {"date_from": self.period_start.isoformat(), "date_to": self.period_start.isoformat()},
            {"date_from": self.period_end.isoformat(), "date_to": self.period_end.isoformat()},
        ):
            with self.subTest(query=query):
                response = self.get_registry(**query)
                ids = {series.pk for series in self.row_series(response)}
                self.assertIn(self.created_series.pk, ids)

        outside = self.get_registry(
            date_from=(self.period_end + timedelta(days=1)).isoformat(),
            date_to=(self.period_end + timedelta(days=2)).isoformat(),
        )
        self.assertEqual(
            {series.pk for series in self.row_series(outside)},
            set(),
        )

    def test_membership_union_keeps_history_and_deduplicates_rows(self):
        historical = self.get_registry(recipient=self.children[1].pk)
        self.assertEqual(
            [series.pk for series in self.row_series(historical)],
            [self.created_series.pk],
        )
        row = historical.context["registry_rows"][0]
        self.assertEqual(
            {item["child"].pk for item in row["participants"]},
            {self.children[0].pk, self.children[2].pk},
        )
        self.assertEqual(row["composition_revision"].pk, self.current_revision.pk)

        current = self.get_registry(recipient=self.children[2].pk)
        ids = [series.pk for series in self.row_series(current)]
        self.assertEqual(ids.count(self.created_series.pk), 1)
        self.assertIn(self.join_series.pk, ids)

        legacy = self.get_registry(
            recipient=self.children[0].pk,
            program=self.blocks[0].program_id,
            date_from=self.legacy_series.start_date.isoformat(),
            date_to=self.legacy_series.end_date.isoformat(),
        )
        legacy_ids = {series.pk for series in self.row_series(legacy)}
        self.assertIn(self.legacy_series.pk, legacy_ids)
        current_program = self.get_registry(
            recipient=self.children[0].pk,
            program=self.blocks[0].program_id,
            date_from=self.created_series.start_date.isoformat(),
            date_to=self.created_series.end_date.isoformat(),
        )
        self.assertIn(self.created_series.pk, {series.pk for series in self.row_series(current_program)})

    def test_status_and_plan_period_filters_are_applied(self):
        self.legacy_series.status = AppointmentSeries.Status.CANCELLED
        self.legacy_series.save(update_fields=["status"])
        cancelled = self.get_registry(status=AppointmentSeries.Status.CANCELLED)
        self.assertEqual(
            [series.pk for series in self.row_series(cancelled)], [self.legacy_series.pk]
        )
        response = self.get_registry(
            status=AppointmentSeries.Status.ACTIVE,
            date_from=self.period_start.isoformat(),
            date_to=self.period_end.isoformat(),
        )
        self.assertTrue(response.context["filters_valid"])
        self.assertIn(self.created_series.pk, {series.pk for series in self.row_series(response)})

    def test_default_group_composition_is_searchable_without_revisions(self):
        group = self.legacy_series
        group.session_type = Appointment.SessionType.GROUP
        group.save(update_fields=["session_type"])
        for position, (child, block) in enumerate(
            zip(self.children[:2], self.blocks[:2], strict=True), 1
        ):
            AppointmentSeriesParticipant.objects.create(
                series=group, child=child, program_block=block,
                billing_account=block.balance_account, position=position,
            )
        response = self.get_registry(
            recipient=self.children[1].pk, program=self.blocks[1].program_id,
            date_from=group.start_date.isoformat(), date_to=group.end_date.isoformat(),
        )
        self.assertEqual([series.pk for series in self.row_series(response)], [group.pk])
        row = response.context["registry_rows"][0]
        self.assertIsNone(row["composition_revision"])
        self.assertEqual(
            [item["child"].pk for item in row["participants"]],
            [child.pk for child in self.children[:2]],
        )

    def test_pagination_preserves_filters_and_order(self):
        query = {
            "recipient": self.children[0].pk,
            "status": AppointmentSeries.Status.ACTIVE,
            "page": 2,
        }
        response = self.get_registry(**query)
        page = response.context["page_obj"]
        self.assertEqual(page.paginator.per_page, 25)
        self.assertEqual(page.number, 2)
        self.assertEqual(page.paginator.count, 28)

        expected = list(
            AppointmentSeries.objects.filter(child=self.children[0])
            .order_by("-start_date", "-pk")
            .values_list("pk", flat=True)
        )
        actual = []
        for page_number in (1, 2):
            page_response = self.get_registry(
                recipient=self.children[0].pk,
                status=AppointmentSeries.Status.ACTIVE,
                page=page_number,
            )
            actual.extend(series.pk for series in self.row_series(page_response))
        self.assertEqual(actual, expected)
        self.assertIn(f"recipient={self.children[0].pk}", response.context["pagination_query"])
        self.assertIn("status=active", response.context["pagination_query"])
        self.assertNotIn("page=", response.context["pagination_query"])

    def test_list_and_related_pages_expose_navigation_without_lifecycle_commands(self):
        response = self.get_registry(
            recipient=self.children[0].pk,
            program=self.blocks[0].program_id,
            date_from=self.created_series.start_date.isoformat(),
            date_to=self.created_series.end_date.isoformat(),
        )
        self.assertContains(response, reverse("appointment_series_detail", args=[self.created_series.pk]))
        self.assertContains(response, reverse("recipient_detail", args=[self.children[0].pk]))
        self.assertContains(response, reverse("schedule"))
        self.assertNotContains(
            response,
            reverse("appointment_series_action", args=[self.created_series.pk, "retry_skipped"]),
        )

        recipient = self.client.get(reverse("recipient_detail", args=[self.children[0].pk]))
        self.assertEqual(recipient.status_code, 200)
        self.assertContains(
            recipient,
            f"{reverse('appointment_series_list')}?recipient={self.children[0].pk}",
        )
        self.assertContains(
            recipient,
            f"{reverse('appointment_series_list')}?recipient={self.children[0].pk}&amp;program={self.blocks[0].program_id}",
        )

        detail = self.client.get(
            reverse("appointment_series_detail", args=[self.created_series.pk])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, reverse("appointment_series_list"))
        schedule = self.client.get(reverse("schedule"))
        self.assertContains(schedule, reverse("appointment_series_list"))

    def test_registry_query_count_stays_bounded_as_rows_grow(self):
        one = self.get_registry(
            recipient=self.children[1].pk,
            program=self.blocks[1].program_id,
        )
        self.assertEqual(one.context["page_obj"].paginator.count, 1)
        with CaptureQueriesContext(connection) as one_queries:
            self.get_registry(
                recipient=self.children[1].pk,
                program=self.blocks[1].program_id,
            )

        for _ in range(4):
            self._make_created_series()
        with CaptureQueriesContext(connection) as versioned_queries:
            versioned = self.get_registry(
                recipient=self.children[1].pk, program=self.blocks[1].program_id,
            )
        self.assertEqual(len(versioned.context["registry_rows"]), 5)
        self.assertEqual(len(versioned_queries), len(one_queries))

        with CaptureQueriesContext(connection) as many_queries:
            self.get_registry(recipient=self.children[0].pk, status=AppointmentSeries.Status.ACTIVE)

        self.assertLessEqual(len(one_queries), 20)
        self.assertLessEqual(len(many_queries), 20)
        self.assertLessEqual(len(many_queries) - len(one_queries), 4)
