"""Расходы центра: черновики и распределение по источникам финансирования."""

from __future__ import annotations

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import CenterExpenseForm, ExpenseFundingSplitFormSet
from operations.models import CenterExpense, CenterExpenseCategory, FundingSource

from ._common import is_admin_user


def _format_money(amount: Decimal) -> str:
    value = amount.quantize(Decimal("0.01"))
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _positive_int_or_none(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decorate_expense_allocations(expenses: list[CenterExpense]) -> None:
    for expense in expenses:
        splits = list(expense.funding_splits.all())
        split_total = sum((split.amount for split in splits), Decimal("0"))
        difference = expense.total_amount - split_total
        expense.ui_funding_splits = splits
        expense.ui_split_total = split_total
        expense.ui_unallocated_amount = difference
        expense.ui_split_total_display = _format_money(split_total)
        expense.ui_total_display = _format_money(expense.total_amount)
        expense.ui_unallocated_display = _format_money(abs(difference))

        if not splits:
            expense.ui_allocation_label = "не распределен"
            expense.ui_allocation_class = "muted-pill"
        elif difference == 0:
            expense.ui_allocation_label = "распределен"
            expense.ui_allocation_class = ""
        elif difference > 0:
            expense.ui_allocation_label = "частично"
            expense.ui_allocation_class = "warning-pill"
        else:
            expense.ui_allocation_label = "сверх суммы"
            expense.ui_allocation_class = "danger-pill"


def _expense_queryset(request) -> list[CenterExpense]:
    queryset = CenterExpense.objects.select_related(
        "category",
        "counterparty",
        "created_by",
    ).prefetch_related("funding_splits__funding_source")

    status = request.GET.get("status")
    if status in {choice[0] for choice in CenterExpense.Status.choices}:
        queryset = queryset.filter(status=status)

    category_id = _positive_int_or_none(request.GET.get("category"))
    if category_id:
        queryset = queryset.filter(category_id=category_id)

    funding_source_id = _positive_int_or_none(request.GET.get("funding_source"))
    if funding_source_id:
        queryset = queryset.filter(funding_splits__funding_source_id=funding_source_id)

    query = request.GET.get("q", "").strip()
    if query:
        queryset = queryset.filter(
            Q(title__icontains=query)
            | Q(description__icontains=query)
            | Q(counterparty__name__icontains=query)
        )

    expenses = list(queryset.distinct().order_by("-expense_date", "-created_at")[:300])
    _decorate_expense_allocations(expenses)
    return expenses


def expense_summary_items(expenses: list[CenterExpense]) -> list[dict[str, str]]:
    total_amount = sum((expense.total_amount for expense in expenses), Decimal("0"))
    draft_count = sum(1 for expense in expenses if expense.status == CenterExpense.Status.DRAFT)
    paid_amount = sum(
        (
            expense.total_amount
            for expense in expenses
            if expense.status == CenterExpense.Status.PAID
        ),
        Decimal("0"),
    )
    unallocated_count = sum(
        1
        for expense in expenses
        if expense.status != CenterExpense.Status.CANCELLED
        and expense.ui_unallocated_amount != 0
    )
    return [
        {
            "label": "Расходов",
            "value": str(len(expenses)),
            "hint": f"черновиков: {draft_count}",
        },
        {
            "label": "Сумма",
            "value": _format_money(total_amount),
            "hint": "по текущему фильтру",
        },
        {
            "label": "Оплачено",
            "value": _format_money(paid_amount),
            "hint": "после будущего статуса оплаты",
        },
        {
            "label": "Проверить",
            "value": str(unallocated_count),
            "hint": "не сходится распределение",
        },
    ]


def expense_next_action(
    expenses: list[CenterExpense],
    *,
    active_category_count: int,
    active_funding_source_count: int,
) -> dict[str, str]:
    if active_category_count == 0:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Настроить категории расходов",
            "detail": "Без активной категории нельзя создать расход центра.",
            "href": "/admin/operations/centerexpensecategory/add/",
        }
    if active_funding_source_count == 0:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Создать источник финансирования",
            "detail": "Распределение расходов использует те же источники, что гранты, фонды и спонсоры.",
            "href": reverse("funding_source_create"),
        }
    if not expenses:
        return {
            "tone": "info",
            "label": "Следующий шаг",
            "title": "Добавить первый расход",
            "detail": "Сохраняйте расход черновиком, затем раскладывайте сумму по источникам финансирования.",
            "href": reverse("center_expense_create"),
        }
    unallocated_count = sum(
        1
        for expense in expenses
        if expense.status != CenterExpense.Status.CANCELLED
        and expense.ui_unallocated_amount != 0
    )
    if unallocated_count:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Проверить распределение",
            "detail": f"Расходов с неполным или избыточным распределением: {unallocated_count}.",
            "href": "#center-expense-list",
        }
    return {
        "tone": "success",
        "label": "Следующий шаг",
        "title": "Расходы готовы к проверке",
        "detail": "Черновики распределены по источникам. Утверждение и оплата будут отдельным контролируемым шагом.",
        "href": "#center-expense-list",
    }


def _disable_formset(formset) -> None:
    for form in formset.forms:
        for field in form.fields.values():
            field.disabled = True


def _split_total_from_formset(formset, expense: CenterExpense | None) -> Decimal:
    if "split_total" in formset.__dict__:
        return formset.split_total
    if expense and expense.pk:
        return expense.funding_split_total
    return Decimal("0")


