"""Пополнение счёта через форму + сервисный слой."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import PaymentForm
from operations.models import BalanceAccount
from operations.services import billing as billing_svc

from ._common import is_admin_user


@login_required
@user_passes_test(is_admin_user)
def payment_create(request, account_id: int | None = None):
    initial = {}
    if account_id:
        initial["balance_account"] = get_object_or_404(BalanceAccount, pk=account_id)
    if request.method == "POST":
        form = PaymentForm(request.POST)
        if form.is_valid():
            try:
                payment = billing_svc.top_up_account(
                    account=form.cleaned_data["balance_account"],
                    amount=form.cleaned_data["amount"],
                    method=form.cleaned_data["method"],
                    paid_at=form.cleaned_data.get("paid_at"),
                    reference=form.cleaned_data.get("reference", ""),
                    comment=form.cleaned_data.get("comment", ""),
                    actor=request.user,
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Платёж #{payment.pk} сохранён и счёт пополнен.")
                return redirect("child_detail", pk=payment.balance_account.child_id)
    else:
        form = PaymentForm(initial=initial)
    return render(
        request,
        "operations/payment_form.html",
        {
            "form": form,
            "title": "Пополнить счёт",
            "cancel_url": reverse("balances"),
        },
    )
