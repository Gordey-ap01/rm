"""Locked availability checks for writes that schedule program work."""

from __future__ import annotations

from collections.abc import Iterable

from django.core.exceptions import ValidationError

from operations.models import ProgramBlock, TreatmentProgram


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
    """Reject a new manual scheduling assignment to a paused program only."""
    blocks = lock_program_blocks(block_ids)
    paused = [
        block
        for block in blocks.values()
        if block.program.status == TreatmentProgram.Status.PAUSED
    ]
    if paused:
        raise ValidationError(
            "Нельзя назначать или переносить занятия для приостановленной программы."
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
