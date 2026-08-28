from django.db import migrations


IMMUTABLE_TABLES = (
    "operations_appointmentseriesrevision",
    "operations_appointmentseriesrevisionparticipant",
    "operations_appointmentseriesrevisionstaffassignment",
    "operations_appointmentseriesmaterializationrun",
    "operations_appointmentseriesmaterializationrunevent",
    "operations_appointmentseriesmaterializationresult",
)


def install_series_revision_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return

    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION operations_block_series_history_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'appointment series history rows are immutable'
                USING DETAIL = TG_TABLE_NAME || '.' || OLD.id::text;
        END;
        $$;
        """
    )
    for table_name in IMMUTABLE_TABLES:
        trigger_name = f"{table_name}_immutable"
        schema_editor.execute(
            f"""
            CREATE TRIGGER {schema_editor.quote_name(trigger_name)}
            BEFORE UPDATE OR DELETE ON {schema_editor.quote_name(table_name)}
            FOR EACH ROW
            EXECUTE FUNCTION operations_block_series_history_mutation();
            """
        )

    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION operations_validate_series_revision_chain()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            previous_series_id bigint;
            previous_number integer;
        BEGIN
            IF NEW.supersedes_id IS NOT NULL THEN
                SELECT series_id, revision_number
                INTO previous_series_id, previous_number
                FROM operations_appointmentseriesrevision
                WHERE id = NEW.supersedes_id;
                IF NOT FOUND
                   OR previous_series_id <> NEW.series_id
                   OR previous_number + 1 <> NEW.revision_number THEN
                    RAISE EXCEPTION 'invalid appointment series revision chain';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER operations_series_revision_chain_guard
        BEFORE INSERT ON operations_appointmentseriesrevision
        FOR EACH ROW
        EXECUTE FUNCTION operations_validate_series_revision_chain();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION operations_validate_series_current_revision()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            revision_series_id bigint;
            successor_count integer;
            revision_count integer;
        BEGIN
            IF NEW.current_revision_id IS NULL THEN
                SELECT COUNT(*)
                INTO revision_count
                FROM operations_appointmentseriesrevision
                WHERE series_id = NEW.id;
                IF revision_count <> 0 THEN
                    RAISE EXCEPTION 'appointment series with revision history requires current revision';
                END IF;
                RETURN NEW;
            END IF;
            SELECT series_id
            INTO revision_series_id
            FROM operations_appointmentseriesrevision
            WHERE id = NEW.current_revision_id;
            SELECT COUNT(*)
            INTO successor_count
            FROM operations_appointmentseriesrevision
            WHERE supersedes_id = NEW.current_revision_id;
            IF revision_series_id IS DISTINCT FROM NEW.id OR successor_count <> 0 THEN
                RAISE EXCEPTION 'invalid current appointment series revision';
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER operations_series_current_revision_guard
        BEFORE INSERT OR UPDATE OF current_revision_id
        ON operations_appointmentseries
        FOR EACH ROW
        EXECUTE FUNCTION operations_validate_series_current_revision();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION operations_validate_series_revision_participant()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            revision_service_id bigint;
            block_child_id bigint;
            block_service_id bigint;
            account_child_id bigint;
            account_service_id bigint;
        BEGIN
            SELECT service_id
            INTO revision_service_id
            FROM operations_appointmentseriesrevision
            WHERE id = NEW.revision_id;
            IF NEW.program_block_id IS NOT NULL THEN
                SELECT program.child_id, block.service_id
                INTO block_child_id, block_service_id
                FROM operations_programblock AS block
                JOIN operations_treatmentprogram AS program
                  ON program.id = block.program_id
                WHERE block.id = NEW.program_block_id;
                IF block_child_id IS DISTINCT FROM NEW.child_id
                   OR block_service_id IS DISTINCT FROM revision_service_id THEN
                    RAISE EXCEPTION 'series revision program block scope mismatch';
                END IF;
            END IF;
            IF NEW.billing_account_id IS NOT NULL THEN
                SELECT child_id, service_id
                INTO account_child_id, account_service_id
                FROM operations_balanceaccount
                WHERE id = NEW.billing_account_id;
                IF account_child_id IS DISTINCT FROM NEW.child_id
                   OR (
                       account_service_id IS NOT NULL
                       AND account_service_id IS DISTINCT FROM revision_service_id
                   ) THEN
                    RAISE EXCEPTION 'series revision billing account scope mismatch';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER operations_series_revision_participant_guard
        BEFORE INSERT ON operations_appointmentseriesrevisionparticipant
        FOR EACH ROW
        EXECUTE FUNCTION operations_validate_series_revision_participant();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION operations_validate_series_revision_composition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            target_revision_id bigint;
            revision_mode varchar;
            revision_session_type varchar;
            revision_provenance varchar;
            revision_series_id bigint;
            current_revision_id bigint;
            participant_count integer;
            staff_count integer;
            primary_count integer;
        BEGIN
            IF TG_TABLE_NAME = 'operations_appointmentseriesrevision' THEN
                target_revision_id := NEW.id;
            ELSE
                target_revision_id := NEW.revision_id;
            END IF;
            SELECT materialization_mode, session_type, provenance_kind, series_id
            INTO revision_mode, revision_session_type, revision_provenance,
                 revision_series_id
            FROM operations_appointmentseriesrevision
            WHERE id = target_revision_id;
            SELECT COUNT(*)
            INTO participant_count
            FROM operations_appointmentseriesrevisionparticipant
            WHERE revision_id = target_revision_id;
            SELECT COUNT(*), COUNT(*) FILTER (WHERE role = 'primary')
            INTO staff_count, primary_count
            FROM operations_appointmentseriesrevisionstaffassignment
            WHERE revision_id = target_revision_id;

            IF revision_mode = 'join_existing' THEN
                IF participant_count <> 1 OR staff_count <> 0 THEN
                    RAISE EXCEPTION 'invalid join-existing revision composition';
                END IF;
            ELSIF revision_session_type = 'individual' THEN
                IF participant_count <> 1 OR staff_count <> 1 OR primary_count <> 1 THEN
                    RAISE EXCEPTION 'invalid individual revision composition';
                END IF;
            ELSIF (
                participant_count < 2
                AND revision_provenance = 'native'
            ) OR participant_count < 1 OR staff_count < 1 OR primary_count <> 1 THEN
                RAISE EXCEPTION 'invalid group revision composition';
            END IF;
            SELECT series.current_revision_id
            INTO current_revision_id
            FROM operations_appointmentseries AS series
            WHERE series.id = revision_series_id;
            IF current_revision_id IS DISTINCT FROM target_revision_id THEN
                RAISE EXCEPTION 'appointment series current revision does not match inserted revision';
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )
    for table_name in (
        "operations_appointmentseriesrevision",
        "operations_appointmentseriesrevisionparticipant",
        "operations_appointmentseriesrevisionstaffassignment",
    ):
        trigger_name = f"{table_name}_composition"
        schema_editor.execute(
            f"""
            CREATE CONSTRAINT TRIGGER {schema_editor.quote_name(trigger_name)}
            AFTER INSERT ON {schema_editor.quote_name(table_name)}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION operations_validate_series_revision_composition();
            """
        )

    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION operations_validate_series_run()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            revision_series_id bigint;
            revision_start_date date;
            revision_end_date date;
            revision_effective_from date;
            successor_effective_from date;
        BEGIN
            SELECT series_id, start_date, end_date, effective_from
            INTO revision_series_id, revision_start_date, revision_end_date,
                 revision_effective_from
            FROM operations_appointmentseriesrevision
            WHERE id = NEW.revision_id;
            IF revision_series_id IS DISTINCT FROM NEW.series_id THEN
                RAISE EXCEPTION 'series run revision belongs to another series';
            END IF;
            SELECT effective_from
            INTO successor_effective_from
            FROM operations_appointmentseriesrevision
            WHERE supersedes_id = NEW.revision_id;
            IF NEW.date_from < GREATEST(revision_start_date, revision_effective_from)
               OR NEW.date_to > revision_end_date
               OR (
                   successor_effective_from IS NOT NULL
                   AND NEW.date_to >= successor_effective_from
               ) THEN
                RAISE EXCEPTION 'series run range crosses revision boundary';
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER operations_series_run_guard
        BEFORE INSERT ON operations_appointmentseriesmaterializationrun
        FOR EACH ROW
        EXECUTE FUNCTION operations_validate_series_run();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION operations_validate_series_result()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            revision_series_id bigint;
            run_series_id bigint;
            run_revision_id bigint;
            run_expected_count integer;
            run_date_from date;
            run_date_to date;
            current_result_count integer;
            latest_run_event_type varchar;
            previous_series_id bigint;
            previous_revision_id bigint;
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
            SELECT series_id, start_date, end_date, effective_from
            INTO revision_series_id, revision_start_date, revision_end_date,
                 revision_effective_from
            FROM operations_appointmentseriesrevision
            WHERE id = NEW.revision_id;
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
                SELECT series_id, revision_id, scheduled_starts_at,
                       scheduled_date, attempt_number
                INTO previous_series_id, previous_revision_id, previous_start,
                     previous_date, previous_attempt
                FROM operations_appointmentseriesmaterializationresult
                WHERE id = NEW.supersedes_id;
                IF previous_series_id IS DISTINCT FROM NEW.series_id
                   OR previous_revision_id IS DISTINCT FROM NEW.revision_id
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

        CREATE TRIGGER operations_series_result_guard
        BEFORE INSERT ON operations_appointmentseriesmaterializationresult
        FOR EACH ROW
        EXECUTE FUNCTION operations_validate_series_result();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION operations_validate_series_run_event()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            previous_number integer;
            previous_type varchar;
            actual_total integer;
            actual_created integer;
            actual_joined integer;
            actual_skipped integer;
            actual_unchanged integer;
            expected_total integer;
        BEGIN
            SELECT expected_result_count
            INTO expected_total
            FROM operations_appointmentseriesmaterializationrun
            WHERE id = NEW.run_id
            FOR UPDATE;
            SELECT event_number, event_type
            INTO previous_number, previous_type
            FROM operations_appointmentseriesmaterializationrunevent
            WHERE run_id = NEW.run_id
            ORDER BY event_number DESC
            LIMIT 1;
            IF NOT FOUND THEN
                IF NEW.event_number <> 1 OR NEW.event_type = 'resumed' THEN
                    RAISE EXCEPTION 'invalid first series run event';
                END IF;
            ELSE
                IF previous_type = 'completed'
                   OR NEW.event_number <> previous_number + 1
                   OR (NEW.event_type = 'resumed' AND previous_type <> 'interrupted')
                   OR (
                       NEW.event_type IN ('interrupted', 'completed')
                       AND previous_type <> 'resumed'
                   ) THEN
                    RAISE EXCEPTION 'invalid series run event transition';
                END IF;
            END IF;

            IF NEW.event_type = 'completed' THEN
                SELECT
                    COUNT(*),
                    COUNT(*) FILTER (WHERE outcome = 'created'),
                    COUNT(*) FILTER (WHERE outcome = 'joined'),
                    COUNT(*) FILTER (WHERE outcome = 'skipped'),
                    COUNT(*) FILTER (WHERE outcome = 'unchanged')
                INTO
                    actual_total,
                    actual_created,
                    actual_joined,
                    actual_skipped,
                    actual_unchanged
                FROM operations_appointmentseriesmaterializationresult
                WHERE run_id = NEW.run_id;
                IF actual_total <> expected_total
                   OR NEW.result_count <> actual_total
                   OR NEW.created_count <> actual_created
                   OR NEW.joined_count <> actual_joined
                   OR NEW.skipped_count <> actual_skipped
                   OR NEW.unchanged_count <> actual_unchanged THEN
                    RAISE EXCEPTION 'series run completion counters do not match results';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER operations_series_run_event_guard
        BEFORE INSERT ON operations_appointmentseriesmaterializationrunevent
        FOR EACH ROW
        EXECUTE FUNCTION operations_validate_series_run_event();
        """
    )


