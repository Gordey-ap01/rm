import hashlib
import json
from collections import defaultdict

from django.db import migrations
from django.db.models import Count, Q
from django.utils import timezone


BATCH_SIZE = 500
LEGACY_REASON = "Перенос существующей серии; автор исходного решения неизвестен."


def _fingerprint(payload):
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _batches(queryset):
    batch = []
    for item in queryset.iterator(chunk_size=BATCH_SIZE):
        batch.append(item)
        if len(batch) == BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


def _grouped_rows(queryset, key_name):
    grouped = defaultdict(list)
    for row in queryset.iterator(chunk_size=BATCH_SIZE):
        grouped[getattr(row, key_name)].append(row)
    return grouped


def _assert_backfill_sources(
    series_batch,
    participants_by_series,
    staff_by_series,
    occurrences_by_series,
    block_scope,
    account_scope,
):
    errors = []
    for series in series_batch:
        participants = participants_by_series.get(series.pk, [])
        assignments = staff_by_series.get(series.pk, [])
        if series.end_date < series.start_date:
            errors.append(f"series={series.pk}: end_date before start_date")
        if not participants:
            errors.append(f"series={series.pk}: no normalized participants")
        if series.materialization_mode == "create_appointments" and not assignments:
            errors.append(f"series={series.pk}: no normalized staff assignments")
        positions = [participant.position for participant in participants]
        if len(positions) != len(set(positions)):
            errors.append(f"series={series.pk}: duplicate participant positions")
        primary_count = sum(assignment.role == "primary" for assignment in assignments)
        if series.materialization_mode == "create_appointments" and primary_count != 1:
            errors.append(f"series={series.pk}: exactly one primary staff is required")
        if series.materialization_mode == "create_appointments" and series.session_type == "individual" and (
            len(participants) != 1 or len(assignments) != 1
        ):
            errors.append(
                f"series={series.pk}: individual composition must be one participant and staff"
            )
        if series.materialization_mode == "join_existing":
            if (
                series.session_type != "group"
                or series.allow_unpaid_reserve
                or len(participants) != 1
                or assignments
            ):
                errors.append(f"series={series.pk}: invalid join-existing configuration")
        if (
            series.allow_unpaid_reserve or series.allow_outside_availability
        ) and not series.override_reason.strip():
            errors.append(f"series={series.pk}: override reason is missing")

        for participant in participants:
            if participant.program_block_id:
                expected = block_scope.get(participant.program_block_id)
                if expected != (participant.child_id, series.service_id):
                    errors.append(
                        f"series={series.pk}: block={participant.program_block_id} scope mismatch"
                    )
            if participant.billing_account_id:
                account_child_id, account_service_id = account_scope.get(
                    participant.billing_account_id,
                    (None, None),
                )
                if account_child_id != participant.child_id or account_service_id not in {
                    None,
                    series.service_id,
                }:
                    errors.append(
                        f"series={series.pk}: account={participant.billing_account_id} scope mismatch"
                    )
        for assignment in assignments:
            if assignment.override_availability and not assignment.override_reason.strip():
                errors.append(
                    f"series={series.pk}: staff={assignment.staff_member_id} override reason missing"
                )
        for occurrence in occurrences_by_series.get(series.pk, []):
            scheduled_date = timezone.localtime(
                occurrence.scheduled_starts_at
            ).date()
            if not series.start_date <= scheduled_date <= series.end_date:
                errors.append(
                    f"series={series.pk}: occurrence={occurrence.pk} outside series range"
                )
        if len(errors) >= 20:
            break

    if errors:
        raise RuntimeError(
            "Series revision backfill preflight failed: " + "; ".join(errors[:20])
        )


