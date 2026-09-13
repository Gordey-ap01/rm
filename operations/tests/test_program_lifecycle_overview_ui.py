from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    Appointment,
    Child,
    ProgramBlock,
    ProgramBlockLifecycleEvent,
    Service,
    StaffMember,
    TreatmentProgram,
    TreatmentProgramLifecycleEvent,
)
from operations.services import program_block_lifecycle, program_lifecycle

User = get_user_model()


def _local(day, clock):
    return timezone.make_aware(
        datetime.combine(day, clock), timezone.get_current_timezone()
    )


class ProgramLifecycleOverviewUiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            "overview-ui-admin", password="x", is_staff=True
        )
        cls.director = User.objects.create_superuser("overview-ui-director", password="x")
        cls.specialist = User.objects.create_user("overview-ui-specialist", password="x")
        cls.staff = StaffMember.objects.create(full_name="Специалист обзора UI")
        cls.service = Service.objects.create(
            name="Услуга обзора UI",
            code="LIFECYCLE-OVERVIEW-UI",
            default_duration_minutes=45,
            default_price=Decimal("1000"),
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def _program(self, suffix, *, status=TreatmentProgram.Status.ACTIVE):
        child = Child.objects.create(last_name="Обзор UI", first_name=suffix)
        return TreatmentProgram.objects.create(
            child=child,
            title=f"Программа {suffix}",
            status=status,
        )

    def _block(self, suffix, *, program=None, planned=1):
        program = program or self._program(suffix)
        return ProgramBlock.objects.create(
            program=program,
            number=program.blocks.count() + 1,
            title=f"Каскад {suffix}",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=planned,
        )

    def _completed_appointment(self, block, *, day_offset=1):
        starts_at = _local(
            timezone.localdate() + timedelta(days=day_offset), time(10, 0)
        )
        return Appointment.objects.create(
            child=block.program.child,
            staff_member=self.staff,
            service=self.service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
            status=Appointment.Status.COMPLETED,
            attendance_status=Appointment.AttendanceStatus.ATTENDED,
            program_block=block,
        )

    def _close_parent(self, block):
        review = program_lifecycle.get_program_lifecycle_review(block.program)
        return program_lifecycle.cancel_program(
            block.program,
            actor=self.director,
            reason="Закрываем программу для обзорной очереди.",
            operation_key=uuid4(),
            expected_event_id=0,
            expected_review_fingerprint=review.fingerprint,
        )

    def _block_action_payload(self, block):
        review = program_block_lifecycle.get_program_block_lifecycle_review(block)
        return {
            "operation_key": str(uuid4()),
            "expected_event_id": "0",
            "expected_review_fingerprint": review.fingerprint,
            "reason": "Подтвержденное решение из обзорного сценария.",
        }

    def _program_action_payload(self, expected):
        return {
            "operation_key": str(uuid4()),
            "expected_event_id": str(expected),
            "reason": "Подтвержденное решение из обзорного сценария.",
        }

    def _block_complete_url(self, block):
        return reverse("program_block_lifecycle_action", args=[block.pk, "complete"])

    def _lifecycle_section(self, response):
        content = response.content.decode()
        start = content.index('id="queue-program-lifecycle"')
        end = content.index("</article>", start) + len("</article>")
        return content[start:end]

    def test_admin_dashboard_and_queue_show_counts_links_and_leave_facts_unchanged(self):
        ready = self._block("готов")
        self._completed_appointment(ready)
        closed_parent = self._block("родитель закрыт")
        self._close_parent(closed_parent)
        paused = self._program("пауза", status=TreatmentProgram.Status.PAUSED)
        ready_program = self._program("готова")
        before = {
            "programs": list(TreatmentProgram.objects.order_by("pk").values("id", "status")),
            "blocks": list(ProgramBlock.objects.order_by("pk").values("id", "status")),
            "block_events": ProgramBlockLifecycleEvent.objects.count(),
            "program_events": TreatmentProgramLifecycleEvent.objects.count(),
            "appointments": Appointment.objects.count(),
        }

        dashboard = self.client.get(reverse("dashboard"))
        queue = self.client.get(reverse("work_queue"))

        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(queue.status_code, 200)
        self.assertContains(
            dashboard, f"{reverse('work_queue')}#queue-program-lifecycle"
        )
        for focus in ("block_ready", "parent_closed", "program_ready", "program_paused"):
            href = f"?lifecycle={focus}"
            self.assertContains(queue, href)
        for row in (ready, closed_parent, paused, ready_program):
            self.assertContains(queue, row.title)
        self.assertContains(queue, reverse("program_block_lifecycle_detail", args=[ready.pk]))
        self.assertContains(queue, reverse("program_detail", args=[ready_program.pk]))
        self.assertEqual(
            {
                "programs": list(TreatmentProgram.objects.order_by("pk").values("id", "status")),
                "blocks": list(ProgramBlock.objects.order_by("pk").values("id", "status")),
                "block_events": ProgramBlockLifecycleEvent.objects.count(),
                "program_events": TreatmentProgramLifecycleEvent.objects.count(),
                "appointments": Appointment.objects.count(),
            },
            before,
        )

    def test_next_action_includes_lifecycle_without_displacing_billing(self):
        self._program("только обзор")
        queue = self.client.get(reverse("work_queue"))
        self.assertEqual(
            queue.context["queue_next_action"]["href"], "#queue-program-lifecycle"
        )
        self.assertNotContains(queue, "Критичных задач нет")

        block = self._block("сначала решение списания")
        self._completed_appointment(block)
        queue = self.client.get(reverse("work_queue"))
        self.assertEqual(queue.context["queue_next_action"]["href"], "#queue-billing")

    def test_focus_filters_are_public_and_unknown_focus_falls_back_to_overview(self):
        ready = self._block("фокус готов")
        self._completed_appointment(ready)
        closed_parent = self._block("фокус родитель")
        self._close_parent(closed_parent)
        ready_program = self._program("фокус программа")
        paused = self._program("фокус пауза", status=TreatmentProgram.Status.PAUSED)

        cases = (
            ("block_ready", ready.title, closed_parent.title),
            ("parent_closed", closed_parent.title, ready.title),
            ("program_ready", ready_program.title, None),
            ("program_paused", paused.title, ready_program.title),
        )
        for focus, included, excluded in cases:
            response = self.client.get(reverse("work_queue"), {"lifecycle": focus})
            self.assertEqual(response.status_code, 200)
            # A fragment-only link would retain the current browser query and
            # silently keep the selected filter instead of restoring all rows.
            self.assertContains(
                response, f'href="{reverse("work_queue")}#queue-program-lifecycle"'
            )
            section = self._lifecycle_section(response)
            self.assertIn(included, section)
            if excluded:
                self.assertNotIn(excluded, section)

        fallback = self.client.get(reverse("work_queue"), {"lifecycle": "not-a-focus"})
        self.assertEqual(fallback.status_code, 200)
        self.assertContains(fallback, ready.title)
        self.assertContains(fallback, closed_parent.title)
        self.assertContains(fallback, ready_program.title)
        self.assertContains(fallback, paused.title)

    def test_queue_paginates_blocks_and_programs_independently_at_twenty_rows(self):
        blocks = []
        for index in range(21):
            block = self._block(f"страница-каскад-{index:02d}")
            self._completed_appointment(block, day_offset=index + 1)
            blocks.append(block)
        programs = [self._program(f"страница-программа-{index:02d}") for index in range(21)]

        first = self.client.get(
            reverse("work_queue"), {"block_page": 1, "program_page": 1}
        )
        second = self.client.get(
            reverse("work_queue"), {"block_page": 2, "program_page": 2}
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_section = self._lifecycle_section(first)
        second_section = self._lifecycle_section(second)
        self.assertIn(blocks[0].title, first_section)
        self.assertNotIn(blocks[-1].title, first_section)
        self.assertIn(blocks[-1].title, second_section)
        self.assertNotIn(blocks[0].title, second_section)
        self.assertIn(programs[0].title, first_section)
        self.assertNotIn(programs[-1].title, first_section)
        self.assertIn(programs[-1].title, second_section)
        self.assertNotIn(programs[0].title, second_section)
        self.assertContains(second, "block_page=1")
        self.assertContains(second, "program_page=1")

    def test_specialist_is_redirected_from_dashboard_and_forbidden_from_queue(self):
        self.client.force_login(self.specialist)

        dashboard = self.client.get(reverse("dashboard"))
        queue = self.client.get(reverse("work_queue"), {"lifecycle": "block_ready"})

        self.assertRedirects(
            dashboard, reverse("specialist_home"), fetch_redirect_response=False
        )
        self.assertEqual(queue.status_code, 302)
        self.assertIn(reverse("login"), queue.url)

    def test_http_block_actions_keep_admin_threshold_and_allow_director_early_completion(self):
        met_plan = self._block("HTTP выполнен")
        self._completed_appointment(met_plan)
        under_plan = self._block("HTTP досрочно", planned=2)

        self.assertEqual(
            self.client.post(
                self._block_complete_url(met_plan), self._block_action_payload(met_plan)
            ).status_code,
            302,
        )
        met_plan.refresh_from_db()
        self.assertEqual(met_plan.status, ProgramBlock.Status.COMPLETED)
        self.assertEqual(
            self.client.post(
                self._block_complete_url(under_plan), self._block_action_payload(under_plan)
            ).status_code,
            403,
        )
        under_plan.refresh_from_db()
        self.assertEqual(under_plan.status, ProgramBlock.Status.PLANNED)

        self.client.force_login(self.director)
        self.assertEqual(
            self.client.post(
                self._block_complete_url(under_plan), self._block_action_payload(under_plan)
            ).status_code,
            302,
        )
        under_plan.refresh_from_db()
        self.assertEqual(under_plan.status, ProgramBlock.Status.COMPLETED)

    def test_http_program_pause_resume_keeps_director_priority(self):
        program = self._program("HTTP приоритет")
        pause_url = reverse("program_lifecycle_action", args=[program.pk, "pause"])
        resume_url = reverse("program_lifecycle_action", args=[program.pk, "resume"])
        self.assertEqual(
            self.client.post(pause_url, self._program_action_payload(0)).status_code, 302
        )
        paused_event = program.lifecycle_events.get()
        self.assertEqual(
            self.client.post(resume_url, self._program_action_payload(paused_event.pk)).status_code,
            403,
        )

        self.client.force_login(self.director)
        self.assertEqual(
            self.client.post(resume_url, self._program_action_payload(paused_event.pk)).status_code,
            302,
        )
        program.refresh_from_db()
        director_event = program.lifecycle_events.latest("event_number")
        self.assertEqual(program.status, TreatmentProgram.Status.ACTIVE)
        self.assertEqual(
            self.client.post(
                pause_url, self._program_action_payload(director_event.pk)
            ).status_code,
            302,
        )
        self.client.force_login(self.admin)
        program.refresh_from_db()
        self.assertEqual(
            self.client.post(
                pause_url,
                self._program_action_payload(
                    program.lifecycle_events.latest("event_number").pk
                ),
            ).status_code,
            403,
        )