def remove_series_revision_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return

    AppointmentSeriesRevision = apps.get_model(
        "operations", "AppointmentSeriesRevision"
    )
    AppointmentSeriesMaterializationRun = apps.get_model(
        "operations", "AppointmentSeriesMaterializationRun"
    )
    AppointmentSeriesMaterializationResult = apps.get_model(
        "operations", "AppointmentSeriesMaterializationResult"
    )
    has_native_history = (
        AppointmentSeriesRevision.objects.exclude(event_type="legacy_import").exists()
        or AppointmentSeriesRevision.objects.filter(revision_number__gt=1).exists()
        or AppointmentSeriesMaterializationRun.objects.exclude(
            mode="legacy_import"
        ).exists()
        or AppointmentSeriesMaterializationResult.objects.filter(
            compatibility_occurrence__isnull=True
        ).exists()
        or AppointmentSeriesMaterializationResult.objects.filter(
            attempt_number__gt=1
        ).exists()
    )
    if has_native_history:
        raise RuntimeError(
            "Cannot remove series revision guards while native history exists."
        )

    trigger_tables = (
        (
            "operations_series_revision_chain_guard",
            "operations_appointmentseriesrevision",
        ),
        (
            "operations_series_current_revision_guard",
            "operations_appointmentseries",
        ),
        (
            "operations_series_revision_participant_guard",
            "operations_appointmentseriesrevisionparticipant",
        ),
        ("operations_series_run_guard", "operations_appointmentseriesmaterializationrun"),
        (
            "operations_series_result_guard",
            "operations_appointmentseriesmaterializationresult",
        ),
        (
            "operations_series_run_event_guard",
            "operations_appointmentseriesmaterializationrunevent",
        ),
    )
    for trigger_name, table_name in trigger_tables:
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(trigger_name)} "
            f"ON {schema_editor.quote_name(table_name)}"
        )
    for table_name in (
        "operations_appointmentseriesrevision",
        "operations_appointmentseriesrevisionparticipant",
        "operations_appointmentseriesrevisionstaffassignment",
    ):
        trigger_name = f"{table_name}_composition"
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(trigger_name)} "
            f"ON {schema_editor.quote_name(table_name)}"
        )
    for table_name in IMMUTABLE_TABLES:
        trigger_name = f"{table_name}_immutable"
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {schema_editor.quote_name(trigger_name)} "
            f"ON {schema_editor.quote_name(table_name)}"
        )

    for function_name in (
        "operations_validate_series_run_event",
        "operations_validate_series_result",
        "operations_validate_series_run",
        "operations_validate_series_revision_composition",
        "operations_validate_series_revision_participant",
        "operations_validate_series_current_revision",
        "operations_validate_series_revision_chain",
        "operations_block_series_history_mutation",
    ):
        schema_editor.execute(f"DROP FUNCTION IF EXISTS {function_name}()")


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0058_backfill_series_revisions"),
    ]

    operations = [
        migrations.RunPython(
            install_series_revision_guards,
            remove_series_revision_guards,
        ),
    ]
