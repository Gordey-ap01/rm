from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import skipUnless
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentSeries,
    AppointmentSeriesLifecycleEvent,
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
from operations.services import series_lifecycle

User = get_user_model()


class SeriesStopResumeTests(TestCase):
    """Acceptance coverage for explicit materialization stop/resume decisions."""

    @classmethod
    def setUpTestData(cls):
        cls.administrator = User.objects.create_user(
            "stop-resume-administrator", password="x", is_staff=True
        )
        cls.director = User.objects.create_superuser(
            "stop-resume-director", password="x"
        )
        cls.specialist_user = User.objects.create_user(
            "stop-resume-specialist", password="x"
        )
        cls.child = Child.objects.create(
            last_name="Остановка", first_name="Получатель"
        )
        cls.staff = StaffMember.objects.create(
            user=cls.specialist_user, full_name="Специалист остановки"
        )
        cls.service = Service.objects.create(
            name="Услуга остановки серии",
            code="STOP-RESUME",
            default_duration_minutes=45,
            default_price=Decimal("1200"),
        )
        cls.room = Room.objects.create(name="Кабинет остановки", capacity=2)
        cls.funding = FundingSource.objects.create(
            name="Оплата остановки",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        cls.account = BalanceAccount.objects.create(
            child=cls.child,
            funding_source=cls.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=cls.service,
            initial_amount=Decimal("20"),
        )
        cls.program = TreatmentProgram.objects.create(
            child=cls.child,
            title="Программа остановки",
            status=TreatmentProgram.Status.ACTIVE,
        )
        cls.block = ProgramBlock.objects.create(
            program=cls.program,
            number=1,
            title="Каскад остановки",
            service=cls.service,
            staff_member=cls.staff,
            planned_sessions=20,
            balance_account=cls.account,
        )
        cls.series = cls._make_series("Серия с сохраненными фактами")
        start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=7), time(10, 0)),
            timezone.get_current_timezone(),
        )
        cls.appointment = Appointment.objects.create(
            child=cls.child,
            staff_member=cls.staff,
            service=cls.service,
            room=cls.room,
            starts_at=start,
            ends_at=start + timedelta(minutes=45),
            status=Appointment.Status.PROPOSED,
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=cls.account,
            series=cls.series,
            program_block=cls.block,
        )
        participant = cls.appointment.participants.get(child=cls.child)
        participant.price_snapshot = Decimal("1200")
        participant.billing_decision = Appointment.BillingDecision.CHARGE
        participant.save(update_fields=["price_snapshot", "billing_decision", "updated_at"])
        LedgerEntry.objects.create(
            account=cls.account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
            appointment=cls.appointment,
            appointment_participant=participant,
            price_snapshot=Decimal("1200"),
            created_by=cls.director,
            reason="Финансовый факт до остановки серии.",
        )

    @classmethod
    def _make_series(cls, title, *, join=False):
        start = timezone.localdate() + timedelta(days=7)
        return AppointmentSeries.objects.create(
            child=cls.child,
            service=cls.service,
            staff_member=cls.staff,
            room=cls.room,
            program_block=cls.block,
            title=title,
            start_date=start,
            end_date=start + timedelta(days=14),
            days_of_week="ПН",
            time=time(10, 0),
            duration_minutes=45,
            session_type=(
                Appointment.SessionType.GROUP
                if join
                else Appointment.SessionType.INDIVIDUAL
            ),
            materialization_mode=(
                AppointmentSeries.MaterializationMode.JOIN_EXISTING
                if join
                else AppointmentSeries.MaterializationMode.CREATE_APPOINTMENTS
            ),
            default_appointment_status=Appointment.Status.PROPOSED,
            status=AppointmentSeries.Status.ACTIVE,
        )

    def setUp(self):
        self.client.force_login(self.administrator)

    def _snapshot_facts(self):
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
                AppointmentStaffAssignment.objects.filter(
                    appointment_id__in=appointment_ids
                )
                .order_by("pk")
                .values()
            ),
            "ledger": list(
                LedgerEntry.objects.filter(appointment_id__in=appointment_ids)
                .order_by("pk")
                .values()
            ),
        }

    def _terminal_event(self, series, event_type):
        event = AppointmentSeriesLifecycleEvent.objects.create(
            series=series,
            operation_key=uuid4(),
            fingerprint="f" * 64,
            event_type=event_type,
            event_number=1,
            status_from=AppointmentSeries.Status.ACTIVE,
            status_to=AppointmentSeries.Status.CANCELLED,
            actor=self.director,
            actor_role_snapshot=AppointmentSeriesLifecycleEvent.ActorRole.DIRECTOR,
            reason="Терминальное решение для проверки запрета возобновления.",
        )
        AppointmentSeries.objects.filter(pk=series.pk).update(
            status=AppointmentSeries.Status.CANCELLED
        )
        series.refresh_from_db()
        return event

    def _action_url(self, series, slug):
        return reverse("appointment_series_action", args=[series.pk, slug])

    def _action_payload(self, *, expected, reason, key=None):
        return {
            "operation_key": str(key or uuid4()),
            "expected_event_id": str(expected),
            "reason": reason,
        }

    def test_service_stop_and_director_resume_preserve_all_existing_facts(self):
        before = self._snapshot_facts()
        appointment_count = Appointment.objects.count()
        stopped = series_lifecycle.stop_materialization(
            self.series,
            operation_key=uuid4(),
            actor=self.administrator,
            reason="Администратор остановил создание новых занятий.",
            expected_event_id=0,
        )

        self.assertEqual(stopped.series.status, AppointmentSeries.Status.CANCELLED)
        self.assertEqual(stopped.event.event_number, 1)
        self.assertIsNone(stopped.event.supersedes_id)
        self.assertEqual(self._snapshot_facts(), before)
        self.assertEqual(Appointment.objects.count(), appointment_count)

        resumed = series_lifecycle.resume_materialization(
            stopped.series,
            operation_key=uuid4(),
            actor=self.director,
            reason="Руководитель разрешил будущие запуски серии.",
            expected_event_id=stopped.event.pk,
        )

        self.assertEqual(resumed.series.status, AppointmentSeries.Status.ACTIVE)
        self.assertEqual(resumed.event.event_number, 2)
        self.assertEqual(resumed.event.supersedes_id, stopped.event.pk)
        self.assertEqual(
            resumed.event.actor_role_snapshot,
            AppointmentSeriesLifecycleEvent.ActorRole.DIRECTOR,
        )
        self.assertEqual(self._snapshot_facts(), before)
        self.assertEqual(Appointment.objects.count(), appointment_count)

    def test_expected_event_is_optional_but_zero_means_empty_history(self):
        legacy_caller_series = self._make_series("Обратная совместимость expected event")
        stopped = series_lifecycle.stop_materialization(
            legacy_caller_series,
            operation_key=uuid4(),
            actor=self.administrator,
            reason="Старый вызывающий код не передает ожидаемое событие.",
            expected_event_id=None,
        )
        resumed = series_lifecycle.resume_materialization(
            stopped.series,
            operation_key=uuid4(),
            actor=self.director,
            reason="Старый вызывающий код возобновляет без expected event.",
            expected_event_id=None,
        )
        self.assertEqual(resumed.event.supersedes_id, stopped.event.pk)

        stale_series = self._make_series("Строгий expected event")
        with self.assertRaises(series_lifecycle.SeriesLifecycleMismatch):
            series_lifecycle.stop_materialization(
                stale_series,
                operation_key=uuid4(),
                actor=self.administrator,
                reason="Неверное ожидаемое событие при пустой истории.",
                expected_event_id=999999,
            )
        self.assertEqual(stale_series.status, AppointmentSeries.Status.ACTIVE)
        self.assertFalse(stale_series.lifecycle_events.exists())

    def test_stale_stop_and_resume_are_atomic(self):
        with self.assertRaises(series_lifecycle.SeriesLifecycleMismatch):
            series_lifecycle.stop_materialization(
                self.series,
                operation_key=uuid4(),
                actor=self.administrator,
                reason="Форма остановки уже устарела.",
                expected_event_id=123456,
            )
        self.assertFalse(self.series.lifecycle_events.exists())

        stopped = series_lifecycle.stop_materialization(
            self.series,
            operation_key=uuid4(),
            actor=self.administrator,
            reason="Актуальная остановка перед stale resume.",
            expected_event_id=0,
        )
        with self.assertRaises(series_lifecycle.SeriesLifecycleMismatch):
            series_lifecycle.resume_materialization(
                stopped.series,
                operation_key=uuid4(),
                actor=self.director,
                reason="Устаревшая форма возобновления.",
                expected_event_id=0,
            )
        stopped.series.refresh_from_db()
        self.assertEqual(stopped.series.status, AppointmentSeries.Status.CANCELLED)
        self.assertEqual(stopped.series.lifecycle_events.count(), 1)

    def test_uuid_replay_wins_after_later_events_and_changed_body_is_rejected(self):
        stop_key = uuid4()
        stop_reason = "Руководитель остановил серию для проверки replay."
        stopped = series_lifecycle.stop_materialization(
            self.series,
            operation_key=stop_key,
            actor=self.director,
            reason=stop_reason,
            expected_event_id=0,
        )
        resume_key = uuid4()
        resume_reason = "Руководитель возобновил серию перед поздним событием."
        resumed = series_lifecycle.resume_materialization(
            stopped.series,
            operation_key=resume_key,
            actor=self.director,
            reason=resume_reason,
            expected_event_id=stopped.event.pk,
        )

        replayed_stop = series_lifecycle.stop_materialization(
            resumed.series,
            operation_key=stop_key,
            actor=self.director,
            reason=stop_reason,
            expected_event_id=0,
        )
        self.assertTrue(replayed_stop.reused_event)
        self.assertEqual(replayed_stop.event.pk, stopped.event.pk)
        self.assertEqual(replayed_stop.series.status, AppointmentSeries.Status.ACTIVE)

        with self.assertRaises(series_lifecycle.SeriesLifecycleMismatch):
            series_lifecycle.stop_materialization(
                resumed.series,
                operation_key=stop_key,
                actor=self.director,
                reason="Тот же UUID с новым основанием запрещен.",
                expected_event_id=resumed.event.pk,
            )
        with self.assertRaises(series_lifecycle.SeriesLifecycleMismatch):
            series_lifecycle.resume_materialization(
                resumed.series,
                operation_key=stop_key,
                actor=self.director,
                reason=stop_reason,
                expected_event_id=resumed.event.pk,
            )

        latest_stop = series_lifecycle.stop_materialization(
            resumed.series,
            operation_key=uuid4(),
            actor=self.director,
            reason="Поздняя остановка после принятого возобновления.",
            expected_event_id=resumed.event.pk,
        )
        replayed_resume = series_lifecycle.resume_materialization(
            latest_stop.series,
            operation_key=resume_key,
            actor=self.director,
            reason=resume_reason,
            expected_event_id=stopped.event.pk,
        )
        self.assertTrue(replayed_resume.reused_event)
        self.assertEqual(replayed_resume.event.pk, resumed.event.pk)
        self.assertEqual(replayed_resume.series.status, AppointmentSeries.Status.CANCELLED)
        self.assertEqual(self.series.lifecycle_events.count(), 3)

    def test_resume_role_and_director_priority_are_enforced(self):
        stopped = series_lifecycle.stop_materialization(
            self.series,
            operation_key=uuid4(),
            actor=self.administrator,
            reason="Администратор остановил серию перед решением руководителя.",
            expected_event_id=0,
        )
        for actor in (self.administrator, self.specialist_user):
            with self.subTest(actor=actor.username), self.assertRaises(PermissionDenied):
                series_lifecycle.resume_materialization(
                    stopped.series,
                    operation_key=uuid4(),
                    actor=actor,
                    reason="Недостаточно полномочий для возобновления серии.",
                    expected_event_id=stopped.event.pk,
                )

        resumed = series_lifecycle.resume_materialization(
            stopped.series,
            operation_key=uuid4(),
            actor=self.director,
            reason="Руководитель возобновил новые запуски.",
            expected_event_id=stopped.event.pk,
        )
        with self.assertRaises(PermissionDenied):
            series_lifecycle.stop_materialization(
                resumed.series,
                operation_key=uuid4(),
                actor=self.administrator,
                reason="Администратор не отменяет решение руководителя.",
                expected_event_id=resumed.event.pk,
            )
        director_stop = series_lifecycle.stop_materialization(
            resumed.series,
            operation_key=uuid4(),
            actor=self.director,
            reason="Руководитель снова остановил новые запуски.",
            expected_event_id=resumed.event.pk,
        )
        self.assertEqual(director_stop.event.event_number, 3)

    def test_resume_rejects_cancel_and_withdraw_predecessors_in_service_and_model(self):
        terminal_types = (
            AppointmentSeriesLifecycleEvent.EventType.CANCEL_FUTURE_UNSTARTED,
            AppointmentSeriesLifecycleEvent.EventType.WITHDRAW_FUTURE_JOINED_PARTICIPATIONS,
        )
        for event_type in terminal_types:
            with self.subTest(event_type=event_type):
                series = self._make_series(f"Терминальная серия {event_type}")
                predecessor = self._terminal_event(series, event_type)
                with self.assertRaises(series_lifecycle.SeriesLifecycleMismatch):
                    series_lifecycle.resume_materialization(
                        series,
                        operation_key=uuid4(),
                        actor=self.director,
                        reason="Возобновление после терминального решения запрещено.",
                        expected_event_id=predecessor.pk,
                    )
                with self.assertRaisesMessage(ValidationError, "явную остановку"):
                    AppointmentSeriesLifecycleEvent.objects.create(
                        series=series,
                        operation_key=uuid4(),
                        fingerprint="m" * 64,
                        event_type=(
                            AppointmentSeriesLifecycleEvent.EventType.RESUME_MATERIALIZATION
                        ),
                        event_number=2,
                        status_from=AppointmentSeries.Status.CANCELLED,
                        status_to=AppointmentSeries.Status.ACTIVE,
                        actor=self.director,
                        actor_role_snapshot=(
                            AppointmentSeriesLifecycleEvent.ActorRole.DIRECTOR
                        ),
                        reason="Model-level invalid terminal resume.",
                        supersedes=predecessor,
                    )
                series.refresh_from_db()
                self.assertEqual(series.status, AppointmentSeries.Status.CANCELLED)
                self.assertEqual(series.lifecycle_events.count(), 1)

    @skipUnless(connection.vendor == "postgresql", "DB guard проверяется на PostgreSQL.")
    def test_postgresql_guard_rejects_resume_after_cancel_and_withdraw(self):
        terminal_types = (
            AppointmentSeriesLifecycleEvent.EventType.CANCEL_FUTURE_UNSTARTED,
            AppointmentSeriesLifecycleEvent.EventType.WITHDRAW_FUTURE_JOINED_PARTICIPATIONS,
        )
        for event_type in terminal_types:
            with self.subTest(event_type=event_type):
                series = self._make_series(f"Raw terminal {event_type}")
                predecessor = self._terminal_event(series, event_type)
                with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO operations_appointmentserieslifecycleevent (
                            created_at, updated_at, operation_key, fingerprint,
                            event_type, event_number, status_from, status_to,
                            actor_role_snapshot, reason, occurred_at, actor_id,
                            series_id, supersedes_id
                        ) VALUES (
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s, %s,
                            %s, 2, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s, %s, %s
                        )
                        """,
                        [
                            str(uuid4()),
                            "r" * 64,
                            AppointmentSeriesLifecycleEvent.EventType.RESUME_MATERIALIZATION,
                            AppointmentSeries.Status.CANCELLED,
                            AppointmentSeries.Status.ACTIVE,
                            AppointmentSeriesLifecycleEvent.ActorRole.DIRECTOR,
                            "Raw resume must require an explicit stop.",
                            self.director.pk,
                            series.pk,
                            predecessor.pk,
                        ],
                    )
                self.assertEqual(series.lifecycle_events.count(), 1)

    def test_action_gets_explain_availability_and_enforce_roles(self):
        stop_url = self._action_url(self.series, "stop-materialization")
        resume_url = self._action_url(self.series, "resume-materialization")
        self.client.logout()
        self.assertEqual(self.client.get(stop_url).status_code, 302)
        self.client.force_login(self.specialist_user)
        self.assertEqual(self.client.get(stop_url).status_code, 403)
        self.assertEqual(self.client.get(resume_url).status_code, 403)

        self.client.force_login(self.administrator)
        stop_get = self.client.get(stop_url)
        resume_get = self.client.get(resume_url)
        self.assertEqual(stop_get.status_code, 200)
        self.assertTrue(stop_get.context["action"]["available"])
        self.assertEqual(stop_get.context["form"]["expected_event_id"].value(), 0)
        self.assertFalse(resume_get.context["action"]["available"])
        self.assertTrue(resume_get.context["action"]["blocked_reason"])

        stop_payload = self._action_payload(
            expected=0, reason="Администратор остановил новые запуски из карточки."
        )
        stopped = self.client.post(stop_url, stop_payload)
        self.assertEqual(stopped.status_code, 302)
        event = self.series.lifecycle_events.get()
        admin_resume_get = self.client.get(resume_url)
        self.assertFalse(admin_resume_get.context["action"]["available"])
        self.assertEqual(
            self.client.post(
                resume_url,
                self._action_payload(
                    expected=event.pk,
                    reason="Администратор не может возобновить новые запуски.",
                ),
            ).status_code,
            403,
        )

        self.client.force_login(self.director)
        director_resume_get = self.client.get(resume_url)
        self.assertTrue(director_resume_get.context["action"]["available"])
        self.assertTrue(director_resume_get.context["materialization_stopped"])
        self.assertEqual(
            director_resume_get.context["form"]["expected_event_id"].value(), event.pk
        )
        resumed = self.client.post(
            resume_url,
            self._action_payload(
                expected=event.pk,
                reason="Руководитель возобновил новые запуски из карточки.",
            ),
        )
        self.assertEqual(resumed.status_code, 302)
        self.series.refresh_from_db()
        self.assertEqual(self.series.status, AppointmentSeries.Status.ACTIVE)

    def test_action_form_stale_replay_and_uuid_mismatch_are_controlled(self):
        stop_url = self._action_url(self.series, "stop-materialization")
        resume_url = self._action_url(self.series, "resume-materialization")
        invalid = self.client.post(
            stop_url,
            {"operation_key": "bad", "expected_event_id": "-1", "reason": "нет"},
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(
            set(invalid.context["form"].errors),
            {"operation_key", "expected_event_id", "reason"},
        )

        stop_key = uuid4()
        stop_reason = "Остановка для проверки повторного POST."
        stop_payload = self._action_payload(
            expected=0, reason=stop_reason, key=stop_key
        )
        self.assertEqual(self.client.post(stop_url, stop_payload).status_code, 302)
        first_stop = self.series.lifecycle_events.get(event_number=1)

        self.client.force_login(self.director)
        resume_key = uuid4()
        resume_reason = "Возобновление перед проверкой replay через UI."
        resume_payload = self._action_payload(
            expected=first_stop.pk, reason=resume_reason, key=resume_key
        )
        self.assertEqual(self.client.post(resume_url, resume_payload).status_code, 302)
        resumed = self.series.lifecycle_events.get(event_number=2)

        self.client.force_login(self.administrator)
        replayed_stop = self.client.post(stop_url, stop_payload)
        self.assertEqual(replayed_stop.status_code, 302)
        self.assertEqual(self.series.lifecycle_events.count(), 2)
        changed_body = stop_payload.copy()
        changed_body["reason"] = "Измененное основание того же UUID."
        mismatch = self.client.post(stop_url, changed_body)
        self.assertEqual(mismatch.status_code, 409)
        self.assertTrue(mismatch.context["form"].non_field_errors())

        stale = self.client.post(
            stop_url,
            self._action_payload(
                expected=0, reason="Устаревшая новая команда остановки."
            ),
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.series.lifecycle_events.count(), 2)

        self.client.force_login(self.director)
        latest_stop = series_lifecycle.stop_materialization(
            self.series,
            operation_key=uuid4(),
            actor=self.director,
            reason="Поздняя остановка после UI-возобновления.",
            expected_event_id=resumed.pk,
        )
        replayed_resume = self.client.post(resume_url, resume_payload)
        self.assertEqual(replayed_resume.status_code, 302)
        self.assertEqual(self.series.lifecycle_events.count(), 3)
        self.assertEqual(
            self.series.lifecycle_events.order_by("-event_number").first().pk,
            latest_stop.event.pk,
        )

    def test_stop_resume_actions_support_join_series(self):
        series = self._make_series("Join-серия остановки", join=True)
        stop_url = self._action_url(series, "stop-materialization")
        resume_url = self._action_url(series, "resume-materialization")
        stopped = self.client.post(
            stop_url,
            self._action_payload(
                expected=0, reason="Остановить новые join-запуски серии."
            ),
        )
        self.assertEqual(stopped.status_code, 302)
        event = series.lifecycle_events.get()
        self.client.force_login(self.director)
        resumed = self.client.post(
            resume_url,
            self._action_payload(
                expected=event.pk, reason="Возобновить новые join-запуски серии."
            ),
        )
        self.assertEqual(resumed.status_code, 302)
        series.refresh_from_db()
        self.assertEqual(series.status, AppointmentSeries.Status.ACTIVE)

    def test_registry_distinguishes_stopped_from_cancelled_without_n_plus_one(self):
        stopped_series = self._make_series("Остановленная серия реестра")
        series_lifecycle.stop_materialization(
            stopped_series,
            operation_key=uuid4(),
            actor=self.administrator,
            reason="Остановка для отдельного статуса реестра.",
            expected_event_id=0,
        )
        cancelled_series = self._make_series("Отмененная серия реестра")
        self._terminal_event(
            cancelled_series,
            AppointmentSeriesLifecycleEvent.EventType.CANCEL_FUTURE_UNSTARTED,
        )
        registry_url = reverse("appointment_series_list")

        stopped = self.client.get(registry_url, {"status": "stopped"})
        cancelled = self.client.get(
            registry_url, {"status": AppointmentSeries.Status.CANCELLED}
        )
        stopped_ids = {row["series"].pk for row in stopped.context["registry_rows"]}
        cancelled_ids = {
            row["series"].pk for row in cancelled.context["registry_rows"]
        }
        self.assertIn(stopped_series.pk, stopped_ids)
        self.assertNotIn(cancelled_series.pk, stopped_ids)
        self.assertIn(cancelled_series.pk, cancelled_ids)
        self.assertNotIn(stopped_series.pk, cancelled_ids)
        self.assertTrue(stopped.context["registry_rows"][0]["materialization_stopped"])

        with CaptureQueriesContext(connection) as one_row_queries:
            self.client.get(registry_url, {"status": "stopped"})
        for index in range(5):
            extra = self._make_series(f"Остановленная серия {index}")
            series_lifecycle.stop_materialization(
                extra,
                operation_key=uuid4(),
                actor=self.administrator,
                reason=f"Остановка строки реестра номер {index}.",
                expected_event_id=0,
            )
        with CaptureQueriesContext(connection) as many_row_queries:
            grown = self.client.get(registry_url, {"status": "stopped"})
        self.assertEqual(len(grown.context["registry_rows"]), 6)
        self.assertEqual(len(many_row_queries), len(one_row_queries))


class SeriesStopResumeMigrationTests(TransactionTestCase):
    migrate_from = [("operations", "0065_appointment_operator_decisions")]
    migrate_to = [("operations", "0066_series_resume_stop_guard")]

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("operations"))
        super().tearDown()

    def _function_definition(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_get_functiondef("
                "'operations_validate_series_lifecycle_event()'::regprocedure)"
            )
            return cursor.fetchone()[0]

    def _historical_series(self, apps, suffix):
        user = apps.get_model("auth", "User").objects.create(
            username=f"stop-resume-migration-{suffix}",
            is_staff=True,
            is_superuser=True,
        )
        child = apps.get_model("operations", "Child").objects.create(
            last_name="Migration", first_name=suffix
        )
        staff = apps.get_model("operations", "StaffMember").objects.create(
            full_name=f"Migration staff {suffix}"
        )
        service = apps.get_model("operations", "Service").objects.create(
            name=f"Migration service {suffix}",
            code=f"STOP-MIGRATION-{suffix}",
        )
        day = timezone.localdate() + timedelta(days=5)
        series = apps.get_model("operations", "AppointmentSeries").objects.create(
            child_id=child.pk,
            service_id=service.pk,
            staff_member_id=staff.pk,
            title=f"Migration series {suffix}",
            start_date=day,
            end_date=day + timedelta(days=2),
            days_of_week="ПН",
            time=time(10, 0),
            duration_minutes=30,
            status="active",
        )
        return user, series

    def _delete_lifecycle_events(self):
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE operations_appointmentserieslifecycleevent "
                    "DISABLE TRIGGER USER"
                )
                try:
                    cursor.execute(
                        "DELETE FROM operations_appointmentserieslifecycleevent"
                    )
                finally:
                    cursor.execute(
                        "ALTER TABLE operations_appointmentserieslifecycleevent "
                        "ENABLE TRIGGER USER"
                    )
        else:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM operations_appointmentserieslifecycleevent"
                )

    def test_0066_forward_reverse_roundtrip_replaces_only_the_guard(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        if connection.vendor == "postgresql":
            self.assertIn(
                "previous_event_type IS DISTINCT FROM 'stop_materialization'",
                self._function_definition(),
            )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        if connection.vendor == "postgresql":
            definition = self._function_definition()
            self.assertIn(
                "previous_event_type = 'resume_materialization'", definition
            )
            self.assertNotIn(
                "previous_event_type IS DISTINCT FROM 'stop_materialization'",
                definition,
            )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        if connection.vendor == "postgresql":
            self.assertIn(
                "previous_event_type IS DISTINCT FROM 'stop_materialization'",
                self._function_definition(),
            )

    def test_0066_preflight_rejects_invalid_history_without_rewriting_it(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        apps = executor.loader.project_state(self.migrate_from).apps
        user, series = self._historical_series(apps, uuid4().hex[:8])
        event_model = apps.get_model(
            "operations", "AppointmentSeriesLifecycleEvent"
        )
        series_model = apps.get_model("operations", "AppointmentSeries")
        with transaction.atomic():
            cancelled = event_model.objects.create(
                series_id=series.pk,
                operation_key=uuid4(),
                fingerprint="c" * 64,
                event_type="cancel_future_unstarted",
                event_number=1,
                status_from="active",
                status_to="cancelled",
                actor_id=user.pk,
                actor_role_snapshot="director",
                reason="Historical cancellation before invalid resume.",
            )
            series_model.objects.filter(pk=series.pk).update(status="cancelled")
            resumed = event_model.objects.create(
                series_id=series.pk,
                operation_key=uuid4(),
                fingerprint="r" * 64,
                event_type="resume_materialization",
                event_number=2,
                status_from="cancelled",
                status_to="active",
                actor_id=user.pk,
                actor_role_snapshot="director",
                reason="Legacy resume after cancellation is invalid in 0066.",
                supersedes_id=cancelled.pk,
            )
            series_model.objects.filter(pk=series.pk).update(status="active")

        before = list(
            event_model.objects.filter(series_id=series.pk)
            .order_by("event_number")
            .values_list("pk", "event_type", "supersedes_id")
        )
        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError, "resume events without an explicit stop predecessor"
        ):
            executor.migrate(self.migrate_to)
        after = list(
            event_model.objects.filter(series_id=series.pk)
            .order_by("event_number")
            .values_list("pk", "event_type", "supersedes_id")
        )
        self.assertEqual(after, before)
        self.assertEqual(after[-1][0], resumed.pk)

        self._delete_lifecycle_events()
        series_model.objects.filter(pk=series.pk).update(status="active")
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
