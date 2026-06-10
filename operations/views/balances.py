"""Счета баланса."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import BalanceAccountForm
from operations.models import Appointment, BalanceAccount, Payment

from ._common import is_admin_user


@login_required
@user_passes_test(is_admin_user)
def balance_account_create(request):
    initial = {}
    if request.GET.get("recipient_id"):
        initial["child"] = request.GET["recipient_id"]
    elif request.GET.get("child_id"):
        initial["child"] = request.GET["child_id"]
    if request.method == "POST":
        form = BalanceAccountForm(request.POST)
        if form.is_valid():
            account = form.save()
            messages.success(request, "Счет баланса создан.")
            return redirect("recipient_detail", pk=account.child_id)
    else:
        form = BalanceAccountForm(initial=initial)
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Создать счет баланса",
            "subtitle": "Счет может быть в занятиях или рублях, для любой услуги или конкретной услуги.",
            "cancel_url": reverse("recipient_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def balance_account_edit(request, pk: int):
    account = get_object_or_404(BalanceAccount, pk=pk)
    if request.method == "POST":
        form = BalanceAccountForm(request.POST, instance=account)
        if form.is_valid():
            account = form.save()
            messages.success(request, "Счет баланса обновлен.")
            return redirect("recipient_detail", pk=account.child_id)
    else:
        form = BalanceAccountForm(instance=account)
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Редактировать счет баланса",
            "subtitle": str(account),
            "cancel_url": reverse("recipient_detail", args=[account.child_id]),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def balances(request):
    accounts = BalanceAccount.objects.select_related("child", "funding_source", "service").order_by(
        "child__last_name", "funding_source__name"
    )
    return render(request, "operations/balances.html", {"accounts": accounts})


@login_required
@user_passes_test(is_admin_user)
def balance_account_delete(request, pk: int):
    account = get_object_or_404(BalanceAccount, pk=pk)
    if request.method != "POST":
        return redirect("balances")
    linked_appointments = Appointment.objects.filter(billing_account=account, status__in=Appointment.ACTIVE_APPOINTMENT_STATUSES).count()
    linked_payments = Payment.objects.filter(balance_account=account).count()
    if linked_appointments or linked_payments:
        messages.error(
            request,
            f"Нельзя удалить: {linked_appointments} активных занятий и {linked_payments} платежей привязаны к этому счёту.",
        )
    else:
        account.delete()
        messages.success(request, "Счёт удалён.")
    return redirect("balances")
