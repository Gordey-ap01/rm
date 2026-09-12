from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from operations.admin import ProgramBlockAdmin, ProgramBlockInline
from operations.forms import ProgramBlockForm
from operations.models import (
    Child,
    ProgramBlock,
    ProgramBlockLifecycleEvent,
    Service,
    TreatmentProgram,
)
from operations.services import program_block_lifecycle

User = get_user_model()


class ProgramBlockLifecycleUiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.administrator = User.objects.create_user(
            "program-block-ui-administrator", password="x", is_staff=True
        )
        cls.director = User.objects.create_superuser(
            "program-block-ui-director", password="x"
        )
        cls.specialist = User.objects.create_user(
            "program-block-ui-specialist", password="x"
        )
        cls.child = Child.objects.create(last_name="Каскад", first_name="Интерфейс")
        cls.service = Service.objects.create(
            name="Услуга каскада UI", code="BLOCK-UI", default_duration_minutes=30
        )

    def setUp(self):
        self.program = TreatmentProgram.objects.create(
            child=self.child,
            title="Программа интерфейса каскада",
            status=TreatmentProgram.Status.ACTIVE,
        )
        self.block = ProgramBlock.objects.create(
            program=self.program,
            number=1,
            title="Каскад интерфейса",
            service=self.service,
            planned_sessions=1,
        )
        self.client.force_login(self.administrator)

    def _detail_url(self, block=None):
        return reverse("program_block_lifecycle_detail", args=[(block or self.block).pk])

    def _action_url(self, action, block=None):
        return reverse(
            "program_block_lifecycle_action", args=[(block or self.block).pk, action]
        )

    def _payload(self, block=None, *, key=None, review=None):
        block = block or self.block
        review = review or program_block_lifecycle.get_program_block_lifecycle_review(block)
        latest = block.lifecycle_events.order_by("-pk").first()
        return {
            "operation_key": str(key or uuid4()),
            "expected_event_id": str(latest.pk if latest else 0),
            "expected_review_fingerprint": review.fingerprint,
            "reason": "Подтвержденное решение по каскаду программы.",
        }

    def test_detail_displays_read_only_review_and_block_action_links(self):
        before = {
            "blocks": ProgramBlock.objects.count(),
            "events": ProgramBlockLifecycleEvent.objects.count(),
        }

        response = self.client.get(self._detail_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Фактический прогресс")
        self.assertContains(
            response,
            "Проведено — занятия с фактическим посещением. Неявки и списания не закрывают план.",
        )
        self.assertContains(response, self._action_url("cancel"))
        self.assertEqual(
            {
                "blocks": ProgramBlock.objects.count(),
                "events": ProgramBlockLifecycleEvent.objects.count(),
            },
            before,
        )

    def test_specialist_cannot_read_or_submit_lifecycle_commands(self):
        self.client.force_login(self.specialist)

        self.assertEqual(self.client.get(self._detail_url()).status_code, 403)
        self.assertEqual(self.client.get(self._action_url("cancel")).status_code, 403)
        self.assertEqual(
            self.client.post(self._action_url("cancel"), self._payload()).status_code,
            403,
        )

    def test_cancel_post_redirects_to_program_detail(self):
        response = self.client.post(self._action_url("cancel"), self._payload())

        self.assertRedirects(response, reverse("program_detail", args=[self.program.pk]))
        self.block.refresh_from_db()
        self.assertEqual(self.block.status, ProgramBlock.Status.CANCELLED)
        self.assertEqual(self.block.lifecycle_events.count(), 1)

    def test_director_sees_early_completion_consequence_before_submitting(self):
        self.client.force_login(self.director)

        response = self.client.get(self._action_url("complete"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "План не выполнен")
        self.assertContains(response, "осталось провести 1.")
        self.assertContains(
            response,
            "После закрытия нельзя назначать новые занятия в этот каскад.",
        )
        self.assertContains(
            response,
            "Уже назначенные занятия сохраняются; проведение можно отметить позже.",
        )

    def test_stale_review_returns_conflict_with_unbound_reload_link(self):
        review = program_block_lifecycle.get_program_block_lifecycle_review(self.block)
        ProgramBlock.objects.filter(pk=self.block.pk).update(status=ProgramBlock.Status.SCHEDULED)

        response = self.client.post(
            self._action_url("complete"), self._payload(review=review)
        )

        self.assertEqual(response.status_code, 409)
        self.assertContains(
            response,
            "Состояние каскада изменилось после открытия формы",
            status_code=409,
        )
        self.assertContains(response, self._action_url("complete"), status_code=409)
        self.assertEqual(self.block.lifecycle_events.count(), 0)

    def test_replayed_terminal_post_is_accepted_after_the_block_is_closed(self):
        operation_key = uuid4()
        payload = self._payload(key=operation_key)
        first = self.client.post(self._action_url("cancel"), payload)
        self.assertRedirects(first, reverse("program_detail", args=[self.program.pk]))

        replay = self.client.post(self._action_url("cancel"), payload)

        self.assertRedirects(replay, reverse("program_detail", args=[self.program.pk]))
        self.assertEqual(
            ProgramBlockLifecycleEvent.objects.filter(block=self.block).count(), 1
        )

    def test_block_status_is_read_only_on_existing_form_and_planned_on_new_form(self):
        existing = ProgramBlockForm(instance=self.block)
        new = ProgramBlockForm()

        self.assertTrue(existing.fields["status"].disabled)
        self.assertEqual(
            list(new.fields["status"].choices),
            [(ProgramBlock.Status.PLANNED, ProgramBlock.Status.PLANNED.label)],
        )
        self.assertIs(ProgramBlockAdmin.form, ProgramBlockForm)
        self.assertIs(ProgramBlockInline.form, ProgramBlockForm)
