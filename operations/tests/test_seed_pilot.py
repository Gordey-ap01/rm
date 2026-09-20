from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from operations.management.commands import seed_pilot
from operations.models import (
    Appointment,
    BalanceAccount,
    Child,
    LedgerEntry,
    ProgramBlock,
    StaffMember,
    TreatmentProgram,
)
from operations.services.authority import AuthorityRole, authority_role

User = get_user_model()


class SeedPilotGuardTests(SimpleTestCase):
    def test_requires_explicit_pilot_mode_before_database_access(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesMessage(CommandError, "RM_PILOT_MODE=1"),
        ):
            seed_pilot.Command()._validate_environment()

    def test_refuses_non_postgresql_database(self):
        fake_connection = SimpleNamespace(vendor="sqlite")
        with (
            patch.dict(os.environ, {"RM_PILOT_MODE": "1"}, clear=True),
            patch.object(seed_pilot, "connection", fake_connection),
            self.assertRaisesMessage(CommandError, "только для PostgreSQL"),
        ):
            seed_pilot.Command()._validate_environment()

    def test_refuses_wrong_configured_database_name(self):
        fake_connection = SimpleNamespace(
            vendor="postgresql",
            settings_dict={"NAME": "another_database"},
        )
        with (
            patch.dict(os.environ, {"RM_PILOT_MODE": "1"}, clear=True),
            patch.object(seed_pilot, "connection", fake_connection),
            self.assertRaisesMessage(CommandError, "rm_pilot_training"),
        ):
            seed_pilot.Command()._validate_environment()

    def test_refuses_when_actual_database_name_does_not_match(self):
        cursor = Mock()
        cursor.fetchone.return_value = ("another_database",)
        context = Mock()
        context.__enter__ = Mock(return_value=cursor)
        context.__exit__ = Mock(return_value=False)
        fake_connection = SimpleNamespace(
            vendor="postgresql",
            settings_dict={"NAME": seed_pilot.PILOT_DATABASE_NAME},
            cursor=Mock(return_value=context),
        )
        with (
            patch.dict(os.environ, {"RM_PILOT_MODE": "1"}, clear=True),
            patch.object(seed_pilot, "connection", fake_connection),
            self.assertRaisesMessage(CommandError, "Фактически подключенная"),
        ):
            seed_pilot.Command()._validate_environment()

    def test_requires_strict_iso_date(self):
        with self.assertRaisesMessage(CommandError, "YYYY-MM-DD"):
            call_command("seed_pilot", date="14.09.2026")


