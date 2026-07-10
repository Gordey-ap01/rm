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


def _payment_form_account(form: PaymentForm) -> BalanceAccount | None:
    initial_account = form.initial.get("balance_account")
    if isinstance(initial_account, BalanceAccount):
        return initial_account

    account_id = None
    if form.is_bound:
        account_id = form.data.get(form.add_prefix("balance_account"))
    elif initial_account:
        account_id = initial_account
    if not account_id:
        return None
    return (
        BalanceAccount.objects.select_related("child", "funding_source", "service")
        .filter(pk=account_id)
        .first()
    )


def _payment_summary_items(account: BalanceAccount | None) -> list[dict[str, str]]:
    if account is None:
        return [
            {
                "label": "Счет",
                "value": "Не выбран",
                "hint": "выберите счет баланса",
            },
            {
                "label": "Платеж",
                "value": "Новый",
                "hint": "после сохранения появится ledger-пополнение",
            },
        ]

    return [
        {
            "label": "Получатель",
            "value": account.child.full_name,
            "hint": str(account.service) if account.service_id else "любой вид услуги",
        },
        {
            "label": "Текущий баланс",
            "value": str(account.current_balance),
            "hint": account.get_unit_display(),
        },
        {
            "label": "Источник",
            "value": str(account.funding_source),
            "hint": account.get_status_display(),
        },
        {
            "label": "Счет",
            "value": f"#{account.pk}",
            "hint": account.get_service_scope_display(),
        },
    ]


def _payment_next_action(form: PaymentForm, account: BalanceAccount | None) -> dict[str, str]:
    if form.is_bound and form.errors:
        return {
            "tone": "danger",
            "label": "Следующий шаг",
            "title": "Исправить платеж",
            "detail": "Проверьте счет, сумму, способ оплаты и дату.",
            "href": "#payment-form",
        }
    if account is None:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Выбрать счет",
            "detail": "Пополнение можно сохранить только для конкретного счета баланса.",
            "href": "#payment-form",
        }
    return {
        "tone": "info",
        "label": "Следующий шаг",
        "title": "Проверить сумму",
        "detail": "После сохранения платеж создаст ledger-пополнение выбранного счета.",
        "href": "#payment-form",
    }


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
    selected_account = _payment_form_account(form)
    return render(
        request,
        "operations/payment_form.html",
        {
            "form": form,
            "title": "Пополнить счёт",
            "cancel_url": reverse("balances"),
            "payment_summary_items": _payment_summary_items(selected_account),
            "payment_next_action": _payment_next_action(form, selected_account),
            "selected_account": selected_account,
        },
    )
