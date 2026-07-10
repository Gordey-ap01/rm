"""Экран «Завтра» для администраторов."""

from __future__ import annotations

import contextlib

from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from operations.services import reports as reports_svc

from ._common import is_admin_user


def _tomorrow_summary_items(overview) -> list[dict[str, str]]:
    summary = overview.summary
    return [
        {
            "label": "Занятий",
            "value": str(summary["appointments_count"]),
            "hint": "в расписании дня",
        },
        {
            "label": "Списание",
            "value": str(summary["needs_billing_count"]),
            "hint": "ждут решения",
        },
        {
            "label": "Посещение",
            "value": str(summary["needs_attendance_count"]),
            "hint": "не отмечено",
        },
        {
            "label": "Согласования",
            "value": str(summary["pending_confirmations_count"]),
            "hint": "ожидают ответа",
        },
        {
            "label": "Отпуска",
            "value": str(summary["pending_time_off_count"]),
            "hint": "заявки специалистов",
        },
        {
            "label": "Балансы",
            "value": str(summary["low_balances_count"]),
            "hint": "низкие остатки",
        },
    ]


def _tomorrow_next_action(overview) -> dict[str, str]:
    summary = overview.summary
    if summary["pending_confirmations_count"]:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Проверить согласования",
            "detail": f"Ожидают ответа или требуют внимания: {summary['pending_confirmations_count']}.",
            "href": "#tomorrow-confirmations",
        }
    if summary["needs_billing_count"]:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Решить списания",
            "detail": f"Занятий без решения по списанию: {summary['needs_billing_count']}.",
            "href": "#tomorrow-billing",
        }
    if summary["needs_attendance_count"]:
        return {
            "tone": "info",
            "label": "Следующий шаг",
            "title": "Проверить посещение",
            "detail": f"Занятий без отметки посещения: {summary['needs_attendance_count']}.",
            "href": "#tomorrow-attendance",
        }
    if summary["pending_time_off_count"]:
        return {
            "tone": "info",
            "label": "Следующий шаг",
            "title": "Разобрать заявки",
            "detail": f"Заявок специалистов на отпуск/больничный: {summary['pending_time_off_count']}.",
            "href": "#tomorrow-time-off",
        }
    if summary["low_balances_count"]:
        return {
            "tone": "info",
            "label": "Следующий шаг",
            "title": "Проверить балансы",
            "detail": f"Счетов с низким остатком: {summary['low_balances_count']}.",
            "href": "#tomorrow-balances",
        }
    return {
        "tone": "success",
        "label": "Следующий шаг",
        "title": "Открыть расписание",
        "detail": "Критичных задач на выбранный день нет.",
        "href": f"{reverse('schedule')}?date={overview.date.isoformat()}",
    }


def _tomorrow_control_items(overview) -> list[dict[str, str]]:
    summary = overview.summary
    items = []
    if summary["appointments_count"]:
        items.append(
            {
                "tone": "info",
                "title": "Проверьте ресурсы дня",
                "text": "Список показывает получателей, специалистов, услуги и кабинеты на выбранную дату.",
            }
        )
    if summary["pending_confirmations_count"]:
        items.append(
            {
                "tone": "warning",
                "title": "Есть незакрытые согласования",
                "text": "Проверьте ответы представителей, получателей и специалистов до начала занятий.",
            }
        )
    if summary["low_balances_count"]:
        items.append(
            {
                "tone": "warning",
                "title": "Есть низкие балансы",
                "text": "Перед подтверждением занятий проверьте счета с недостаточным остатком.",
            }
        )
    if summary["pending_time_off_count"]:
        items.append(
            {
                "tone": "info",
                "title": "Есть заявки специалистов",
                "text": "Согласование отпуска или больничного может потребовать переноса занятий.",
            }
        )
    if not items:
        items.append(
            {
                "tone": "success",
                "title": "Критичных задач нет",
                "text": "На выбранный день нет срочных предупреждений в текущей сводке.",
            }
        )
    return items


@login_required
@user_passes_test(is_admin_user)
def tomorrow(request):
    target = timezone.localdate() + timezone.timedelta(days=1)
    if request.GET.get("date"):
        from datetime import datetime

        with contextlib.suppress(ValueError):
            target = datetime.fromisoformat(request.GET["date"]).date()
    overview = reports_svc.tomorrow_overview(target)
    return render(
        request,
        "operations/tomorrow.html",
        {
            "overview": overview,
            "tomorrow_summary_items": _tomorrow_summary_items(overview),
            "tomorrow_next_action": _tomorrow_next_action(overview),
            "tomorrow_control_items": _tomorrow_control_items(overview),
            "schedule_day_url": f"{reverse('schedule')}?date={overview.date.isoformat()}",
        },
    )
