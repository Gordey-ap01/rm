from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from django import forms
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    ACTIVE_APPOINTMENT_STATUSES,
    Appointment,
    AppointmentConfirmation,
    BalanceAccount,
    Child,
    Consent,
    Document,
    FundingSource,
    LedgerEntry,
    ParentGuardian,
    Payment,
    ProgramBlock,
    Recommendation,
    Room,
    Service,
    StaffAvailability,
    StaffMember,
    TimeOffRequest,
    TreatmentProgram,
)

DATE_INPUT = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
TIME_INPUT = forms.TimeInput(attrs={"type": "time"}, format="%H:%M")


def appointment_conflicts(starts_at, ends_at, child, staff_member, room=None, exclude_pk=None):
    qs = Appointment.objects.filter(
        status__in=ACTIVE_APPOINTMENT_STATUSES,
        starts_at__lt=ends_at,
        ends_at__gt=starts_at,
    )
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)

    conflicts = {}
    if child:
        conflicts["child"] = qs.filter(child=child)
    if staff_member:
        conflicts["staff"] = qs.filter(staff_member=staff_member)
    if room:
        room_qs = qs.filter(room=room)
        if room_qs.count() >= max(room.capacity, 1):
            conflicts["room"] = room_qs
        else:
            conflicts["room"] = qs.none()
    return conflicts


def conflict_messages(conflicts):
    messages = []
    if conflicts.get("child") and conflicts["child"].exists():
        messages.append("у получателя уже есть занятие в это время")
    if conflicts.get("staff") and conflicts["staff"].exists():
        messages.append("специалист уже занят в это время")
    if conflicts.get("room") and conflicts["room"].exists():
        messages.append("кабинет уже занят в это время")
    return messages


def staff_unavailability_reason(staff_member, starts_at, ends_at):
    if not staff_member or not starts_at or not ends_at:
        return ""

    local_start = timezone.localtime(starts_at)
    local_end = timezone.localtime(ends_at)
    day = local_start.date()
    if local_end.date() != day:
        return "занятие должно помещаться в один рабочий день"

    if TimeOffRequest.objects.filter(
        staff_member=staff_member,
        status=TimeOffRequest.Status.APPROVED,
        starts_on__lte=day,
        ends_on__gte=day,
    ).exists():
        return "у специалиста согласован отпуск/отгул на эту дату"

    windows = list(
        StaffAvailability.objects.filter(
            staff_member=staff_member,
            weekday=day.weekday(),
            is_active=True,
        ).order_by("starts_at")
    )

    start_time = local_start.time().replace(second=0, microsecond=0)
    end_time = local_end.time().replace(second=0, microsecond=0)
    if not windows:
        if time(9, 0) <= start_time and end_time <= time(18, 0):
            return ""
        return "время вне базового рабочего окна 09:00-18:00"

    if any(window.starts_at <= start_time and end_time <= window.ends_at for window in windows):
        return ""
    return "время вне рабочего графика специалиста"


def build_local_datetime(day, clock):
    value = datetime.combine(day, clock)
    return timezone.make_aware(value, timezone.get_current_timezone())


def default_charge_amount(account, appointment):
    if not account:
        return Decimal("0")
    if account.unit == BalanceAccount.Unit.SESSIONS:
        return Decimal("-1")
    return -appointment.service.default_price


def sync_ledger_to_target(appointment, targets_by_account, user, reason):
    current_by_account = defaultdict(Decimal)
    accounts_by_id = {}
    entries = LedgerEntry.objects.filter(appointment=appointment).select_related("account")
    for entry in entries:
        current_by_account[entry.account_id] += entry.amount
        accounts_by_id[entry.account_id] = entry.account

    target_ids = set(current_by_account) | set(targets_by_account)
    for account_id in target_ids:
        account = targets_by_account.get(account_id, {}).get("account") or accounts_by_id[account_id]
        target_amount = targets_by_account.get(account_id, {}).get("amount", Decimal("0"))
        delta = target_amount - current_by_account[account_id]
        if delta == 0:
            continue

        entry_type = LedgerEntry.EntryType.CORRECTION
        if current_by_account[account_id] == 0 and delta < 0:
            entry_type = LedgerEntry.EntryType.DEBIT

        LedgerEntry.objects.create(
            account=account,
            entry_type=entry_type,
            amount=delta,
            appointment=appointment,
            created_by=user,
            reason=reason,
        )


