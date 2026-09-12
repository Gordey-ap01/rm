"""Locked availability checks for writes that schedule program work."""

from __future__ import annotations

from collections.abc import Iterable

from django.core.exceptions import ValidationError
from django.utils import timezone

from operations.models import ProgramBlock, TreatmentProgram


def mark_program_blocks_scheduled(block_ids: Iterable[int | None]) -> int:
    """Advance newly allocated blocks inside the caller's scheduling transaction.

    A stale scheduling object must never reopen or regress an existing block.
    Callers retain their block/program locks until the appointment writes commit.
    """
    return ProgramBlock.objects.filter(
        pk__in={pk for pk in block_ids if pk is not None}, status=ProgramBlock.Status.PLANNED,
    ).update(status=ProgramBlock.Status.SCHEDULED, updated_at=timezone.now())


def lock_program_blocks(block_ids: Iterable[int | None]) -> dict[int, ProgramBlock]:
    """Lock cascades and their program roots in the scheduling lock order."""
    ids = sorted({block_id for block_id in block_ids if block_id is not None})
    if not ids:
        return {}

    blocks = {
        block.pk: block
        for block in ProgramBlock.objects.select_for_update(of=("self",))
        .select_related("program")
        .filter(pk__in=ids)
        .order_by("pk")
    }
    program_ids = sorted({block.program_id for block in blocks.values()})
    programs = {
        program.pk: program
        for program in TreatmentProgram.objects.select_for_update()
        .filter(pk__in=program_ids)
        .order_by("pk")
    }
    for block in blocks.values():
        block.program = programs[block.program_id]
    return blocks


def assert_program_blocks_not_paused(
    block_ids: Iterable[int | None],
) -> dict[int, ProgramBlock]:
    """Reject new work for paused/terminal programs and terminal cascades."""
    blocks = lock_program_blocks(block_ids)
    paused = [
        block
        for block in blocks.values()
        if block.program.status in {
            TreatmentProgram.Status.PAUSED, TreatmentProgram.Status.COMPLETED,
            TreatmentProgram.Status.CANCELLED,
        } or block.status in {ProgramBlock.Status.COMPLETED, ProgramBlock.Status.CANCELLED}
    ]
    if paused:
        raise ValidationError(
            "Нельзя назначать или переносить занятия для приостановленной, завершенной "
            "или отмененной программы либо закрытого каскада."
        )
    return blocks


def assert_program_blocks_active(
    block_ids: Iterable[int | None],
) -> dict[int, ProgramBlock]:
    """Recheck group-series definition inputs before persisting a new series."""
    blocks = lock_program_blocks(block_ids)
    unavailable = [
        block
        for block in blocks.values()
        if block.program.status != TreatmentProgram.Status.ACTIVE
    ]
    if unavailable:
        raise ValidationError("Групповую серию можно создавать только для активных программ.")
    return blocks