def expense_form_control_items(
    expense: CenterExpense | None,
    *,
    split_total: Decimal,
) -> list[dict[str, str]]:
    total_amount = expense.total_amount if expense and expense.total_amount else Decimal("0")
    difference = total_amount - split_total
    if not expense or not expense.pk:
        status_detail = "Новый расход сохраняется только как черновик."
    else:
        status_detail = f"Текущий статус: {expense.get_status_display()}."

    if split_total == 0:
        allocation_detail = "Распределение по источникам пока не заполнено."
        allocation_tone = "warning"
    elif difference == 0:
        allocation_detail = "Сумма распределения совпадает с суммой расхода."
        allocation_tone = "success"
    elif difference > 0:
        allocation_detail = f"Осталось распределить: {_format_money(difference)}."
        allocation_tone = "warning"
    else:
        allocation_detail = f"Распределение превышает расход на {_format_money(abs(difference))}."
        allocation_tone = "danger"

    return [
        {
            "title": "Черновик расхода",
            "detail": status_detail,
        },
        {
            "title": "Источники финансирования",
            "detail": allocation_detail,
            "tone": allocation_tone,
        },
        {
            "title": "Без ledger",
            "detail": "Этот экран не создает проводки балансов получателей и не списывает занятия.",
        },
        {
            "title": "Утверждение позже",
            "detail": "Статусы утверждения и оплаты будут отдельным шагом с обязательной проверкой суммы.",
        },
    ]


@login_required
@user_passes_test(is_admin_user)
def center_expense_list(request):
    expenses = _expense_queryset(request)
    categories = CenterExpenseCategory.objects.order_by("sort_order", "name")
    funding_sources = FundingSource.all_objects.order_by("archived_at", "name")
    active_category_count = CenterExpenseCategory.objects.filter(is_active=True).count()
    active_funding_source_count = FundingSource.objects.count()
    return render(
        request,
        "operations/center_expense_list.html",
        {
            "expenses": expenses,
            "expense_summary_items": expense_summary_items(expenses),
            "expense_next_action": expense_next_action(
                expenses,
                active_category_count=active_category_count,
                active_funding_source_count=active_funding_source_count,
            ),
            "categories": categories,
            "funding_sources": funding_sources,
            "status_choices": CenterExpense.Status.choices,
            "filters": {
                "q": request.GET.get("q", "").strip(),
                "status": request.GET.get("status", ""),
                "category": request.GET.get("category", ""),
                "funding_source": request.GET.get("funding_source", ""),
            },
        },
    )


@login_required
@user_passes_test(is_admin_user)
def center_expense_create(request):
    expense = CenterExpense(created_by=request.user, status=CenterExpense.Status.DRAFT)
    if request.method == "POST":
        form = CenterExpenseForm(request.POST, instance=expense)
        formset = ExpenseFundingSplitFormSet(request.POST, instance=expense)
        form_valid = form.is_valid()
        formset_valid = formset.is_valid()
        if form_valid and formset_valid:
            with transaction.atomic():
                expense = form.save(commit=False)
                expense.status = CenterExpense.Status.DRAFT
                expense.created_by = request.user
                expense.save()
                formset.instance = expense
                formset.save()
            messages.success(request, "Расход центра сохранен как черновик.")
            return redirect("center_expense_edit", pk=expense.pk)
    else:
        form = CenterExpenseForm(instance=expense)
        formset = ExpenseFundingSplitFormSet(instance=expense)

    split_total = _split_total_from_formset(formset, None)
    return render(
        request,
        "operations/center_expense_form.html",
        {
            "title": "Добавить расход центра",
            "subtitle": "Черновик без списаний по балансам получателей.",
            "expense": None,
            "form": form,
            "formset": formset,
            "is_locked": False,
            "cancel_url": reverse("center_expense_list"),
            "expense_form_control_items": expense_form_control_items(
                None,
                split_total=split_total,
            ),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def center_expense_edit(request, pk: int):
    expense = get_object_or_404(
        CenterExpense.objects.select_related("category", "counterparty"),
        pk=pk,
    )
    is_locked = expense.status != CenterExpense.Status.DRAFT
    if request.method == "POST" and is_locked:
        messages.error(request, "Можно редактировать только черновики расходов.")
        return redirect("center_expense_edit", pk=expense.pk)

    if request.method == "POST":
        form = CenterExpenseForm(request.POST, instance=expense)
        formset = ExpenseFundingSplitFormSet(request.POST, instance=expense)
        form_valid = form.is_valid()
        formset_valid = formset.is_valid()
        if form_valid and formset_valid:
            with transaction.atomic():
                expense = form.save(commit=False)
                expense.status = CenterExpense.Status.DRAFT
                expense.save()
                formset.instance = expense
                formset.save()
            messages.success(request, "Расход центра обновлен.")
            return redirect("center_expense_list")
    else:
        form = CenterExpenseForm(instance=expense, readonly=is_locked)
        formset = ExpenseFundingSplitFormSet(instance=expense)
        if is_locked:
            _disable_formset(formset)

    split_total = _split_total_from_formset(formset, expense)
    return render(
        request,
        "operations/center_expense_form.html",
        {
            "title": "Редактировать расход центра",
            "subtitle": str(expense),
            "expense": expense,
            "form": form,
            "formset": formset,
            "is_locked": is_locked,
            "cancel_url": reverse("center_expense_list"),
            "expense_form_control_items": expense_form_control_items(
                expense,
                split_total=split_total,
            ),
        },
    )
