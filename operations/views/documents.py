"""Документы получателей (загрузка файлов, срок действия)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import DocumentForm
from operations.models import Child, Document

from ._common import is_admin_user


def _document_create_url(child_id: int | str | None) -> str:
    if child_id:
        return reverse("document_create_for_child", args=[child_id])
    return reverse("document_create")


def _document_summary_items(documents: list[Document]) -> list[dict[str, str]]:
    expired = [document for document in documents if document.is_expired]
    expires_soon = [document for document in documents if document.expires_soon]
    with_files = [document for document in documents if document.file]
    return [
        {
            "label": "Всего в списке",
            "value": str(len(documents)),
            "hint": "показаны последние 80",
        },
        {
            "label": "С файлами",
            "value": str(len(with_files)),
            "hint": "доступны для открытия",
        },
        {
            "label": "Просрочены",
            "value": str(len(expired)),
            "hint": "срок действия прошел",
        },
        {
            "label": "Скоро истекают",
            "value": str(len(expires_soon)),
            "hint": "30 дней до окончания",
        },
    ]


def _document_next_action(documents: list[Document], child_id: int | str | None) -> dict[str, str]:
    expired_count = sum(1 for document in documents if document.is_expired)
    expiring_count = sum(1 for document in documents if document.expires_soon)
    if expired_count:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Обновить просроченные",
            "detail": f"Просроченных документов: {expired_count}.",
            "href": "#document-list",
        }
    if expiring_count:
        return {
            "tone": "info",
            "label": "Следующий шаг",
            "title": "Проверить сроки",
            "detail": f"Документов скоро истекает: {expiring_count}.",
            "href": "#document-list",
        }
    return {
        "tone": "success",
        "label": "Следующий шаг",
        "title": "Загрузить документ",
        "detail": "Критичных сроков в текущем списке нет.",
        "href": _document_create_url(child_id),
    }


def _document_control_items(
    documents: list[Document],
    child_id: int | str | None,
) -> list[dict[str, str]]:
    expired_count = sum(1 for document in documents if document.is_expired)
    expiring_count = sum(1 for document in documents if document.expires_soon)
    items = []
    if child_id:
        items.append(
            {
                "tone": "info",
                "title": "Фильтр по получателю",
                "text": "Список показывает документы только выбранного получателя.",
            }
        )
    if expired_count:
        items.append(
            {
                "tone": "warning",
                "title": "Есть просроченные документы",
                "text": f"Просроченных документов: {expired_count}.",
            }
        )
    if expiring_count:
        items.append(
            {
                "tone": "info",
                "title": "Проверьте ближайшие сроки",
                "text": f"Истекают в ближайшие 30 дней: {expiring_count}.",
            }
        )
    if not documents:
        items.append(
            {
                "tone": "info",
                "title": "Документов пока нет",
                "text": "Загрузите договор, ИПР, медицинское заключение или другой файл.",
            }
        )
    if not items:
        items.append(
            {
                "tone": "success",
                "title": "Критичных сроков нет",
                "text": "В текущем списке нет просроченных или истекающих документов.",
            }
        )
    return items


@login_required
@user_passes_test(is_admin_user)
def document_list(request, child_id: int | None = None):
    qs = Document.objects.select_related("child", "uploaded_by").order_by("-created_at")
    if child_id is None:
        child_id = request.GET.get("child_id")
    if child_id:
        qs = qs.filter(child_id=child_id)
    documents = list(qs[:80])
    return render(
        request,
        "operations/document_list.html",
        {
            "documents": documents,
            "child_id": child_id,
            "document_create_url": _document_create_url(child_id),
            "document_summary_items": _document_summary_items(documents),
            "document_next_action": _document_next_action(documents, child_id),
            "document_control_items": _document_control_items(documents, child_id),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def document_create(request, child_id: int | None = None):
    initial = {}
    if child_id:
        initial["child"] = get_object_or_404(Child, pk=child_id)
    if request.method == "POST":
        form = DocumentForm(request.POST, request.FILES)
        if form.is_valid():
            doc = form.save(commit=False)
            doc.uploaded_by = request.user
            doc.save()
            messages.success(request, "Документ загружен.")
            return redirect("child_detail", pk=doc.child_id)
    else:
        form = DocumentForm(initial=initial)
    return render(
        request,
        "operations/document_form.html",
        {
            "form": form,
            "title": "Загрузить документ",
            "cancel_url": reverse("document_list"),
        },
    )
