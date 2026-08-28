from django.db import migrations
from django.db.models import F


def _result_guard_sql(*, allow_cross_revision: bool) -> str:
    revision_guard = (
        "previous_revision_number > target_revision_number"
        if allow_cross_revision
        else "previous_revision_id IS DISTINCT FROM NEW.revision_id"
    )
    return f"""
        CREATE OR REPLACE FUNCTION operations_validate_series_result()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            revision_series_id bigint;
            target_revision_number integer;
            run_series_id bigint;
            run_revision_id bigint;
            run_expected_count integer;
            run_date_from date;
            run_date_to date;
            current_result_count integer;
            latest_run_event_type varchar;
            previous_series_id bigint;
            previous_revision_id bigint;
            previous_revision_number integer;
            previous_start timestamptz;
            previous_date date;
            previous_attempt integer;
            revision_start_date date;
            revision_end_date date;
            revision_effective_from date;
            occurrence_series_id bigint;
            occurrence_start timestamptz;
            occurrence_appointment_id bigint;
            occurrence_participant_id bigint;
            occurrence_outcome varchar;
            occurrence_reason_code varchar;
            occurrence_reason text;
            participant_appointment_id bigint;
        BEGIN
            SELECT revision.series_id, revision.revision_number,
                   revision.start_date, revision.end_date, revision.effective_from
            INTO revision_series_id, target_revision_number, revision_start_date,
                 revision_end_date, revision_effective_from
            FROM operations_appointmentseriesrevision AS revision
            WHERE revision.id = NEW.revision_id;
            SELECT series_id, revision_id, expected_result_count, date_from, date_to
            INTO run_series_id, run_revision_id, run_expected_count,
                 run_date_from, run_date_to
            FROM operations_appointmentseriesmaterializationrun
            WHERE id = NEW.run_id
            FOR UPDATE;
            IF revision_series_id IS DISTINCT FROM NEW.series_id
               OR run_series_id IS DISTINCT FROM NEW.series_id
               OR run_revision_id IS DISTINCT FROM NEW.revision_id THEN
                RAISE EXCEPTION 'series result root mismatch';
            END IF;
            IF NEW.scheduled_date IS DISTINCT FROM (
                NEW.scheduled_starts_at AT TIME ZONE 'Asia/Vladivostok'
            )::date THEN
                RAISE EXCEPTION 'series result local date does not match scheduled start';
            END IF;
            IF NEW.scheduled_date < run_date_from
               OR NEW.scheduled_date > run_date_to
               OR NEW.scheduled_date < GREATEST(
                   revision_start_date,
                   revision_effective_from
               )
               OR NEW.scheduled_date > revision_end_date THEN
                RAISE EXCEPTION 'series result date is outside run or revision range';
            END IF;
            SELECT event_type
            INTO latest_run_event_type
            FROM operations_appointmentseriesmaterializationrunevent
            WHERE run_id = NEW.run_id
            ORDER BY event_number DESC
            LIMIT 1;
            IF latest_run_event_type = 'completed' THEN
                RAISE EXCEPTION 'completed series run does not accept results';
            ELSIF latest_run_event_type = 'interrupted' THEN
                RAISE EXCEPTION 'interrupted series run must be resumed before results';
            END IF;
            SELECT COUNT(*)
            INTO current_result_count
            FROM operations_appointmentseriesmaterializationresult
            WHERE run_id = NEW.run_id;
            IF current_result_count >= run_expected_count THEN
                RAISE EXCEPTION 'series run result count exceeds expected count';
            END IF;

            IF NEW.supersedes_id IS NOT NULL THEN
                SELECT result.series_id, result.revision_id,
                       previous_revision.revision_number,
                       result.scheduled_starts_at, result.scheduled_date,
                       result.attempt_number
                INTO previous_series_id, previous_revision_id,
                     previous_revision_number, previous_start, previous_date,
                     previous_attempt
                FROM operations_appointmentseriesmaterializationresult AS result
                JOIN operations_appointmentseriesrevision AS previous_revision
                  ON previous_revision.id = result.revision_id
                WHERE result.id = NEW.supersedes_id;
                IF previous_series_id IS DISTINCT FROM NEW.series_id
                   OR {revision_guard}
                   OR previous_start IS DISTINCT FROM NEW.scheduled_starts_at
                   OR previous_date IS DISTINCT FROM NEW.scheduled_date
                   OR previous_attempt + 1 <> NEW.attempt_number THEN
                    RAISE EXCEPTION 'invalid series result attempt chain';
                END IF;
            END IF;

            IF NEW.compatibility_occurrence_id IS NOT NULL THEN
                SELECT
                    series_id,
                    scheduled_starts_at,
                    appointment_id,
                    appointment_participant_id,
                    outcome,
                    reason_code,
                    reason
                INTO
                    occurrence_series_id,
                    occurrence_start,
                    occurrence_appointment_id,
                    occurrence_participant_id,
                    occurrence_outcome,
                    occurrence_reason_code,
                    occurrence_reason
                FROM operations_appointmentseriesoccurrence
                WHERE id = NEW.compatibility_occurrence_id;
                IF occurrence_series_id IS DISTINCT FROM NEW.series_id
                   OR occurrence_start IS DISTINCT FROM NEW.scheduled_starts_at
                   OR occurrence_appointment_id IS DISTINCT FROM NEW.appointment_id
                   OR occurrence_participant_id IS DISTINCT FROM NEW.appointment_participant_id
                   OR occurrence_outcome IS DISTINCT FROM NEW.outcome
                   OR occurrence_reason_code IS DISTINCT FROM NEW.reason_code
                   OR occurrence_reason IS DISTINCT FROM NEW.reason THEN
                    RAISE EXCEPTION 'compatibility occurrence does not match series result';
                END IF;
            END IF;

            IF NEW.appointment_participant_id IS NOT NULL THEN
                SELECT appointment_id
                INTO participant_appointment_id
                FROM operations_appointmentparticipant
                WHERE id = NEW.appointment_participant_id;
                IF participant_appointment_id IS DISTINCT FROM NEW.appointment_id THEN
                    RAISE EXCEPTION 'series result participant belongs to another appointment';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
    """


def allow_cross_revision_attempts(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(_result_guard_sql(allow_cross_revision=True))


def restore_same_revision_attempts(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return

    schema_editor.execute(
        "LOCK TABLE operations_appointmentseriesmaterializationresult "
        "IN ACCESS EXCLUSIVE MODE"
    )
    Result = apps.get_model("operations", "AppointmentSeriesMaterializationResult")
    if (
        Result.objects.filter(supersedes__isnull=False)
        .exclude(revision_id=F("supersedes__revision_id"))
        .exists()
    ):
        raise RuntimeError(
            "Cannot restore same-revision attempt guards while cross-revision "
            "series results exist."
        )
    schema_editor.execute(_result_guard_sql(allow_cross_revision=False))


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0059_series_revision_guards"),
    ]

    operations = [
        migrations.RunPython(
            allow_cross_revision_attempts,
            restore_same_revision_attempts,
        ),
    ]