def _assert_existing_revision_history(
    series_batch,
    AppointmentSeriesRevision,
    AppointmentSeriesMaterializationRun,
    AppointmentSeriesMaterializationRunEvent,
    AppointmentSeriesMaterializationResult,
    AppointmentSeriesOccurrence,
):
    series_ids = [series.pk for series in series_batch]
    errors = []
    revisions = list(
        AppointmentSeriesRevision.objects.filter(series_id__in=series_ids)
        .annotate(
            participant_count=Count("participants", distinct=True),
            staff_count=Count("staff_assignments", distinct=True),
            primary_count=Count(
                "staff_assignments",
                filter=Q(staff_assignments__role="primary"),
                distinct=True,
            ),
        )
        .order_by("series_id", "revision_number")
    )
    revision_by_id = {revision.pk: revision for revision in revisions}
    for series in series_batch:
        current = revision_by_id.get(series.current_revision_id)
        if current is None or current.series_id != series.pk:
            errors.append(f"series={series.pk}: invalid current revision")
    for revision in revisions:
        if revision.materialization_mode == "join_existing":
            valid = revision.participant_count == 1 and revision.staff_count == 0
        elif revision.session_type == "individual":
            valid = (
                revision.participant_count == 1
                and revision.staff_count == 1
                and revision.primary_count == 1
            )
        else:
            minimum = 2 if revision.provenance_kind == "native" else 1
            valid = (
                revision.participant_count >= minimum
                and revision.staff_count >= 1
                and revision.primary_count == 1
            )
        if not valid:
            errors.append(f"revision={revision.pk}: invalid composition")

    runs = list(
        AppointmentSeriesMaterializationRun.objects.filter(series_id__in=series_ids)
        .select_related("revision")
        .order_by("series_id", "pk")
    )
    run_by_id = {run.pk: run for run in runs}
    for run in runs:
        if run.revision.series_id != run.series_id:
            errors.append(f"run={run.pk}: revision root mismatch")
        if (
            run.date_from < max(run.revision.start_date, run.revision.effective_from)
            or run.date_to > run.revision.end_date
        ):
            errors.append(f"run={run.pk}: range outside revision")

    results = list(
        AppointmentSeriesMaterializationResult.objects.filter(
            series_id__in=series_ids
        )
        .select_related("revision", "run", "compatibility_occurrence")
        .order_by("run_id", "scheduled_starts_at")
    )
    result_by_occurrence = {
        result.compatibility_occurrence_id: result
        for result in results
        if result.compatibility_occurrence_id is not None
    }
    outcomes_by_run = defaultdict(lambda: defaultdict(int))
    for result in results:
        run = run_by_id.get(result.run_id)
        local_date = timezone.localtime(result.scheduled_starts_at).date()
        if (
            run is None
            or result.revision_id != run.revision_id
            or result.series_id != run.series_id
            or result.revision.series_id != result.series_id
        ):
            errors.append(f"result={result.pk}: root mismatch")
        elif not (
            run.date_from <= result.scheduled_date <= run.date_to
            and max(result.revision.start_date, result.revision.effective_from)
            <= result.scheduled_date
            <= result.revision.end_date
        ):
            errors.append(f"result={result.pk}: date outside run or revision")
        if result.scheduled_date != local_date:
            errors.append(f"result={result.pk}: incorrect local date")
        occurrence = result.compatibility_occurrence
        if occurrence is not None and (
            occurrence.series_id != result.series_id
            or occurrence.scheduled_starts_at != result.scheduled_starts_at
            or occurrence.appointment_id != result.appointment_id
            or occurrence.appointment_participant_id
            != result.appointment_participant_id
            or occurrence.outcome != result.outcome
            or occurrence.reason_code != result.reason_code
            or occurrence.reason != result.reason
        ):
            errors.append(f"result={result.pk}: compatibility occurrence mismatch")
        outcomes_by_run[result.run_id][result.outcome] += 1

    occurrences = AppointmentSeriesOccurrence.objects.filter(
        series_id__in=series_ids
    ).order_by("series_id", "scheduled_starts_at")
    for occurrence in occurrences.iterator(chunk_size=BATCH_SIZE):
        if occurrence.pk not in result_by_occurrence:
            errors.append(f"occurrence={occurrence.pk}: canonical result is missing")

    events = list(
        AppointmentSeriesMaterializationRunEvent.objects.filter(
            run__series_id__in=series_ids
        ).order_by("run_id", "event_number")
    )
    events_by_run = defaultdict(list)
    for event in events:
        events_by_run[event.run_id].append(event)
    for run in runs:
        previous_type = None
        for expected_number, event in enumerate(events_by_run[run.pk], start=1):
            valid_transition = (
                event.event_number == expected_number
                and previous_type != "completed"
                and not (event.event_type == "resumed" and previous_type != "interrupted")
                and not (
                    event.event_type in {"interrupted", "completed"}
                    and previous_type not in {None, "resumed"}
                )
            )
            if not valid_transition:
                errors.append(f"run={run.pk}: invalid event chain")
                break
            previous_type = event.event_type
            if event.event_type == "completed":
                outcomes = outcomes_by_run[run.pk]
                actual_total = sum(outcomes.values())
                if (
                    actual_total != run.expected_result_count
                    or event.result_count != actual_total
                    or event.created_count != outcomes["created"]
                    or event.joined_count != outcomes["joined"]
                    or event.skipped_count != outcomes["skipped"]
                    or event.unchanged_count != outcomes["unchanged"]
                ):
                    errors.append(f"run={run.pk}: completion counters mismatch")
        if len(errors) >= 20:
            break

    if errors:
        raise RuntimeError(
            "Series revision mixed-state preflight failed: "
            + "; ".join(errors[:20])
        )