class SeedPilotCommandTests(TestCase):
    def setUp(self):
        self.training_date = timezone.localdate() + timedelta(days=7)
        self.environment = patch.dict(
            os.environ,
            {"RM_PILOT_MODE": "1", "RM_PILOT_PASSWORD": "pilot-first-password"},
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

        environment_guard = patch.object(seed_pilot.Command, "_validate_environment")
        advisory_lock = patch.object(seed_pilot.Command, "_acquire_initialization_lock")
        self.mock_environment_guard = environment_guard.start()
        self.mock_advisory_lock = advisory_lock.start()
        self.addCleanup(environment_guard.stop)
        self.addCleanup(advisory_lock.stop)

    def seed(self):
        output = StringIO()
        call_command(
            "seed_pilot",
            date=self.training_date.isoformat(),
            stdout=output,
        )
        return output.getvalue()

    def test_first_initialization_creates_roles_and_unmarked_group_baseline(self):
        output = self.seed()

        self.assertIn(self.training_date.isoformat(), output)
        self.mock_environment_guard.assert_called_once_with()
        self.mock_advisory_lock.assert_called_once_with()

        administrator = User.objects.get(username="admin")
        director = User.objects.get(username="director")
        specialist_users = list(
            User.objects.filter(username__startswith="specialist").order_by("username")
        )
        self.assertEqual(authority_role(administrator), AuthorityRole.ADMINISTRATOR)
        self.assertTrue(administrator.is_staff)
        self.assertFalse(administrator.is_superuser)
        self.assertEqual(authority_role(director), AuthorityRole.DIRECTOR)
        self.assertFalse(director.is_staff)
        self.assertFalse(director.is_superuser)
        self.assertEqual(
            list(director.groups.values_list("name", flat=True)),
            [seed_pilot.DIRECTOR_GROUP],
        )
        self.assertEqual(len(specialist_users), 2)
        for specialist_user in specialist_users:
            self.assertEqual(authority_role(specialist_user), AuthorityRole.SPECIALIST)
            self.assertEqual(specialist_user.staff_profile.user_id, specialist_user.pk)
            self.assertTrue(specialist_user.check_password("pilot-first-password"))
        self.assertTrue(administrator.check_password("pilot-first-password"))
        self.assertTrue(director.check_password("pilot-first-password"))
        self.assertFalse(User.objects.exclude(email="").exists())
        self.assertEqual(StaffMember.objects.count(), 2)

        marker = Group.objects.get(name=seed_pilot.PILOT_MARKER_GROUP)
        self.assertFalse(marker.user_set.exists())
        self.assertEqual(Child.objects.count(), 2)
        self.assertFalse(Child.objects.exclude(email="").exists())
        self.assertEqual(BalanceAccount.objects.count(), 2)
        self.assertEqual(TreatmentProgram.objects.count(), 2)
        self.assertEqual(ProgramBlock.objects.count(), 2)
        self.assertEqual(
            set(ProgramBlock.objects.values_list("status", flat=True)),
            {ProgramBlock.Status.SCHEDULED},
        )

        appointment = Appointment.objects.get()
        self.assertEqual(appointment.starts_at.date(), self.training_date)
        self.assertEqual(appointment.session_type, Appointment.SessionType.GROUP)
        self.assertEqual(appointment.status, Appointment.Status.PROPOSED)
        self.assertEqual(appointment.attendance_status, Appointment.AttendanceStatus.UNKNOWN)
        self.assertEqual(appointment.billing_decision, Appointment.BillingDecision.UNDECIDED)
        self.assertIsNone(appointment.specialist_marked_at)
        self.assertEqual(appointment.participants.count(), 2)
        self.assertFalse(
            appointment.participants.exclude(
                attendance_status=Appointment.AttendanceStatus.UNKNOWN,
                billing_decision=Appointment.BillingDecision.UNDECIDED,
                marked_by_staff_at__isnull=True,
            ).exists()
        )
        self.assertFalse(LedgerEntry.objects.exists())

    def test_unknown_nonempty_database_is_refused_without_changes(self):
        outsider = User.objects.create_user("existing-user", password="keep-me")

        with self.assertRaisesMessage(CommandError, "уже содержит данные"):
            self.seed()

        outsider.refresh_from_db()
        self.assertTrue(outsider.check_password("keep-me"))
        self.assertFalse(Group.objects.filter(name=seed_pilot.PILOT_MARKER_GROUP).exists())
        self.assertFalse(
            User.objects.filter(
                username__in=["admin", "director", "specialist1", "specialist2"]
            ).exists()
        )
        self.assertFalse(Child.objects.exists())

    def test_environment_guard_runs_before_known_marker_check(self):
        Group.objects.create(name=seed_pilot.PILOT_MARKER_GROUP)
        self.mock_environment_guard.side_effect = CommandError("environment rejected")

        with self.assertRaisesMessage(CommandError, "environment rejected"):
            self.seed()

        self.mock_advisory_lock.assert_not_called()

    def test_repeat_preserves_user_changes_and_does_not_require_password(self):
        self.seed()
        child = Child.objects.order_by("pk").first()
        account = BalanceAccount.objects.order_by("pk").first()
        specialist = User.objects.get(username="specialist1")
        child.first_name = "Изменено оператором"
        child.save(update_fields=["first_name"])
        account.initial_amount = Decimal("7.00")
        account.save(update_fields=["initial_amount"])
        specialist.set_password("operator-password")
        specialist.save(update_fields=["password"])
        counts = {
            "users": User.objects.count(),
            "groups": Group.objects.count(),
            "children": Child.objects.count(),
            "appointments": Appointment.objects.count(),
        }
        os.environ.pop("RM_PILOT_PASSWORD")

        output = self.seed()

        child.refresh_from_db()
        account.refresh_from_db()
        specialist.refresh_from_db()
        self.assertIn("уже инициализирована", output)
        self.assertEqual(child.first_name, "Изменено оператором")
        self.assertEqual(account.initial_amount, Decimal("7.00"))
        self.assertTrue(specialist.check_password("operator-password"))
        self.assertEqual(
            counts,
            {
                "users": User.objects.count(),
                "groups": Group.objects.count(),
                "children": Child.objects.count(),
                "appointments": Appointment.objects.count(),
            },
        )

    def test_missing_first_password_leaves_database_empty(self):
        os.environ.pop("RM_PILOT_PASSWORD")

        with self.assertRaisesMessage(CommandError, "RM_PILOT_PASSWORD"):
            self.seed()

        self.assertFalse(User.objects.exists())
        self.assertFalse(Group.objects.exists())
        self.assertFalse(Child.objects.exists())

    def test_failure_rolls_back_marker_and_all_baseline_data(self):
        with (
            patch.object(
                seed_pilot.program_series,
                "create_group_series",
                side_effect=RuntimeError("synthetic failure"),
            ),
            self.assertRaisesMessage(RuntimeError, "synthetic failure"),
        ):
            self.seed()

        self.assertFalse(Group.objects.exists())
        self.assertFalse(User.objects.exists())
        self.assertFalse(Child.objects.exists())
        self.assertFalse(BalanceAccount.objects.exists())
        self.assertFalse(Appointment.objects.exists())
