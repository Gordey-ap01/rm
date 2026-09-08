from __future__ import annotations

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentSeries,
    AppointmentSeriesOccurrence,
    AppointmentSeriesParticipant,
    AppointmentSeriesRevision,
    AppointmentSeriesRevisionParticipant,
    AppointmentSeriesRevisionStaffAssignment,
    AppointmentSeriesStaffAssignment,
    AppointmentStaffAssignment,
    BalanceAccount,
    Child,
    FundingSource,
    LedgerEntry,
    ProgramBlock,
    Room,
    Service,
    StaffMember,
    TreatmentProgram,
)
from operations.services import program_series, series_revisions

User = get_user_model()


class SeriesCompositionViewTests(TestCase):
    """Acceptance coverage for the read-only-preview future composition editor."""

    @classmethod
    def setUpTestData(cls):
        cls.administrator = User.objects.create_user(
            "composition-administrator", password="x", is_staff=True
        )
        cls.director = User.objects.create_superuser(
            "composition-director", password="x"
        )
        cls.specialist_user = User.objects.create_user(
            "composition-specialist", password="x"
        )
        cls.staff = [
            StaffMember.objects.create(
                user=cls.specialist_user if index == 0 else None,
                full_name=f"Специалист состава {index + 1}",
            )
            for index in range(3)
        ]
        cls.service = Service.objects.create(
            name="Групповая услуга редактора",
            code="COMPOSITION-GROUP",
            category=Service.Category.GROUP,
            default_duration_minutes=45,
            default_price=Decimal("1200"),
        )
        cls.other_service = Service.objects.create(
            name="Другая услуга редактора",
            code="COMPOSITION-OTHER",
            category=Service.Category.OTHER,
            default_duration_minutes=45,
            default_price=Decimal("900"),
        )
        cls.room = Room.objects.create(
            name="Кабинет редактора состава",
            room_type=Room.RoomType.GROUP,
            allow_group_sessions=True,
            limit_staff_count=True,
            max_staff_count=3,
            limit_recipient_count=True,
            max_recipient_count=4,
        )
        cls.funding = FundingSource.objects.create(
            name="Оплата редактора состава",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        cls.children = [
            Child.objects.create(last_name="Состав", first_name=f"Ребенок {index + 1}")
            for index in range(4)
        ]
        cls.blocks = [
            cls._make_block(child=child, service=cls.service, number=1)
            for child in cls.children
        ]
        cls.alternate_first_block = cls._make_block(
            child=cls.children[0], service=cls.service, number=2
        )
        cls.other_service_block = cls._make_block(
            child=cls.children[3], service=cls.other_service, number=2
        )

        cls.period_start = timezone.localdate() + timedelta(days=10)
        cls.period_end = cls.period_start + timedelta(days=20)
        materialized = program_series.create_group_series(
            cls._group_preview(), operation_key=uuid4(), actor=cls.administrator
        )
        cls.series = materialized.series
        cls.series.refresh_from_db()
        cls._flush_pending_revision_constraints()

        first_appointment = Appointment.objects.filter(series=cls.series).order_by("pk").first()
        first_participant = first_appointment.participants.order_by("pk").first()
        LedgerEntry.objects.create(
            account=first_participant.billing_account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
            appointment=first_appointment,
            appointment_participant=first_participant,
            price_snapshot=Decimal("1200"),
            created_by=cls.administrator,
            reason="Финансовый снимок до изменения будущего состава.",
        )

    @classmethod
    def _make_block(cls, *, child, service, number):
        account = BalanceAccount.objects.create(
            child=child,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=service,
            initial_amount=Decimal("30"),
        )
        program = TreatmentProgram.objects.create(
            child=child,
            title=f"Программа {child.first_name} / {service.code}",
            status=TreatmentProgram.Status.ACTIVE,
        )
        return ProgramBlock.objects.create(
            program=program,
            number=number,
            title=f"Каскад {number}",
            service=service,
            staff_member=cls.staff[0],
            planned_sessions=30,
            balance_account=account,
        )

    @classmethod
    def _group_preview(cls, *, start_date=None, end_date=None):
        return program_series.preview_group_series(
            blocks=cls.blocks[:2],
            staff_members=cls.staff[:2],
            room=cls.room,
            title="Редактируемая постоянная группа",
            start_date=start_date or cls.period_start,
            end_date=end_date or cls.period_end,
            weekdays=set(range(7)),
            start_time=time(10, 0),
            duration_minutes=45,
            default_appointment_status=Appointment.Status.PROPOSED,
        )

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
        self.url = reverse("appointment_series_composition", args=[self.series.pk])

    def _payload(
        self,
        *,
        series=None,
        blocks=None,
        staff_rows=None,
        effective_from=None,
        reason="Меняем будущий состав группы.",
        action="preview",
        preview_token="",
        expected_revision_id=None,
    ):
        series = series or self.series
        if blocks is None:
            blocks = [self.blocks[1], self.blocks[2]]
        if staff_rows is None:
            staff_rows = [
                (self.staff[0], AppointmentSeriesStaffAssignment.Role.PRIMARY, False, "", False),
                (self.staff[1], AppointmentSeriesStaffAssignment.Role.ASSISTANT, False, "", True),
                (self.staff[2], AppointmentSeriesStaffAssignment.Role.ASSISTANT, False, "", False),
            ]
        current_revision = series.current_revision
        payload = {
            "action": action,
            "expected_revision_id": str(
                expected_revision_id
                if expected_revision_id is not None
                else current_revision.pk if current_revision else 999999
            ),
            "preview_token": preview_token,
            "effective_from": (
                effective_from
                or (
                    current_revision.effective_from + timedelta(days=1)
                    if current_revision
                    else series.start_date
                )
            ).isoformat(),
            "reason": reason,
            "program_blocks": [str(block.pk) for block in blocks],
            "staff-TOTAL_FORMS": str(len(staff_rows)),
            "staff-INITIAL_FORMS": "2",
            "staff-MIN_NUM_FORMS": "0",
            "staff-MAX_NUM_FORMS": "100",
        }
        for index, (member, role, override, override_reason, delete) in enumerate(staff_rows):
            payload[f"staff-{index}-staff_member"] = "" if member is None else str(member.pk)
            payload[f"staff-{index}-role"] = role
            payload[f"staff-{index}-override_reason"] = override_reason
            if override:
                payload[f"staff-{index}-override_availability"] = "on"
            if delete:
                payload[f"staff-{index}-DELETE"] = "on"
        return payload

    def _preview(self, **overrides):
        payload = self._payload(**overrides)
        response = self.client.post(
            reverse(
                "appointment_series_composition",
                args=[(overrides.get("series") or self.series).pk],
            ),
            payload,
        )
        return response, payload

    def _preview_token(self, response):
        return response.context["form"]["preview_token"].value()

    def _apply_preview(self, response, payload):
        applied = payload.copy()
        applied["action"] = "apply"
        applied["preview_token"] = self._preview_token(response)
        return self.client.post(response.request["PATH_INFO"], applied), applied

    def _snapshot_materialized_history(self):
        appointment_ids = Appointment.objects.filter(series=self.series).values("pk")
        return {
            "appointments": list(
                Appointment.objects.filter(series=self.series).order_by("pk").values()
            ),
            "participants": list(
                AppointmentParticipant.objects.filter(appointment_id__in=appointment_ids)
                .order_by("pk")
                .values()
            ),
            "staff": list(
                AppointmentStaffAssignment.objects.filter(appointment_id__in=appointment_ids)
                .order_by("pk")
                .values()
            ),
            "occurrences": list(
                AppointmentSeriesOccurrence.objects.filter(series=self.series)
                .order_by("pk")
                .values()
            ),
            "ledger": list(
                LedgerEntry.objects.filter(appointment_id__in=appointment_ids)
                .order_by("pk")
                .values()
            ),
        }

    def _new_revisioned_group(self, *, actor=None, start_date=None, end_date=None):
        preview = self._group_preview(start_date=start_date, end_date=end_date)
        series, reused = program_series._create_series_definition(
            preview, operation_key=uuid4()
        )
        self.assertFalse(reused)
        series_revisions.ensure_initial_revision(
            series, actor=actor or self.administrator
        )
        series.refresh_from_db()
        self._flush_pending_revision_constraints()
        return series

    def _revise_direct(self, series, *, actor, effective_from, reason):
        series.refresh_from_db()
        self._flush_pending_revision_constraints()
        revision = series_revisions.revise_future_composition(
            series,
            expected_revision_id=series.current_revision_id,
            effective_from=effective_from,
            participants=[
                series_revisions.SeriesParticipantInput(
                    child_id=self.children[1].pk,
                    program_block_id=self.blocks[1].pk,
                    billing_account_id=self.blocks[1].balance_account_id,
                    position=1,
                ),
                series_revisions.SeriesParticipantInput(
                    child_id=self.children[2].pk,
                    program_block_id=self.blocks[2].pk,
                    billing_account_id=self.blocks[2].balance_account_id,
                    position=2,
                ),
            ],
            staff_assignments=[
                series_revisions.SeriesStaffInput(
                    staff_member_id=self.staff[0].pk,
                    role=AppointmentSeriesStaffAssignment.Role.PRIMARY,
                ),
                series_revisions.SeriesStaffInput(
                    staff_member_id=self.staff[2].pk,
                    role=AppointmentSeriesStaffAssignment.Role.ASSISTANT,
                ),
            ],
            actor=actor,
            reason=reason,
        )
        self._flush_pending_revision_constraints()
        series.refresh_from_db()
        return revision

    def _legacy_revisioned_group(self):
        series, reused = program_series._create_series_definition(
            self._group_preview(), operation_key=uuid4()
        )
        self.assertFalse(reused)
        participants = list(series.default_participants.order_by("position", "pk"))
        assignments = list(series.default_staff_assignments.order_by("pk"))
        fingerprint = series_revisions.canonical_fingerprint(
            series_revisions._revision_payload(
                series, participants, assignments, revision_number=1
            )
        )
        revision = AppointmentSeriesRevision.objects.create(
            series=series,
            revision_number=1,
            event_type=AppointmentSeriesRevision.EventType.LEGACY_IMPORT,
            provenance_kind=AppointmentSeriesRevision.ProvenanceKind.LEGACY_RECONSTRUCTED,
            effective_from=series.start_date,
            title=series.title,
            service=series.service,
            room=series.room,
            start_date=series.start_date,
            end_date=series.end_date,
            days_of_week=series.days_of_week,
            time=series.time,
            duration_minutes=series.duration_minutes,
            session_type=series.session_type,
            materialization_mode=series.materialization_mode,
            default_appointment_status=series.default_appointment_status,
            allow_unpaid_reserve=series.allow_unpaid_reserve,
            allow_outside_availability=series.allow_outside_availability,
            override_reason=series.override_reason,
            fingerprint=fingerprint,
            actor=None,
            actor_role_snapshot=AppointmentSeriesRevision.ActorRole.LEGACY,
            reason="Восстановленная редакция прежней серии.",
            decided_at=None,
        )
        for item in participants:
            AppointmentSeriesRevisionParticipant.objects.create(
                revision=revision,
                child=item.child,
                program_block=item.program_block,
                billing_account=item.billing_account,
                position=item.position,
            )
        for item in assignments:
            AppointmentSeriesRevisionStaffAssignment.objects.create(
                revision=revision,
                staff_member=item.staff_member,
                role=item.role,
                override_availability=item.override_availability,
                override_reason=item.override_reason,
            )
        AppointmentSeries.objects.filter(pk=series.pk).update(current_revision=revision)
        series.refresh_from_db()
        self._flush_pending_revision_constraints()
        return series

    def test_editor_requires_management_role_and_exposes_initial_forms(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

        self.client.force_login(self.specialist_user)
        self.assertEqual(self.client.get(self.url).status_code, 403)

        self.client.force_login(self.administrator)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["series"].pk, self.series.pk)
        self.assertEqual(
            response.context["composition_access"],
            {"allowed": True, "reason": "", "url": self.url},
        )
        self.assertEqual(
            response.context["form"]["expected_revision_id"].value(),
            self.series.current_revision_id,
        )
        formset = response.context["staff_formset"]
        self.assertEqual(formset.prefix, "staff")
        self.assertEqual(formset.initial_form_count(), 2)
        self.assertEqual(formset.total_form_count(), 3)
        self.assertEqual(formset.max_num, 100)

    def test_noneditable_series_get_explains_reason_and_post_is_forbidden(self):
        AppointmentSeries.objects.filter(pk=self.series.pk).update(
            status=AppointmentSeries.Status.CANCELLED
        )
        cancelled = AppointmentSeries.objects.get(pk=self.series.pk)

        no_revision, _ = program_series._create_series_definition(
            self._group_preview(), operation_key=uuid4()
        )
        no_future = self._new_revisioned_group(
            start_date=timezone.localdate() + timedelta(days=1),
            end_date=timezone.localdate() + timedelta(days=1),
        )
        join_series = AppointmentSeries.objects.create(
            child=self.children[0],
            service=self.service,
            staff_member=self.staff[0],
            room=self.room,
            program_block=self.blocks[0],
            title="Join-серия",
            start_date=self.period_start,
            end_date=self.period_end,
            days_of_week="ПН",
            time=time(12, 0),
            duration_minutes=45,
            session_type=Appointment.SessionType.GROUP,
            materialization_mode=AppointmentSeries.MaterializationMode.JOIN_EXISTING,
            default_appointment_status=Appointment.Status.PROPOSED,
            status=AppointmentSeries.Status.ACTIVE,
        )
        AppointmentSeriesParticipant.objects.create(
            series=join_series,
            child=self.children[0],
            program_block=self.blocks[0],
            billing_account=self.blocks[0].balance_account,
            position=1,
        )

        cases = (
            (cancelled, "активной"),
            (join_series, "присоединения"),
            (no_revision, "сохраненная редакция"),
            (no_future, "не осталось даты"),
        )
        for series, reason_fragment in cases:
            with self.subTest(series=series.pk):
                url = reverse("appointment_series_composition", args=[series.pk])
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context["composition_access"]["allowed"])
                self.assertIn(reason_fragment, response.context["composition_access"]["reason"])
                self.assertNotIn("form", response.context)
                denied = self.client.post(url, self._payload(series=series))
                self.assertEqual(denied.status_code, 403)

    def test_preview_is_read_only_and_issues_exact_confirmation_token(self):
        before = self._snapshot_materialized_history()
        revision_count = AppointmentSeriesRevision.objects.count()

        response, payload = self._preview()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].is_valid())
        self.assertTrue(response.context["staff_formset"].is_valid())
        self.assertIsNotNone(response.context["preview"])
        self.assertTrue(self._preview_token(response))
        self.assertEqual(
            response.context["preview"]["existing_appointments_count"],
            Appointment.objects.filter(
                series=self.series,
                starts_at__date__gte=payload["effective_from"],
            ).count(),
        )
        self.assertEqual(AppointmentSeriesRevision.objects.count(), revision_count)
        self.assertEqual(self._snapshot_materialized_history(), before)

    def test_apply_uses_block_accounts_and_preserves_materialized_history(self):
        before = self._snapshot_materialized_history()
        previous = self.series.current_revision
        response, payload = self._preview(
            blocks=[self.blocks[2], self.blocks[1]],
            reason="Сохраняем новый проверенный состав.",
        )

        applied, apply_payload = self._apply_preview(response, payload)

        self.assertRedirects(
            applied,
            reverse("appointment_series_detail", args=[self.series.pk]),
            fetch_redirect_response=False,
        )
        self.series.refresh_from_db()
        revision = self.series.current_revision
        self.assertEqual(revision.revision_number, previous.revision_number + 1)
        self.assertEqual(revision.supersedes_id, previous.pk)
        self.assertEqual(revision.actor_id, self.administrator.pk)
        self.assertEqual(
            revision.actor_role_snapshot,
            AppointmentSeriesRevision.ActorRole.ADMINISTRATOR,
        )
        self.assertEqual(revision.reason, apply_payload["reason"])
        self.assertEqual(
            list(
                revision.participants.order_by("position").values_list(
                    "program_block_id", "billing_account_id", "position"
                )
            ),
            [
                (self.blocks[1].pk, self.blocks[1].balance_account_id, 1),
                (self.blocks[2].pk, self.blocks[2].balance_account_id, 2),
            ],
        )
        self.assertEqual(
            list(
                self.series.default_participants.order_by("position").values_list(
                    "program_block_id", flat=True
                )
            ),
            [self.blocks[1].pk, self.blocks[2].pk],
        )
        self.assertEqual(self._snapshot_materialized_history(), before)

    def test_replayed_apply_is_stale_and_never_duplicates_revision(self):
        preview, payload = self._preview(effective_from=self.series.end_date)
        first, apply_payload = self._apply_preview(preview, payload)
        self.assertEqual(first.status_code, 302)
        self.series.refresh_from_db()
        self.assertEqual(self.series.current_revision.effective_from, self.series.end_date)
        revision_count = AppointmentSeriesRevision.objects.filter(series=self.series).count()

        repeated = self.client.post(self.url, apply_payload)

        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(
            AppointmentSeriesRevision.objects.filter(series=self.series).count(),
            revision_count,
        )
        self.assertFalse(repeated.context["composition_access"]["allowed"])
        self.assertIn(
            "уже изменился", repeated.context["composition_access"]["reason"]
        )
        refreshed = self.client.get(self.url)
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn(
            "не осталось даты", refreshed.context["composition_access"]["reason"]
        )

    def test_stale_valid_preview_returns_conflict_without_writes(self):
        preview, payload = self._preview()
        token = self._preview_token(preview)
        old_revision_id = self.series.current_revision_id
        self._revise_direct(
            self.series,
            actor=self.administrator,
            effective_from=self.period_start + timedelta(days=2),
            reason="Конкурирующее изменение будущего состава.",
        )
        revision_count = AppointmentSeriesRevision.objects.filter(series=self.series).count()
        payload.update(action="apply", preview_token=token)
        payload["expected_revision_id"] = str(old_revision_id)

        stale = self.client.post(self.url, payload)

        self.assertEqual(stale.status_code, 409)
        self.assertEqual(
            AppointmentSeriesRevision.objects.filter(series=self.series).count(),
            revision_count,
        )

    def test_confirmation_rejects_missing_expired_tampered_and_cross_actor_tokens(self):
        preview, payload = self._preview()
        token = self._preview_token(preview)
        revision_count = AppointmentSeriesRevision.objects.filter(series=self.series).count()
        cases = []
        missing = payload.copy()
        missing.update(action="apply", preview_token="")
        cases.append(("missing", missing, self.administrator))
        malformed = payload.copy()
        malformed.update(action="apply", preview_token="not-a-signed-value")
        cases.append(("malformed", malformed, self.administrator))
        changed_reason = payload.copy()
        changed_reason.update(action="apply", preview_token=token, reason="Измененное основание состава.")
        cases.append(("reason", changed_reason, self.administrator))
        changed_blocks = payload.copy()
        changed_blocks.update(action="apply", preview_token=token)
        changed_blocks["program_blocks"] = [str(self.blocks[0].pk), str(self.blocks[2].pk)]
        cases.append(("blocks", changed_blocks, self.administrator))
        changed_date = payload.copy()
        changed_date.update(
            action="apply",
            preview_token=token,
            effective_from=(self.period_start + timedelta(days=2)).isoformat(),
        )
        cases.append(("date", changed_date, self.administrator))
        changed_staff = payload.copy()
        changed_staff.update(action="apply", preview_token=token)
        changed_staff["staff-0-staff_member"] = str(self.staff[1].pk)
        cases.append(("staff", changed_staff, self.administrator))
        cross_actor = payload.copy()
        cross_actor.update(action="apply", preview_token=token)
        cases.append(("actor", cross_actor, self.director))

        for label, submitted, actor in cases:
            with self.subTest(label=label):
                self.client.force_login(actor)
                response = self.client.post(self.url, submitted)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.context["form"].non_field_errors())
                self.assertEqual(
                    AppointmentSeriesRevision.objects.filter(series=self.series).count(),
                    revision_count,
                )

        self.client.force_login(self.administrator)
        with patch("django.core.signing.time.time", return_value=1):
            expired_preview, expired_payload = self._preview()
        expired_payload.update(
            action="apply", preview_token=self._preview_token(expired_preview)
        )
        expired = self.client.post(self.url, expired_payload)
        self.assertEqual(expired.status_code, 400)
        self.assertEqual(
            AppointmentSeriesRevision.objects.filter(series=self.series).count(),
            revision_count,
        )

    def test_required_fields_unknown_action_and_formset_limit_are_controlled(self):
        short_reason = self._payload(reason="мало")
        short_reason_response = self.client.post(self.url, short_reason)
        self.assertEqual(short_reason_response.status_code, 200)
        self.assertIn("reason", short_reason_response.context["form"].errors)

        missing_revision = self._payload()
        missing_revision.pop("expected_revision_id")
        missing_revision_response = self.client.post(self.url, missing_revision)
        self.assertEqual(missing_revision_response.status_code, 200)
        self.assertIn(
            "expected_revision_id", missing_revision_response.context["form"].errors
        )

        missing_blocks = self._payload(blocks=[])
        missing_blocks_response = self.client.post(self.url, missing_blocks)
        self.assertEqual(missing_blocks_response.status_code, 200)
        self.assertIn("program_blocks", missing_blocks_response.context["form"].errors)

        unknown_action = self._payload(action="unexpected")
        unknown_action_response = self.client.post(self.url, unknown_action)
        self.assertEqual(unknown_action_response.status_code, 400)
        self.assertTrue(unknown_action_response.context["form"].non_field_errors())

        too_many_rows = self._payload(
            staff_rows=[(None, "", False, "", False)] * 101
        )
        too_many_rows_response = self.client.post(self.url, too_many_rows)
        self.assertEqual(too_many_rows_response.status_code, 200)
        self.assertTrue(
            too_many_rows_response.context["staff_formset"].non_form_errors()
        )

    def test_form_rejects_date_boundaries_duplicate_children_and_staff_rules(self):
        bad_dates = (
            timezone.localdate(),
            self.series.current_revision.effective_from,
            self.series.end_date + timedelta(days=1),
        )
        for submitted_date in bad_dates:
            with self.subTest(date=submitted_date):
                response, _ = self._preview(effective_from=submitted_date)
                self.assertEqual(response.status_code, 200)
                self.assertIn("effective_from", response.context["form"].errors)

        duplicate_child, _ = self._preview(
            blocks=[self.blocks[0], self.alternate_first_block]
        )
        self.assertIn("program_blocks", duplicate_child.context["form"].errors)

        one_child, _ = self._preview(blocks=[self.blocks[0]])
        self.assertIn("program_blocks", one_child.context["form"].errors)

        duplicate_staff, _ = self._preview(
            staff_rows=[
                (self.staff[0], "primary", False, "", False),
                (self.staff[0], "assistant", False, "", False),
            ]
        )
        self.assertTrue(duplicate_staff.context["staff_formset"].non_form_errors())

        no_primary, _ = self._preview(
            staff_rows=[
                (self.staff[0], "assistant", False, "", False),
                (self.staff[1], "assistant", False, "", False),
            ]
        )
        self.assertTrue(no_primary.context["staff_formset"].non_form_errors())

        two_primary, _ = self._preview(
            staff_rows=[
                (self.staff[0], "primary", False, "", False),
                (self.staff[1], "primary", False, "", False),
            ]
        )
        self.assertTrue(two_primary.context["staff_formset"].non_form_errors())

    def test_form_rejects_wrong_service_inactive_program_and_inactive_staff(self):
        wrong_service, _ = self._preview(
            blocks=[self.blocks[0], self.other_service_block]
        )
        self.assertIn("program_blocks", wrong_service.context["form"].errors)

        self.blocks[2].program.status = TreatmentProgram.Status.COMPLETED
        self.blocks[2].program.save(update_fields=["status", "updated_at"])
        inactive_program, _ = self._preview(blocks=[self.blocks[0], self.blocks[2]])
        self.assertIn("program_blocks", inactive_program.context["form"].errors)

        self.staff[0].status = StaffMember.Status.INACTIVE
        self.staff[0].save(update_fields=["status", "updated_at"])
        inactive_staff, _ = self._preview(
            blocks=[self.blocks[0], self.blocks[1]],
            staff_rows=[
                (self.staff[0], "primary", False, "", False),
                (self.staff[1], "assistant", False, "", False),
            ],
        )
        self.assertIn("staff_member", inactive_staff.context["staff_formset"].forms[0].errors)

    def test_individual_series_keeps_single_recipient_and_staff_cardinality(self):
        series = AppointmentSeries.objects.create(
            child=self.children[0],
            service=self.service,
            staff_member=self.staff[0],
            room=self.room,
            program_block=self.blocks[0],
            title="Индивидуальная серия",
            start_date=self.period_start,
            end_date=self.period_end,
            days_of_week="ПН",
            time=time(14, 0),
            duration_minutes=45,
            session_type=Appointment.SessionType.INDIVIDUAL,
            materialization_mode=AppointmentSeries.MaterializationMode.CREATE_APPOINTMENTS,
            default_appointment_status=Appointment.Status.PROPOSED,
            status=AppointmentSeries.Status.ACTIVE,
        )
        AppointmentSeriesParticipant.objects.create(
            series=series,
            child=self.children[0],
            program_block=self.blocks[0],
            billing_account=self.blocks[0].balance_account,
            position=1,
        )
        AppointmentSeriesStaffAssignment.objects.create(
            series=series,
            staff_member=self.staff[0],
            role=AppointmentSeriesStaffAssignment.Role.PRIMARY,
        )
        series_revisions.ensure_initial_revision(series, actor=self.administrator)
        series.refresh_from_db()
        self._flush_pending_revision_constraints()

        response, _ = self._preview(
            series=series,
            blocks=[self.blocks[0], self.blocks[1]],
            staff_rows=[
                (self.staff[0], "primary", False, "", False),
                (self.staff[1], "assistant", False, "", False),
            ],
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("program_blocks", response.context["form"].errors)
        self.assertTrue(response.context["staff_formset"].non_form_errors())

    def test_domain_validation_is_repeated_on_apply_without_partial_writes(self):
        preview, payload = self._preview()
        self.blocks[2].program.ends_on = self.period_start + timedelta(days=2)
        self.blocks[2].program.save(update_fields=["ends_on", "updated_at"])
        before = self._snapshot_materialized_history()
        revision_count = AppointmentSeriesRevision.objects.filter(series=self.series).count()

        response, _ = self._apply_preview(preview, payload)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        self.assertEqual(
            AppointmentSeriesRevision.objects.filter(series=self.series).count(),
            revision_count,
        )
        self.assertEqual(self._snapshot_materialized_history(), before)

    def test_director_precedence_is_enforced_by_service_and_editor(self):
        series = self._new_revisioned_group(actor=self.director)
        first = self._revise_direct(
            series,
            actor=self.director,
            effective_from=self.period_start + timedelta(days=1),
            reason="Руководитель утвердил будущий состав.",
        )
        self.client.force_login(self.administrator)
        url = reverse("appointment_series_composition", args=[series.pk])
        denied = self.client.get(url)
        self.assertEqual(denied.status_code, 200)
        self.assertFalse(denied.context["composition_access"]["allowed"])
        self.assertIn("руководител", denied.context["composition_access"]["reason"].lower())
        self.assertEqual(self.client.post(url, self._payload(series=series)).status_code, 403)

        with self.assertRaises(PermissionDenied):
            self._revise_direct(
                series,
                actor=self.administrator,
                effective_from=self.period_start + timedelta(days=2),
                reason="Администратор не может заменить решение руководителя.",
            )

        second = self._revise_direct(
            series,
            actor=self.director,
            effective_from=self.period_start + timedelta(days=2),
            reason="Руководитель обновил собственное решение.",
        )
        self.assertEqual(second.supersedes_id, first.pk)

    def test_administrator_can_supersede_director_created_and_legacy_initial_revision(self):
        director_created = self._new_revisioned_group(actor=self.director)
        legacy = self._legacy_revisioned_group()

        for series in (director_created, legacy):
            with self.subTest(event=series.current_revision.event_type):
                self.client.force_login(self.administrator)
                preview, payload = self._preview(series=series)
                self.assertEqual(preview.status_code, 200)
                applied, _ = self._apply_preview(preview, payload)
                self.assertEqual(applied.status_code, 302)
                series.refresh_from_db()
                self.assertEqual(
                    series.current_revision.actor_role_snapshot,
                    AppointmentSeriesRevision.ActorRole.ADMINISTRATOR,
                )

    def test_detail_paginates_revision_history_and_shows_author_reason_and_access(self):
        series = self._new_revisioned_group()
        reasons = []
        for offset in range(1, 12):
            reason = f"Редакция истории состава номер {offset}."
            reasons.append(reason)
            self._revise_direct(
                series,
                actor=self.director,
                effective_from=self.period_start + timedelta(days=offset),
                reason=reason,
            )

        self.client.force_login(self.director)
        detail_url = reverse("appointment_series_detail", args=[series.pk])
        first_page = self.client.get(detail_url)
        second_page = self.client.get(detail_url, {"revision_page": 2})

        self.assertEqual(first_page.status_code, 200)
        self.assertTrue(first_page.context["composition_access"]["allowed"])
        self.assertEqual(
            first_page.context["composition_access"]["url"],
            reverse("appointment_series_composition", args=[series.pk]),
        )
        page = second_page.context["revision_page"]
        self.assertEqual(page.paginator.per_page, 10)
        self.assertEqual(page.paginator.count, 12)
        self.assertEqual(page.number, 2)
        self.assertEqual(len(page.object_list), 2)
        newest = first_page.context["revision_page"].object_list[0]
        self.assertEqual(newest.actor_id, self.director.pk)
        self.assertEqual(newest.reason, reasons[-1])
        self.assertContains(first_page, reasons[-1])
        self.assertContains(first_page, self.director.username)