class AppointmentForm(forms.ModelForm):
    date = forms.DateField(label="Дата", widget=DATE_INPUT, input_formats=["%Y-%m-%d"])
    time = forms.TimeField(label="Время", widget=TIME_INPUT, input_formats=["%H:%M"])
    duration_minutes = forms.IntegerField(label="Длительность, минут", min_value=5, max_value=240)
    staff_availability_override = forms.BooleanField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = Appointment
        fields = (
            "child",
            "service",
            "staff_member",
            "room",
            "program_block",
            "billing_account",
            "status",
            "admin_note",
        )
        widgets = {
            "admin_note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        instance = kwargs.get("instance")
        initial = kwargs.pop("initial", {}).copy()
        if instance and instance.pk:
            local_start = timezone.localtime(instance.starts_at)
            initial.setdefault("date", local_start.date())
            initial.setdefault("time", local_start.time().replace(second=0, microsecond=0))
            initial.setdefault("duration_minutes", instance.duration_minutes)
        else:
            initial.setdefault("status", Appointment.Status.CONFIRMED)

        super().__init__(*args, initial=initial, **kwargs)
        self.availability_warning = ""
        self.fields["child"].queryset = Child.objects.order_by("last_name", "first_name")
        self.fields["child"].label = "Получатель"
        self.fields["service"].queryset = Service.objects.filter(is_active=True).order_by("name")
        self.fields["staff_member"].queryset = StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by("full_name")
        self.fields["room"].queryset = Room.objects.filter(is_active=True).order_by("name")
        self.fields["room"].required = False
        self.fields["program_block"].queryset = ProgramBlock.objects.select_related("program", "service").order_by(
            "program__child__last_name", "program__title", "number"
        )
        self.fields["program_block"].required = False
        self.fields["billing_account"].required = False
        self.fields["billing_account"].queryset = self._billing_accounts_queryset()
        self.fields["admin_note"].required = False
        self.fields["staff_availability_override"].initial = bool(
            instance and getattr(instance, "staff_availability_override", False)
        )

    def _selected_child_id(self):
        if self.is_bound:
            return self.data.get(self.add_prefix("child"))
        if self.instance and self.instance.pk:
            return self.instance.child_id
        initial = self.initial.get("child")
        return getattr(initial, "id", initial)

    def _selected_service_id(self):
        if self.is_bound:
            return self.data.get(self.add_prefix("service"))
        if self.instance and self.instance.pk:
            return self.instance.service_id
        initial = self.initial.get("service")
        return getattr(initial, "id", initial)

    def _billing_accounts_queryset(self):
        qs = BalanceAccount.objects.select_related("child", "funding_source", "service").filter(
            status=BalanceAccount.Status.ACTIVE
        )
        service_id = self._selected_service_id()
        if service_id:
            qs = qs.filter(Q(service_scope=BalanceAccount.ServiceScope.ANY) | Q(service_id=service_id))
        return qs.order_by("child__last_name", "funding_source__name", "service__name")

    def _staff_override_requested(self) -> bool:
        if not self.is_bound:
            return bool(self.initial.get("staff_availability_override"))
        raw = self.data.get(self.add_prefix("staff_availability_override"))
        return str(raw).lower() in {"1", "on", "true", "yes"}

    def clean(self):
        cleaned = super().clean()
        day = cleaned.get("date")
        clock = cleaned.get("time")
        duration = cleaned.get("duration_minutes")
        child = cleaned.get("child")
        service = cleaned.get("service")
        staff_member = cleaned.get("staff_member")
        room = cleaned.get("room")
        account = cleaned.get("billing_account")
        program_block = cleaned.get("program_block")

        if day and clock and duration:
            starts_at = build_local_datetime(day, clock)
            ends_at = starts_at + timedelta(minutes=duration)
            cleaned["starts_at"] = starts_at
            cleaned["ends_at"] = ends_at
            self.instance.starts_at = starts_at
            self.instance.ends_at = ends_at
            messages = conflict_messages(
                appointment_conflicts(starts_at, ends_at, child, staff_member, room, exclude_pk=self.instance.pk)
            )
            if messages and cleaned.get("status") in ACTIVE_APPOINTMENT_STATUSES:
                raise forms.ValidationError("Конфликт расписания: " + ", ".join(messages) + ".")
            unavailable = staff_unavailability_reason(staff_member, starts_at, ends_at)
            if unavailable and cleaned.get("status") in ACTIVE_APPOINTMENT_STATUSES:
                self.availability_warning = unavailable
                if not self._staff_override_requested():
                    raise forms.ValidationError("Недоступность специалиста: " + unavailable + ".")
                cleaned["staff_availability_override"] = True
                cleaned["staff_availability_override_reason"] = unavailable
                self.instance.staff_availability_override = True
                self.instance.staff_availability_override_reason = unavailable
            else:
                cleaned["staff_availability_override"] = False
                cleaned["staff_availability_override_reason"] = ""
                self.instance.staff_availability_override = False
                self.instance.staff_availability_override_reason = ""

        if account and child and account.child_id != child.id:
            self.add_error("billing_account", "Счет должен принадлежать выбранному получателю.")
        if account and service and not account.can_pay_for(service):
            self.add_error("billing_account", "Счет не подходит для выбранной услуги.")
        if program_block and child and program_block.program.child_id != child.id:
            self.add_error("program_block", "Блок программы должен принадлежать выбранному получателю.")
        if program_block and service and program_block.service_id != service.id:
            self.add_error("program_block", "Блок программы должен соответствовать выбранной услуге.")
        return cleaned

    def save(self, commit=True):
        appointment = super().save(commit=False)
        appointment.starts_at = self.cleaned_data["starts_at"]
        appointment.ends_at = self.cleaned_data["ends_at"]
        appointment.staff_availability_override = self.cleaned_data.get("staff_availability_override", False)
        appointment.staff_availability_override_reason = self.cleaned_data.get("staff_availability_override_reason", "")
        if commit:
            appointment.save()
            self.save_m2m()
        return appointment


class AppointmentMoveForm(forms.Form):
    date = forms.DateField(label="Новая дата", widget=DATE_INPUT, input_formats=["%Y-%m-%d"])
    time = forms.TimeField(label="Новое время", widget=TIME_INPUT, input_formats=["%H:%M"])
    duration_minutes = forms.IntegerField(label="Длительность, минут", min_value=5, max_value=240)
    staff_member: Any = forms.ModelChoiceField(label="Специалист", queryset=StaffMember.objects.none())
    room: Any = forms.ModelChoiceField(label="Кабинет", queryset=Room.objects.none(), required=False)
    staff_availability_override = forms.BooleanField(required=False, widget=forms.HiddenInput())
    admin_note = forms.CharField(label="Комментарий к переносу", widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def __init__(self, *args, appointment, **kwargs):
        self.appointment = appointment
        super().__init__(*args, **kwargs)
        self.availability_warning = ""
        self.fields["staff_member"].queryset = StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by("full_name")
        self.fields["room"].queryset = Room.objects.filter(is_active=True).order_by("name")

    def _staff_override_requested(self) -> bool:
        if not self.is_bound:
            return bool(self.initial.get("staff_availability_override"))
        raw = self.data.get(self.add_prefix("staff_availability_override"))
        return str(raw).lower() in {"1", "on", "true", "yes"}

    def clean(self):
        cleaned = super().clean()
        day = cleaned.get("date")
        clock = cleaned.get("time")
        duration = cleaned.get("duration_minutes")
        staff_member = cleaned.get("staff_member")
        room = cleaned.get("room")
        if day and clock and duration and staff_member:
            starts_at = build_local_datetime(day, clock)
            ends_at = starts_at + timedelta(minutes=duration)
            cleaned["starts_at"] = starts_at
            cleaned["ends_at"] = ends_at
            if (
                starts_at == self.appointment.starts_at
                and ends_at == self.appointment.ends_at
                and staff_member == self.appointment.staff_member
                and room == self.appointment.room
            ):
                raise forms.ValidationError("Новое время совпадает с текущим занятием.")
            messages = conflict_messages(
                appointment_conflicts(
                    starts_at,
                    ends_at,
                    self.appointment.child,
                    staff_member,
                    room,
                    exclude_pk=self.appointment.pk,
                )
            )
            if messages:
                raise forms.ValidationError("Конфликт расписания: " + ", ".join(messages) + ".")
            unavailable = staff_unavailability_reason(staff_member, starts_at, ends_at)
            if unavailable:
                self.availability_warning = unavailable
                if not self._staff_override_requested():
                    raise forms.ValidationError("Недоступность специалиста: " + unavailable + ".")
                cleaned["staff_availability_override"] = True
                cleaned["staff_availability_override_reason"] = unavailable
            else:
                cleaned["staff_availability_override"] = False
                cleaned["staff_availability_override_reason"] = ""
        return cleaned

    @transaction.atomic
    def save(self):
        old = self.appointment
        starts_at = self.cleaned_data["starts_at"]
        ends_at = self.cleaned_data["ends_at"]
        staff_member = self.cleaned_data["staff_member"]
        room = self.cleaned_data["room"]
        note = self.cleaned_data.get("admin_note", "").strip()
        local_start = timezone.localtime(starts_at)

        old.status = Appointment.Status.RESCHEDULED
        old.admin_note = "\n".join(
            part
            for part in [
                old.admin_note,
                f"Перенесено на {local_start:%d.%m.%Y %H:%M}.",
                note,
            ]
            if part
        )
        old.save(update_fields=["status", "admin_note", "updated_at"])

        return Appointment.objects.create(
            child=old.child,
            service=old.service,
            staff_member=staff_member,
            room=room,
            starts_at=starts_at,
            ends_at=ends_at,
            status=Appointment.Status.CONFIRMED,
            attendance_status=Appointment.AttendanceStatus.UNKNOWN,
            billing_decision=Appointment.BillingDecision.UNDECIDED,
            billing_account=old.billing_account,
            source_appointment=old,
            series=old.series,
            program_block=old.program_block,
            sequence_number=old.sequence_number,
            staff_availability_override=self.cleaned_data.get("staff_availability_override", False),
            staff_availability_override_reason=self.cleaned_data.get("staff_availability_override_reason", ""),
            admin_note=note,
        )


class AppointmentCancelForm(forms.Form):
    STATUS_CHOICES = (
        (Appointment.Status.CANCELLED, "Отменено"),
        (Appointment.Status.NO_SHOW, "Неявка"),
    )
    REASON_CHOICES = (
        ("sick", "Получатель заболел"),
        ("representative_cancel", "Отмена представителем"),
        ("specialist_cancel", "Отмена специалистом"),
        ("center_cancel", "Отмена центром"),
        ("other", "Другое"),
    )

    status = forms.ChoiceField(label="Статус", choices=STATUS_CHOICES)
    reason = forms.ChoiceField(label="Причина", choices=REASON_CHOICES)
    admin_note = forms.CharField(label="Комментарий", widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def __init__(self, *args, appointment, **kwargs):
        self.appointment = appointment
        super().__init__(*args, **kwargs)

    def save(self):
        appointment = self.appointment
        reason = dict(self.REASON_CHOICES)[self.cleaned_data["reason"]]
        note = self.cleaned_data.get("admin_note", "").strip()
        appointment.status = self.cleaned_data["status"]
        appointment.admin_note = "\n".join(
            part for part in [appointment.admin_note, f"Причина отмены: {reason}.", note] if part
        )
        appointment.save(update_fields=["status", "admin_note", "updated_at"])
        return appointment


class BillingDecisionForm(forms.Form):
    DECISION_CHOICES = (
        (Appointment.BillingDecision.CHARGE, "Списать"),
        (Appointment.BillingDecision.DO_NOT_CHARGE, "Не списывать"),
    )

    billing_decision = forms.ChoiceField(label="Решение", choices=DECISION_CHOICES)
    billing_account: Any = forms.ModelChoiceField(label="Счет баланса", queryset=BalanceAccount.objects.none(), required=False)
    amount = forms.DecimalField(label="Сумма операции", max_digits=12, decimal_places=2, required=False)
    reason = forms.CharField(label="Основание", widget=forms.Textarea(attrs={"rows": 2}), required=False)

    def __init__(self, *args, appointment, **kwargs):
        self.appointment = appointment
        super().__init__(*args, **kwargs)
        self.fields["billing_account"].queryset = (
            BalanceAccount.objects.select_related("child", "funding_source", "service")
            .filter(
                Q(service_scope=BalanceAccount.ServiceScope.ANY) | Q(service=appointment.service),
                child=appointment.child,
                status=BalanceAccount.Status.ACTIVE,
            )
            .order_by("funding_source__name", "service__name")
        )
        account = appointment.billing_account
        if account:
            self.initial.setdefault("billing_account", account)
        self.initial.setdefault("billing_decision", appointment.billing_decision)

    def clean(self):
        cleaned = super().clean()
        decision = cleaned.get("billing_decision")
        account = cleaned.get("billing_account")
        amount = cleaned.get("amount")
        if decision == Appointment.BillingDecision.CHARGE:
            if not account:
                self.add_error("billing_account", "Для списания нужен счет баланса.")
            else:
                if amount is None:
                    amount = default_charge_amount(account, self.appointment)
                    cleaned["amount"] = amount
                if amount >= 0:
                    self.add_error("amount", "Списание должно быть отрицательным числом.")
                if not account.can_pay_for(self.appointment.service):
                    self.add_error("billing_account", "Счет не подходит для услуги занятия.")
        return cleaned

    @transaction.atomic
    def save(self, user):
        decision = self.cleaned_data["billing_decision"]
        reason = self.cleaned_data.get("reason", "").strip() or "Решение администратора по занятию."
        appointment = self.appointment

        if decision == Appointment.BillingDecision.CHARGE:
            account = self.cleaned_data["billing_account"]
            amount = self.cleaned_data["amount"]
            appointment.billing_decision = Appointment.BillingDecision.CHARGE
            appointment.billing_account = account
            appointment.save(update_fields=["billing_decision", "billing_account", "updated_at"])
            sync_ledger_to_target(
                appointment,
                {account.id: {"account": account, "amount": amount}},
                user,
                reason,
            )
        else:
            appointment.billing_decision = Appointment.BillingDecision.DO_NOT_CHARGE
            appointment.billing_account = None
            appointment.save(update_fields=["billing_decision", "billing_account", "updated_at"])
            sync_ledger_to_target(appointment, {}, user, reason)
        return appointment


class RepresentativeForm(forms.ModelForm):
    class Meta:
        model = ParentGuardian
        fields = (
            "last_name",
            "first_name",
            "middle_name",
            "relationship_type",
            "phone",
            "phone_alt",
            "email",
            "notes",
        )
        labels = {
            "last_name": "Фамилия",
            "first_name": "Имя",
            "middle_name": "Отчество",
            "relationship_type": "Тип представительства",
            "phone": "Телефон",
            "phone_alt": "Дополнительный телефон",
            "notes": "Примечания",
        }
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class RecipientForm(forms.ModelForm):
    class Meta:
        model = Child
        fields = (
            "last_name",
            "first_name",
            "middle_name",
            "birth_date",
            "phone",
            "email",
            "status",
            "color",
            "primary_parent",
            "diagnosis",
            "notes",
        )
        labels = {
            "last_name": "Фамилия",
            "first_name": "Имя",
            "middle_name": "Отчество",
            "birth_date": "Дата рождения",
            "phone": "Телефон получателя",
            "email": "Email получателя",
            "status": "Статус",
            "color": "Цветовая метка",
            "primary_parent": "Основной представитель",
            "diagnosis": "Диагноз/особенности",
            "notes": "Примечания",
        }
        widgets = {
            "birth_date": DATE_INPUT,
            "color": forms.TextInput(attrs={"type": "color"}),
            "diagnosis": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["primary_parent"].queryset = ParentGuardian.objects.order_by("last_name", "first_name")
        self.fields["color"].required = False

    def clean_color(self):
        return self.cleaned_data.get("color") or Child._meta.get_field("color").default


class BalanceAccountForm(forms.ModelForm):
    class Meta:
        model = BalanceAccount
        fields = (
            "child",
            "funding_source",
            "unit",
            "service_scope",
            "service",
            "initial_amount",
            "valid_from",
            "valid_until",
            "status",
            "color",
            "notes",
        )
        labels = {
            "child": "Получатель",
            "funding_source": "Источник финансирования",
            "unit": "Единица учета",
            "service_scope": "Область применения",
            "service": "Услуга",
            "initial_amount": "Начальный остаток",
            "valid_from": "Действует с",
            "valid_until": "Действует до",
            "status": "Статус",
            "color": "Цветовая метка",
            "notes": "Примечания",
        }
        widgets = {
            "valid_from": DATE_INPUT,
            "valid_until": DATE_INPUT,
            "color": forms.TextInput(attrs={"type": "color"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["child"].queryset = Child.objects.order_by("last_name", "first_name")
        self.fields["funding_source"].queryset = FundingSource.objects.order_by("name")
        self.fields["service"].queryset = Service.objects.filter(is_active=True).order_by("name")
        self.fields["service"].required = False
        self.fields["color"].required = False

    def clean_color(self):
        return self.cleaned_data.get("color") or BalanceAccount._meta.get_field("color").default

    def clean(self):
        cleaned = super().clean()
        unit = cleaned.get("unit")
        amount = cleaned.get("initial_amount")
        if unit == BalanceAccount.Unit.SESSIONS and amount is not None and amount != int(amount):
            self.add_error("initial_amount", "Для учёта в занятиях число должно быть целым.")
        return cleaned


class TreatmentProgramForm(forms.ModelForm):
    class Meta:
        model = TreatmentProgram
        fields = ("child", "title", "consultation", "status", "starts_on", "ends_on", "color", "notes")
        widgets = {
            "starts_on": DATE_INPUT,
            "ends_on": DATE_INPUT,
            "color": forms.TextInput(attrs={"type": "color"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, child: Child | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.child = child
        self.fields["child"].queryset = Child.objects.order_by("last_name", "first_name")
        self.fields["consultation"].required = False
        consultations = Appointment.objects.select_related("service", "staff_member").order_by("-starts_at")
        if child is not None:
            self.fields["child"].initial = child
            self.fields["child"].disabled = True
            consultations = consultations.filter(child=child)
        self.fields["consultation"].queryset = consultations
        self.fields["color"].required = False

    def clean_color(self):
        return self.cleaned_data.get("color") or TreatmentProgram._meta.get_field("color").default

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.child is not None:
            obj.child = self.child
        if commit:
            obj.save()
            self.save_m2m()
        return obj


class ProgramBlockForm(forms.ModelForm):
    class Meta:
        model = ProgramBlock
        fields = (
            "program",
            "number",
            "title",
            "service",
            "staff_member",
            "planned_sessions",
            "balance_account",
            "status",
            "color",
            "notes",
        )
        widgets = {
            "color": forms.TextInput(attrs={"type": "color"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, program: TreatmentProgram | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.program = program
        self.fields["program"].queryset = TreatmentProgram.objects.select_related("child").order_by("child__last_name", "title")
        self.fields["service"].queryset = Service.objects.filter(is_active=True).order_by("name")
        self.fields["staff_member"].queryset = StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by("full_name")
        self.fields["staff_member"].required = False
        self.fields["balance_account"].required = False
        accounts = BalanceAccount.objects.select_related("child", "funding_source", "service").filter(status=BalanceAccount.Status.ACTIVE)
        if program is not None:
            self.fields["program"].initial = program
            self.fields["program"].disabled = True
            accounts = accounts.filter(child=program.child)
        self.fields["balance_account"].queryset = accounts.order_by("funding_source__name", "service__name")
        self.fields["color"].required = False

    def clean_color(self):
        return self.cleaned_data.get("color") or ProgramBlock._meta.get_field("color").default

    def clean(self):
        cleaned = super().clean()
        program = self.program or cleaned.get("program")
        service = cleaned.get("service")
        account = cleaned.get("balance_account")
        if account and program and account.child_id != program.child_id:
            self.add_error("balance_account", "Счёт должен принадлежать получателю программы.")
        if account and service and not account.can_pay_for(service):
            self.add_error("balance_account", "Счёт не подходит для услуги блока.")
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.program is not None:
            obj.program = self.program
        if commit:
            obj.save()
            self.save_m2m()
        return obj


WEEKDAY_CHOICES = (
    ("0", "ПН"),
    ("1", "ВТ"),
    ("2", "СР"),
    ("3", "ЧТ"),
    ("4", "ПТ"),
    ("5", "СБ"),
    ("6", "ВС"),
)


class ProgramBlockScheduleWizardForm(forms.Form):
    start_date = forms.DateField(label="Начать с", widget=DATE_INPUT)
    end_date = forms.DateField(label="Закончить до", widget=DATE_INPUT)
    weekdays = forms.MultipleChoiceField(
        label="Дни недели",
        choices=WEEKDAY_CHOICES,
        widget=forms.CheckboxSelectMultiple,
    )
    time_from = forms.TimeField(label="Искать с", widget=TIME_INPUT)
    time_until = forms.TimeField(label="Искать до", widget=TIME_INPUT)
    duration_minutes = forms.IntegerField(label="Длительность, мин", min_value=15, max_value=240)
    requested_count = forms.IntegerField(label="Сколько занятий подобрать", min_value=1, max_value=120)
    staff_member = forms.ModelChoiceField(label="Специалист", queryset=StaffMember.objects.none())
    room = forms.ModelChoiceField(
        label="Кабинет",
        queryset=Room.objects.none(),
        help_text="Обязателен: вместимость кабинета ограничивает число одновременных специалистов.",
    )
    appointment_status = forms.ChoiceField(
        label="Статус создаваемых занятий",
        choices=(
            (Appointment.Status.PROPOSED, "Предложено / на согласование"),
            (Appointment.Status.RESERVED, "Бронь"),
            (Appointment.Status.CONFIRMED, "Подтверждено"),
        ),
        initial=Appointment.Status.PROPOSED,
    )
    allow_unpaid_reserve = forms.BooleanField(
        label="Разрешить бронь сверх доступной оплаты",
        required=False,
        help_text="Использовать только если администратор осознанно ставит неоплаченные занятия.",
    )
    allow_outside_availability = forms.BooleanField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, block: ProgramBlock, **kwargs):
        self.block = block
        super().__init__(*args, **kwargs)
        self.fields["staff_member"].queryset = StaffMember.objects.filter(
            status=StaffMember.Status.ACTIVE
        ).order_by("full_name")
        self.fields["room"].queryset = Room.objects.filter(is_active=True).order_by("name")

        remaining = max(block.planned_sessions - block.scheduled_count, 1)
        today = timezone.localdate()
        self.initial.setdefault("start_date", block.program.starts_on or today)
        self.initial.setdefault("end_date", block.program.ends_on or today + timedelta(days=30))
        self.initial.setdefault("weekdays", ["0", "1", "2", "3", "4"])
        self.initial.setdefault("time_from", time(9, 0))
        self.initial.setdefault("time_until", time(18, 0))
        self.initial.setdefault("duration_minutes", block.service.default_duration_minutes)
        self.initial.setdefault("requested_count", remaining)
        if block.staff_member_id:
            self.initial.setdefault("staff_member", block.staff_member_id)

    def clean(self):
        cleaned = super().clean()
        start_date = cleaned.get("start_date")
        end_date = cleaned.get("end_date")
        time_from = cleaned.get("time_from")
        time_until = cleaned.get("time_until")
        duration = cleaned.get("duration_minutes")

        if start_date and end_date and end_date < start_date:
            self.add_error("end_date", "Дата окончания не может быть раньше даты начала.")
        if time_from and time_until and time_until <= time_from:
            self.add_error("time_until", "Время окончания поиска должно быть позже начала.")
        if time_from and time_until and duration:
            start_dt = datetime.combine(timezone.localdate(), time_from)
            end_dt = datetime.combine(timezone.localdate(), time_until)
            if start_dt + timedelta(minutes=duration) > end_dt:
                self.add_error("duration_minutes", "Длительность не помещается в выбранное окно.")
        return cleaned


class ProgramFundsTransferForm(forms.Form):
    from_account = forms.ModelChoiceField(label="Откуда переносим", queryset=BalanceAccount.objects.none())
    to_account = forms.ModelChoiceField(
        label="Куда переносим",
        queryset=BalanceAccount.objects.none(),
        required=False,
        help_text="Если у каскада уже выбран счёт, он будет подставлен автоматически.",
    )
    amount = forms.DecimalField(label="Сумма / количество", min_value=Decimal("0.01"), max_digits=12, decimal_places=2)
    reason = forms.CharField(
        label="Основание переноса",
        widget=forms.Textarea(attrs={"rows": 3}),
        initial="Миграция средств между каскадами занятий.",
    )

    def __init__(self, *args, block: ProgramBlock, **kwargs):
        self.block = block
        super().__init__(*args, **kwargs)
        accounts = BalanceAccount.objects.select_related("funding_source", "service").filter(
            child=block.program.child,
            status=BalanceAccount.Status.ACTIVE,
        )
        target = block.balance_account
        self.fields["from_account"].queryset = accounts.exclude(pk=target.pk).order_by(
            "funding_source__name", "service__name"
        ) if target else accounts.order_by("funding_source__name", "service__name")
        self.fields["to_account"].queryset = accounts.order_by("funding_source__name", "service__name")
        if target:
            self.fields["to_account"].initial = target.pk
            self.fields["to_account"].disabled = True

    def clean(self):
        cleaned = super().clean()
        from_account = cleaned.get("from_account")
        to_account = cleaned.get("to_account") or self.block.balance_account
        amount = cleaned.get("amount")

        if not to_account:
            self.add_error("to_account", "Укажите счёт каскада, куда переносим средства.")
            return cleaned
        cleaned["to_account"] = to_account

        if from_account and from_account.pk == to_account.pk:
            self.add_error("from_account", "Нельзя переносить в тот же счёт.")
        if from_account and from_account.unit != to_account.unit:
            self.add_error("to_account", "Счета должны быть в одинаковых единицах: занятия или рубли.")
        if to_account and not to_account.can_pay_for(self.block.service):
            self.add_error("to_account", "Целевой счёт не подходит для услуги этого каскада.")
        if from_account and amount and amount > from_account.current_balance:
            self.add_error("amount", "На исходном счёте недостаточно средств.")
        return cleaned

    def estimated_sessions_after_transfer(self) -> int | None:
        if not self.is_valid():
            return None
        to_account = self.cleaned_data["to_account"]
        amount = self.cleaned_data["amount"]
        if to_account.unit == BalanceAccount.Unit.SESSIONS:
            return int(amount.to_integral_value(rounding=ROUND_FLOOR))
        price = self.block.service.default_price
        if not price or price <= 0:
            return None
        return int((amount / price).to_integral_value(rounding=ROUND_FLOOR))


class AppointmentConfirmationSendForm(forms.Form):
    target_type = forms.ChoiceField(label="Кому отправить")
    subject = forms.CharField(label="Тема письма", max_length=200)
    message = forms.CharField(label="Текст письма", widget=forms.Textarea(attrs={"rows": 7}))

    def __init__(self, *args, appointment, **kwargs):
        self.appointment = appointment
        super().__init__(*args, **kwargs)
        local_start = timezone.localtime(appointment.starts_at)
        self.targets = self._build_targets(appointment)
        if self.targets:
            self.fields["target_type"].choices = [(key, target["label"]) for key, target in self.targets.items()]
        else:
            self.fields["target_type"].choices = [("", "Нет email у специалиста, представителя или получателя")]
            self.fields["target_type"].disabled = True
        self.initial.setdefault("subject", f"Подтверждение занятия {local_start:%d.%m.%Y %H:%M}")
        self.initial.setdefault(
            "message",
            "\n".join(
                [
                    "Здравствуйте.",
                    "",
                    "Просим подтвердить занятие:",
                    f"Получатель: {appointment.child.full_name}",
                    f"Услуга: {appointment.service.name}",
                    f"Специалист: {appointment.staff_member.full_name}",
                    f"Дата и время: {local_start:%d.%m.%Y %H:%M}",
                    f"Кабинет: {appointment.room.name if appointment.room else 'не указан'}",
                    "",
                    "Ответьте по ссылке ниже: подтвердить или отклонить.",
                ]
            ),
        )

    def _build_targets(self, appointment):
        targets = {}
        staff_email = appointment.staff_member.email or (
            appointment.staff_member.user.email if appointment.staff_member.user_id else ""
        )
        if staff_email:
            targets[AppointmentConfirmation.TargetType.SPECIALIST] = {
                "label": f"Сначала специалисту: {appointment.staff_member.full_name} ({staff_email})",
                "email": staff_email,
                "representative": None,
            }
        representative = appointment.child.primary_parent
        if representative.email:
            targets[AppointmentConfirmation.TargetType.REPRESENTATIVE] = {
                "label": f"Представителю: {representative.full_name} ({representative.email})",
                "email": representative.email,
                "representative": representative,
            }
        if appointment.child.email:
            targets[AppointmentConfirmation.TargetType.RECIPIENT] = {
                "label": f"Получателю: {appointment.child.full_name} ({appointment.child.email})",
                "email": appointment.child.email,
                "representative": None,
            }
        return targets

    def clean_target_type(self):
        target_type = self.cleaned_data["target_type"]
        if target_type not in self.targets:
            raise forms.ValidationError("Для выбранного адресата нет email.")
        return target_type

    def save(self, user):
        target = self.targets[self.cleaned_data["target_type"]]
        return AppointmentConfirmation.objects.create(
            appointment=self.appointment,
            target_type=self.cleaned_data["target_type"],
            representative=target["representative"],
            email=target["email"],
            subject=self.cleaned_data["subject"],
            message=self.cleaned_data["message"],
            sent_by=user,
        )


class ConfirmationResponseForm(forms.Form):
    ACTION_CHOICES = (
        ("confirm", "Подтвердить"),
        ("decline", "Отклонить"),
    )
    action = forms.ChoiceField(label="Решение", choices=ACTION_CHOICES)
    response_note = forms.CharField(label="Комментарий", widget=forms.Textarea(attrs={"rows": 3}), required=False)


class StaffAvailabilityForm(forms.ModelForm):
    class Meta:
        model = StaffAvailability
        fields = ("weekday", "starts_at", "ends_at", "note")
        labels = {
            "weekday": "День недели",
            "starts_at": "Начало",
            "ends_at": "Окончание",
            "note": "Комментарий",
        }
        widgets = {
            "starts_at": TIME_INPUT,
            "ends_at": TIME_INPUT,
        }


class TimeOffRequestForm(forms.ModelForm):
    class Meta:
        model = TimeOffRequest
        fields = ("request_type", "starts_on", "ends_on", "reason")
        labels = {
            "request_type": "Тип заявки",
            "starts_on": "Дата начала",
            "ends_on": "Дата окончания",
            "reason": "Причина/комментарий",
        }
        widgets = {
            "starts_on": DATE_INPUT,
            "ends_on": DATE_INPUT,
            "reason": forms.Textarea(attrs={"rows": 3}),
        }


class RecommendationForm(forms.ModelForm):
    class Meta:
        model = Recommendation
        fields = ("child", "staff_member", "appointment", "category", "title", "body", "due_on")
        labels = {
            "child": "Получатель",
            "staff_member": "Специалист",
            "appointment": "Занятие",
            "category": "Категория",
            "title": "Заголовок",
            "body": "Текст",
            "due_on": "Срок",
        }
        widgets = {"due_on": DATE_INPUT, "body": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["child"].queryset = Child.objects.order_by("last_name", "first_name")
        self.fields["staff_member"].queryset = StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by("full_name")
        self.fields["appointment"].required = False


class DocumentForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ("child", "category", "title", "file", "issued_on", "expires_on", "note")
        labels = {
            "child": "Получатель",
            "category": "Категория",
            "title": "Название",
            "file": "Файл",
            "issued_on": "Дата выпуска",
            "expires_on": "Действителен до",
            "note": "Комментарий",
        }
        widgets = {
            "issued_on": DATE_INPUT,
            "expires_on": DATE_INPUT,
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["child"].queryset = Child.objects.order_by("last_name", "first_name")


class ConsentForm(forms.ModelForm):
    class Meta:
        model = Consent
        fields = ("child", "consent_type", "document", "signed_on", "expires_on", "note")
        labels = {
            "child": "Получатель",
            "consent_type": "Тип согласия",
            "document": "Документ",
            "signed_on": "Дата подписания",
            "expires_on": "Действителен до",
            "note": "Примечание",
        }
        widgets = {
            "signed_on": DATE_INPUT,
            "expires_on": DATE_INPUT,
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["child"].queryset = Child.objects.order_by("last_name", "first_name")
        self.fields["document"].required = False


class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ("balance_account", "amount", "method", "paid_at", "reference", "comment")
        labels = {
            "balance_account": "Счёт",
            "amount": "Сумма",
            "method": "Способ оплаты",
            "paid_at": "Дата оплаты",
            "reference": "Номер платёжки / комментарий",
            "comment": "Комментарий",
        }
        widgets = {
            "paid_at": DATE_INPUT,
            "comment": forms.Textarea(attrs={"rows": 3}),
        }


class TimeSheetFilterForm(forms.Form):
    date_from = forms.DateField(label="Дата начала", widget=DATE_INPUT)
    date_to = forms.DateField(label="Дата окончания", widget=DATE_INPUT)

    def __init__(self, *args, staff: StaffMember | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        if staff is not None:
            self.staff = staff

    def clean(self):
        cleaned = super().clean()
        date_from = cleaned.get("date_from")
        date_to = cleaned.get("date_to")
        if date_from and date_to and date_to < date_from:
            raise forms.ValidationError("Дата окончания не может быть раньше даты начала.")
        return cleaned


class GrantReportFilterForm(forms.Form):
    funding: Any = forms.ModelChoiceField(
        label="Источник финансирования",
        queryset=FundingSource.objects.filter(archived_at__isnull=True).order_by("name"),
    )
    date_from = forms.DateField(label="Дата начала", widget=DATE_INPUT)
    date_to = forms.DateField(label="Дата окончания", widget=DATE_INPUT)

    def clean(self):
        cleaned = super().clean()
        date_from = cleaned.get("date_from")
        date_to = cleaned.get("date_to")
        if date_from and date_to and date_to < date_from:
            raise forms.ValidationError("Дата окончания не может быть раньше даты начала.")
        return cleaned
