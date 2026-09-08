"""Only an explicit stop can be resumed; keep existing history immutable."""

from django.db import migrations


# Exact pre-0066 insert guard. Replacing the function preserves its trigger,
# owner and privileges; reverse restores the earlier semantics without edits
# to lifecycle history or the root projection.
PREVIOUS_GUARD_SQL = """
CREATE OR REPLACE FUNCTION operations_validate_series_lifecycle_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    series_status varchar;
    latest_event_id bigint;
    latest_event_number integer;
    latest_occurred_at timestamptz;
    latest_actor_role varchar;
    previous_series_id bigint;
    previous_event_type varchar;
    previous_status_to varchar;
BEGIN
    SELECT status
    INTO series_status
    FROM operations_appointmentseries
    WHERE id = NEW.series_id
    FOR UPDATE;
    IF NOT FOUND OR series_status IS DISTINCT FROM NEW.status_from THEN
        RAISE EXCEPTION 'series lifecycle event starts from stale status';
    END IF;

    SELECT id, event_number, occurred_at, actor_role_snapshot
    INTO latest_event_id, latest_event_number, latest_occurred_at, latest_actor_role
    FROM operations_appointmentserieslifecycleevent
    WHERE series_id = NEW.series_id
    ORDER BY event_number DESC, id DESC
    LIMIT 1;
    IF NEW.event_number IS DISTINCT FROM COALESCE(latest_event_number, 0) + 1 THEN
        RAISE EXCEPTION 'series lifecycle event number is not monotonic';
    END IF;
    IF latest_occurred_at IS NOT NULL
       AND NEW.occurred_at < latest_occurred_at THEN
        RAISE EXCEPTION 'series lifecycle event order is not monotonic';
    END IF;
    IF NEW.event_type <> 'resume_materialization'
       AND NEW.actor_role_snapshot = 'administrator'
       AND latest_actor_role = 'director' THEN
        RAISE EXCEPTION 'administrator cannot override director lifecycle decision';
    END IF;

    IF NEW.event_type = 'resume_materialization' THEN
        SELECT series_id, event_type, status_to
        INTO previous_series_id, previous_event_type, previous_status_to
        FROM operations_appointmentserieslifecycleevent
        WHERE id = NEW.supersedes_id;
        IF NOT FOUND
           OR previous_series_id IS DISTINCT FROM NEW.series_id
           OR previous_event_type = 'resume_materialization'
           OR previous_status_to <> 'cancelled'
           OR latest_event_id IS DISTINCT FROM NEW.supersedes_id THEN
            RAISE EXCEPTION 'invalid series lifecycle supersedes chain';
        END IF;
    END IF;

    IF char_length(NEW.fingerprint) <> 64 THEN
        RAISE EXCEPTION 'invalid series lifecycle fingerprint';
    END IF;
    RETURN NEW;
END;
$$;
"""

STOP_ONLY_GUARD_SQL = PREVIOUS_GUARD_SQL.replace(
    "previous_event_type = 'resume_materialization'",
    "previous_event_type IS DISTINCT FROM 'stop_materialization'",
)


def install_stop_only_resume_guard(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor == "postgresql":
        # Block inserts through the old function between the audit and the
        # replacement. The migration transaction holds this lock to commit.
        schema_editor.execute(
            "LOCK TABLE operations_appointmentserieslifecycleevent "
            "IN SHARE ROW EXCLUSIVE MODE"
        )
    event_model = apps.get_model("operations", "AppointmentSeriesLifecycleEvent")
    invalid_ids = list(
        event_model.objects.using(connection.alias)
        .filter(event_type="resume_materialization")
        .exclude(supersedes__event_type="stop_materialization")
        .order_by("pk")
        .values_list("pk", flat=True)[:10]
    )
    if invalid_ids:
        raise RuntimeError(
            "Cannot install stop-only resume guard: lifecycle history contains "
            f"resume events without an explicit stop predecessor (IDs {invalid_ids}). "
            "Review the immutable history before deployment; no events were changed."
        )
    if connection.vendor == "postgresql":
        schema_editor.execute(STOP_ONLY_GUARD_SQL)


def restore_previous_resume_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(PREVIOUS_GUARD_SQL)


class Migration(migrations.Migration):
    dependencies = [("operations", "0065_appointment_operator_decisions")]

    operations = [
        migrations.RunPython(
            install_stop_only_resume_guard,
            restore_previous_resume_guard,
        ),
    ]
