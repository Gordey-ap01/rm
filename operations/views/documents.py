"""Документы получателей (загрузка файлов, срок действия)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import DocumentForm
from operations.models import Child, Document

from ._common import is_admin_user


@login_required
@user_passes_test(is_admin_user)
def document_list(request, child_id: int | None = None):
    qs = Document.objects.select_related("child", "uploaded_by").order_by("-created_at")
    if child_id is None:
        child_id = request.GET.get("child_id")
    if child_id:
        qs = qs.filter(child_id=child_id)
    return render(request, "operations/document_list.html", {"documents": qs[:80], "child_id": child_id})


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
