from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import ProgramBlockForm, TreatmentProgramForm
from operations.models import Child, TreatmentProgram

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
