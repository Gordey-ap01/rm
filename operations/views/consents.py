"""Согласия представителей."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import ConsentForm
from operations.models import Child, Consent, RecipientRepresentative
from operations.services import contract_documents as contract_doc_svc

from ._common import is_admin_user


def _consent_create_url(child_id: int | str | None) -> str:
    if child_id:
        return reverse("consent_create_for_child", args=[child_id])
    return reverse("consent_create")


def _consent_summary_items(consents: list[Consent]) -> list[dict[str, str]]:
    active = [consent for consent in consents if consent.is_valid]
    expired = [consent for consent in consents if not consent.is_valid]
    linked_documents = [consent for consent in consents if consent.document_id]
    unsigned = [consent for consent in consents if consent.signed_on is None]
    return [
        {
            "label": "Всего в списке",
            "value": str(len(consents)),
            "hint": "показаны последние 80",
        },
        {
            "label": "Активны",
            "value": str(len(active)),
            "hint": "можно использовать",
        },
        {
            "label": "Истекли",
            "value": str(len(expired)),
            "hint": "нет действующего срока",
        },
        {
            "label": "С документом",
            "value": str(len(linked_documents)),
            "hint": f"без даты подписи: {len(unsigned)}",
        },
    ]


def _consent_next_action(consents: list[Consent], child_id: int | str | None) -> dict[str, str]:
    expired_count = sum(1 for consent in consents if not consent.is_valid)
    if expired_count:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Обновить истекшие",
            "detail": f"Согласий без действующего срока: {expired_count}.",
            "href": "#consent-list",
        }
    return {
        "tone": "success",
        "label": "Следующий шаг",
        "title": "Зафиксировать согласие",
        "detail": "Истекших согласий в текущем списке нет.",
        "href": _consent_create_url(child_id),
    }


def _consent_control_items(
    consents: list[Consent],
    child_id: int | str | None,
) -> list[dict[str, str]]:
    expired_count = sum(1 for consent in consents if not consent.is_valid)
    unsigned_count = sum(1 for consent in consents if consent.signed_on is None)
    items = []
    if child_id:
        items.append(
            {
                "tone": "info",
                "title": "Фильтр по получателю",
                "text": "Список показывает согласия только выбранного получателя.",
            }
        )
    if expired_count:
        items.append(
            {
                "tone": "warning",
                "title": "Есть недействующие согласия",
                "text": f"Согласий без действующего срока: {expired_count}.",
            }
        )
    if unsigned_count:
        items.append(
            {
                "tone": "warning",
                "title": "Не указана дата подписи",
                "text": f"Согласий без даты подписи: {unsigned_count}.",
            }
        )
    if not consents:
        items.append(
            {
                "tone": "info",
                "title": "Согласий пока нет",
                "text": "Зафиксируйте согласие на персональные данные, фото/видео или внешнего специалиста.",
            }
        )
    if not items:
        items.append(
            {
                "tone": "success",
                "title": "Критичных согласий нет",
                "text": "В текущем списке все согласия действуют.",
            }
        )
    return items


@login_required
@user_passes_test(is_admin_user)
def consent_list(request, child_id: int | None = None):
    qs = Consent.objects.select_related(
        "child",
        "document",
        "signatory_representative__representative",
        "template",
    ).order_by("-signed_on")
    if child_id is None:
        child_id = request.GET.get("child_id")
    if child_id:
        qs = qs.filter(child_id=child_id)
    consents = list(qs[:80])
    return render(
        request,
        "operations/consent_list.html",
        {
            "consents": consents,
            "child_id": child_id,
            "consent_create_url": _consent_create_url(child_id),
            "consent_summary_items": _consent_summary_items(consents),
            "consent_next_action": _consent_next_action(consents, child_id),
            "consent_control_items": _consent_control_items(consents, child_id),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def consent_create(request, child_id: int | None = None):
    initial = {}
    if child_id:
        child = get_object_or_404(Child, pk=child_id)
        initial["child"] = child
        signatory = (
            RecipientRepresentative.objects.filter(child=child, signs_contract=True)
            .select_related("representative")
            .order_by("-is_primary", "representative__last_name")
            .first()
        )
        if signatory is None:
            signatory = (
                RecipientRepresentative.objects.filter(child=child, is_primary=True)
                .select_related("representative")
                .order_by("representative__last_name")
                .first()
            )
        if signatory is not None:
            initial["signatory_representative"] = signatory
    if request.method == "POST":
        form = ConsentForm(request.POST)
        if form.is_valid():
            consent = form.save()
            messages.success(request, "Согласие зафиксировано.")
            return redirect("child_detail", pk=consent.child_id)
    else:
        form = ConsentForm(initial=initial)
    return render(
        request,
        "operations/consent_form.html",
        {
            "form": form,
            "title": "Зафиксировать согласие",
            "cancel_url": reverse("consent_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def consent_word(request, pk: int):
    consent = get_object_or_404(
        Consent.objects.select_related(
            "child",
            "signatory_representative__representative",
            "template",
            "document",
        ),
        pk=pk,
    )
    if request.method != "POST":
        messages.warning(request, "Сформируйте Word-файл кнопкой в реестре согласий.")
        return redirect("consent_list")
    try:
        generated = contract_doc_svc.save_consent_docx(consent, actor=request.user)
    except contract_doc_svc.ContractDocumentError as exc:
        messages.error(request, str(exc))
        return redirect("consent_list")
    return FileResponse(
        generated.payload,
        as_attachment=True,
        filename=generated.filename,
        content_type=contract_doc_svc.DOCX_CONTENT_TYPE,
    )
