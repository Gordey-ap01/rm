"""Счета баланса."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import BalanceAccountForm
from operations.models import (
    Appointment,
    BalanceAccount,
    GrantRecipientAllocation,
    LedgerEntry,
    Payment,
)

from ._common import is_admin_user


def _balance_summary_items(accounts: list[BalanceAccount]) -> list[dict[str, str]]:
    active_accounts = [
        account for account in accounts if account.status == BalanceAccount.Status.ACTIVE
    ]
    low_accounts = [account for account in accounts if account.warning_level != "ok"]
    session_accounts = [account for account in accounts if account.unit == BalanceAccount.Unit.SESSIONS]
    money_accounts = [account for account in accounts if account.unit == BalanceAccount.Unit.MONEY]
    funding_count = len({account.funding_source_id for account in accounts})
    return [
        {
            "label": "Счетов",
            "value": str(len(accounts)),
            "hint": f"активных: {len(active_accounts)}",
        },
        {
            "label": "Риски",
            "value": str(len(low_accounts)),
            "hint": "исчерпаны или близки к нулю",
        },
        {
            "label": "Занятия",
            "value": str(len(session_accounts)),
            "hint": "счета в занятиях",
        },
        {
            "label": "Рубли",
            "value": str(len(money_accounts)),
            "hint": "денежные счета",
        },
        {
            "label": "Источники",
            "value": str(funding_count),
            "hint": "финансирования в списке",
        },
    ]


def _balance_next_action(accounts: list[BalanceAccount]) -> dict[str, str]:
    if not accounts:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Создать первый счет",
            "detail": "Без счета баланса нельзя привязать оплату, грант или бронь занятий.",
            "href": reverse("balance_account_create"),
        }

    low_accounts = [account for account in accounts if account.warning_level != "ok"]
    if low_accounts:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Проверить низкие остатки",
            "detail": f"Счетов с предупреждениями: {len(low_accounts)}.",
            "href": "#balance-list",
        }

    return {
        "tone": "success",
        "label": "Следующий шаг",
        "title": "Балансы без критичных рисков",
        "detail": "Можно пополнить счет, создать новый источник или перейти к расписанию.",
        "href": "#balance-list",
    }


def _balance_account_control_items(account: BalanceAccount | None = None) -> list[dict[str, str]]:
    unit_detail = (
        "Счета в занятиях списываются целыми занятиями; рублевые счета используются для денежных остатков."
    )
    source_detail = (
        "Источник отделяет личные оплаты, гранты, фонды и спонсоров; услуга ограничивает, где счет можно выбрать."
    )
    ledger_detail = (
        "Начальный остаток входит в расчет текущего баланса, а платежи и списания остаются отдельными ledger-операциями."
    )
    status_detail = (
        "Активный счет доступен для занятий и программ; даты действия помогают не выбрать счет вне периода финансирования."
    )
    if account:
        unit_detail = (
            f"Сейчас: {account.get_unit_display()}, остаток {account.current_balance}. "
            "При смене единицы проверьте связанные занятия и программы."
        )
        source_detail = (
            f"Источник: {account.funding_source}. "
            f"Услуга: {account.service or 'любые услуги'}."
        )
        ledger_detail = (
            "Изменение начального остатка сразу меняет расчет текущего баланса; "
            "история платежей и списаний не переписывается."
        )
        status_detail = (
            f"Текущий статус: {account.get_status_display()}. "
            "Не архивируйте счет, если он еще должен выбираться в расписании."
        )
    return [
        {
            "title": "Единица учета",
            "detail": unit_detail,
        },
        {
            "title": "Источник и услуга",
            "detail": source_detail,
        },
        {
            "title": "Остаток и ledger",
            "detail": ledger_detail,
        },
        {
            "title": "Статус и период",
            "detail": status_detail,
        },
    ]


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
            "form_panel_title": "Параметры счета",
            "form_intro": (
                "Счет связывает получателя, источник финансирования, единицу учета и область применения."
            ),
            "control_title": "Контроль балансового счета",
            "object_form_control_items": _balance_account_control_items(),
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
            "form_panel_title": "Параметры счета",
            "form_intro": (
                "Проверьте единицу учета, источник, услугу и статус перед изменением счета, "
                "который уже мог использоваться в расписании или программах."
            ),
            "control_title": "Контроль балансового счета",
            "object_form_control_items": _balance_account_control_items(account),
            "cancel_url": reverse("recipient_detail", args=[account.child_id]),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def balances(request):
    accounts = list(
        BalanceAccount.objects.select_related("child", "funding_source", "service").order_by(
            "child__last_name", "funding_source__name"
        )
    )
    return render(
        request,
        "operations/balances.html",
        {
            "accounts": accounts,
            "balance_summary_items": _balance_summary_items(accounts),
            "balance_next_action": _balance_next_action(accounts),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def balance_account_delete(request, pk: int):
    account = get_object_or_404(BalanceAccount, pk=pk)
    if request.method != "POST":
        return redirect("balances")
    linked_appointments = (
        Appointment.objects.filter(
            Q(billing_account=account) | Q(participants__billing_account=account),
        )
        .distinct()
        .count()
    )
    linked_payments = Payment.objects.filter(balance_account=account).count()
    linked_ledger_entries = LedgerEntry.objects.filter(account=account).count()
    linked_grant_allocations = GrantRecipientAllocation.objects.filter(
        balance_account=account
    ).count()
    if (
        linked_appointments
        or linked_payments
        or linked_ledger_entries
        or linked_grant_allocations
    ):
        messages.error(
            request,
            (
                "Нельзя удалить: "
                f"{linked_appointments} занятий, "
                f"{linked_payments} платежей, "
                f"{linked_ledger_entries} ledger-операций и "
                f"{linked_grant_allocations} грантовых выделений привязаны к этому счёту."
            ),
        )
    else:
        try:
            account.delete()
        except ProtectedError:
            messages.error(
                request,
                "Нельзя удалить: счет связан с историческими данными. Архивируйте или отключите его вместо удаления.",
            )
        else:
            messages.success(request, "Счёт удалён.")
    return redirect("balances")
