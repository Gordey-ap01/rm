from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from queue import Queue
from threading import Event, Thread
from time import monotonic
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from operations.forms import TreatmentProgramForm
from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentSeries,
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
    TreatmentProgramLifecycleEvent,
)
from operations.services import program_lifecycle
from operations.services.series_revisions import canonical_fingerprint

User = get_user_model()


def _local(day, clock):
    return timezone.make_aware(
        datetime.combine(day, clock), timezone.get_current_timezone()
    )


class ProgramLifecycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.administrator = User.objects.create_user(
            "program-lifecycle-administrator", password="x", is_staff=True
        )
        cls.director = User.objects.create_superuser(
            "program-lifecycle-director", password="x"
        )
        cls.specialist_user = User.objects.create_user(
            "program-lifecycle-specialist", password="x"
        )
        cls.child = Child.objects.create(
            last_name="Жизненный цикл", first_name="Программы"
        )
        cls.staff = StaffMember.objects.create(
            user=cls.specialist_user, full_name="Специалист программы"
        )
        cls.service = Service.objects.create(
            name="Услуга программы",
            code="PROGRAM-LIFECYCLE",
            default_duration_minutes=45,
            default_price=Decimal("1500"),
        )
        cls.room = Room.objects.create(
            name="Кабинет программы",
            capacity=3,
            allow_group_sessions=True,
            max_recipient_count=3,
        )
        cls.funding = FundingSource.objects.create(
            name="Источник программы",
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
            title="Управляемая программа",
            status=TreatmentProgram.Status.ACTIVE,
        )
        cls.block = ProgramBlock.objects.create(
            program=cls.program,
            number=1,
            title="Каскад программы",
            service=cls.service,
            staff_member=cls.staff,
            planned_sessions=20,
            balance_account=cls.account,
        )
        cls.day = timezone.localdate() + timedelta(days=7)
        starts_at = _local(cls.day, time(10, 0))
        cls.appointment = Appointment.objects.create(
            child=cls.child,
            staff_member=cls.staff,
            service=cls.service,
            room=cls.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
            status=Appointment.Status.PROPOSED,
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=cls.account,
            program_block=cls.block,
        )
        participant = cls.appointment.participants.get(child=cls.child)
        participant.price_snapshot = Decimal("1500")
        participant.save(update_fields=["price_snapshot", "updated_at"])
        LedgerEntry.objects.create(
            account=cls.account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
            appointment=cls.appointment,
            appointment_participant=participant,
            price_snapshot=Decimal("1500"),
            created_by=cls.director,
            reason="Финансовый факт до паузы программы.",
        )
        cls.series = AppointmentSeries.objects.create(
            child=cls.child,
            service=cls.service,
            staff_member=cls.staff,
            room=cls.room,
            program_block=cls.block,
            title="Серия программы",
            start_date=cls.day,
            end_date=cls.day + timedelta(days=14),
            days_of_week="ПН",
            time=time(10, 0),
            duration_minutes=45,
            status=AppointmentSeries.Status.ACTIVE,
        )

    def setUp(self):
        self.client.force_login(self.administrator)

    def _pause(self, program=None, *, actor=None, key=None, expected=0, reason=None):
        return program_lifecycle.pause_program(
            program or self.program,
            actor=actor or self.administrator,
            reason=reason or "Приостановить новые назначения по программе.",
            operation_key=key or uuid4(),
            expected_event_id=expected,
        )

    def _resume(self, program=None, *, key=None, expected=None, reason=None):
        return program_lifecycle.resume_program(
            program or self.program,
            actor=self.director,
            reason=reason or "Возобновить новые назначения по программе.",
            operation_key=key or uuid4(),
            expected_event_id=expected,
        )

    def _facts(self):
        appointment_ids = Appointment.objects.filter(
            participants__program_block__program=self.program
        ).values("pk")
        return {
            "blocks": list(
                ProgramBlock.objects.filter(program=self.program).order_by("pk").values()
            ),
            "appointments": list(
                Appointment.objects.filter(pk__in=appointment_ids).order_by("pk").values()
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
            "series": list(
                AppointmentSeries.objects.filter(program_block__program=self.program)
                .order_by("pk")
                .values()
            ),
            "ledger": list(
                LedgerEntry.objects.filter(appointment_id__in=appointment_ids)
                .order_by("pk")
                .values()
            ),
        }

    def _action_url(self, program, action):
        return reverse("program_lifecycle_action", args=[program.pk, action])

    def _payload(self, *, expected, key=None, reason=None):
        return {
            "operation_key": str(key or uuid4()),
            "expected_event_id": str(expected),
            "reason": reason or "Подтвержденное основание команды программы.",
        }

    def _appointment_payload(self, appointment, **overrides):
        local_start = timezone.localtime(appointment.starts_at)
        payload = {
            "session_type": appointment.session_type,
            "child": str(appointment.child_id),
            "participants": [str(appointment.child_id)],
            "service": str(appointment.service_id),
            "staff_member": str(appointment.staff_member_id),
            "staff_members": [str(appointment.staff_member_id)],
            "room": str(appointment.room_id),
            "program_block": str(appointment.program_block_id or ""),
            "billing_account": str(appointment.billing_account_id or ""),
            "status": appointment.status,
            "admin_note": appointment.admin_note,
            "date": local_start.date().isoformat(),
            "time": local_start.strftime("%H:%M"),
            "duration_minutes": str(appointment.duration_minutes),
        }
        payload.update(overrides)
        return payload

    def test_pause_and_resume_append_a_chain_without_touching_existing_facts(self):
        before = self._facts()
        appointment_count = Appointment.objects.count()
        paused = self._pause(expected=0)

        self.assertEqual(paused.program.status, TreatmentProgram.Status.PAUSED)
        self.assertEqual(paused.event.event_number, 1)
        self.assertEqual(
            paused.event.actor_role_snapshot,
            TreatmentProgramLifecycleEvent.ActorRole.ADMINISTRATOR,
        )
        self.assertIsNone(paused.event.supersedes_id)
        self.assertEqual(self._facts(), before)
        self.assertEqual(Appointment.objects.count(), appointment_count)

        resumed = self._resume(expected=paused.event.pk)
        self.assertEqual(resumed.program.status, TreatmentProgram.Status.ACTIVE)
        self.assertEqual(resumed.event.event_number, 2)
        self.assertEqual(resumed.event.supersedes_id, paused.event.pk)
        self.assertEqual(
            resumed.event.actor_role_snapshot,
            TreatmentProgramLifecycleEvent.ActorRole.DIRECTOR,
        )
        self.assertEqual(self._facts(), before)
        self.assertEqual(Appointment.objects.count(), appointment_count)

    def test_director_can_resume_a_legacy_paused_program_without_history(self):
        legacy = TreatmentProgram.objects.create(
            child=self.child,
            title="Legacy pause without history",
            status=TreatmentProgram.Status.PAUSED,
        )
        result = self._resume(legacy, expected=0)

        self.assertEqual(result.event.event_number, 1)
        self.assertIsNone(result.event.supersedes_id)
        self.assertEqual(result.event.status_from, TreatmentProgram.Status.PAUSED)
        self.assertEqual(result.program.status, TreatmentProgram.Status.ACTIVE)

    def test_expected_event_optional_and_stale_commands_are_atomic(self):
        legacy_caller = TreatmentProgram.objects.create(
            child=self.child,
            title="Optional expected event",
            status=TreatmentProgram.Status.ACTIVE,
        )
        paused = self._pause(legacy_caller, expected=None)
        resumed = self._resume(paused.program, expected=None)
        self.assertEqual(resumed.event.supersedes_id, paused.event.pk)

        stale = TreatmentProgram.objects.create(
            child=self.child,
            title="Stale expected event",
            status=TreatmentProgram.Status.ACTIVE,
        )
        with self.assertRaises(program_lifecycle.ProgramLifecycleMismatch):
            self._pause(stale, expected=999999)
        stale.refresh_from_db()
        self.assertEqual(stale.status, TreatmentProgram.Status.ACTIVE)
        self.assertFalse(stale.lifecycle_events.exists())

        paused = self._pause(stale, expected=0)
        with self.assertRaises(program_lifecycle.ProgramLifecycleMismatch):
            self._resume(stale, expected=0)
        paused.program.refresh_from_db()
        self.assertEqual(paused.program.status, TreatmentProgram.Status.PAUSED)
        self.assertEqual(paused.program.lifecycle_events.count(), 1)

    def test_replay_precedes_state_stale_and_priority_but_not_authorization(self):
        stop_key = uuid4()
        stop_reason = "Остановка программы для проверки повторной доставки."
        paused = self._pause(
            actor=self.director,
            key=stop_key,
            expected=0,
            reason=stop_reason,
        )
        resume_key = uuid4()
        resume_reason = "Возобновление программы перед поздним повтором."
        resumed = self._resume(
            paused.program,
            key=resume_key,
            expected=paused.event.pk,
            reason=resume_reason,
        )

        replay = self._pause(
            resumed.program,
            actor=self.director,
            key=stop_key,
            expected=0,
            reason=stop_reason,
        )
        self.assertTrue(replay.reused_event)
        self.assertEqual(replay.event.pk, paused.event.pk)
        self.assertEqual(replay.program.status, TreatmentProgram.Status.ACTIVE)
        self.assertEqual(self.program.lifecycle_events.count(), 2)

        with self.assertRaises(PermissionDenied):
            self._pause(
                resumed.program,
                actor=self.specialist_user,
                key=stop_key,
                expected=0,
                reason=stop_reason,
            )
        with self.assertRaises(program_lifecycle.ProgramLifecycleMismatch):
            self._pause(
                resumed.program,
                actor=self.administrator,
                key=stop_key,
                expected=resumed.event.pk,
                reason="Измененное основание с тем же UUID.",
            )

    def test_roles_and_director_priority_are_enforced(self):
        with self.assertRaises(PermissionDenied):
            self._pause(actor=self.specialist_user)
        paused = self._pause(expected=0)
        with self.assertRaises(PermissionDenied):
            program_lifecycle.resume_program(
                paused.program,
                actor=self.administrator,
                reason="Администратор пытается возобновить программу.",
                operation_key=uuid4(),
                expected_event_id=paused.event.pk,
            )
        resumed = self._resume(paused.program, expected=paused.event.pk)
        with self.assertRaises(PermissionDenied):
            self._pause(
                resumed.program,
                actor=self.administrator,
                expected=resumed.event.pk,
            )
        director_pause = self._pause(
            resumed.program,
            actor=self.director,
            expected=resumed.event.pk,
        )
        self.assertEqual(director_pause.event.event_number, 3)

    def test_model_queryset_role_and_immutability_guards(self):
        direct = TreatmentProgram.objects.create(
            child=self.child,
            title="Direct transition",
            status=TreatmentProgram.Status.ACTIVE,
        )
        direct.status = TreatmentProgram.Status.PAUSED
        with self.assertRaises(ValidationError):
            direct.save(update_fields=["status", "updated_at"])
        with self.assertRaises(ValidationError):
            TreatmentProgram.objects.filter(pk=direct.pk).update(
                status=TreatmentProgram.Status.PAUSED
            )

        legacy_non_pause = TreatmentProgram.objects.create(
            child=self.child,
            title="Legacy non-pause transition",
            status=TreatmentProgram.Status.ACTIVE,
        )
        legacy_non_pause.status = TreatmentProgram.Status.COMPLETED
        with self.assertRaises(ValidationError):
            legacy_non_pause.save(update_fields=["status", "updated_at"])
        legacy_non_pause.refresh_from_db()
        self.assertEqual(legacy_non_pause.status, TreatmentProgram.Status.ACTIVE)

        fake_role_program = TreatmentProgram.objects.create(
            child=self.child,
            title="Fake event role",
            status=TreatmentProgram.Status.ACTIVE,
        )
        fake = TreatmentProgramLifecycleEvent(
            program=fake_role_program,
            operation_key=uuid4(),
            fingerprint="",
            event_type=TreatmentProgramLifecycleEvent.EventType.PAUSED,
            event_number=1,
            status_from=TreatmentProgram.Status.ACTIVE,
            status_to=TreatmentProgram.Status.PAUSED,
            actor=self.administrator,
            actor_role_snapshot=TreatmentProgramLifecycleEvent.ActorRole.DIRECTOR,
            reason="Поддельная роль руководителя в событии.",
        )
        fake.fingerprint = canonical_fingerprint(fake.fingerprint_payload())
        with self.assertRaises(ValidationError):
            fake.save()

        event = self._pause(expected=0).event
        event.reason = "Попытка изменить сохраненную историю."
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            TreatmentProgramLifecycleEvent.objects.filter(pk=event.pk).update(
                reason="QuerySet bypass"
            )
        with self.assertRaises(ValidationError):
            TreatmentProgramLifecycleEvent.objects.filter(pk=event.pk).delete()

    def test_actions_enforce_roles_form_versions_replay_and_legacy_pause(self):
        detail_url = reverse("program_detail", args=[self.program.pk])
        pause_url = self._action_url(self.program, "pause")
        resume_url = self._action_url(self.program, "resume")
        self.client.logout()
        self.assertEqual(self.client.get(detail_url).status_code, 302)
        self.client.force_login(self.specialist_user)
        self.assertEqual(self.client.get(detail_url).status_code, 403)
        self.assertEqual(self.client.get(pause_url).status_code, 403)

        self.client.force_login(self.administrator)
        recipient = self.client.get(reverse("recipient_detail", args=[self.child.pk]))
        self.assertContains(
            recipient, reverse("program_detail", args=[self.program.pk])
        )
        pause_get = self.client.get(pause_url)
        self.assertEqual(pause_get.status_code, 200)
        self.assertTrue(pause_get.context["action"]["available"])
        self.assertEqual(pause_get.context["form"]["expected_event_id"].value(), 0)
        invalid = self.client.post(
            pause_url,
            {"operation_key": "bad", "expected_event_id": "-1", "reason": "нет"},
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(
            set(invalid.context["form"].errors),
            {"operation_key", "expected_event_id", "reason"},
        )

        pause_key = uuid4()
        pause_reason = "Пауза через карточку программы."
        pause_payload = self._payload(
            expected=0, key=pause_key, reason=pause_reason
        )
        self.assertEqual(self.client.post(pause_url, pause_payload).status_code, 302)
        paused = self.program.lifecycle_events.get(event_number=1)
        resume_get = self.client.get(resume_url)
        self.assertFalse(resume_get.context["action"]["available"])
        self.assertTrue(resume_get.context["action"]["blocked_reason"])
        self.assertEqual(
            self.client.post(
                resume_url,
                self._payload(expected=paused.pk),
            ).status_code,
            403,
        )

        self.client.force_login(self.director)
        resume_key = uuid4()
        resume_reason = "Возобновление через карточку программы."
        resume_payload = self._payload(
            expected=paused.pk, key=resume_key, reason=resume_reason
        )
        director_get = self.client.get(resume_url)
        self.assertTrue(director_get.context["action"]["available"])
        self.assertEqual(
            director_get.context["form"]["expected_event_id"].value(), paused.pk
        )
        self.assertEqual(self.client.post(resume_url, resume_payload).status_code, 302)

        self.client.force_login(self.administrator)
        self.assertEqual(self.client.post(pause_url, pause_payload).status_code, 302)
        changed = pause_payload.copy()
        changed["reason"] = "Другое основание при том же UUID."
        self.assertEqual(self.client.post(pause_url, changed).status_code, 409)
        self.assertEqual(
            self.client.post(
                pause_url,
                self._payload(expected=0, reason="Устаревшая новая команда паузы."),
            ).status_code,
            409,
        )
        self.assertEqual(self.program.lifecycle_events.count(), 2)

        legacy = TreatmentProgram.objects.create(
            child=self.child,
            title="Legacy paused UI",
            status=TreatmentProgram.Status.PAUSED,
        )
        self.client.force_login(self.director)
        detail = self.client.get(reverse("program_detail", args=[legacy.pk]))
        self.assertTrue(detail.context["legacy_paused_without_history"])
        self.assertEqual(
            self.client.post(
                self._action_url(legacy, "resume"), self._payload(expected=0)
            ).status_code,
            302,
        )

    def test_activation_completion_and_cancellation_actions_use_frozen_review(self):
        draft = TreatmentProgram.objects.create(
            child=self.child,
            title="Черновик для активации через карточку",
            status=TreatmentProgram.Status.DRAFT,
        )
        ProgramBlock.objects.create(
            program=draft,
            number=1,
            title="Каскад черновика",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=3,
            balance_account=self.account,
        )
        activation_url = self._action_url(draft, "activate")
        activation = self.client.get(activation_url)
        self.assertEqual(activation.status_code, 200)
        self.assertTrue(activation.context["action"]["available"])
        self.assertIn("expected_review_fingerprint", activation.context["form"].fields)
        activation_review = program_lifecycle.get_program_lifecycle_review(draft)
        activation_payload = self._payload(expected=0)
        activation_payload["expected_review_fingerprint"] = activation_review.fingerprint
        self.assertEqual(self.client.post(activation_url, activation_payload).status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.status, TreatmentProgram.Status.ACTIVE)

        completion_url = self._action_url(draft, "complete")
        completion = self.client.get(completion_url)
        self.assertFalse(completion.context["action"]["available"])
        self.assertEqual(len(completion.context["review_blocks"]), 1)
        self.assertEqual(
            completion.context["review_blocks"][0]["status_display"],
            ProgramBlock.Status.PLANNED.label,
        )

        self.client.force_login(self.director)
        completion_review = program_lifecycle.get_program_lifecycle_review(draft)
        completion_payload = self._payload(
            expected=draft.lifecycle_events.latest("event_number").pk,
            reason="Руководитель завершает программу с незавершенным каскадом.",
        )
        completion_payload["expected_review_fingerprint"] = completion_review.fingerprint
        self.assertEqual(self.client.post(completion_url, completion_payload).status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.status, TreatmentProgram.Status.COMPLETED)

        cancelled = TreatmentProgram.objects.create(
            child=self.child,
            title="Программа для отмены через карточку",
            status=TreatmentProgram.Status.ACTIVE,
        )
        cancel_url = self._action_url(cancelled, "cancel")
        cancellation = self.client.get(cancel_url)
        self.assertTrue(cancellation.context["action"]["available"])
        cancellation_review = program_lifecycle.get_program_lifecycle_review(cancelled)
        cancellation_payload = self._payload(expected=0)
        cancellation_payload["expected_review_fingerprint"] = cancellation_review.fingerprint
        self.assertEqual(self.client.post(cancel_url, cancellation_payload).status_code, 302)
        cancelled.refresh_from_db()
        self.assertEqual(cancelled.status, TreatmentProgram.Status.CANCELLED)

    def test_program_status_is_not_ordinary_editable_data(self):
        new_form = TreatmentProgramForm()
        self.assertTrue(new_form.fields["status"].disabled)
        self.assertEqual(
            list(new_form.fields["status"].choices),
            [(TreatmentProgram.Status.DRAFT, "Черновик")],
        )
        existing_form = TreatmentProgramForm(instance=self.program)
        self.assertTrue(existing_form.fields["status"].disabled)

    def test_review_action_stale_post_explains_how_to_refresh(self):
        draft = TreatmentProgram.objects.create(
            child=self.child,
            title="Черновик для устаревшей проверки",
            status=TreatmentProgram.Status.DRAFT,
        )
        block = ProgramBlock.objects.create(
            program=draft,
            number=1,
            title="Каскад устаревшей проверки",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=2,
            balance_account=self.account,
        )
        activation_url = self._action_url(draft, "activate")
        review = program_lifecycle.get_program_lifecycle_review(draft)
        block.planned_sessions = 3
        block.save(update_fields=["planned_sessions", "updated_at"])
        payload = self._payload(expected=0)
        payload["expected_review_fingerprint"] = review.fingerprint

        response = self.client.post(activation_url, payload)

        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.context["review_is_stale"])
        self.assertContains(response, "Обновить проверку", status_code=409)
        self.assertEqual(response.context["refresh_url"], activation_url)

    def test_program_detail_shows_participant_first_block_progress(self):
        self.appointment.status = Appointment.Status.COMPLETED
        self.appointment.attendance_status = Appointment.AttendanceStatus.ATTENDED
        self.appointment.save(update_fields=["status", "attendance_status", "updated_at"])
        AppointmentParticipant.objects.filter(appointment=self.appointment).update(
            appointment_status=Appointment.Status.COMPLETED,
            attendance_status=Appointment.AttendanceStatus.ATTENDED,
        )

        response = self.client.get(reverse("program_detail", args=[self.program.pk]))

        self.assertEqual(response.status_code, 200)
        progress = response.context["blocks"][0].progress
        self.assertEqual(progress.planned, 20)
        self.assertEqual(progress.allocated, 1)
        self.assertEqual(progress.completed, 1)
        self.assertEqual(progress.remaining, 19)
        self.assertEqual(progress.activity_status_label, "Идёт")
        self.assertContains(response, "Назначено")
        self.assertContains(response, "Проведено")
        self.assertContains(response, "Осталось провести")
        self.assertContains(response, "Идёт")
        self.assertContains(response, "Неявка и списание не закрывают план")
        self.assertNotContains(response, "Приостановить можно только активную программу.")

    def test_recipient_detail_shows_progress_without_hiding_charged_count(self):
        response = self.client.get(reverse("recipient_detail", args=[self.child.pk]))

        self.assertEqual(response.status_code, 200)
        progress = response.context["programs"][0].blocks.all()[0].progress
        self.assertEqual(progress.allocated, 1)
        self.assertEqual(progress.completed, 0)
        self.assertEqual(progress.charged, 1)
        self.assertEqual(progress.activity_status_label, "Расписан")
        self.assertContains(response, 'data-label="Назначено"')
        self.assertContains(response, 'data-label="Проведено"')
        self.assertContains(response, 'data-label="Осталось провести"')
        self.assertContains(response, 'data-label="Списано"')

    def test_program_detail_paginates_append_only_history_by_ten(self):
        expected = 0
        for index in range(11):
            if index % 2 == 0:
                result = self._pause(
                    actor=self.director,
                    expected=expected,
                    reason=f"Пауза программы для страницы истории {index}.",
                )
            else:
                result = self._resume(
                    expected=expected,
                    reason=f"Возобновление программы для страницы истории {index}.",
                )
            expected = result.event.pk

        self.client.force_login(self.director)
        first = self.client.get(reverse("program_detail", args=[self.program.pk]))
        second = self.client.get(
            reverse("program_detail", args=[self.program.pk]), {"history_page": 2}
        )
        self.assertEqual(len(first.context["lifecycle_events"]), 10)
        self.assertEqual(len(second.context["lifecycle_events"]), 1)
        self.assertEqual(first.context["lifecycle_events"][0].event_number, 11)
        self.assertEqual(second.context["lifecycle_events"][0].event_number, 1)

    def test_paused_program_blocks_manual_create_move_and_attach(self):
        self._pause(expected=0)
        count = Appointment.objects.count()

        create_payload = self._appointment_payload(
            self.appointment,
            date=(self.day + timedelta(days=1)).isoformat(),
            time="12:00",
            admin_note="Новая запись на паузе.",
        )
        create = self.client.post(reverse("appointment_create"), create_payload)
        self.assertEqual(create.status_code, 200)
        self.assertTrue(create.context["form"].non_field_errors())
        self.assertEqual(Appointment.objects.count(), count)

        move = self.client.post(
            reverse("appointment_move", args=[self.appointment.pk]),
            {
                "date": (self.day + timedelta(days=1)).isoformat(),
                "time": "11:00",
                "duration_minutes": "45",
                "staff_member": str(self.staff.pk),
                "room": str(self.room.pk),
                "admin_note": "Попытка переноса на паузе.",
            },
        )
        self.assertEqual(move.status_code, 200)
        self.assertTrue(move.context["form"].non_field_errors())
        self.assertEqual(Appointment.objects.count(), count)

        unattached = Appointment.objects.create(
            child=self.child,
            staff_member=self.staff,
            service=self.service,
            room=self.room,
            starts_at=_local(self.day + timedelta(days=2), time(14, 0)),
            ends_at=_local(self.day + timedelta(days=2), time(14, 45)),
            status=Appointment.Status.PROPOSED,
        )
        participant = unattached.participants.get(child=self.child)
        attach = self.client.post(
            reverse("appointment_participant_program", args=[unattached.pk]),
            {"participant_id": str(participant.pk), "program_block": str(self.block.pk)},
        )
        self.assertEqual(attach.status_code, 400)
        bound_forms = [
            row["form"]
            for row in attach.context["participant_program_rows"]
            if row["form"].is_bound
        ]
        self.assertEqual(len(bound_forms), 1)
        self.assertTrue(bound_forms[0].non_field_errors())
        participant.refresh_from_db()
        self.assertIsNone(participant.program_block_id)

    def test_stale_noop_attach_cannot_overwrite_a_new_paused_program_link(self):
        from operations.forms import AppointmentParticipantProgramForm

        participant = self.appointment.participants.get(child=self.child)
        stale = AppointmentParticipantProgramForm(
            {
                "participant_id": str(participant.pk),
                "program_block": str(self.block.pk),
            },
            appointment=self.appointment,
            participant=participant,
        )
        self.assertTrue(stale.is_valid(), stale.errors)

        other_program = TreatmentProgram.objects.create(
            child=self.child,
            title="Concurrent relink target",
            status=TreatmentProgram.Status.ACTIVE,
        )
        other_block = ProgramBlock.objects.create(
            program=other_program,
            number=1,
            title="Concurrent relink block",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=3,
        )
        fresh = AppointmentParticipantProgramForm(
            {
                "participant_id": str(participant.pk),
                "program_block": str(other_block.pk),
            },
            appointment=self.appointment,
        )
        self.assertTrue(fresh.is_valid(), fresh.errors)
        fresh.save()
        program_lifecycle.pause_program(
            other_program,
            actor=self.administrator,
            reason="Конкурентно назначенная программа поставлена на паузу.",
            operation_key=uuid4(),
            expected_event_id=0,
        )

        with self.assertRaises(ValidationError):
            stale.save()
        participant.refresh_from_db()
        self.assertEqual(participant.program_block_id, other_block.pk)

    def test_move_checks_a_paused_secondary_group_participant_program(self):
        self._pause(expected=0)
        primary_child = Child.objects.create(
            last_name="Активная программа", first_name="Основной участник"
        )
        primary_program = TreatmentProgram.objects.create(
            child=primary_child,
            title="Активная программа основного участника",
            status=TreatmentProgram.Status.ACTIVE,
        )
        primary_block = ProgramBlock.objects.create(
            program=primary_program,
            number=1,
            title="Активный основной каскад",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=3,
        )
        starts_at = _local(self.day + timedelta(days=3), time(10, 0))
        group = Appointment.objects.create(
            child=primary_child,
            staff_member=self.staff,
            service=self.service,
            room=self.room,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
            status=Appointment.Status.PROPOSED,
            session_type=Appointment.SessionType.GROUP,
            program_block=primary_block,
        )
        AppointmentParticipant.objects.create(
            appointment=group,
            child=self.child,
            program_block=self.block,
            starts_at_snapshot=group.starts_at,
            ends_at_snapshot=group.ends_at,
            appointment_status=group.status,
        )
        count = Appointment.objects.count()

        response = self.client.post(
            reverse("appointment_move", args=[group.pk]),
            {
                "date": (self.day + timedelta(days=4)).isoformat(),
                "time": "11:00",
                "duration_minutes": "45",
                "staff_member": str(self.staff.pk),
                "room": str(self.room.pk),
                "admin_note": "Перенос группы со вторичной паузой.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        self.assertEqual(Appointment.objects.count(), count)

    def test_paused_program_does_not_block_note_or_attendance_updates(self):
        self._pause(expected=0)
        note = self.client.post(
            reverse("appointment_edit", args=[self.appointment.pk]),
            self._appointment_payload(
                self.appointment, admin_note="Операционная заметка во время паузы."
            ),
        )
        self.assertEqual(note.status_code, 302)
        self.appointment.refresh_from_db()
        self.assertEqual(
            self.appointment.admin_note, "Операционная заметка во время паузы."
        )

        participant = self.appointment.participants.get(child=self.child)
        attendance = self.client.post(
            reverse("appointment_attendance_decide", args=[self.appointment.pk]),
            {
                "action": "completed",
                "reason": "Занятие фактически проведено во время паузы программы.",
                "note": "Фактическая отметка не является новым назначением.",
                "operation_key": str(uuid4()),
                f"participant_status_{participant.pk}": (
                    Appointment.AttendanceStatus.ATTENDED
                ),
            },
        )
        self.assertEqual(attendance.status_code, 302)
        self.assertTrue(self.appointment.attendance_decisions.exists())


@skipUnless(connection.vendor == "postgresql", "DB guards проверяются на PostgreSQL.")
class ProgramLifecyclePostgreSQLTests(TransactionTestCase):
    def setUp(self):
        self.administrator = User.objects.create_user(
            "program-pg-administrator", password="x", is_staff=True
        )
        self.director = User.objects.create_superuser(
            "program-pg-director", password="x"
        )
        self.child = Child.objects.create(last_name="PG", first_name="Program")
        self.staff = StaffMember.objects.create(full_name="PG program staff")
        self.service = Service.objects.create(
            name="PG program service",
            code=f"PG-PROGRAM-{uuid4().hex[:8]}",
            default_duration_minutes=30,
        )
        self.room = Room.objects.create(name=f"PG program room {uuid4().hex[:8]}")
        self.program = TreatmentProgram.objects.create(
            child=self.child,
            title="PG managed program",
            status=TreatmentProgram.Status.ACTIVE,
        )
        self.block = ProgramBlock.objects.create(
            program=self.program,
            number=1,
            title="PG program block",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=3,
        )
        self.day = timezone.localdate() + timedelta(days=20)

    def _raw_status(self, status):
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operations_treatmentprogram SET status = %s WHERE id = %s",
                [status, self.program.pk],
            )

    def test_raw_sql_blocks_pause_bypass_and_all_projection_bypass_after_history(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            self._raw_status(TreatmentProgram.Status.PAUSED)
        self.program.refresh_from_db()
        self.assertEqual(self.program.status, TreatmentProgram.Status.ACTIVE)

        paused = program_lifecycle.pause_program(
            self.program,
            actor=self.administrator,
            reason="Корректная пауза перед raw SQL bypass.",
            operation_key=uuid4(),
            expected_event_id=0,
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            self._raw_status(TreatmentProgram.Status.COMPLETED)
        paused.program.refresh_from_db()
        self.assertEqual(paused.program.status, TreatmentProgram.Status.PAUSED)
        event = paused.event
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operations_treatmentprogramlifecycleevent "
                "SET reason = %s WHERE id = %s",
                ["Raw SQL cannot rewrite history.", event.pk],
            )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM operations_treatmentprogramlifecycleevent WHERE id = %s",
                [event.pk],
            )
        self.assertTrue(
            TreatmentProgramLifecycleEvent.objects.filter(pk=event.pk).exists()
        )

    def test_raw_sql_rejects_fake_role_and_deferred_projection_mismatch(self):
        operation_key = uuid4()
        probe = TreatmentProgramLifecycleEvent(
            program=self.program,
            event_type=TreatmentProgramLifecycleEvent.EventType.PAUSED,
            event_number=1,
            status_from=TreatmentProgram.Status.ACTIVE,
            status_to=TreatmentProgram.Status.PAUSED,
            actor=self.administrator,
            actor_role_snapshot=TreatmentProgramLifecycleEvent.ActorRole.DIRECTOR,
            reason="Raw SQL поддельная роль руководителя.",
            operation_key=operation_key,
            fingerprint="",
        )
        probe.fingerprint = canonical_fingerprint(probe.fingerprint_payload())
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO operations_treatmentprogramlifecycleevent (
                    created_at, updated_at, operation_key, fingerprint, event_type,
                    event_number, status_from, status_to, actor_role_snapshot,
                    reason, occurred_at, actor_id, program_id, supersedes_id
                ) VALUES (
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s, %s, %s, 1, %s, %s,
                    %s, %s, CURRENT_TIMESTAMP, %s, %s, NULL
                )
                """,
                [
                    str(operation_key),
                    probe.fingerprint,
                    probe.event_type,
                    probe.status_from,
                    probe.status_to,
                    probe.actor_role_snapshot,
                    probe.reason,
                    self.administrator.pk,
                    self.program.pk,
                ],
            )

        valid = TreatmentProgramLifecycleEvent(
            program=self.program,
            event_type=TreatmentProgramLifecycleEvent.EventType.PAUSED,
            event_number=1,
            status_from=TreatmentProgram.Status.ACTIVE,
            status_to=TreatmentProgram.Status.PAUSED,
            actor=self.administrator,
            actor_role_snapshot=TreatmentProgramLifecycleEvent.ActorRole.ADMINISTRATOR,
            reason="Событие без обновления проекции должно откатиться.",
            operation_key=uuid4(),
            fingerprint="",
        )
        valid.fingerprint = canonical_fingerprint(valid.fingerprint_payload())
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            valid.save()
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.assertFalse(self.program.lifecycle_events.exists())

    def test_postgresql_pause_wins_real_race_with_manual_scheduling(self):
        lock_acquired = Event()
        release_pause = Event()
        scheduling_at_program_lock = Event()
        outcomes = Queue()
        application_name = f"program-lifecycle-race-{uuid4().hex}"

        def pause_worker():
            close_old_connections()
            try:
                with transaction.atomic():
                    program = TreatmentProgram.objects.select_for_update().get(
                        pk=self.program.pk
                    )
                    result = program_lifecycle.pause_program(
                        program,
                        actor=User.objects.get(pk=self.administrator.pk),
                        reason="Пауза выигрывает гонку с ручным назначением.",
                        operation_key=uuid4(),
                        expected_event_id=0,
                    )
                    lock_acquired.set()
                    if not release_pause.wait(timeout=10):
                        raise TimeoutError("Scheduling thread did not reach the write gate.")
                outcomes.put(("pause", result.event.pk))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        def scheduling_worker():
            close_old_connections()
            try:
                if not lock_acquired.wait(timeout=10):
                    raise TimeoutError("Pause thread did not acquire the program lock.")
                from operations.forms import AppointmentForm
                from operations.services import program_scheduling

                with connection.cursor() as cursor:
                    cursor.execute("SET application_name = %s", [application_name])

                starts_at = _local(self.day, time(12, 0))
                form = AppointmentForm(
                    {
                        "session_type": Appointment.SessionType.INDIVIDUAL,
                        "child": str(self.child.pk),
                        "participants": [str(self.child.pk)],
                        "service": str(self.service.pk),
                        "staff_member": str(self.staff.pk),
                        "staff_members": [str(self.staff.pk)],
                        "room": str(self.room.pk),
                        "program_block": str(self.block.pk),
                        "billing_account": "",
                        "status": Appointment.Status.PROPOSED,
                        "admin_note": "Конкурентное назначение.",
                        "date": starts_at.date().isoformat(),
                        "time": starts_at.strftime("%H:%M"),
                        "duration_minutes": "30",
                    },
                    actor=User.objects.get(pk=self.administrator.pk),
                )
                if not form.is_valid():
                    raise AssertionError(form.errors.as_json())
                original_lock = program_scheduling.lock_program_blocks

                def observed_lock(block_ids):
                    scheduling_at_program_lock.set()
                    return original_lock(block_ids)

                with patch.object(
                    program_scheduling, "lock_program_blocks", observed_lock
                ):
                    try:
                        form.save()
                    except ValidationError as exc:
                        outcomes.put(("schedule_blocked", exc))
                    else:
                        outcomes.put(("schedule_created", None))
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        pause_thread = Thread(target=pause_worker)
        schedule_thread = Thread(target=scheduling_worker)
        pause_thread.start()
        self.assertTrue(lock_acquired.wait(timeout=10))
        schedule_thread.start()
        self.assertTrue(scheduling_at_program_lock.wait(timeout=10))
        deadline = monotonic() + 10
        blocked = False
        while monotonic() < deadline and not blocked:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT cardinality(pg_blocking_pids(pid)) > 0
                    FROM pg_stat_activity
                    WHERE application_name = %s
                    """,
                    [application_name],
                )
                row = cursor.fetchone()
            blocked = bool(row and row[0])
        self.assertTrue(blocked, "Scheduling writer never waited on the program root lock.")
        release_pause.set()
        pause_thread.join(timeout=15)
        schedule_thread.join(timeout=15)
        self.assertFalse(pause_thread.is_alive())
        self.assertFalse(schedule_thread.is_alive())
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertFalse(any(isinstance(item, BaseException) for item in results), results)
        self.assertEqual({item[0] for item in results}, {"pause", "schedule_blocked"})
        self.program.refresh_from_db()
        self.assertEqual(self.program.status, TreatmentProgram.Status.PAUSED)
        self.assertFalse(Appointment.objects.filter(admin_note="Конкурентное назначение.").exists())


class ProgramLifecycleMigrationTests(TransactionTestCase):
    migrate_from = [("operations", "0066_series_resume_stop_guard")]
    migrate_to = [("operations", "0067_program_pause_resume")]

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("operations"))
        super().tearDown()

    def _historical_program(self, apps, suffix, *, status="active"):
        user = apps.get_model("auth", "User").objects.create(
            username=f"program-migration-{suffix}",
            is_staff=True,
            is_superuser=True,
        )
        child = apps.get_model("operations", "Child").objects.create(
            last_name="Migration", first_name=suffix
        )
        program = apps.get_model("operations", "TreatmentProgram").objects.create(
            child_id=child.pk,
            title=f"Migration program {suffix}",
            status=status,
        )
        return user, program

    def _delete_events(self, *, reset_program_id=None):
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE operations_treatmentprogramlifecycleevent "
                    "DISABLE TRIGGER USER"
                )
                cursor.execute(
                    "ALTER TABLE operations_treatmentprogram "
                    "DISABLE TRIGGER program_status_guard"
                )
                try:
                    cursor.execute(
                        "DELETE FROM operations_treatmentprogramlifecycleevent"
                    )
                    if reset_program_id is not None:
                        cursor.execute(
                            "UPDATE operations_treatmentprogram SET status = %s "
                            "WHERE id = %s",
                            ["active", reset_program_id],
                        )
                finally:
                    cursor.execute(
                        "ALTER TABLE operations_treatmentprogramlifecycleevent "
                        "ENABLE TRIGGER USER"
                    )
                    cursor.execute(
                        "ALTER TABLE operations_treatmentprogram "
                        "ENABLE TRIGGER program_status_guard"
                    )
        else:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM operations_treatmentprogramlifecycleevent"
                )
                if reset_program_id is not None:
                    cursor.execute(
                        "UPDATE operations_treatmentprogram SET status = %s WHERE id = %s",
                        ["active", reset_program_id],
                    )

    def test_0067_forward_and_empty_reverse_roundtrip(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        self.assertIsNotNone(
            apps.get_model("operations", "TreatmentProgramLifecycleEvent")
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)

    def test_0067_reverse_is_blocked_after_history_without_deleting_it(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        user, program = self._historical_program(apps, uuid4().hex[:8])
        event_model = apps.get_model("operations", "TreatmentProgramLifecycleEvent")
        with transaction.atomic():
            event_model.objects.create(
                program_id=program.pk,
                operation_key=uuid4(),
                fingerprint="a" * 64,
                event_type="paused",
                event_number=1,
                status_from="active",
                status_to="paused",
                actor_id=user.pk,
                actor_role_snapshot="director",
                reason="History blocks schema reversal.",
            )
            apps.get_model("operations", "TreatmentProgram").objects.filter(
                pk=program.pk
            ).update(status="paused")

        executor = MigrationExecutor(connection)
        with self.assertRaises(RuntimeError):
            executor.migrate(self.migrate_from)
        self.assertEqual(event_model.objects.filter(program_id=program.pk).count(), 1)

        self._delete_events(reset_program_id=program.pk)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)

    def test_0067_preflight_rejects_unknown_status_without_rewriting_it(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        apps = executor.loader.project_state(self.migrate_from).apps
        _, program = self._historical_program(apps, uuid4().hex[:8])
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operations_treatmentprogram SET status = %s WHERE id = %s",
                ["unknown-legacy-state", program.pk],
            )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(RuntimeError, "Unknown treatment program statuses"):
            executor.migrate(self.migrate_to)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM operations_treatmentprogram WHERE id = %s",
                [program.pk],
            )
            self.assertEqual(cursor.fetchone()[0], "unknown-legacy-state")
            cursor.execute(
                "UPDATE operations_treatmentprogram SET status = %s WHERE id = %s",
                ["active", program.pk],
            )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
