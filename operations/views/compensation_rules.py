"""Правила начисления специалистам."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.forms import StaffCompensationRuleForm
from operations.models import StaffCompensationRule

from ._common import is_admin_user


def compensation_rule_is_effective(rule: StaffCompensationRule, today) -> bool:
    return (
        rule.is_active
        and (rule.starts_on is None or rule.starts_on <= today)
        and (rule.ends_on is None or rule.ends_on >= today)
    )


def compensation_rule_status(rule: StaffCompensationRule, today) -> dict[str, str]:
    if not rule.is_active:
        return {"label": "отключена", "class": "muted-pill"}
    if rule.starts_on is not None and rule.starts_on > today:
        return {"label": "ожидает", "class": "muted-pill"}
    if rule.ends_on is not None and rule.ends_on < today:
        return {"label": "истекла", "class": "balance-warning"}
    return {"label": "активна", "class": ""}


def prepare_compensation_rules(
    rules: list[StaffCompensationRule],
) -> list[StaffCompensationRule]:
    today = timezone.localdate()
    for rule in rules:
        status = compensation_rule_status(rule, today)
        rule.effective_status_label = status["label"]
        rule.effective_status_class = status["class"]
    return rules


def compensation_rule_summary_items(
    rules: list[StaffCompensationRule],
) -> list[dict[str, str]]:
    today = timezone.localdate()
    active_count = sum(1 for rule in rules if compensation_rule_is_effective(rule, today))
    source_specific_count = sum(1 for rule in rules if rule.funding_source_id)
    service_specific_count = sum(1 for rule in rules if rule.service_id)
    duration_specific_count = sum(
        1 for rule in rules if rule.min_duration_minutes or rule.max_duration_minutes
    )
    inactive_count = sum(1 for rule in rules if not rule.is_active)
    return [
        {
            "label": "Всего",
            "value": str(len(rules)),
            "hint": "правил начисления",
        },
        {
            "label": "Активны",
            "value": str(active_count),
            "hint": "действуют сегодня",
        },
        {
            "label": "По источнику",
            "value": str(source_specific_count),
            "hint": "грант, фонд или личные средства",
        },
        {
            "label": "По услуге",
            "value": str(service_specific_count),
            "hint": "ставка зависит от услуги",
        },
        {
            "label": "По длительности",
            "value": str(duration_specific_count),
            "hint": "есть границы минут",
        },
        {
            "label": "Отключены",
            "value": str(inactive_count),
            "hint": "не участвуют в расчете",
        },
    ]


def compensation_rule_next_action(rules: list[StaffCompensationRule]) -> dict[str, str]:
    today = timezone.localdate()
    inactive_count = sum(1 for rule in rules if not rule.is_active)
    future_count = sum(
        1 for rule in rules if rule.is_active and rule.starts_on is not None and rule.starts_on > today
    )
    expired_count = sum(
        1 for rule in rules if rule.is_active and rule.ends_on is not None and rule.ends_on < today
    )
    source_specific_count = sum(
        1
        for rule in rules
        if compensation_rule_is_effective(rule, today) and rule.funding_source_id
    )
    if not rules:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Создать первую ставку",
            "detail": "Без ставки табель покажет начисление 0 и предупреждение руководителю.",
            "href": reverse("staff_compensation_rule_create"),
        }
    if expired_count:
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Проверить истекшие ставки",
            "detail": f"Активных правил с прошедшей датой окончания: {expired_count}.",
            "href": "#compensation-rule-list",
        }
    if future_count:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Проверить будущие ставки",
            "detail": f"Активных правил с будущей датой начала: {future_count}.",
            "href": "#compensation-rule-list",
        }
    if inactive_count:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Проверить отключенные ставки",
            "detail": f"Отключенных правил: {inactive_count}.",
            "href": "#compensation-rule-list",
        }
    if not source_specific_count:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Проверить ставки по грантам",
            "detail": "Если зарплата по грантам отличается, добавьте ставки по источнику.",
            "href": "#compensation-rule-list",
        }
    return {
        "tone": "success",
        "label": "Следующее действие",
        "title": "Ставки готовы к табелю",
        "detail": "Табель сможет подобрать правила по специалисту, услуге, источнику и длительности.",
        "href": "#compensation-rule-list",
    }


def compensation_rule_form_control_items(
    rule: StaffCompensationRule | None = None,
) -> list[dict[str, str]]:
    scope_detail = (
        "Правило может быть общим для специалиста или уточненным по услуге и источнику финансирования."
    )
    rate_detail = (
        "Тип ставки определяет расчет: фиксированно за занятие или по длительности, если выбран почасовой вариант."
    )
    duration_detail = (
        "Границы минут помогают разделить ставки для коротких, стандартных и длинных занятий."
    )
    period_detail = (
        "Период и активность ограничивают, какие правила доступны табелю и расчетному листу."
    )
    if rule:
        scope_detail = (
            f"Специалист: {rule.staff_member}. "
            f"Услуга: {rule.service or 'любая'}. Источник: {rule.funding_source or 'любой'}."
        )
        rate_detail = f"Сейчас: {rule.get_rate_type_display()}, сумма {rule.amount}."
        duration_detail = (
            f"Минуты: {rule.min_duration_minutes or 'без минимума'} - "
            f"{rule.max_duration_minutes or 'без максимума'}."
        )
        period_detail = (
            f"Период: {rule.starts_on or 'без начала'} - {rule.ends_on or 'без окончания'}, "
            f"{'активно' if rule.is_active else 'выключено'}."
        )
    return [
        {
            "title": "Область правила",
            "detail": scope_detail,
        },
        {
            "title": "Тип ставки",
            "detail": rate_detail,
        },
        {
            "title": "Длительность занятия",
            "detail": duration_detail,
        },
        {
            "title": "Период и payroll",
            "detail": period_detail,
        },
    ]


@login_required
@user_passes_test(is_admin_user)
def staff_compensation_rule_list(request):
    rules = list(StaffCompensationRule.objects.select_related(
        "staff_member", "service", "funding_source"
    ).order_by(
        "staff_member__full_name",
        "service__name",
        "funding_source__name",
        "-is_active",
        "-starts_on",
    ))
    prepare_compensation_rules(rules)
    return render(
        request,
        "operations/staff_compensation_rule_list.html",
        {
            "rules": rules,
            "compensation_summary_items": compensation_rule_summary_items(rules),
            "compensation_next_action": compensation_rule_next_action(rules),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def staff_compensation_rule_create(request):
    if request.method == "POST":
        form = StaffCompensationRuleForm(request.POST)
        if form.is_valid():
            rule = form.save()
            messages.success(request, "Правило начисления создано.")
            return redirect("staff_compensation_rule_edit", pk=rule.pk)
    else:
        form = StaffCompensationRuleForm()
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Создать правило начисления",
            "subtitle": "Ставка специалиста по услуге, источнику финансирования и периоду.",
            "form": form,
            "form_panel_title": "Параметры ставки",
            "form_intro": (
                "Ставка определяет, как табель и расчетный лист начисляют оплату специалисту."
            ),
            "control_title": "Контроль ставки",
            "object_form_control_items": compensation_rule_form_control_items(),
            "cancel_url": reverse("staff_compensation_rule_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def staff_compensation_rule_edit(request, pk: int):
    rule = get_object_or_404(
        StaffCompensationRule.objects.select_related("staff_member", "service", "funding_source"),
        pk=pk,
    )
    if request.method == "POST":
        form = StaffCompensationRuleForm(request.POST, instance=rule)
        if form.is_valid():
            form.save()
            messages.success(request, "Правило начисления обновлено.")
            return redirect("staff_compensation_rule_list")
    else:
        form = StaffCompensationRuleForm(instance=rule)
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Редактировать правило начисления",
            "subtitle": str(rule),
            "form": form,
            "form_panel_title": "Параметры ставки",
            "form_intro": (
                "Изменения ставки влияют на будущий подбор правил в табеле и расчетных листах."
            ),
            "control_title": "Контроль ставки",
            "object_form_control_items": compensation_rule_form_control_items(rule),
            "cancel_url": reverse("staff_compensation_rule_list"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def staff_compensation_rule_toggle(request, pk: int):
    rule = get_object_or_404(StaffCompensationRule, pk=pk)
    if request.method == "POST":
        rule.is_active = not rule.is_active
        rule.save(update_fields=["is_active", "updated_at"])
        if rule.is_active:
            messages.success(request, "Правило начисления включено.")
        else:
            messages.success(request, "Правило начисления отключено.")
    return redirect("staff_compensation_rule_list")
