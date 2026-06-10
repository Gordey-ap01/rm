from django.db import migrations
from django.db.migrations.operations.special import SeparateDatabaseAndState


ACTIVE_STATUSES = ("proposed", "confirmed", "completed", "reserved")


def create_pg_constraints(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        cursor.execute(
            """
            ALTER TABLE operations_appointment
            ADD CONSTRAINT no_child_overlap_active_appointments
            EXCLUDE USING gist (
                child_id WITH =,
                tstzrange(starts_at, ends_at, '[)') WITH &&
            )
            WHERE (status IN ('proposed', 'confirmed', 'completed', 'reserved'))
            """
        )
        cursor.execute(
            """
            ALTER TABLE operations_appointment
            ADD CONSTRAINT no_staff_overlap_active_appointments
            EXCLUDE USING gist (
                staff_member_id WITH =,
                tstzrange(starts_at, ends_at, '[)') WITH &&
            )
            WHERE (status IN ('proposed', 'confirmed', 'completed', 'reserved'))
            """
        )
        cursor.execute(
            """
            ALTER TABLE operations_appointment
            ADD CONSTRAINT no_room_overlap_active_appointments
            EXCLUDE USING gist (
                room_id WITH =,
                tstzrange(starts_at, ends_at, '[)') WITH &&
            )
            WHERE (room_id IS NOT NULL AND status IN ('proposed', 'confirmed', 'completed', 'reserved'))
            """
        )


def drop_pg_constraints(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for name in (
            "no_child_overlap_active_appointments",
            "no_staff_overlap_active_appointments",
            "no_room_overlap_active_appointments",
        ):
            cursor.execute(
                "ALTER TABLE operations_appointment DROP CONSTRAINT IF EXISTS %s" % name
            )


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0003_child_email_child_phone_appointmentconfirmation_and_more"),
    ]

    operations = [
        SeparateDatabaseAndState(
            state_operations=[],
            database_operations=[
                migrations.RunPython(create_pg_constraints, drop_pg_constraints),
            ],
        ),
    ]
