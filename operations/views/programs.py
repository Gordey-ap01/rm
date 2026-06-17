from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import (
    ProgramBlockForm,
    ProgramBlockScheduleWizardForm,
    ProgramFundsTransferForm,
    TreatmentProgramForm,
)
from operations.models import Child, ProgramBlock, TreatmentProgram
from operations.services import billing as billing_svc, program_wizard

from ._common import is_admin_user


@login_required
@user_passes_test(is_admin_user)
def program_create(request, child_id: int | None = None):
    child = get_object_or_404(Child, pk=child_id) if child_id else None
    if request.method == "POST":
        form = TreatmentProgramForm(request.POST, child=child)
        if form.is_valid():
            program = form.save()
            messages.success(request, "Программа занятий создана.")
            return redirect("recipient_detail", pk=program.child_id)
    else:
        form = TreatmentProgramForm(child=child)

    cancel_url = reverse("recipient_detail", args=[child.pk]) if child else reverse("recipient_list")
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Создать программу занятий",
            "subtitle": child.full_name if child else "",
            "cancel_url": cancel_url,
        },
    )


@login_required
@user_passes_test(is_admin_user)
def program_block_create(request, program_id: int):
    program = get_object_or_404(TreatmentProgram.objects.select_related("child"), pk=program_id)
    next_number = (program.blocks.order_by("-number").values_list("number", flat=True).first() or 0) + 1
    if request.method == "POST":
        form = ProgramBlockForm(request.POST, program=program)
        if form.is_valid():
            block = form.save()
            messages.success(request, "Блок программы создан.")
            return redirect("recipient_detail", pk=block.program.child_id)
    else:
        form = ProgramBlockForm(program=program, initial={"number": next_number})

    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Добавить блок программы",
            "subtitle": str(program),
            "cancel_url": reverse("recipient_detail", args=[program.child_id]),
        },
    )


def _program_block_or_404(block_id: int) -> ProgramBlock:
    return get_object_or_404(
        ProgramBlock.objects.select_related(
            "program",
            "program__child",
            "service",
            "staff_member",
            "balance_account",
            "balance_account__funding_source",
            "balance_account__service",
        ),
        pk=block_id,
    )


@login_required
@user_passes_test(is_admin_user)
def program_block_schedule_wizard(request, block_id: int):
    block = _program_block_or_404(block_id)
    preview = None

    if request.method == "POST":
        form = ProgramBlockScheduleWizardForm(request.POST, block=block)
        if form.is_valid():
            data = form.cleaned_data
            preview = program_wizard.suggest_program_block_slots(
                block,
                date_from=data["start_date"],
                date_to=data["end_date"],
                weekdays={int(value) for value in data["weekdays"]},
                time_from=data["time_from"],
                time_until=data["time_until"],
                duration_minutes=data["duration_minutes"],
                staff_member=data["staff_member"],
                room=data["room"],
                requested_count=data["requested_count"],
                allow_outside_availability=data["allow_outside_availability"],
                allow_unpaid_reserve=data["allow_unpaid_reserve"],
            )
            if request.POST.get("action") == "create":
                if not preview.slots:
                    messages.warning(request, "Мастер не нашёл подходящих окон. Расписание не создано.")
                else:
                    try:
                        result = program_wizard.create_schedule_from_preview(
                            preview,
                            status=data["appointment_status"],
                            actor=request.user,
                        )
                    except ValidationError as exc:
                        form.add_error(None, exc)
                        messages.warning(
                            request,
                            "За время согласования расписание изменилось. Нажмите «Подобрать окна» ещё раз.",
                        )
                    else:
                        messages.success(request, f"Создано занятий: {len(result.appointments)}.")
                        if preview.limited_by_balance:
                            messages.warning(
                                request,
                                "Количество было ограничено доступным балансом. Для продолжения пополните счёт "
                                "или включите бронь сверх оплаты.",
                            )
                        if preview.missing_count:
                            messages.warning(
                                request,
                                f"Не хватило свободных окон: {preview.missing_count}. Расширьте даты или время поиска.",
                            )
                        return redirect("recipient_detail", pk=block.program.child_id)
            elif not preview.slots:
                messages.warning(request, "Подходящих окон не найдено. Попробуйте расширить период, время или кабинет.")
    else:
        form = ProgramBlockScheduleWizardForm(block=block)

    return render(
        request,
        "operations/program_block_schedule_wizard.html",
        {
            "block": block,
            "form": form,
            "preview": preview,
            "cancel_url": reverse("recipient_detail", args=[block.program.child_id]),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def program_block_transfer_funds(request, block_id: int):
    block = _program_block_or_404(block_id)

    if request.method == "POST":
        form = ProgramFundsTransferForm(request.POST, block=block)
        if form.is_valid():
            try:
                debit, credit = billing_svc.transfer_between_accounts(
                    from_account=form.cleaned_data["from_account"],
                    to_account=form.cleaned_data["to_account"],
                    amount=form.cleaned_data["amount"],
                    reason=form.cleaned_data["reason"],
                    actor=request.user,
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                if not block.balance_account_id:
                    block.balance_account = form.cleaned_data["to_account"]
                    block.save(update_fields=["balance_account", "updated_at"])
                estimated = form.estimated_sessions_after_transfer()
                suffix = f" Это примерно {estimated} зан." if estimated is not None else ""
                messages.success(request, f"Средства перенесены: {abs(debit.amount)}.{suffix}")
                return redirect("recipient_detail", pk=block.program.child_id)
    else:
        form = ProgramFundsTransferForm(block=block)

    return render(
        request,
        "operations/program_block_transfer_funds.html",
        {
            "block": block,
            "form": form,
            "cancel_url": reverse("recipient_detail", args=[block.program.child_id]),
        },
    )
