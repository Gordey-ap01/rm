from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from operations.models import ProgramBlock
from operations.services import program_block_lifecycle


class ProgramBlockLifecycleMigrationTests(TransactionTestCase):
    before = [("operations", "0068_program_activation_completion")]
    after = [("operations", "0069_program_block_lifecycle")]

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _block(self, apps, status="planned"):
        child = apps.get_model("operations", "Child").objects.create(
            last_name="Migration", first_name="Block"
        )
        service = apps.get_model("operations", "Service").objects.create(
            name="Migration service", code=f"block-{uuid4().hex[:8]}",
            default_duration_minutes=30,
        )
        program = apps.get_model("operations", "TreatmentProgram").objects.create(
            child_id=child.pk, title="Migration program", status="active",
        )
        return apps.get_model("operations", "ProgramBlock").objects.create(
            program_id=program.pk, number=1, title="Migration block",
            service_id=service.pk, planned_sessions=2, status=status,
        )

    def test_empty_round_trip_preserves_legacy_terminal_status_without_history(self):
        old = self._migrate(self.before)
        block = self._block(old, status="completed")
        new = self._migrate(self.after)
        self.assertEqual(new.get_model("operations", "ProgramBlock").objects.get(pk=block.pk).status, "completed")
        self.assertFalse(new.get_model("operations", "ProgramBlockLifecycleEvent").objects.exists())
        restored = self._migrate(self.before)
        self.assertEqual(restored.get_model("operations", "ProgramBlock").objects.get(pk=block.pk).status, "completed")
        self._migrate(self.after)

    def test_preflight_reports_unknown_status_without_rewriting_it(self):
        old = self._migrate(self.before)
        block = self._block(old, status="unknown-legacy-status")
        try:
            with self.assertRaisesMessage(RuntimeError, "Unknown program block statuses"):
                self._migrate(self.after)
            block.refresh_from_db()
            self.assertEqual(block.status, "unknown-legacy-status")
        finally:
            type(block).objects.filter(pk=block.pk).update(status="planned")
            self._migrate(self.after)

    def test_reverse_refuses_to_discard_an_accepted_terminal_decision(self):
        apps = self._migrate(self.after)
        block = ProgramBlock.objects.get(pk=self._block(apps).pk)
        director = get_user_model().objects.create_user(
            f"block-migration-{uuid4().hex[:8]}", is_staff=True, is_superuser=True,
        )
        result = program_block_lifecycle.cancel_block(
            block, actor=director, reason="Сохранить историю закрытия каскада.",
            operation_key=uuid4(), expected_event_id=0,
            expected_review_fingerprint=program_block_lifecycle.get_program_block_lifecycle_review(block).fingerprint,
        )
        with self.assertRaisesMessage(RuntimeError, "Cannot reverse block lifecycle migration"):
            self._migrate(self.before)
        self.assertTrue(type(result.event).objects.filter(pk=result.event.pk).exists())