def backfill_series_revisions(apps, schema_editor):
    AppointmentSeries = apps.get_model("operations", "AppointmentSeries")
    AppointmentSeriesParticipant = apps.get_model(
        "operations", "AppointmentSeriesParticipant"
    )
    AppointmentSeriesStaffAssignment = apps.get_model(
        "operations", "AppointmentSeriesStaffAssignment"
    )
    AppointmentSeriesOccurrence = apps.get_model(
        "operations", "AppointmentSeriesOccurrence"
    )
    AppointmentParticipant = apps.get_model("operations", "AppointmentParticipant")
    AppointmentStaffAssignment = apps.get_model(
        "operations", "AppointmentStaffAssignment"
    )
    AppointmentSeriesRevision = apps.get_model(
        "operations", "AppointmentSeriesRevision"
    )
    AppointmentSeriesRevisionParticipant = apps.get_model(
        "operations", "AppointmentSeriesRevisionParticipant"
    )
    AppointmentSeriesRevisionStaffAssignment = apps.get_model(
        "operations", "AppointmentSeriesRevisionStaffAssignment"
    )
    AppointmentSeriesMaterializationRun = apps.get_model(
        "operations", "AppointmentSeriesMaterializationRun"
    )
    AppointmentSeriesMaterializationRunEvent = apps.get_model(
        "operations", "AppointmentSeriesMaterializationRunEvent"
    )
    AppointmentSeriesMaterializationResult = apps.get_model(
        "operations", "AppointmentSeriesMaterializationResult"
    )
    ProgramBlock = apps.get_model("operations", "ProgramBlock")
    BalanceAccount = apps.get_model("operations", "BalanceAccount")

    invalid_existing = []
    existing_series = AppointmentSeries.objects.select_for_update().exclude(
        current_revision_id=None
    )
    for series_batch in _batches(existing_series.order_by("pk")):
        _assert_existing_revision_history(
            series_batch,
            AppointmentSeriesRevision,
            AppointmentSeriesMaterializationRun,
            AppointmentSeriesMaterializationRunEvent,
            AppointmentSeriesMaterializationResult,
            AppointmentSeriesOccurrence,
        )
    pending_series = AppointmentSeries.objects.select_for_update().filter(
        current_revision_id=None
    )
    if (
        AppointmentSeriesRevision.objects.filter(
            series__current_revision_id=None
        ).exists()
        or AppointmentSeriesMaterializationRun.objects.filter(
            series__current_revision_id=None
        ).exists()
        or AppointmentSeriesMaterializationResult.objects.filter(
            series__current_revision_id=None
        ).exists()
    ):
        invalid_existing.append("series without current revision already has revision history")
    if invalid_existing:
        raise RuntimeError(
            "Series revision mixed-state preflight failed: "
            + "; ".join(invalid_existing[:20])
        )

    for series_batch in _batches(pending_series.order_by("pk")):
        series_ids = [series.pk for series in series_batch]
        participants_by_series = _grouped_rows(
            AppointmentSeriesParticipant.objects.filter(
                series_id__in=series_ids
            ).order_by("series_id", "position", "pk"),
            "series_id",
        )
        staff_by_series = _grouped_rows(
            AppointmentSeriesStaffAssignment.objects.filter(
                series_id__in=series_ids
            ).order_by("series_id", "pk"),
            "series_id",
        )
        block_ids = {
            participant.program_block_id
            for participants in participants_by_series.values()
            for participant in participants
            if participant.program_block_id is not None
        }
        account_ids = {
            participant.billing_account_id
            for participants in participants_by_series.values()
            for participant in participants
            if participant.billing_account_id is not None
        }
        block_scope = {
            block_id: (child_id, service_id)
            for block_id, child_id, service_id in ProgramBlock.objects.filter(
                pk__in=block_ids
            ).values_list("pk", "program__child_id", "service_id")
        }
        account_scope = {
            account_id: (child_id, service_id)
            for account_id, child_id, service_id in BalanceAccount.objects.filter(
                pk__in=account_ids
            ).values_list("pk", "child_id", "service_id")
        }
        occurrences_by_series = _grouped_rows(
            AppointmentSeriesOccurrence.objects.filter(
                series_id__in=series_ids
            ).order_by("series_id", "scheduled_starts_at", "pk"),
            "series_id",
        )
        _assert_backfill_sources(
            series_batch,
            participants_by_series,
            staff_by_series,
            occurrences_by_series,
            block_scope,
            account_scope,
        )
        appointment_ids = {
            occurrence.appointment_id
            for occurrences in occurrences_by_series.values()
            for occurrence in occurrences
            if occurrence.appointment_id is not None
        }
        appointment_participants = list(
            AppointmentParticipant.objects.filter(
                appointment_id__in=appointment_ids
            ).order_by("appointment_id", "pk")
        )
        appointment_staff = list(
            AppointmentStaffAssignment.objects.filter(
                appointment_id__in=appointment_ids
            ).order_by("appointment_id", "pk")
        )
        participants_by_appointment = defaultdict(list)
        participants_by_id = {}
        for participant in appointment_participants:
            participants_by_appointment[participant.appointment_id].append(participant)
            participants_by_id[participant.pk] = participant
        staff_by_appointment = defaultdict(list)
        for assignment in appointment_staff:
            staff_by_appointment[assignment.appointment_id].append(assignment)

        revisions = []
        for series in series_batch:
            participants = participants_by_series.get(series.pk, [])
            assignments = staff_by_series.get(series.pk, [])
            payload = {
                "series_id": series.pk,
                "revision_number": 1,
                "title": series.title,
                "service_id": series.service_id,
                "room_id": series.room_id,
                "start_date": series.start_date,
                "end_date": series.end_date,
                "days_of_week": series.days_of_week,
                "time": series.time,
                "duration_minutes": series.duration_minutes,
                "session_type": series.session_type,
                "materialization_mode": series.materialization_mode,
                "default_appointment_status": series.default_appointment_status,
                "allow_unpaid_reserve": series.allow_unpaid_reserve,
                "allow_outside_availability": series.allow_outside_availability,
                "override_reason": series.override_reason,
                "participants": [
                    (
                        item.child_id,
                        item.program_block_id,
                        item.billing_account_id,
                        item.position,
                    )
                    for item in participants
                ],
                "staff": [
                    (
                        item.staff_member_id,
                        item.role,
                        item.override_availability,
                        item.override_reason,
                    )
                    for item in assignments
                ],
            }
            revisions.append(
                AppointmentSeriesRevision(
                    series_id=series.pk,
                    revision_number=1,
                    event_type="legacy_import",
                    provenance_kind="legacy_reconstructed",
                    effective_from=series.start_date,
                    title=series.title,
                    service_id=series.service_id,
                    room_id=series.room_id,
                    start_date=series.start_date,
                    end_date=series.end_date,
                    days_of_week=series.days_of_week,
                    time=series.time,
                    duration_minutes=series.duration_minutes,
                    session_type=series.session_type,
                    materialization_mode=series.materialization_mode,
                    default_appointment_status=series.default_appointment_status,
                    allow_unpaid_reserve=series.allow_unpaid_reserve,
                    allow_outside_availability=series.allow_outside_availability,
                    override_reason=series.override_reason,
                    fingerprint=_fingerprint(payload),
                    actor_id=None,
                    actor_role_snapshot="legacy",
                    reason=LEGACY_REASON,
                    supersedes_id=None,
                    decided_at=None,
                )
            )
        AppointmentSeriesRevision.objects.bulk_create(revisions, batch_size=BATCH_SIZE)
        revision_by_series = {revision.series_id: revision for revision in revisions}

        revision_participants = []
        revision_staff = []
        for series in series_batch:
            revision = revision_by_series[series.pk]
            revision_participants.extend(
                AppointmentSeriesRevisionParticipant(
                    revision_id=revision.pk,
                    child_id=item.child_id,
                    program_block_id=item.program_block_id,
                    billing_account_id=item.billing_account_id,
                    position=item.position,
                )
                for item in participants_by_series.get(series.pk, [])
            )
            revision_staff.extend(
                AppointmentSeriesRevisionStaffAssignment(
                    revision_id=revision.pk,
                    staff_member_id=item.staff_member_id,
                    role=item.role,
                    override_availability=item.override_availability,
                    override_reason=item.override_reason,
                )
                for item in staff_by_series.get(series.pk, [])
            )
        AppointmentSeriesRevisionParticipant.objects.bulk_create(
            revision_participants,
            batch_size=BATCH_SIZE,
        )
        AppointmentSeriesRevisionStaffAssignment.objects.bulk_create(
            revision_staff,
            batch_size=BATCH_SIZE,
        )

        runs = []
        for series in series_batch:
            revision = revision_by_series[series.pk]
            targets = sorted(
                occurrence.appointment_id
                for occurrence in occurrences_by_series.get(series.pk, [])
                if occurrence.appointment_id is not None
            )
            run_payload = {
                "series_id": series.pk,
                "revision_id": revision.pk,
                "revision_fingerprint": revision.fingerprint,
                "mode": "legacy_import",
                "date_from": series.start_date,
                "date_to": series.end_date,
                "actor_id": None,
                "reason": LEGACY_REASON,
                "target_appointment_ids": targets,
            }
            runs.append(
                AppointmentSeriesMaterializationRun(
                    series_id=series.pk,
                    revision_id=revision.pk,
                    operation_key=series.operation_key,
                    fingerprint=_fingerprint(run_payload),
                    mode="legacy_import",
                    date_from=series.start_date,
                    date_to=series.end_date,
                    expected_result_count=len(
                        occurrences_by_series.get(series.pk, [])
                    ),
                    actor_id=None,
                    actor_role_snapshot="legacy",
                    reason=LEGACY_REASON,
                )
            )
        AppointmentSeriesMaterializationRun.objects.bulk_create(
            runs,
            batch_size=BATCH_SIZE,
        )
        run_by_series = {run.series_id: run for run in runs}

        results = []
        events = []
        for series in series_batch:
            revision = revision_by_series[series.pk]
            run = run_by_series[series.pk]
            counts = defaultdict(int)
            occurrences = occurrences_by_series.get(series.pk, [])
            for occurrence in occurrences:
                counts[occurrence.outcome] += 1
                provenance_kind = "legacy_unknown"
                if occurrence.outcome == "created" and occurrence.appointment_id:
                    expected_participants = {
                        (
                            item.child_id,
                            item.program_block_id,
                            item.billing_account_id,
                        )
                        for item in participants_by_series.get(series.pk, [])
                    }
                    actual_participants = {
                        (
                            item.child_id,
                            item.program_block_id,
                            item.billing_account_id,
                        )
                        for item in participants_by_appointment.get(
                            occurrence.appointment_id,
                            [],
                        )
                    }
                    expected_staff = {
                        (
                            item.staff_member_id,
                            item.role,
                            item.override_availability,
                            item.override_reason,
                        )
                        for item in staff_by_series.get(series.pk, [])
                    }
                    actual_staff = {
                        (
                            item.staff_member_id,
                            item.role,
                            item.override_availability,
                            item.override_reason,
                        )
                        for item in staff_by_appointment.get(
                            occurrence.appointment_id,
                            [],
                        )
                    }
                    if (
                        expected_participants == actual_participants
                        and expected_staff == actual_staff
                    ):
                        provenance_kind = "legacy_reconstructed"
                elif occurrence.outcome == "joined":
                    actual_participant = participants_by_id.get(
                        occurrence.appointment_participant_id
                    )
                    expected = participants_by_series.get(series.pk, [])
                    if actual_participant and len(expected) == 1:
                        membership = expected[0]
                        if (
                            actual_participant.appointment_id
                            == occurrence.appointment_id
                            and actual_participant.child_id == membership.child_id
                            and actual_participant.program_block_id
                            == membership.program_block_id
                            and actual_participant.billing_account_id
                            == membership.billing_account_id
                        ):
                            provenance_kind = "legacy_reconstructed"
                results.append(
                    AppointmentSeriesMaterializationResult(
                        series_id=series.pk,
                        revision_id=revision.pk,
                        run_id=run.pk,
                        scheduled_starts_at=occurrence.scheduled_starts_at,
                        scheduled_date=timezone.localtime(
                            occurrence.scheduled_starts_at
                        ).date(),
                        attempt_number=1,
                        provenance_kind=provenance_kind,
                        appointment_id=occurrence.appointment_id,
                        appointment_participant_id=(
                            occurrence.appointment_participant_id
                        ),
                        outcome=occurrence.outcome,
                        reason_code=occurrence.reason_code,
                        reason=occurrence.reason,
                        supersedes_id=None,
                        compatibility_occurrence_id=occurrence.pk,
                    )
                )
            events.append(
                AppointmentSeriesMaterializationRunEvent(
                    run_id=run.pk,
                    event_number=1,
                    event_type="completed",
                    result_count=len(occurrences),
                    created_count=counts["created"],
                    joined_count=counts["joined"],
                    skipped_count=counts["skipped"],
                    unchanged_count=counts["unchanged"],
                    reason=LEGACY_REASON,
                )
            )
            series.current_revision_id = revision.pk
        AppointmentSeriesMaterializationResult.objects.bulk_create(
            results,
            batch_size=BATCH_SIZE,
        )
        AppointmentSeriesMaterializationRunEvent.objects.bulk_create(
            events,
            batch_size=BATCH_SIZE,
        )
        AppointmentSeries.objects.bulk_update(
            series_batch,
            ["current_revision"],
            batch_size=BATCH_SIZE,
        )


