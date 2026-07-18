"""Product UI for center-expense categories and counterparties."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import CenterExpenseCategoryForm, CounterpartyForm
from operations.models import CenterExpenseCategory, Counterparty

from ._common import is_admin_user


def _directory_filters(request) -> dict[str, str]:
    return {
        "q": request.GET.get("q", "").strip(),
        "category_status": request.GET.get("category_status", ""),
        "expense_type": request.GET.get("expense_type", ""),
        "counterparty_status": request.GET.get("counterparty_status", ""),
        "counterparty_type": request.GET.get("counterparty_type", ""),
    }


def _category_queryset(filters: dict[str, str]) -> list[CenterExpenseCategory]:
    queryset = CenterExpenseCategory.objects.annotate(
        expense_count=Count("center_expenses", distinct=True)
    )
    if filters["category_status"] == "active":
        queryset = queryset.filter(is_active=True)
    elif filters["category_status"] == "inactive":
        queryset = queryset.filter(is_active=False)
    if filters["expense_type"] in {choice[0] for choice in CenterExpenseCategory.ExpenseType.choices}:
        queryset = queryset.filter(expense_type=filters["expense_type"])
    if filters["q"]:
        queryset = queryset.filter(Q(name__icontains=filters["q"]) | Q(notes__icontains=filters["q"]))
    return list(queryset.order_by("sort_order", "name")[:300])


def _counterparty_queryset(filters: dict[str, str]) -> list[Counterparty]:
    queryset = Counterparty.all_objects.annotate(
        expense_count=Count("center_expenses", distinct=True),
        donation_contract_count=Count("donation_contracts", distinct=True),
    )
    if filters["counterparty_status"] == "active":
        queryset = queryset.filter(archived_at__isnull=True)
    elif filters["counterparty_status"] == "archived":
        queryset = queryset.filter(archived_at__isnull=False)
    if filters["counterparty_type"] in {choice[0] for choice in Counterparty.CounterpartyType.choices}:
        queryset = queryset.filter(counterparty_type=filters["counterparty_type"])
    if filters["q"]:
        query = filters["q"]
        queryset = queryset.filter(
            Q(name__icontains=query)
            | Q(inn__icontains=query)
            | Q(contact_person__icontains=query)
            | Q(phone__icontains=query)
            | Q(email__icontains=query)
            | Q(notes__icontains=query)
        )
    return list(queryset.order_by("archived_at", "name")[:300])


def directory_summary_items(
    categories: list[CenterExpenseCategory],
    counterparties: list[Counterparty],
) -> list[dict[str, str]]:
    active_categories = sum(1 for category in categories if category.is_active)
    inactive_categories = sum(1 for category in categories if not category.is_active)
    active_counterparties = sum(1 for counterparty in counterparties if not counterparty.is_archived)
    archived_counterparties = sum(1 for counterparty in counterparties if counterparty.is_archived)
    used_categories = sum(1 for category in categories if category.expense_count)
    used_counterparties = sum(
        1
        for counterparty in counterparties
        if counterparty.expense_count or counterparty.donation_contract_count
    )
    return [
        {
            "label": "Категорий",
            "value": str(len(categories)),
            "hint": f"активных: {active_categories}",
        },
        {
            "label": "Отключены",
            "value": str(inactive_categories),
            "hint": "не попадают в новые расходы",
        },
        {
            "label": "Контрагентов",
            "value": str(len(counterparties)),
            "hint": f"активных: {active_counterparties}",
        },
        {
            "label": "Архив",
            "value": str(archived_counterparties),
            "hint": "связи и история сохранены",
        },
        {
            "label": "Используются",
            "value": str(used_categories + used_counterparties),
            "hint": "есть расходы или договоры",
        },
    ]


def directory_next_action(
    categories: list[CenterExpenseCategory],
    counterparties: list[Counterparty],
) -> dict[str, str]:
    if not any(category.is_active for category in categories):
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Добавить активную категорию",
            "detail": "Без активной категории администратор не сможет создать расход центра.",
            "href": reverse("expense_category_create"),
        }
    if not any(not counterparty.is_archived for counterparty in counterparties):
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Добавить активного контрагента",
            "detail": "Расходы и договоры пожертвования используют контрагентов из этого справочника.",
            "href": reverse("counterparty_create"),
        }
    inactive_categories = sum(1 for category in categories if not category.is_active)
    archived_counterparties = sum(1 for counterparty in counterparties if counterparty.is_archived)
    if inactive_categories or archived_counterparties:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Проверить отключенные записи",
            "detail": f"Неактивных категорий: {inactive_categories}; контрагентов в архиве: {archived_counterparties}.",
            "href": "#expense-directory-categories",
        }
    return {
        "tone": "success",
        "label": "Следующее действие",
        "title": "Справочники готовы",
        "detail": "Можно создавать расходы, договоры и проверять import preview без обращения к Django admin.",
        "href": "#expense-directory-categories",
    }


def category_form_control_items(
    category: CenterExpenseCategory | None = None,
) -> list[dict[str, str]]:
    active_detail = (
        "Активная категория доступна в новых расходах. Отключение не меняет уже созданные расходы."
    )
    if category:
        active_detail = (
            "Категория доступна для новых расходов."
            if category.is_active
            else "Категория скрыта из новых расходов, но остается в старых отчетах."
        )
    return [
        {
            "title": "Тип расхода",
            "detail": "Тип помогает отчетам и проверкам, например оборудование привязывается к категории equipment.",
        },
        {
            "title": "Активность",
            "detail": active_detail,
        },
        {
            "title": "Без удаления",
            "detail": "Product UI отключает категорию, но не удаляет ее из истории расходов.",
        },
    ]


def counterparty_form_control_items(counterparty: Counterparty | None = None) -> list[dict[str, str]]:
    archive_detail = (
        "Архив скрывает контрагента из новых расходов и договоров, но сохраняет связанные записи."
    )
    if counterparty:
        archive_detail = (
            "Контрагент в архиве; старые расходы и договоры остаются видимыми."
            if counterparty.is_archived
            else "Контрагент доступен для новых расходов, договоров и preview импорта."
        )
    return [
        {
            "title": "Реквизиты",
            "detail": "Поля хранят юридическую и контактную информацию, но не создают платежи.",
        },
        {
            "title": "Архив",
            "detail": archive_detail,
        },
        {
            "title": "Связанные документы",
            "detail": "Изменение справочника не переписывает расходы, договоры и финансовую историю.",
        },
    ]


@login_required
@user_passes_test(is_admin_user)
def expense_directory_list(request):
    filters = _directory_filters(request)
    categories = _category_queryset(filters)
    counterparties = _counterparty_queryset(filters)
    return render(
        request,
        "operations/expense_directory_list.html",
        {
            "categories": categories,
            "counterparties": counterparties,
            "directory_summary_items": directory_summary_items(categories, counterparties),
            "directory_next_action": directory_next_action(categories, counterparties),
            "expense_type_choices": CenterExpenseCategory.ExpenseType.choices,
            "counterparty_type_choices": Counterparty.CounterpartyType.choices,
            "filters": filters,
        },
    )


@login_required
@user_passes_test(is_admin_user)
def expense_category_create(request):
    if request.method == "POST":
        form = CenterExpenseCategoryForm(request.POST)
        if form.is_valid():
            category = form.save()
            messages.success(request, "Категория расхода создана.")
            return redirect("expense_category_edit", pk=category.pk)
    else:
        form = CenterExpenseCategoryForm()
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Создать категорию расхода",
            "subtitle": "Справочник для расходов центра и отчетов.",
            "form": form,
            "form_panel_title": "Параметры категории",
            "form_intro": "Категория влияет на новые расходы и отчеты, но не создает финансовые факты.",
            "control_title": "Контроль категории",
            "object_form_control_items": category_form_control_items(),
            "cancel_url": reverse("expense_directory_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def expense_category_edit(request, pk: int):
    category = get_object_or_404(CenterExpenseCategory, pk=pk)
    if request.method == "POST":
        form = CenterExpenseCategoryForm(request.POST, instance=category)
        if form.is_valid():
            form.save()
            messages.success(request, "Категория расхода обновлена.")
            return redirect("expense_directory_list")
    else:
        form = CenterExpenseCategoryForm(instance=category)
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Редактировать категорию расхода",
            "subtitle": category.name,
            "form": form,
            "form_panel_title": "Параметры категории",
            "form_intro": "Отключение категории скрывает ее из новых расходов, но сохраняет историю.",
            "control_title": "Контроль категории",
            "object_form_control_items": category_form_control_items(category),
            "cancel_url": reverse("expense_directory_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def expense_category_deactivate(request, pk: int):
    category = get_object_or_404(CenterExpenseCategory, pk=pk)
    if request.method == "POST":
        category.is_active = False
        category.save(update_fields=["is_active", "updated_at"])
        messages.success(request, "Категория отключена для новых расходов.")
    return redirect("expense_directory_list")


@login_required
@user_passes_test(is_admin_user)
def expense_category_activate(request, pk: int):
    category = get_object_or_404(CenterExpenseCategory, pk=pk)
    if request.method == "POST":
        category.is_active = True
        category.save(update_fields=["is_active", "updated_at"])
        messages.success(request, "Категория снова доступна для новых расходов.")
    return redirect("expense_directory_list")


@login_required
@user_passes_test(is_admin_user)
def counterparty_create(request):
    if request.method == "POST":
        form = CounterpartyForm(request.POST)
        if form.is_valid():
            counterparty = form.save()
            messages.success(request, "Контрагент создан.")
            return redirect("counterparty_edit", pk=counterparty.pk)
    else:
        form = CounterpartyForm()
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Создать контрагента",
            "subtitle": "Поставщик, фонд, спонсор или другая сторона договора.",
            "form": form,
            "form_panel_title": "Реквизиты контрагента",
            "form_intro": "Контрагент используется в расходах и договорах, но не создает платежи.",
            "control_title": "Контроль контрагента",
            "object_form_control_items": counterparty_form_control_items(),
            "cancel_url": reverse("expense_directory_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def counterparty_edit(request, pk: int):
    counterparty = get_object_or_404(Counterparty.all_objects, pk=pk)
    if request.method == "POST":
        form = CounterpartyForm(request.POST, instance=counterparty)
        if form.is_valid():
            form.save()
            messages.success(request, "Контрагент обновлен.")
            return redirect("expense_directory_list")
    else:
        form = CounterpartyForm(instance=counterparty)
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Редактировать контрагента",
            "subtitle": counterparty.name,
            "form": form,
            "form_panel_title": "Реквизиты контрагента",
            "form_intro": "Изменение справочника не переписывает связанные расходы и договоры.",
            "control_title": "Контроль контрагента",
            "object_form_control_items": counterparty_form_control_items(counterparty),
            "cancel_url": reverse("expense_directory_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def counterparty_archive(request, pk: int):
    counterparty = get_object_or_404(Counterparty.all_objects, pk=pk)
    if request.method == "POST":
        counterparty.archive()
        messages.success(request, "Контрагент архивирован.")
    return redirect("expense_directory_list")


@login_required
@user_passes_test(is_admin_user)
def counterparty_restore(request, pk: int):
    counterparty = get_object_or_404(Counterparty.all_objects, pk=pk)
    if request.method == "POST":
        counterparty.restore()
        messages.success(request, "Контрагент восстановлен.")
    return redirect("expense_directory_list")
