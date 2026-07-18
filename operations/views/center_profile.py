"""Юридический профиль центра для договоров и документов."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import redirect, render
from django.urls import reverse

from operations.forms import CenterLegalProfileForm
from operations.models import CenterLegalProfile

from ._common import is_admin_user


def center_legal_profile_control_items(
    profile: CenterLegalProfile | None,
) -> list[dict[str, str]]:
    active_detail = (
        "Этот профиль будет использоваться для новых Word-документов."
        if profile and profile.is_active
        else "Сделайте профиль активным, чтобы реквизиты центра попадали в новые Word-документы."
    )
    return [
        {
            "title": "Новые документы",
            "detail": active_detail,
        },
        {
            "title": "Подписанные файлы",
            "detail": "Изменение профиля не переписывает уже сформированные документы.",
        },
        {
            "title": "Один активный профиль",
            "detail": "Система не даст сохранить два активных юридических профиля центра.",
        },
    ]


@login_required
@user_passes_test(is_admin_user)
def center_legal_profile_edit(request):
    profile = CenterLegalProfile.get_active()
    form_instance = profile if profile and profile.pk else None
    if request.method == "POST":
        form = CenterLegalProfileForm(request.POST, instance=form_instance)
        if form.is_valid():
            profile = form.save()
            messages.success(request, "Юридический профиль центра сохранен.")
            return redirect("center_legal_profile")
    else:
        initial = {"is_active": True}
        form = CenterLegalProfileForm(instance=form_instance, initial=initial)
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Юридический профиль центра",
            "subtitle": "Реквизиты для новых договоров и юридических шаблонов.",
            "form": form,
            "form_panel_title": "Реквизиты центра",
            "form_intro": "Эти данные подставляются в новые Word-документы. Старые файлы не меняются.",
            "control_title": "Контроль профиля",
            "object_form_control_items": center_legal_profile_control_items(profile),
            "cancel_url": reverse("contract_list"),
        },
    )