def remove_backfilled_series_revisions(apps, schema_editor):
    AppointmentSeries = apps.get_model("operations", "AppointmentSeries")
    AppointmentSeriesRevision = apps.get_model(
        "operations", "AppointmentSeriesRevision"
    )
    AppointmentSeriesRevisionParticipant = apps.get_model(
        "operations", "AppointmentSeriesRevisionParticipant"
    )
    AppointmentSeriesRevisionStaffAssignment = apps.get_model(
        "operations", "AppointmentSeriesRevisionStaffAssignment"
    )
    AppointmentSeriesMaterializationRun = apps.get_model(
        "operations", "AppointmentSeriesMaterializationRun"
    )
    AppointmentSeriesMaterializationRunEvent = apps.get_model(
        "operations", "AppointmentSeriesMaterializationRunEvent"
    )
    AppointmentSeriesMaterializationResult = apps.get_model(
        "operations", "AppointmentSeriesMaterializationResult"
    )

    has_new_history = (
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
    if has_new_history:
        raise RuntimeError(
            "Cannot reverse series revision backfill while new revision or run history exists."
        )

    AppointmentSeriesMaterializationRunEvent.objects.all().delete()
    AppointmentSeriesMaterializationResult.objects.all().delete()
    AppointmentSeriesMaterializationRun.objects.all().delete()
    AppointmentSeriesRevisionParticipant.objects.all().delete()
    AppointmentSeriesRevisionStaffAssignment.objects.all().delete()
    AppointmentSeries.objects.update(current_revision_id=None)
    AppointmentSeriesRevision.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0057_series_revision_expand"),
    ]

    operations = [
        migrations.RunPython(
            backfill_series_revisions,
            remove_backfilled_series_revisions,
        ),
    ]
