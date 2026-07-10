"""Источники финансирования: гранты, фонды, спонсоры, личные средства."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import FundingSourceForm
from operations.models import FundingSource

from ._common import is_admin_user

PROJECT_SOURCE_TYPES = {
    FundingSource.SourceType.GRANT,
    FundingSource.SourceType.SPONSOR,
    FundingSource.SourceType.CHARITY_FUND,
    FundingSource.SourceType.MATERNITY_CAPITAL,
    FundingSource.SourceType.CERTIFICATE,
}


def funding_source_summary_items(sources: list[FundingSource]) -> list[dict[str, str]]:
    active_count = sum(1 for source in sources if not source.is_archived)
    project_count = sum(
        1
        for source in sources
        if source.source_type in PROJECT_SOURCE_TYPES and not source.is_archived
    )
    transferable_count = sum(
        1
        for source in sources
        if source.transfer_policy != FundingSource.TransferPolicy.NOT_TRANSFERABLE
        and not source.is_archived
    )
    missing_period_count = sum(
        1
        for source in sources
        if source.source_type in PROJECT_SOURCE_TYPES
        and not source.is_archived
        and (not source.starts_on or not source.ends_on)
    )
    archived_count = sum(1 for source in sources if source.is_archived)
    return [
        {
            "label": "Всего",
            "value": str(len(sources)),
            "hint": "источников финансирования",
        },
        {
            "label": "Активны",
            "value": str(active_count),
            "hint": "доступны для счетов",
        },
        {
            "label": "Гранты/фонды",
            "value": str(project_count),
            "hint": "проектные источники",
        },
        {
            "label": "Перенос средств",
            "value": str(transferable_count),
            "hint": "разрешен внутри или между получателями",
        },
        {
            "label": "Без периода",
            "value": str(missing_period_count),
            "hint": "проверьте сроки проектов",
        },
        {
            "label": "Архив",
            "value": str(archived_count),
            "hint": "история счетов сохранена",
        },
    ]


def funding_source_next_action(sources: list[FundingSource]) -> dict[str, str]:
    active_count = sum(1 for source in sources if not source.is_archived)
    missing_period_count = sum(
        1
        for source in sources
        if source.source_type in PROJECT_SOURCE_TYPES
        and not source.is_archived
        and (not source.starts_on or not source.ends_on)
    )
    transferable_count = sum(
        1
        for source in sources
        if source.transfer_policy != FundingSource.TransferPolicy.NOT_TRANSFERABLE
        and not source.is_archived
    )
    archived_count = sum(1 for source in sources if source.is_archived)
    if not sources:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Создать первый источник",
            "detail": "Нужен минимум один источник для счетов баланса и оплат.",
            "href": reverse("funding_source_create"),
        }
    if active_count == 0:
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Создать или восстановить источник",
            "detail": f"Все источники в архиве: {archived_count}. Новые счета требуют активного источника.",
            "href": "#funding-source-list",
        }
    if missing_period_count:
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Проверить сроки грантов и фондов",
            "detail": f"Проектных источников без полного периода: {missing_period_count}.",
            "href": "#funding-source-list",
        }
    if not transferable_count:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Проверить правила переноса средств",
            "detail": "Если средства можно переносить между каскадами, настройте политику источника.",
            "href": "#funding-source-list",
        }
    if archived_count:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Проверить архив источников",
            "detail": f"В архиве: {archived_count}. История счетов и проводок сохраняется.",
            "href": "#funding-source-list",
        }
    return {
        "tone": "success",
        "label": "Следующее действие",
        "title": "Источники готовы к работе",
        "detail": "Можно вести счета баланса, грантовые квоты и отчеты.",
        "href": "#funding-source-list",
    }


def funding_source_form_control_items(
    source: FundingSource | None = None,
) -> list[dict[str, str]]:
    type_detail = (
        "Тип отделяет личные оплаты от грантов, фондов, спонсоров, сертификатов и материнского капитала."
    )
    period_detail = (
        "Для грантов, фондов и проектов задавайте даты действия, чтобы отчеты и квоты имели понятный период."
    )
    transfer_detail = (
        "Политика переноса определяет, можно ли двигать средства между каскадами одного получателя "
        "или между получателями."
    )
    archive_detail = (
        "Архивирование скрывает источник из новых операций, но сохраняет историю счетов, платежей и проводок."
    )
    if source:
        type_detail = f"Сейчас: {source.get_source_type_display()}."
        period_detail = f"Период: {source.starts_on or 'не задан'} - {source.ends_on or 'не задан'}."
        transfer_detail = f"Сейчас: {source.get_transfer_policy_display()}."
        archive_detail = (
            "Источник в архиве; новые счета и операции должны использовать другой источник."
            if source.is_archived
            else "Источник доступен для новых счетов, квот и платежей."
        )
    return [
        {
            "title": "Тип источника",
            "detail": type_detail,
        },
        {
            "title": "Период проекта",
            "detail": period_detail,
        },
        {
            "title": "Перенос средств",
            "detail": transfer_detail,
        },
        {
            "title": "Счета и архив",
            "detail": archive_detail,
        },
    ]


@login_required
@user_passes_test(is_admin_user)
def funding_source_list(request):
    sources = list(FundingSource.all_objects.order_by("archived_at", "name"))
    return render(
        request,
        "operations/funding_source_list.html",
        {
            "sources": sources,
            "funding_summary_items": funding_source_summary_items(sources),
            "funding_next_action": funding_source_next_action(sources),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def funding_source_create(request):
    if request.method == "POST":
        form = FundingSourceForm(request.POST)
        if form.is_valid():
            source = form.save()
            messages.success(request, "Источник финансирования создан.")
            return redirect("funding_source_edit", pk=source.pk)
    else:
        form = FundingSourceForm()
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Создать источник финансирования",
            "subtitle": "Грант, фонд, спонсор, личные средства или сертификат.",
            "form": form,
            "form_panel_title": "Параметры источника",
            "form_intro": (
                "Источник финансирования задает происхождение средств для балансовых счетов, квот и отчетов."
            ),
            "control_title": "Контроль источника",
            "object_form_control_items": funding_source_form_control_items(),
            "cancel_url": reverse("funding_source_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def funding_source_edit(request, pk: int):
    source = get_object_or_404(FundingSource.all_objects, pk=pk)
    if request.method == "POST":
        form = FundingSourceForm(request.POST, instance=source)
        if form.is_valid():
            form.save()
            messages.success(request, "Источник финансирования обновлен.")
            return redirect("funding_source_list")
    else:
        form = FundingSourceForm(instance=source)
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Редактировать источник финансирования",
            "subtitle": source.name,
            "form": form,
            "form_panel_title": "Параметры источника",
            "form_intro": (
                "Изменения источника влияют на новые счета, квоты и отчеты, но не переписывают ledger-историю."
            ),
            "control_title": "Контроль источника",
            "object_form_control_items": funding_source_form_control_items(source),
            "cancel_url": reverse("funding_source_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def funding_source_archive(request, pk: int):
    source = get_object_or_404(FundingSource.all_objects, pk=pk)
    if request.method == "POST":
        source.archive()
        messages.success(request, "Источник финансирования архивирован.")
    return redirect("funding_source_list")


@login_required
@user_passes_test(is_admin_user)
def funding_source_restore(request, pk: int):
    source = get_object_or_404(FundingSource.all_objects, pk=pk)
    if request.method == "POST":
        source.restore()
        messages.success(request, "Источник финансирования восстановлен.")
    return redirect("funding_source_list")
