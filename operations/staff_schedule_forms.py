"""Forms for proposed recurring staff schedules.

The weekly payload deliberately stays in regular HTML form fields.  JavaScript
only adds another pair of time inputs; all validation and persistence remain on
the server and in ``operations.services.staff_schedules``.
"""

from __future__ import annotations

import uuid
from datetime import time, timedelta

from django import forms
from django.utils import timezone

from operations.models import StaffMember

WEEKDAY_LABELS = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)


def empty_week() -> list[dict]:
    """Return the explicit seven-day payload used for a new request."""
    return [
        {
            "weekday": weekday,
            "label": WEEKDAY_LABELS[weekday],
            "closed": weekday >= 5,
            "windows": [] if weekday >= 5 else [{"start": "09:00", "end": "18:00"}],
        }
        for weekday in range(7)
    ]


class StaffScheduleRequestForm(forms.Form):
    staff_member = forms.ModelChoiceField(
        label="Специалист",
        queryset=StaffMember.objects.none(),
    )
    effective_from = forms.DateField(
        label="График действует с",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    reason = forms.CharField(
        label="Основание изменения",
        min_length=5,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Объясните, почему меняется постоянный график.",
    )
    request_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(
        self,
        *args,
        staff_queryset=None,
        selected_staff=None,
        show_staff_select: bool = True,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.fields["staff_member"].queryset = (
            staff_queryset
            if staff_queryset is not None
            else StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by("full_name")
        )
        if selected_staff is not None:
            self.fields["staff_member"].initial = selected_staff
        if not show_staff_select:
            self.fields["staff_member"].required = False
            self.fields["staff_member"].widget = forms.HiddenInput()
        if not self.is_bound:
            self.initial.setdefault("effective_from", timezone.localdate() + timedelta(days=1))
            self.initial.setdefault("request_key", uuid.uuid4())
        self.week = empty_week()

    @staticmethod
    def _read_time(raw: str) -> time | None:
        try:
            return time.fromisoformat(raw)
        except (TypeError, ValueError):
            return None

    def clean_effective_from(self):
        effective_from = self.cleaned_data["effective_from"]
        if effective_from <= timezone.localdate():
            raise forms.ValidationError("Выберите дату, начиная с завтрашнего дня.")
        return effective_from

    def clean(self):
        cleaned = super().clean()
        week: list[dict] = []
        for weekday, label in enumerate(WEEKDAY_LABELS):
            closed = self.data.get(f"day_{weekday}_closed") in {"1", "true", "on"}
            raw_starts = self.data.getlist(f"day_{weekday}_start")
            raw_ends = self.data.getlist(f"day_{weekday}_end")
            windows: list[dict[str, str]] = []
            malformed = False

            if len(raw_starts) != len(raw_ends):
                malformed = True
            for start, end in zip(raw_starts, raw_ends, strict=False):
                start, end = start.strip(), end.strip()
                if not start and not end:
                    continue
                if not start or not end or not self._read_time(start) or not self._read_time(end):
                    malformed = True
                    # Keep the exact row visible after a validation error.
                    windows.append({"start": start, "end": end})
                    continue
                windows.append({"start": start, "end": end})

            if malformed:
                self.add_error(None, f"{label}: заполните время начала и окончания каждого интервала.")
            if closed and windows:
                self.add_error(None, f"{label}: у выходного дня не должно быть рабочих интервалов.")
            if not closed and not windows:
                self.add_error(None, f"{label}: укажите хотя бы один рабочий интервал или отметьте выходной.")
            week.append(
                {"weekday": weekday, "label": label, "closed": closed, "windows": windows}
            )
        self.week = week
        cleaned["week"] = [
            {"weekday": item["weekday"], "closed": item["closed"], "windows": item["windows"]}
            for item in week
        ]
        return cleaned


class StaffScheduleDecisionForm(forms.Form):
    ACTION_LABELS = {
        "approve": "Согласовать",
        "reject": "Отклонить",
        "confirm": "Подтвердить решение администратора",
    }

    action = forms.ChoiceField(label="Решение")
    reason = forms.CharField(
        label="Основание решения",
        min_length=5,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    expected_decision_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    expected_revision_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    request_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, is_director: bool, allowed_actions: tuple[str, ...], **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["action"].choices = [("", "Выберите решение")] + [
            (action, self.ACTION_LABELS[action]) for action in allowed_actions
        ]
        self.fields["reason"].label = (
            "Основание решения руководителя" if is_director else "Основание решения администратора"
        )
        if not self.is_bound:
            self.initial.setdefault("request_key", uuid.uuid4())
