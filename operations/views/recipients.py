"""Получатели и их представители."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.forms import RecipientForm, RepresentativeForm
from operations.models import Child, ParentGuardian, TreatmentProgram
from operations.services.pdf import contract_pdf

from ._common import is_admin_user


@login_required
@user_passes_test(is_admin_user)
def recipient_list(request):
    query = request.GET.get("q", "").strip()
    recipients = list(
        Child.objects.select_related("primary_parent")
        .annotate(
            appointments_count=Count("appointments"),
            balance_accounts_count=Count("balance_accounts", distinct=True),
        )
        .order_by("last_name", "first_name")[:80]
    )
    if query:
        ql = query.lower()
        recipients = [
            r
            for r in recipients
            if ql in r.last_name.lower()
            or ql in r.first_name.lower()
            or ql in r.middle_name.lower()
            or (
                r.primary_parent_id
                and (
                    ql in r.primary_parent.last_name.lower()
                    or ql in r.primary_parent.first_name.lower()
                    or ql in r.primary_parent.phone.lower()
                    or ql in r.primary_parent.phone_alt.lower()
                )
            )
        ]
    return render(request, "operations/recipient_list.html", {"recipients": recipients, "query": query})


@login_required
@user_passes_test(is_admin_user)
def recipient_detail(request, pk: int):
    recipient = get_object_or_404(Child.objects.select_related("primary_parent"), pk=pk)
    now = timezone.now()
    accounts = recipient.balance_accounts.select_related("funding_source", "service").order_by(
        "funding_source__name",
        "service__name",
    )
    upcoming_appointments = (
        recipient.appointments.filter(starts_at__gte=now)
        .select_related("staff_member", "service", "room", "billing_account")
        .order_by("starts_at")[:20]
    )
    recent_appointments = (
        recipient.appointments.filter(starts_at__lt=now)
        .select_related("staff_member", "service", "room", "billing_account")
        .order_by("-starts_at")[:20]
    )
    programs = (
        TreatmentProgram.objects.filter(child=recipient)
        .prefetch_related("blocks", "blocks__service", "blocks__staff_member", "blocks__balance_account")
        .order_by("-starts_on", "title")
    )
    return render(
        request,
        "operations/recipient_detail.html",
        {
            "recipient": recipient,
            "representative": getattr(recipient, "primary_parent", None),
            "accounts": accounts,
            "programs": programs,
            "upcoming_appointments": upcoming_appointments,
            "recent_appointments": recent_appointments,
        },
    )


@login_required
@user_passes_test(is_admin_user)
def representative_create(request):
    if request.method == "POST":
        form = RepresentativeForm(request.POST)
        if form.is_valid():
            representative = form.save()
            messages.success(request, "Представитель создан.")
            return redirect("representative_edit", pk=representative.pk)
    else:
        form = RepresentativeForm()
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Создать представителя",
            "subtitle": "После сохранения можно привязать к нему получателя.",
            "cancel_url": reverse("recipient_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def representative_edit(request, pk: int):
    representative = get_object_or_404(ParentGuardian, pk=pk)
    if request.method == "POST":
        form = RepresentativeForm(request.POST, instance=representative)
        if form.is_valid():
            form.save()
            messages.success(request, "Представитель обновлен.")
            return redirect("recipient_list")
    else:
        form = RepresentativeForm(instance=representative)
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Редактировать представителя",
            "subtitle": representative.full_name,
            "cancel_url": reverse("recipient_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def recipient_contract_pdf(request, pk: int):
    child = get_object_or_404(Child.objects.select_related("primary_parent"), pk=pk)
    pdf_buf = contract_pdf(child)
    filename = f"contract_{child.last_name}_{child.first_name}.pdf".replace(" ", "_")
    return HttpResponse(pdf_buf.read(), content_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{filename}"'})


@login_required
@user_passes_test(is_admin_user)
def recipient_create(request):
    initial = {}
    if request.GET.get("representative_id"):
        initial["primary_parent"] = request.GET["representative_id"]
    if request.method == "POST":
        form = RecipientForm(request.POST)
        if form.is_valid():
            recipient = form.save()
            messages.success(request, "Получатель создан.")
            return redirect("recipient_detail", pk=recipient.pk)
    else:
        form = RecipientForm(initial=initial)
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Создать получателя",
            "subtitle": "Заполните основные данные и выберите представителя.",
            "cancel_url": reverse("recipient_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def recipient_edit(request, pk: int):
    recipient = get_object_or_404(Child, pk=pk)
    if request.method == "POST":
        form = RecipientForm(request.POST, instance=recipient)
        if form.is_valid():
            form.save()
            messages.success(request, "Получатель обновлен.")
            return redirect("recipient_detail", pk=recipient.pk)
    else:
        form = RecipientForm(instance=recipient)
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Редактировать получателя",
            "subtitle": recipient.full_name,
            "cancel_url": reverse("recipient_detail", args=[recipient.pk]),
        },
    )
