from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from django import forms
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import Q, Sum
from django.forms.models import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from .models import (
    ACTIVE_APPOINTMENT_STATUSES,
    Appointment,
    AppointmentConfirmation,
    AppointmentParticipant,
    AppointmentRoomOverride,
    AppointmentStaffAssignment,
    BalanceAccount,
    CenterExpense,
    CenterExpenseCategory,
    CenterLegalProfile,
    Certificate,
    Child,
    Consent,
    ContractAct,
    ContractTemplate,
    Counterparty,
    Document,
    DonationContract,
    EquipmentAsset,
    ExpenseFundingSplit,
    FundingServiceQuota,
    FundingSource,
    FundingStaffAllocation,
    GrantRecipientAllocation,
    LedgerEntry,
    OrganizationServiceContract,
    OrganizationServiceContractLine,
    ParentGuardian,
    Payment,
    ProgramBlock,
    RecipientRepresentative,
    Recommendation,
    Room,
    Service,
    ServiceContract,
    ServiceContractLine,
    StaffAvailability,
    StaffCompensationRule,
    StaffMember,
    TimeOffRequest,
    TreatmentProgram,
)
from .schedule_validation import (
    appointment_group_conflicts,
    build_local_datetime,
    conflict_messages,
    staff_unavailability_reason,
)

DATE_INPUT = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
TIME_INPUT = forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
GROUP_BILLING_PARTICIPANT_REQUIRED = (
    "Для группового занятия выберите конкретного участника."
)
CONTRACT_IMPORT_TYPE_CHOICES = (
    ("counterparties", "Контрагенты"),
    ("expenses", "Расходы"),
    ("donation_contracts", "Договоры пожертвования"),
    ("service_contracts", "Договоры с получателями"),
)


def default_charge_amount(account, appointment):
    if not account:
        return Decimal("0")
    if account.unit == BalanceAccount.Unit.SESSIONS:
        return Decimal("-1")
    return -appointment.service.default_price


def single_participant_or_none(appointment):
    participants = list(appointment.participants.order_by("pk")[:2])
    if len(participants) > 1:
        raise ValueError(GROUP_BILLING_PARTICIPANT_REQUIRED)
    return participants[0] if participants else None


def sync_ledger_to_target(appointment, targets_by_account, user, reason):
    participant = single_participant_or_none(appointment)
    current_by_account = defaultdict(Decimal)
    accounts_by_id = {}
    entries = LedgerEntry.objects.filter(appointment=appointment).select_related("account")
    if participant:
        entries = entries.filter(
            Q(appointment_participant=participant) | Q(appointment_participant__isnull=True)
        )
    else:
        entries = entries.filter(appointment_participant__isnull=True)
    for entry in entries:
        current_by_account[entry.account_id] += entry.amount
        accounts_by_id[entry.account_id] = entry.account

    target_ids = set(current_by_account) | set(targets_by_account)
    for account_id in target_ids:
        account = (
            targets_by_account.get(account_id, {}).get("account") or accounts_by_id[account_id]
        )
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
            appointment_participant=participant,
            price_snapshot=appointment.service.default_price if appointment.service_id else None,
            created_by=user,
            reason=reason,
        )


def sync_participant_ledger_to_target(participant, target, user, reason):
    current_by_account = defaultdict(Decimal)
    accounts_by_id = {}
    entries = LedgerEntry.objects.filter(
        appointment=participant.appointment, appointment_participant=participant
    ).select_related("account")
    for entry in entries:
        current_by_account[entry.account_id] += entry.amount
        accounts_by_id[entry.account_id] = entry.account

    targets_by_account = {}
    if target:
        targets_by_account[target["account"].id] = target

    target_ids = set(current_by_account) | set(targets_by_account)
    for account_id in target_ids:
        account = (
            targets_by_account.get(account_id, {}).get("account") or accounts_by_id[account_id]
        )
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
            appointment=participant.appointment,
            appointment_participant=participant,
            price_snapshot=participant.appointment.service.default_price
            if participant.appointment.service_id
            else None,
            created_by=user,
            reason=reason,
        )


def sync_appointment_billing_summary(appointment):
    participants = list(appointment.participants.select_related("billing_account").order_by("pk"))
    if not participants:
        return
    if any(
        participant.billing_decision == Appointment.BillingDecision.UNDECIDED
        for participant in participants
    ):
        appointment.billing_decision = Appointment.BillingDecision.UNDECIDED
        appointment.billing_account = None
    elif all(
        participant.billing_decision == Appointment.BillingDecision.DO_NOT_CHARGE
        for participant in participants
    ):
        appointment.billing_decision = Appointment.BillingDecision.DO_NOT_CHARGE
        appointment.billing_account = None
    elif all(
        participant.billing_decision == Appointment.BillingDecision.CHARGE
        for participant in participants
    ):
        charged_participants = list(participants)
        appointment.billing_decision = Appointment.BillingDecision.CHARGE
        appointment.billing_account = (
            charged_participants[0].billing_account
            if len(charged_participants) == 1
            and charged_participants[0].child_id == appointment.child_id
            else None
        )
        if appointment.billing_account is None:
            appointment.billing_decision = Appointment.BillingDecision.UNDECIDED
    else:
        appointment.billing_decision = Appointment.BillingDecision.UNDECIDED
        appointment.billing_account = None
    Appointment.objects.filter(pk=appointment.pk).update(
        billing_decision=appointment.billing_decision,
        billing_account=appointment.billing_account,
        updated_at=timezone.now(),
    )


class AppointmentForm(forms.ModelForm):
    date = forms.DateField(label="Дата", widget=DATE_INPUT, input_formats=["%Y-%m-%d"])
    time = forms.TimeField(label="Время", widget=TIME_INPUT, input_formats=["%H:%M"])
    duration_minutes = forms.IntegerField(label="Длительность, минут", min_value=5, max_value=240)
    participants = forms.ModelMultipleChoiceField(
        label="Получатели",
        queryset=Child.objects.none(),
        required=False,
        help_text="Для индивидуального занятия выберите одного получателя. Для группового можно выбрать несколько.",
        widget=forms.SelectMultiple(attrs={"size": 8, "data-searchable": "off"}),
    )
    staff_members = forms.ModelMultipleChoiceField(
        label="Специалисты",
        queryset=StaffMember.objects.none(),
        required=False,
        help_text="Первый выбранный специалист станет основным для совместимости с календарем.",
        widget=forms.SelectMultiple(attrs={"size": 6, "data-searchable": "off"}),
    )
    staff_availability_override = forms.BooleanField(required=False, widget=forms.HiddenInput())
    room_limit_override = forms.BooleanField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = Appointment
        fields = (
            "session_type",
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
        self.actor = kwargs.pop("actor", None)
        instance = kwargs.get("instance")
        initial = kwargs.pop("initial", {}).copy()
        self._original_child_id = instance.child_id if instance else None
        self._original_staff_member_id = instance.staff_member_id if instance else None
        self._original_participant_ids: set[int] = set()
        self._original_staff_assignment_ids: set[int] = set()
        if instance and instance.pk:
            local_start = timezone.localtime(instance.starts_at)
            self._original_participant_ids = set(
                instance.participants.values_list("child_id", flat=True)
            )
            self._original_staff_assignment_ids = set(
                instance.staff_assignments.values_list("staff_member_id", flat=True)
            )
            initial.setdefault("date", local_start.date())
            initial.setdefault("time", local_start.time().replace(second=0, microsecond=0))
            initial.setdefault("duration_minutes", instance.duration_minutes)
            initial.setdefault("participants", list(self._original_participant_ids))
            initial.setdefault("staff_members", list(self._original_staff_assignment_ids))
        else:
            initial.setdefault("status", Appointment.Status.CONFIRMED)
            if initial.get("child") and not initial.get("participants"):
                initial["participants"] = [initial["child"]]
            if initial.get("staff_member") and not initial.get("staff_members"):
                initial["staff_members"] = [initial["staff_member"]]

        super().__init__(*args, initial=initial, **kwargs)
        self.availability_warning = ""
        self.room_limit_warning = ""
        self.fields["session_type"].required = False
        self.fields["child"].queryset = Child.objects.order_by("last_name", "first_name")
        self.fields["child"].label = "Основной получатель"
        self.fields["child"].required = False
        self.fields["service"].queryset = Service.objects.filter(is_active=True).order_by("name")
        self.fields["staff_member"].queryset = StaffMember.objects.filter(
            status=StaffMember.Status.ACTIVE
        ).order_by("full_name")
        self.fields["staff_member"].label = "Основной специалист"
        self.fields["staff_member"].required = False
        self.fields["participants"].queryset = Child.objects.order_by("last_name", "first_name")
        self.fields["staff_members"].queryset = StaffMember.objects.filter(
            status=StaffMember.Status.ACTIVE
        ).order_by("full_name")
        self.fields["room"].queryset = Room.objects.filter(is_active=True).order_by("name")
        self.fields["room"].required = False
        self.fields["program_block"].queryset = ProgramBlock.objects.select_related(
            "program", "service"
        ).order_by("program__child__last_name", "program__title", "number")
        self.fields["program_block"].required = False
        self.fields["billing_account"].required = False
        self.fields["billing_account"].queryset = self._billing_accounts_queryset()
        self.fields["admin_note"].required = False
        self.fields["staff_availability_override"].initial = bool(
            instance and getattr(instance, "staff_availability_override", False)
        )

    def _is_stale_legacy_child(self, child: Child | None) -> bool:
        return bool(
            self.instance.pk
            and child
            and child.pk == self._original_child_id
            and self._original_participant_ids
            and child.pk not in self._original_participant_ids
        )

    def _is_stale_legacy_staff(self, staff: StaffMember | None) -> bool:
        return bool(
            self.instance.pk
            and staff
            and staff.pk == self._original_staff_member_id
            and self._original_staff_assignment_ids
            and staff.pk not in self._original_staff_assignment_ids
        )

    def _selected_children(self, cleaned) -> list[Child]:
        children = list(cleaned.get("participants") or [])
        primary = cleaned.get("child")
        if primary and primary not in children and not self._is_stale_legacy_child(primary):
            children.insert(0, primary)
        if children and not primary:
            cleaned["child"] = children[0]
            self.instance.child = children[0]
        return children

    def _selected_staff_members(self, cleaned) -> list[StaffMember]:
        staff_members = list(cleaned.get("staff_members") or [])
        primary = cleaned.get("staff_member")
        if primary and primary not in staff_members and not self._is_stale_legacy_staff(primary):
            staff_members.insert(0, primary)
        if staff_members and not primary:
            cleaned["staff_member"] = staff_members[0]
            self.instance.staff_member = staff_members[0]
        return staff_members

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
            qs = qs.filter(
                Q(service_scope=BalanceAccount.ServiceScope.ANY) | Q(service_id=service_id)
            )
        return qs.order_by("child__last_name", "funding_source__name", "service__name")

    def _staff_override_requested(self) -> bool:
        if not self.is_bound:
            return bool(self.initial.get("staff_availability_override"))
        raw = self.data.get(self.add_prefix("staff_availability_override"))
        return str(raw).lower() in {"1", "on", "true", "yes"}

    def _room_override_requested(self) -> bool:
        if not self.is_bound:
            return bool(self.initial.get("room_limit_override"))
        raw = self.data.get(self.add_prefix("room_limit_override"))
        return str(raw).lower() in {"1", "on", "true", "yes"}

    def _room_limit_message(self, room: Room, conflicts: dict) -> str:
        reasons = conflicts.get("room_limit_reasons") or {}
        parts = []
        if reasons.get("staff"):
            parts.append(
                f"специалистов {reasons.get('staff_total')} при лимите {room.effective_max_staff_count}"
            )
        if reasons.get("recipients"):
            parts.append(
                f"получателей {reasons.get('recipient_total')} при лимите {room.effective_max_recipient_count}"
            )
        if reasons.get("group"):
            parts.append("кабинет не отмечен как разрешенный для групповых занятий")
        return "; ".join(parts) or "кабинет превышает правила вместимости"

    def clean(self):
        cleaned = super().clean()
        day = cleaned.get("date")
        clock = cleaned.get("time")
        duration = cleaned.get("duration_minutes")
        children = self._selected_children(cleaned)
        child = cleaned.get("child")
        service = cleaned.get("service")
        staff_members = self._selected_staff_members(cleaned)
        staff_member = cleaned.get("staff_member")
        room = cleaned.get("room")
        account = cleaned.get("billing_account")
        program_block = cleaned.get("program_block")

        if not cleaned.get("session_type"):
            cleaned["session_type"] = Appointment.SessionType.INDIVIDUAL
            self.instance.session_type = Appointment.SessionType.INDIVIDUAL
        if not children:
            self.add_error("participants", "Выберите хотя бы одного получателя.")
        if not staff_members:
            self.add_error("staff_members", "Выберите хотя бы одного специалиста.")
        if len(children) > 1 or len(staff_members) > 1:
            cleaned["session_type"] = Appointment.SessionType.GROUP
            self.instance.session_type = Appointment.SessionType.GROUP
        if child:
            self.instance.child = child
        if staff_member:
            self.instance.staff_member = staff_member

        if day and clock and duration:
            starts_at = build_local_datetime(day, clock)
            ends_at = starts_at + timedelta(minutes=duration)
            cleaned["starts_at"] = starts_at
            cleaned["ends_at"] = ends_at
            self.instance.starts_at = starts_at
            self.instance.ends_at = ends_at
            conflicts = appointment_group_conflicts(
                starts_at,
                ends_at,
                children,
                staff_members,
                room,
                exclude_pk=self.instance.pk,
            )
            messages = conflict_messages(conflicts)
            if (
                conflicts.get("room_over_limit")
                and room
                and cleaned.get("status") in ACTIVE_APPOINTMENT_STATUSES
            ):
                self.room_limit_warning = self._room_limit_message(room, conflicts)
                if not self._room_override_requested():
                    raise forms.ValidationError(
                        "Ограничение кабинета: " + self.room_limit_warning + "."
                    )
                cleaned["room_limit_override"] = True
                messages = [
                    message
                    for message in messages
                    if message != "кабинет превышает правила вместимости"
                ]
            else:
                cleaned["room_limit_override"] = False
            if messages and cleaned.get("status") in ACTIVE_APPOINTMENT_STATUSES:
                raise forms.ValidationError("Конфликт расписания: " + ", ".join(messages) + ".")
            unavailable_by_staff = [
                (staff, reason)
                for staff in staff_members
                if (reason := staff_unavailability_reason(staff, starts_at, ends_at))
            ]
            unavailable = "; ".join(f"{staff}: {reason}" for staff, reason in unavailable_by_staff)
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
            self.add_error(
                "program_block", "Блок программы должен принадлежать выбранному получателю."
            )
        if program_block and service and program_block.service_id != service.id:
            self.add_error(
                "program_block", "Блок программы должен соответствовать выбранной услуге."
            )
        return cleaned

    def _sync_participants(self, appointment: Appointment) -> None:
        children = list(self.cleaned_data.get("participants") or [])
        if (
            appointment.child
            and appointment.child not in children
            and not self._is_stale_legacy_child(appointment.child)
        ):
            children.insert(0, appointment.child)
        child_ids = [child.pk for child in children]
        AppointmentParticipant.objects.filter(appointment=appointment).exclude(
            child_id__in=child_ids
        ).delete()
        existing_by_child = {
            participant.child_id: participant
            for participant in AppointmentParticipant.objects.filter(appointment=appointment)
        }
        for child in children:
            is_primary = child.pk == appointment.child_id
            existing = existing_by_child.get(child.pk)
            if existing:
                existing.starts_at_snapshot = appointment.starts_at
                existing.ends_at_snapshot = appointment.ends_at
                existing.appointment_status = appointment.status
                existing.save(
                    update_fields=[
                        "starts_at_snapshot",
                        "ends_at_snapshot",
                        "appointment_status",
                        "updated_at",
                    ]
                )
                continue

            AppointmentParticipant.objects.create(
                appointment=appointment,
                child=child,
                attendance_status=appointment.attendance_status,
                billing_decision=(
                    appointment.billing_decision
                    if is_primary
                    else Appointment.BillingDecision.UNDECIDED
                ),
                billing_account=appointment.billing_account if is_primary else None,
                price_snapshot=(
                    appointment.service.default_price
                    if is_primary and appointment.service_id
                    else None
                ),
                program_block=appointment.program_block if is_primary else None,
                sequence_number=appointment.sequence_number if is_primary else None,
                starts_at_snapshot=appointment.starts_at,
                ends_at_snapshot=appointment.ends_at,
                appointment_status=appointment.status,
            )

    def _sync_staff_assignments(self, appointment: Appointment) -> None:
        staff_members = list(self.cleaned_data.get("staff_members") or [])
        if (
            appointment.staff_member
            and appointment.staff_member not in staff_members
            and not self._is_stale_legacy_staff(appointment.staff_member)
        ):
            staff_members.insert(0, appointment.staff_member)
        staff_ids = [staff.pk for staff in staff_members]
        AppointmentStaffAssignment.objects.filter(appointment=appointment).exclude(
            staff_member_id__in=staff_ids
        ).delete()
        existing_by_staff = {
            assignment.staff_member_id: assignment
            for assignment in AppointmentStaffAssignment.objects.filter(appointment=appointment)
        }
        primary_changed = (
            self._original_staff_member_id is not None
            and self._original_staff_member_id != appointment.staff_member_id
        )
        for staff in staff_members:
            is_primary = staff.pk == appointment.staff_member_id
            unavailable = staff_unavailability_reason(
                staff, appointment.starts_at, appointment.ends_at
            )
            override_availability = bool(
                unavailable and self.cleaned_data.get("staff_availability_override")
            )
            override_reason = (
                unavailable if unavailable and self.cleaned_data.get("staff_availability_override") else ""
            )
            role = (
                AppointmentStaffAssignment.Role.PRIMARY
                if is_primary
                else AppointmentStaffAssignment.Role.ASSISTANT
            )
            existing = existing_by_staff.get(staff.pk)
            if existing:
                if primary_changed:
                    if is_primary:
                        existing.role = AppointmentStaffAssignment.Role.PRIMARY
                    elif (
                        staff.pk == self._original_staff_member_id
                        and existing.role == AppointmentStaffAssignment.Role.PRIMARY
                    ):
                        existing.role = AppointmentStaffAssignment.Role.ASSISTANT
                existing.starts_at_snapshot = appointment.starts_at
                existing.ends_at_snapshot = appointment.ends_at
                existing.appointment_status = appointment.status
                existing.override_availability = override_availability
                existing.override_reason = override_reason
                existing.save(
                    update_fields=[
                        "role",
                        "starts_at_snapshot",
                        "ends_at_snapshot",
                        "appointment_status",
                        "override_availability",
                        "override_reason",
                        "updated_at",
                    ]
                )
                continue

            AppointmentStaffAssignment.objects.create(
                appointment=appointment,
                staff_member=staff,
                role=role,
                starts_at_snapshot=appointment.starts_at,
                ends_at_snapshot=appointment.ends_at,
                appointment_status=appointment.status,
                override_availability=override_availability,
                override_reason=override_reason,
            )

    def _save_room_override(self, appointment: Appointment) -> None:
        if not self.cleaned_data.get("room_limit_override"):
            return
        reason = (
            self.room_limit_warning
            or "Одноразовое разрешение администратора на кабинет вне правил."
        )
        override_type = AppointmentRoomOverride.OverrideType.OTHER
        if "специалистов" in reason and "получателей" not in reason:
            override_type = AppointmentRoomOverride.OverrideType.STAFF_LIMIT
        elif "получателей" in reason and "специалистов" not in reason:
            override_type = AppointmentRoomOverride.OverrideType.RECIPIENT_LIMIT
        defaults = {"created_by": self.actor} if self.actor else {}
        AppointmentRoomOverride.objects.get_or_create(
            appointment=appointment,
            override_type=override_type,
            reason=reason,
            defaults=defaults,
        )

    def _post_clean(self):
        if self._room_override_requested():
            self.instance._skip_room_limit_validation = True
        try:
            super()._post_clean()
        finally:
            if hasattr(self.instance, "_skip_room_limit_validation"):
                del self.instance._skip_room_limit_validation

    def save(self, commit=True):
        appointment = super().save(commit=False)
        appointment.starts_at = self.cleaned_data["starts_at"]
        appointment.ends_at = self.cleaned_data["ends_at"]
        appointment.staff_availability_override = self.cleaned_data.get(
            "staff_availability_override", False
        )
        appointment.staff_availability_override_reason = self.cleaned_data.get(
            "staff_availability_override_reason", ""
        )
        if commit:
            with transaction.atomic():
                if self.cleaned_data.get("room_limit_override"):
                    appointment._skip_room_limit_validation = True
                try:
                    appointment.save()
                finally:
                    if hasattr(appointment, "_skip_room_limit_validation"):
                        del appointment._skip_room_limit_validation
                self.save_m2m()
                self._sync_participants(appointment)
                self._sync_staff_assignments(appointment)
                self._save_room_override(appointment)
        return appointment


class AppointmentMoveForm(forms.Form):
    date = forms.DateField(label="Новая дата", widget=DATE_INPUT, input_formats=["%Y-%m-%d"])
    time = forms.TimeField(label="Новое время", widget=TIME_INPUT, input_formats=["%H:%M"])
    duration_minutes = forms.IntegerField(label="Длительность, минут", min_value=5, max_value=240)
    staff_member: Any = forms.ModelChoiceField(
        label="Специалист", queryset=StaffMember.objects.none()
    )
    room: Any = forms.ModelChoiceField(
        label="Кабинет", queryset=Room.objects.none(), required=False
    )
    staff_availability_override = forms.BooleanField(required=False, widget=forms.HiddenInput())
    room_limit_override = forms.BooleanField(required=False, widget=forms.HiddenInput())
    admin_note = forms.CharField(
        label="Комментарий к переносу", widget=forms.Textarea(attrs={"rows": 3}), required=False
    )

    def __init__(self, *args, appointment, actor=None, **kwargs):
        self.appointment = appointment
        self.actor = actor
        super().__init__(*args, **kwargs)
        self.availability_warning = ""
        self.room_limit_warning = ""
        self.fields["staff_member"].queryset = StaffMember.objects.filter(
            status=StaffMember.Status.ACTIVE
        ).order_by("full_name")
        self.fields["room"].queryset = Room.objects.filter(is_active=True).order_by("name")

    def _staff_override_requested(self) -> bool:
        if not self.is_bound:
            return bool(self.initial.get("staff_availability_override"))
        raw = self.data.get(self.add_prefix("staff_availability_override"))
        return str(raw).lower() in {"1", "on", "true", "yes"}

    def _room_override_requested(self) -> bool:
        if not self.is_bound:
            return bool(self.initial.get("room_limit_override"))
        raw = self.data.get(self.add_prefix("room_limit_override"))
        return str(raw).lower() in {"1", "on", "true", "yes"}

    def _room_limit_message(self, room: Room, conflicts: dict) -> str:
        reasons = conflicts.get("room_limit_reasons") or {}
        parts = []
        if reasons.get("staff"):
            parts.append(
                f"специалистов {reasons.get('staff_total')} при лимите {room.effective_max_staff_count}"
            )
        if reasons.get("recipients"):
            parts.append(
                f"получателей {reasons.get('recipient_total')} при лимите {room.effective_max_recipient_count}"
            )
        if reasons.get("group"):
            parts.append("кабинет не отмечен как разрешенный для групповых занятий")
        return "; ".join(parts) or "кабинет превышает правила вместимости"

    def _participant_children(self):
        participants = list(self.appointment.participants.select_related("child").order_by("pk"))
        if participants:
            return [participant.child for participant in participants]
        return [self.appointment.child]

    def _source_staff_assignments(self):
        return list(
            self.appointment.staff_assignments.select_related("staff_member").order_by("pk")
        )

    def _move_staff_members(self, selected_staff):
        assignments = self._source_staff_assignments()
        if not assignments:
            return [selected_staff]
        members = []
        seen = set()
        primary_replaced = False
        for assignment in assignments:
            staff = assignment.staff_member
            if assignment.role == AppointmentStaffAssignment.Role.PRIMARY and not primary_replaced:
                staff = selected_staff
                primary_replaced = True
            if staff.pk not in seen:
                members.append(staff)
                seen.add(staff.pk)
        if selected_staff.pk not in seen:
            members.insert(0, selected_staff)
        return members

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
                and room == self.appointment.room
            ):
                raise forms.ValidationError("Новое время совпадает с текущим занятием.")
            conflicts = appointment_group_conflicts(
                starts_at,
                ends_at,
                self._participant_children(),
                self._move_staff_members(staff_member),
                room,
                exclude_pk=self.appointment.pk,
            )
            messages = conflict_messages(conflicts)
            if conflicts.get("room_over_limit") and room:
                self.room_limit_warning = self._room_limit_message(room, conflicts)
                if not self._room_override_requested():
                    raise forms.ValidationError(
                        "Ограничение кабинета: " + self.room_limit_warning + "."
                    )
                cleaned["room_limit_override"] = True
                messages = [
                    message
                    for message in messages
                    if message != "кабинет превышает правила вместимости"
                ]
            else:
                cleaned["room_limit_override"] = False
            if messages:
                raise forms.ValidationError("Конфликт расписания: " + ", ".join(messages) + ".")
            cleaned["staff_availability_override"] = False
            cleaned["staff_availability_override_reason"] = ""
            unavailable_messages = []
            for move_staff in self._move_staff_members(staff_member):
                unavailable = staff_unavailability_reason(move_staff, starts_at, ends_at)
                if not unavailable:
                    continue
                unavailable_messages.append(f"{move_staff.full_name}: {unavailable}")
            if unavailable_messages:
                unavailable = "; ".join(unavailable_messages)
                self.availability_warning = unavailable
                if not self._staff_override_requested():
                    raise forms.ValidationError(
                        "Недоступность специалиста: " + unavailable + "."
                    )
                cleaned["staff_availability_override"] = True
                cleaned["staff_availability_override_reason"] = unavailable
        return cleaned

    def _copy_participants(self, old, new):
        for participant in old.participants.select_related(
            "child", "billing_account", "program_block"
        ).order_by("pk"):
            AppointmentParticipant.objects.update_or_create(
                appointment=new,
                child=participant.child,
                defaults={
                    "attendance_status": Appointment.AttendanceStatus.UNKNOWN,
                    "billing_decision": Appointment.BillingDecision.UNDECIDED,
                    "billing_account": participant.billing_account,
                    "price_snapshot": None,
                    "program_block": participant.program_block,
                    "sequence_number": participant.sequence_number,
                    "source_participant": participant,
                    "admin_note": participant.admin_note,
                    "specialist_note": "",
                    "marked_by_staff_at": None,
                    "starts_at_snapshot": new.starts_at,
                    "ends_at_snapshot": new.ends_at,
                    "appointment_status": new.status,
                },
            )

    def _copy_staff_assignments(self, old, new, selected_staff):
        assignments = list(old.staff_assignments.select_related("staff_member").order_by("pk"))
        if not assignments:
            return
        primary_replaced = False
        seen = set()
        for assignment in assignments:
            staff = assignment.staff_member
            if assignment.role == AppointmentStaffAssignment.Role.PRIMARY and not primary_replaced:
                staff = selected_staff
                primary_replaced = True
            if staff.pk in seen:
                continue
            seen.add(staff.pk)
            unavailable = staff_unavailability_reason(staff, new.starts_at, new.ends_at)
            override_availability = bool(
                unavailable and self.cleaned_data.get("staff_availability_override", False)
            )
            override_reason = (
                unavailable
                if unavailable and self.cleaned_data.get("staff_availability_override", False)
                else ""
            )
            AppointmentStaffAssignment.objects.update_or_create(
                appointment=new,
                staff_member=staff,
                defaults={
                    "role": assignment.role,
                    "starts_at_snapshot": new.starts_at,
                    "ends_at_snapshot": new.ends_at,
                    "appointment_status": new.status,
                    "override_availability": override_availability,
                    "override_reason": override_reason,
                },
            )

    def _save_room_override(self, appointment: Appointment) -> None:
        if not self.cleaned_data.get("room_limit_override"):
            return
        reason = (
            self.room_limit_warning
            or "Одноразовое разрешение администратора на кабинет вне правил."
        )
        override_type = AppointmentRoomOverride.OverrideType.OTHER
        if "специалистов" in reason and "получателей" not in reason:
            override_type = AppointmentRoomOverride.OverrideType.STAFF_LIMIT
        elif "получателей" in reason and "специалистов" not in reason:
            override_type = AppointmentRoomOverride.OverrideType.RECIPIENT_LIMIT
        defaults = {"created_by": self.actor} if self.actor else {}
        AppointmentRoomOverride.objects.get_or_create(
            appointment=appointment,
            override_type=override_type,
            reason=reason,
            defaults=defaults,
        )

    @transaction.atomic
    def save(self):
        old = self.appointment
        starts_at = self.cleaned_data["starts_at"]
        ends_at = self.cleaned_data["ends_at"]
        staff_member = self.cleaned_data["staff_member"]
        room = self.cleaned_data["room"]
        note = self.cleaned_data.get("admin_note", "").strip()
        participants = list(
            old.participants.select_related("child", "billing_account").order_by("pk")
        )
        legacy_child = old.child
        legacy_billing_account = old.billing_account
        if participants and all(participant.child_id != old.child_id for participant in participants):
            legacy_child = participants[0].child
            legacy_billing_account = participants[0].billing_account
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

        new = Appointment(
            child=legacy_child,
            service=old.service,
            staff_member=staff_member,
            room=room,
            starts_at=starts_at,
            ends_at=ends_at,
            status=Appointment.Status.CONFIRMED,
            attendance_status=Appointment.AttendanceStatus.UNKNOWN,
            billing_decision=Appointment.BillingDecision.UNDECIDED,
            billing_account=legacy_billing_account,
            source_appointment=old,
            series=old.series,
            program_block=old.program_block,
            sequence_number=old.sequence_number,
            session_type=old.session_type,
            title=old.title,
            staff_availability_override=self.cleaned_data.get("staff_availability_override", False),
            staff_availability_override_reason=self.cleaned_data.get(
                "staff_availability_override_reason", ""
            ),
            admin_note=note,
        )
        if self.cleaned_data.get("room_limit_override"):
            new._skip_room_limit_validation = True
        try:
            new.save()
        finally:
            if hasattr(new, "_skip_room_limit_validation"):
                del new._skip_room_limit_validation
        self._copy_participants(old, new)
        self._copy_staff_assignments(old, new, staff_member)
        self._save_room_override(new)
        return new


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
    admin_note = forms.CharField(
        label="Комментарий", widget=forms.Textarea(attrs={"rows": 3}), required=False
    )
    same_day_billing_ack = forms.BooleanField(
        label="Отмена день-в-день: решение по списанию приму отдельно",
        required=False,
    )

    def __init__(self, *args, appointment, **kwargs):
        self.appointment = appointment
        self.is_same_day_cancellation = (
            timezone.localtime(appointment.starts_at).date() == timezone.localdate()
        )
        super().__init__(*args, **kwargs)
        if not self.is_same_day_cancellation:
            self.fields.pop("same_day_billing_ack")

    def clean(self):
        cleaned = super().clean()
        if self.is_same_day_cancellation and not cleaned.get("same_day_billing_ack"):
            self.add_error(
                "same_day_billing_ack",
                "Подтвердите отдельное решение по списанию для отмены день-в-день.",
            )
        return cleaned

    def save(self):
        appointment = self.appointment
        reason = dict(self.REASON_CHOICES)[self.cleaned_data["reason"]]
        note = self.cleaned_data.get("admin_note", "").strip()
        appointment.status = self.cleaned_data["status"]
        same_day_note = (
            "Отмена день-в-день: решение по списанию принимает администратор отдельно."
            if self.is_same_day_cancellation
            else ""
        )
        appointment.admin_note = "\n".join(
            part
            for part in [appointment.admin_note, f"Причина отмены: {reason}.", same_day_note, note]
            if part
        )
        appointment.save(update_fields=["status", "admin_note", "updated_at"])
        return appointment


class BillingDecisionForm(forms.Form):
    DECISION_CHOICES = (
        (Appointment.BillingDecision.CHARGE, "Списать"),
        (Appointment.BillingDecision.DO_NOT_CHARGE, "Не списывать"),
    )

    participant_id = forms.IntegerField(required=False, widget=forms.HiddenInput())
    billing_decision = forms.ChoiceField(label="Решение", choices=DECISION_CHOICES)
    billing_account: Any = forms.ModelChoiceField(
        label="Счет баланса", queryset=BalanceAccount.objects.none(), required=False
    )
    amount = forms.DecimalField(
        label="Сумма операции", max_digits=12, decimal_places=2, required=False
    )
    reason = forms.CharField(
        label="Основание", widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    def __init__(self, *args, appointment, participant=None, **kwargs):
        self.appointment = appointment
        self.participant = participant
        self.participant_lookup_error = ""
        self.participant_required_error = ""
        data = args[0] if args else kwargs.get("data")
        participant_id = data.get("participant_id") if data else None
        if self.participant is None and participant_id:
            try:
                self.participant = appointment.participants.select_related(
                    "child", "billing_account"
                ).get(pk=participant_id)
            except (AppointmentParticipant.DoesNotExist, ValueError, TypeError):
                self.participant_lookup_error = "Участник занятия не найден."
        elif self.participant is None:
            participants = list(
                appointment.participants.select_related("child", "billing_account").order_by("pk")[
                    :2
                ]
            )
            if len(participants) == 1:
                self.participant = participants[0]
            elif len(participants) > 1:
                self.participant_required_error = GROUP_BILLING_PARTICIPANT_REQUIRED
        super().__init__(*args, **kwargs)
        target_child = self.participant.child if self.participant else appointment.child
        self.fields["billing_account"].queryset = (
            BalanceAccount.objects.select_related("child", "funding_source", "service")
            .filter(
                Q(service_scope=BalanceAccount.ServiceScope.ANY) | Q(service=appointment.service),
                child=target_child,
                status=BalanceAccount.Status.ACTIVE,
            )
            .order_by("funding_source__name", "service__name")
        )
        if self.participant:
            self.fields["participant_id"].initial = self.participant.pk
        account = (
            self.participant.billing_account if self.participant else appointment.billing_account
        )
        if account:
            self.initial.setdefault("billing_account", account)
        decision = (
            self.participant.billing_decision if self.participant else appointment.billing_decision
        )
        self.initial.setdefault("billing_decision", decision)

    def clean(self):
        cleaned = super().clean()
        decision = cleaned.get("billing_decision")
        account = cleaned.get("billing_account")
        amount = cleaned.get("amount")
        if self.participant_lookup_error:
            self.add_error("participant_id", self.participant_lookup_error)
        if self.participant_required_error:
            self.add_error("participant_id", self.participant_required_error)
        if decision == Appointment.BillingDecision.CHARGE:
            if not account:
                self.add_error("billing_account", "Для списания нужен счет баланса.")
            else:
                if self.participant and account.child_id != self.participant.child_id:
                    self.add_error(
                        "billing_account", "Счет должен принадлежать выбранному участнику занятия."
                    )
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

        if self.participant:
            participant = self.participant
            if decision == Appointment.BillingDecision.CHARGE:
                account = self.cleaned_data["billing_account"]
                amount = self.cleaned_data["amount"]
                participant.billing_decision = Appointment.BillingDecision.CHARGE
                participant.billing_account = account
                participant.price_snapshot = (
                    appointment.service.default_price if appointment.service_id else None
                )
                participant.save(
                    update_fields=[
                        "billing_decision",
                        "billing_account",
                        "price_snapshot",
                        "updated_at",
                    ]
                )
                sync_participant_ledger_to_target(
                    participant, {"account": account, "amount": amount}, user, reason
                )
            else:
                participant.billing_decision = Appointment.BillingDecision.DO_NOT_CHARGE
                participant.billing_account = None
                participant.price_snapshot = None
                participant.save(
                    update_fields=[
                        "billing_decision",
                        "billing_account",
                        "price_snapshot",
                        "updated_at",
                    ]
                )
                sync_participant_ledger_to_target(participant, None, user, reason)
            sync_appointment_billing_summary(appointment)
            return appointment

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


class AppointmentParticipantProgramForm(forms.Form):
    participant_id = forms.IntegerField(required=True, widget=forms.HiddenInput())
    program_block: Any = forms.ModelChoiceField(
        label="Каскад участника",
        queryset=ProgramBlock.objects.none(),
        required=False,
    )
    sequence_number = forms.IntegerField(label="Номер", min_value=1, required=False)

    def __init__(self, *args, appointment, participant=None, **kwargs):
        self.appointment = appointment
        self.participant = participant
        self.participant_lookup_error = ""
        data = args[0] if args else kwargs.get("data")
        participant_id = data.get("participant_id") if data else None
        if self.participant is None and participant_id:
            try:
                self.participant = appointment.participants.select_related(
                    "child", "program_block"
                ).get(pk=participant_id)
            except (AppointmentParticipant.DoesNotExist, ValueError, TypeError):
                self.participant_lookup_error = "Участник занятия не найден."
        super().__init__(*args, **kwargs)
        if self.participant:
            self.fields["participant_id"].initial = self.participant.pk
            self.fields["program_block"].queryset = (
                ProgramBlock.objects.select_related("program", "service")
                .filter(
                    program__child=self.participant.child,
                    service=appointment.service,
                )
                .order_by("program__title", "number")
            )
            self.initial.setdefault("program_block", self.participant.program_block)
            self.initial.setdefault("sequence_number", self.participant.sequence_number)

    def clean(self):
        cleaned = super().clean()
        program_block = cleaned.get("program_block")
        if self.participant_lookup_error:
            self.add_error("participant_id", self.participant_lookup_error)
        if program_block and self.participant:
            if program_block.program.child_id != self.participant.child_id:
                self.add_error(
                    "program_block", "Блок программы должен принадлежать участнику занятия."
                )
            if program_block.service_id != self.appointment.service_id:
                self.add_error(
                    "program_block", "Блок программы должен соответствовать услуге занятия."
                )
        return cleaned

    def save(self):
        participant = self.participant
        if participant is None:
            raise ValueError("Участник занятия не найден.")
        program_block = self.cleaned_data.get("program_block")
        participant.program_block = program_block
        participant.sequence_number = (
            self.cleaned_data.get("sequence_number") if program_block else None
        )
        participant.save(update_fields=["program_block", "sequence_number", "updated_at"])
        if participant.child_id == self.appointment.child_id:
            Appointment.objects.filter(pk=self.appointment.pk).update(
                program_block=participant.program_block,
                sequence_number=participant.sequence_number,
                updated_at=timezone.now(),
            )
        return participant


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
            "passport_series",
            "passport_number",
            "passport_issued_by",
            "passport_issued_on",
            "registration_address",
            "notes",
        )
        labels = {
            "last_name": "Фамилия",
            "first_name": "Имя",
            "middle_name": "Отчество",
            "relationship_type": "Тип представительства",
            "phone": "Телефон",
            "phone_alt": "Дополнительный телефон",
            "passport_series": "Серия паспорта",
            "passport_number": "Номер паспорта",
            "passport_issued_by": "Кем выдан паспорт",
            "passport_issued_on": "Дата выдачи паспорта",
            "registration_address": "Адрес регистрации",
            "notes": "Примечания",
        }
        widgets = {
            "passport_issued_on": DATE_INPUT,
            "passport_issued_by": forms.Textarea(attrs={"rows": 3}),
            "registration_address": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


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
            "registration_address",
            "residential_address",
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
            "registration_address": "Адрес регистрации",
            "residential_address": "Адрес проживания",
            "status": "Статус",
            "color": "Цветовая метка",
            "primary_parent": "Основной представитель",
            "diagnosis": "Диагноз/особенности",
            "notes": "Примечания",
        }
        widgets = {
            "birth_date": DATE_INPUT,
            "color": forms.TextInput(attrs={"type": "color"}),
            "registration_address": forms.Textarea(attrs={"rows": 3}),
            "residential_address": forms.Textarea(attrs={"rows": 3}),
            "diagnosis": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["primary_parent"].queryset = ParentGuardian.objects.order_by(
            "last_name", "first_name"
        )
        self.fields["color"].required = False

    def clean_color(self):
        return self.cleaned_data.get("color") or Child._meta.get_field("color").default


class RecipientRepresentativeForm(forms.ModelForm):
    class Meta:
        model = RecipientRepresentative
        fields = (
            "representative",
            "relationship_type",
            "is_primary",
            "receives_schedule",
            "is_payer",
            "notes",
        )
        labels = {
            "representative": "Представитель",
            "relationship_type": "Тип связи",
            "is_primary": "Основной представитель и подписант договора",
            "receives_schedule": "Получает расписание",
            "is_payer": "Плательщик",
            "notes": "Примечания",
        }
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, child: Child | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.child = child or self.instance.child
        self.fields["representative"].queryset = ParentGuardian.objects.order_by(
            "last_name", "first_name"
        )

    def clean_representative(self):
        representative = self.cleaned_data["representative"]
        duplicate = RecipientRepresentative.objects.filter(
            child=self.child,
            representative=representative,
        )
        if self.instance.pk:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise forms.ValidationError("Этот представитель уже привязан к получателю.")
        return representative

    def save(self, commit=True):
        link = super().save(commit=False)
        link.child = self.child
        link.signs_contract = link.is_primary
        if not commit:
            return link

        with transaction.atomic():
            if link.is_primary:
                RecipientRepresentative.objects.filter(child=link.child).exclude(pk=link.pk).filter(
                    Q(is_primary=True) | Q(signs_contract=True)
                ).update(
                    is_primary=False,
                    signs_contract=False,
                )
            link.save()
            if link.is_primary and link.child.primary_parent_id != link.representative_id:
                link.child.primary_parent = link.representative
                link.child.save(update_fields=["primary_parent", "updated_at"])
        return link


class RoomForm(forms.ModelForm):
    class Meta:
        model = Room
        fields = (
            "name",
            "room_type",
            "capacity",
            "limit_staff_count",
            "max_staff_count",
            "limit_recipient_count",
            "max_recipient_count",
            "allow_group_sessions",
            "is_active",
            "color",
        )
        labels = {
            "name": "Название",
            "room_type": "Тип",
            "capacity": "Общая вместимость",
            "limit_staff_count": "Ограничивать число специалистов",
            "max_staff_count": "Максимум специалистов одновременно",
            "limit_recipient_count": "Ограничивать число получателей",
            "max_recipient_count": "Максимум получателей одновременно",
            "allow_group_sessions": "Разрешены групповые занятия",
            "is_active": "Активен",
            "color": "Цвет",
        }
        help_texts = {
            "capacity": "Справочное поле. Проверки расписания используют отдельные лимиты ниже.",
            "limit_staff_count": "Если выключено, кабинет не ограничивает число специалистов.",
            "limit_recipient_count": "Если выключено, кабинет не ограничивает число получателей.",
        }
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}

    def clean_color(self):
        return self.cleaned_data.get("color") or Room._meta.get_field("color").default


class FundingSourceForm(forms.ModelForm):
    class Meta:
        model = FundingSource
        fields = (
            "name",
            "source_type",
            "starts_on",
            "ends_on",
            "transfer_policy",
            "notes",
        )
        labels = {
            "name": "Название",
            "source_type": "Тип",
            "starts_on": "Действует с",
            "ends_on": "Действует по",
            "transfer_policy": "Правило передачи средств",
            "notes": "Примечания",
        }
        widgets = {
            "starts_on": DATE_INPUT,
            "ends_on": DATE_INPUT,
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def clean(self):
        cleaned = super().clean()
        starts_on = cleaned.get("starts_on")
        ends_on = cleaned.get("ends_on")
        if starts_on and ends_on and ends_on < starts_on:
            raise forms.ValidationError("Дата окончания не может быть раньше даты начала.")
        return cleaned


class StaffMemberForm(forms.ModelForm):
    class Meta:
        model = StaffMember
        fields = (
            "user",
            "full_name",
            "specializations",
            "phone",
            "email",
            "status",
            "color",
            "can_use_mobile",
        )
        labels = {
            "user": "Пользователь для входа",
            "full_name": "ФИО",
            "specializations": "Специализации",
            "phone": "Телефон",
            "email": "Email",
            "status": "Статус",
            "color": "Цвет",
            "can_use_mobile": "Доступ к мобильному кабинету",
        }
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user_model = get_user_model()
        linked_user_ids = (
            StaffMember.all_objects.filter(user__isnull=False)
            .exclude(pk=self.instance.pk)
            .values_list("user_id", flat=True)
        )
        self.fields["user"].queryset = user_model.objects.exclude(pk__in=linked_user_ids).order_by(
            "username"
        )
        self.fields["user"].required = False
        self.fields["color"].required = False

    def clean_color(self):
        return self.cleaned_data.get("color") or StaffMember._meta.get_field("color").default


class ServiceForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = (
            "name",
            "code",
            "category",
            "default_duration_minutes",
            "default_price",
            "is_active",
            "color",
        )
        labels = {
            "name": "Название",
            "code": "Код",
            "category": "Категория",
            "default_duration_minutes": "Длительность по умолчанию, мин",
            "default_price": "Цена по умолчанию",
            "is_active": "Активна",
            "color": "Цвет",
        }
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}

    def clean_color(self):
        return self.cleaned_data.get("color") or Service._meta.get_field("color").default


class StaffCompensationRuleForm(forms.ModelForm):
    class Meta:
        model = StaffCompensationRule
        fields = (
            "staff_member",
            "service",
            "funding_source",
            "session_scope",
            "rate_type",
            "amount",
            "group_pay_policy",
            "group_fixed_amount",
            "min_duration_minutes",
            "max_duration_minutes",
            "starts_on",
            "ends_on",
            "is_active",
            "note",
        )
        labels = {
            "staff_member": "Специалист",
            "service": "Услуга",
            "funding_source": "Источник финансирования",
            "session_scope": "Формат занятий",
            "rate_type": "Тип ставки",
            "amount": "Сумма",
            "group_pay_policy": "Начисление в группе",
            "group_fixed_amount": "Фиксированная сумма за группу",
            "min_duration_minutes": "Мин. длительность, мин",
            "max_duration_minutes": "Макс. длительность, мин",
            "starts_on": "Действует с",
            "ends_on": "Действует по",
            "is_active": "Активна",
            "note": "Примечание",
        }
        widgets = {
            "starts_on": DATE_INPUT,
            "ends_on": DATE_INPUT,
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["staff_member"].queryset = StaffMember.objects.order_by("full_name")
        self.fields["service"].queryset = Service.objects.filter(is_active=True).order_by("name")
        self.fields["service"].required = False
        self.fields["funding_source"].queryset = FundingSource.objects.filter(
            archived_at__isnull=True
        ).order_by("name")
        self.fields["funding_source"].required = False

    def clean(self):
        cleaned = super().clean()
        starts_on = cleaned.get("starts_on")
        ends_on = cleaned.get("ends_on")
        min_duration = cleaned.get("min_duration_minutes")
        max_duration = cleaned.get("max_duration_minutes")
        if starts_on and ends_on and ends_on < starts_on:
            self.add_error("ends_on", "Дата окончания не может быть раньше даты начала.")
        if min_duration and max_duration and max_duration < min_duration:
            self.add_error(
                "max_duration_minutes",
                "Максимальная длительность не может быть меньше минимальной.",
            )
        return cleaned


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
        fields = (
            "child",
            "title",
            "consultation",
            "status",
            "starts_on",
            "ends_on",
            "color",
            "notes",
        )
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
        consultations = Appointment.objects.select_related("service", "staff_member").order_by(
            "-starts_at"
        )
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
        self.fields["program"].queryset = TreatmentProgram.objects.select_related("child").order_by(
            "child__last_name", "title"
        )
        self.fields["service"].queryset = Service.objects.filter(is_active=True).order_by("name")
        self.fields["staff_member"].queryset = StaffMember.objects.filter(
            status=StaffMember.Status.ACTIVE
        ).order_by("full_name")
        self.fields["staff_member"].required = False
        self.fields["balance_account"].required = False
        accounts = BalanceAccount.objects.select_related(
            "child", "funding_source", "service"
        ).filter(status=BalanceAccount.Status.ACTIVE)
        if program is not None:
            self.fields["program"].initial = program
            self.fields["program"].disabled = True
            accounts = accounts.filter(child=program.child)
        self.fields["balance_account"].queryset = accounts.order_by(
            "funding_source__name", "service__name"
        )
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
    requested_count = forms.IntegerField(
        label="Сколько занятий подобрать", min_value=1, max_value=120
    )
    staff_member = forms.ModelChoiceField(
        label="Специалист",
        queryset=StaffMember.objects.none(),
        required=False,
        empty_label="Автоматически подобрать",
        help_text="Оставьте пустым, чтобы мастер сам выбрал свободного специалиста.",
    )
    room = forms.ModelChoiceField(
        label="Кабинет",
        queryset=Room.objects.none(),
        required=False,
        empty_label="Автоматически подобрать",
        help_text="Оставьте пустым, чтобы мастер сам выбрал кабинет. Вместимость всё равно ограничивает число одновременных специалистов.",
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
    from_account = forms.ModelChoiceField(
        label="Откуда переносим", queryset=BalanceAccount.objects.none()
    )
    to_account = forms.ModelChoiceField(
        label="Куда переносим",
        queryset=BalanceAccount.objects.none(),
        required=False,
        help_text="Если у каскада уже выбран счёт, он будет подставлен автоматически.",
    )
    amount = forms.DecimalField(
        label="Сумма / количество", min_value=Decimal("0.01"), max_digits=12, decimal_places=2
    )
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
        self.fields["from_account"].queryset = (
            accounts.exclude(pk=target.pk).order_by("funding_source__name", "service__name")
            if target
            else accounts.order_by("funding_source__name", "service__name")
        )
        self.fields["to_account"].queryset = accounts.order_by(
            "funding_source__name", "service__name"
        )
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
            self.add_error(
                "to_account", "Счета должны быть в одинаковых единицах: занятия или рубли."
            )
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
        self.appointment_participants = self._appointment_participants(appointment)
        self.participant_by_child_id = {
            participant.child_id: participant for participant in self.appointment_participants
        }
        self.appointment_staff_assignments = self._appointment_staff_assignments(appointment)
        self.appointment_children = self._appointment_children(appointment)
        child_names = ", ".join(child.full_name for child in self.appointment_children)
        staff_names = ", ".join(
            assignment.staff_member.full_name for assignment in self.appointment_staff_assignments
        ) or appointment.staff_member.full_name
        self.targets = self._build_targets(appointment)
        if self.targets:
            self.fields["target_type"].choices = [
                (key, target["label"]) for key, target in self.targets.items()
            ]
        else:
            self.fields["target_type"].choices = [
                ("", "Нет email у специалиста, представителя или получателя")
            ]
            self.fields["target_type"].disabled = True
        self.initial.setdefault("subject", f"Подтверждение занятия {local_start:%d.%m.%Y %H:%M}")
        self.initial.setdefault(
            "message",
            self._default_message(child_names=child_names, staff_names=staff_names),
        )

    def _default_message(self, *, child_names: str, staff_names: str) -> str:
        local_start = timezone.localtime(self.appointment.starts_at)
        return "\n".join(
            [
                "Здравствуйте.",
                "",
                "Просим подтвердить занятие:",
                f"Получатель: {child_names}",
                f"Услуга: {self.appointment.service.name}",
                f"Специалист: {staff_names}",
                f"Дата и время: {local_start:%d.%m.%Y %H:%M}",
                f"Кабинет: {self.appointment.room.name if self.appointment.room else 'не указан'}",
                "",
                "Ответьте по ссылке ниже: подтвердить или отклонить.",
            ]
        )

    def _staff_names_for_message(self) -> str:
        return (
            ", ".join(
                assignment.staff_member.full_name
                for assignment in self.appointment_staff_assignments
            )
            or self.appointment.staff_member.full_name
        )

    def _message_for_target(self, target) -> str:
        message = self.cleaned_data["message"]
        if (
            target["participant"] is None
            or target["target_type"]
            not in {
                AppointmentConfirmation.TargetType.REPRESENTATIVE,
                AppointmentConfirmation.TargetType.RECIPIENT,
            }
            or message != self.initial.get("message")
        ):
            return message
        return self._default_message(
            child_names=target["participant"].child.full_name,
            staff_names=self._staff_names_for_message(),
        )

    def _appointment_participants(self, appointment):
        return list(
            appointment.participants.select_related("child", "child__primary_parent").order_by(
                "starts_at_snapshot", "child__last_name", "child__first_name"
            )
        )

    def _appointment_staff_assignments(self, appointment):
        assignments = list(
            appointment.staff_assignments.select_related(
                "staff_member", "staff_member__user"
            ).order_by("starts_at_snapshot", "staff_member__full_name")
        )
        assignments.sort(
            key=lambda assignment: (
                0
                if assignment.role == AppointmentStaffAssignment.Role.PRIMARY
                or assignment.staff_member_id == appointment.staff_member_id
                else 1,
                assignment.staff_member.full_name,
                assignment.pk,
            )
        )
        return assignments

    def _appointment_children(self, appointment):
        participants = self.appointment_participants
        if not participants:
            return [appointment.child]
        children = [participant.child for participant in participants]
        if appointment.child_id and all(child.pk != appointment.child_id for child in children):
            children.insert(0, appointment.child)
        return children

    def _build_targets(self, appointment):
        targets = {}
        specialist_targets_added = False
        for assignment in self.appointment_staff_assignments:
            staff = assignment.staff_member
            staff_email = staff.email or (staff.user.email if staff.user_id else "")
            if not staff_email:
                continue
            is_primary_assignment = (
                assignment.role == AppointmentStaffAssignment.Role.PRIMARY
                or assignment.staff_member_id == appointment.staff_member_id
            )
            key = (
                AppointmentConfirmation.TargetType.SPECIALIST
                if is_primary_assignment
                and AppointmentConfirmation.TargetType.SPECIALIST not in targets
                else f"{AppointmentConfirmation.TargetType.SPECIALIST}:{assignment.pk}"
            )
            targets[key] = {
                "target_type": AppointmentConfirmation.TargetType.SPECIALIST,
                "label": f"Специалисту: {staff.full_name} ({staff_email})",
                "email": staff_email,
                "representative": None,
                "participant": None,
                "staff_assignment": assignment,
            }
            specialist_targets_added = True
        if not specialist_targets_added:
            staff_email = appointment.staff_member.email or (
                appointment.staff_member.user.email if appointment.staff_member.user_id else ""
            )
            if staff_email:
                targets[AppointmentConfirmation.TargetType.SPECIALIST] = {
                    "target_type": AppointmentConfirmation.TargetType.SPECIALIST,
                    "label": f"Специалисту: {appointment.staff_member.full_name} ({staff_email})",
                    "email": staff_email,
                    "representative": None,
                    "participant": None,
                    "staff_assignment": None,
                }
        for child in self.appointment_children:
            participant = self.participant_by_child_id.get(child.pk)
            representative_links = list(
                child.representative_links.select_related("representative").order_by(
                    "-is_primary", "representative__last_name", "representative__first_name"
                )
            )
            if representative_links:
                for link in representative_links:
                    representative = link.representative
                    if (
                        not link.receives_schedule
                        or not representative.email
                    ):
                        continue
                    key = (
                        AppointmentConfirmation.TargetType.REPRESENTATIVE
                        if (
                            child.pk == appointment.child_id
                            and (link.is_primary or representative.pk == child.primary_parent_id)
                            and AppointmentConfirmation.TargetType.REPRESENTATIVE not in targets
                        )
                        else f"{AppointmentConfirmation.TargetType.REPRESENTATIVE}:{link.pk}"
                    )
                    targets[key] = {
                        "target_type": AppointmentConfirmation.TargetType.REPRESENTATIVE,
                        "label": (
                            f"Представителю: {representative.full_name} "
                            f"({representative.email}) · {child.full_name}"
                        ),
                        "email": representative.email,
                        "representative": representative,
                        "participant": participant,
                        "staff_assignment": None,
                    }
                continue
            if not child.primary_parent_id or not child.primary_parent.email:
                continue
            representative = child.primary_parent
            key = (
                AppointmentConfirmation.TargetType.REPRESENTATIVE
                if child.pk == appointment.child_id
                and AppointmentConfirmation.TargetType.REPRESENTATIVE not in targets
                else f"{AppointmentConfirmation.TargetType.REPRESENTATIVE}:primary:{child.pk}"
            )
            targets[key] = {
                "target_type": AppointmentConfirmation.TargetType.REPRESENTATIVE,
                "label": (
                    f"Представителю: {representative.full_name} "
                    f"({representative.email}) · {child.full_name}"
                ),
                "email": representative.email,
                "representative": representative,
                "participant": participant,
                "staff_assignment": None,
            }
        for child in self.appointment_children:
            if child.email:
                participant = self.participant_by_child_id.get(child.pk)
                key = (
                    AppointmentConfirmation.TargetType.RECIPIENT
                    if child.pk == appointment.child_id
                    and AppointmentConfirmation.TargetType.RECIPIENT not in targets
                    else f"{AppointmentConfirmation.TargetType.RECIPIENT}:{child.pk}"
                )
                targets[key] = {
                    "target_type": AppointmentConfirmation.TargetType.RECIPIENT,
                    "label": f"Получателю: {child.full_name} ({child.email})",
                    "email": child.email,
                    "representative": None,
                    "participant": participant,
                    "staff_assignment": None,
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
            target_type=target["target_type"],
            representative=target["representative"],
            participant=target["participant"],
            staff_assignment=target["staff_assignment"],
            email=target["email"],
            subject=self.cleaned_data["subject"],
            message=self._message_for_target(target),
            sent_by=user,
        )


class ConfirmationResponseForm(forms.Form):
    ACTION_CHOICES = (
        ("confirm", "Подтвердить"),
        ("decline", "Отклонить"),
    )
    action = forms.ChoiceField(label="Решение", choices=ACTION_CHOICES)
    response_note = forms.CharField(
        label="Комментарий", widget=forms.Textarea(attrs={"rows": 3}), required=False
    )


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
        self.fields["staff_member"].queryset = StaffMember.objects.filter(
            status=StaffMember.Status.ACTIVE
        ).order_by("full_name")
        self.fields["appointment"].required = False


class DocumentForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = (
            "target_type",
            "child",
            "counterparty",
            "category",
            "title",
            "file",
            "issued_on",
            "expires_on",
            "note",
        )
        labels = {
            "target_type": "К чему относится",
            "child": "Получатель",
            "counterparty": "Контрагент",
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
        self.fields["child"].required = False
        self.fields["counterparty"].queryset = Counterparty.all_objects.order_by("name")
        self.fields["counterparty"].required = False
        self.fields["target_type"].help_text = (
            "Для документов получателя выберите получателя. Для документов контрагента "
            "выберите контрагента. Для центра и общих договоров получатель не нужен."
        )

    def clean(self):
        cleaned = super().clean()
        target_type = cleaned.get("target_type")
        child = cleaned.get("child")
        counterparty = cleaned.get("counterparty")
        if target_type == Document.TargetType.RECIPIENT and child is None:
            self.add_error("child", "Для документа получателя выберите получателя.")
        if target_type == Document.TargetType.COUNTERPARTY and counterparty is None:
            self.add_error("counterparty", "Для документа контрагента выберите контрагента.")
        return cleaned


class ConsentForm(forms.ModelForm):
    class Meta:
        model = Consent
        fields = (
            "child",
            "consent_type",
            "signatory_representative",
            "template",
            "document",
            "signed_on",
            "expires_on",
            "note",
        )
        labels = {
            "child": "Получатель",
            "consent_type": "Тип согласия",
            "signatory_representative": "Подписант",
            "template": "Шаблон",
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
        self.fields["signatory_representative"].required = False
        self.fields["signatory_representative"].queryset = RecipientRepresentative.objects.none()
        self.fields["template"].required = False
        self.fields["template"].queryset = ContractTemplate.objects.filter(
            is_active=True,
            template_type__in=ContractTemplate.consent_template_types(),
        ).order_by("template_type", "title", "version")
        self.fields["document"].required = False
        self.fields["document"].queryset = Document.objects.none()
        child = self._selected_child()
        if child is not None:
            self.fields["signatory_representative"].queryset = (
                RecipientRepresentative.objects.select_related("representative")
                .filter(child=child)
                .order_by("-is_primary", "-signs_contract", "representative__last_name")
            )
            self.fields["document"].queryset = Document.objects.filter(
                target_type=Document.TargetType.RECIPIENT,
                category=Document.Category.CONSENT,
                child=child,
            ).order_by("-issued_on", "-created_at")
        elif self.instance.pk:
            self.fields["signatory_representative"].queryset = (
                RecipientRepresentative.objects.select_related("representative")
                .filter(child=self.instance.child)
                .order_by("-is_primary", "-signs_contract", "representative__last_name")
            )
            self.fields["document"].queryset = Document.objects.filter(
                target_type=Document.TargetType.RECIPIENT,
                category=Document.Category.CONSENT,
                child=self.instance.child,
            ).order_by("-issued_on", "-created_at")

    def _selected_child(self) -> Child | None:
        child_id = self.data.get(self.add_prefix("child")) if self.is_bound else None
        if child_id:
            try:
                return Child.objects.get(pk=child_id)
            except (Child.DoesNotExist, ValueError):
                return None
        child = self.initial.get("child")
        if isinstance(child, Child):
            return child
        if child:
            try:
                return Child.objects.get(pk=child)
            except (Child.DoesNotExist, ValueError):
                return None
        if self.instance.pk:
            return self.instance.child
        return None

    def clean(self):
        cleaned = super().clean()
        child = cleaned.get("child")
        signatory = cleaned.get("signatory_representative")
        document = cleaned.get("document")
        template = cleaned.get("template")
        if signatory is not None and child is not None and signatory.child_id != child.pk:
            self.add_error(
                "signatory_representative",
                "Подписант должен быть представителем выбранного получателя.",
            )
        if document is not None and child is not None:
            if document.category != Document.Category.CONSENT:
                self.add_error("document", "Связанный документ должен иметь категорию согласия.")
            elif document.target_type != Document.TargetType.RECIPIENT:
                self.add_error("document", "Согласие должно ссылаться на документ получателя.")
            elif document.child_id != child.pk:
                self.add_error(
                    "document",
                    "Документ согласия должен относиться к выбранному получателю.",
                )
        if (
            template is not None
            and template.template_type not in ContractTemplate.consent_template_types()
        ):
            self.add_error("template", "Выберите шаблон согласия.")
        return cleaned


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


class CenterExpenseCategoryForm(forms.ModelForm):
    class Meta:
        model = CenterExpenseCategory
        fields = ("name", "expense_type", "is_active", "sort_order", "notes")
        labels = {
            "name": "Название",
            "expense_type": "Тип расхода",
            "is_active": "Доступна для новых расходов",
            "sort_order": "Порядок",
            "notes": "Примечания",
        }
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        normalized_name = name.casefold()
        duplicate_names = CenterExpenseCategory.objects.all()
        if self.instance.pk:
            duplicate_names = duplicate_names.exclude(pk=self.instance.pk)
        if any(existing_name.casefold() == normalized_name for existing_name in duplicate_names.values_list("name", flat=True)):
            raise forms.ValidationError("Категория с таким названием уже есть.")
        return name


class CounterpartyForm(forms.ModelForm):
    class Meta:
        model = Counterparty
        fields = (
            "name",
            "counterparty_type",
            "inn",
            "kpp",
            "ogrn",
            "legal_address",
            "postal_address",
            "bank_details",
            "contact_person",
            "phone",
            "email",
            "notes",
        )
        labels = {
            "name": "Наименование",
            "counterparty_type": "Тип",
            "inn": "ИНН",
            "kpp": "КПП",
            "ogrn": "ОГРН/ОГРНИП",
            "legal_address": "Юридический адрес",
            "postal_address": "Почтовый адрес",
            "bank_details": "Банковские реквизиты",
            "contact_person": "Контактное лицо",
            "phone": "Телефон",
            "email": "Email",
            "notes": "Примечания",
        }
        help_texts = {
            "name": "Используется в расходах, договорах пожертвования и preview импорта.",
            "bank_details": "Реквизиты хранятся справочно и не создают платежи.",
        }
        widgets = {
            "legal_address": forms.Textarea(attrs={"rows": 3}),
            "postal_address": forms.Textarea(attrs={"rows": 3}),
            "bank_details": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_name(self):
        return self.cleaned_data["name"].strip()


class CenterLegalProfileForm(forms.ModelForm):
    class Meta:
        model = CenterLegalProfile
        fields = (
            "full_name",
            "short_name",
            "director_full_name",
            "director_short_name",
            "director_position",
            "authority_basis",
            "license_number",
            "license_date",
            "license_authority",
            "ogrn",
            "inn",
            "kpp",
            "legal_address",
            "location_address",
            "phone",
            "email",
            "site",
            "bank_name",
            "bank_bik",
            "bank_account",
            "bank_corr_account",
            "is_active",
            "notes",
        )
        labels = {
            "full_name": "Полное наименование",
            "short_name": "Краткое наименование",
            "director_full_name": "ФИО руководителя",
            "director_short_name": "ФИО руководителя кратко",
            "director_position": "Должность руководителя",
            "authority_basis": "Основание полномочий",
            "license_number": "Номер лицензии",
            "license_date": "Дата лицензии",
            "license_authority": "Кем выдана лицензия",
            "ogrn": "ОГРН",
            "inn": "ИНН",
            "kpp": "КПП",
            "legal_address": "Юридический адрес",
            "location_address": "Адрес места оказания услуг",
            "phone": "Телефон",
            "email": "Email",
            "site": "Сайт",
            "bank_name": "Банк",
            "bank_bik": "БИК",
            "bank_account": "Расчетный счет",
            "bank_corr_account": "Корреспондентский счет",
            "is_active": "Использовать для новых документов",
            "notes": "Примечания",
        }
        help_texts = {
            "short_name": "Если оставить пустым, в шаблонах можно использовать полное наименование.",
            "director_short_name": "Например: И. И. Иванов.",
            "authority_basis": "Например: Устава или доверенности.",
            "is_active": "Только один профиль может быть активным.",
        }
        widgets = {
            "license_date": DATE_INPUT,
            "legal_address": forms.Textarea(attrs={"rows": 3}),
            "location_address": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_full_name(self):
        return self.cleaned_data["full_name"].strip()


class CenterExpenseForm(forms.ModelForm):
    class Meta:
        model = CenterExpense
        fields = (
            "expense_date",
            "category",
            "title",
            "description",
            "counterparty",
            "total_amount",
            "notes",
        )
        labels = {
            "expense_date": "Дата расхода",
            "category": "Категория",
            "title": "Название",
            "description": "Описание",
            "counterparty": "Контрагент",
            "total_amount": "Сумма",
            "notes": "Примечания",
        }
        widgets = {
            "expense_date": DATE_INPUT,
            "description": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, readonly: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        category_filter = Q(is_active=True)
        if self.instance and self.instance.category_id:
            category_filter |= Q(pk=self.instance.category_id)
        self.fields["category"].queryset = CenterExpenseCategory.objects.filter(
            category_filter
        ).order_by("sort_order", "name")

        counterparty_filter = Q(archived_at__isnull=True)
        if self.instance and self.instance.counterparty_id:
            counterparty_filter |= Q(pk=self.instance.counterparty_id)
        self.fields["counterparty"].queryset = Counterparty.all_objects.filter(
            counterparty_filter
        ).order_by("name")
        self.fields["counterparty"].required = False

        if readonly:
            for field in self.fields.values():
                field.disabled = True


class ExpenseFundingSplitForm(forms.ModelForm):
    class Meta:
        model = ExpenseFundingSplit
        fields = ("funding_source", "amount", "notes")
        labels = {
            "funding_source": "Источник финансирования",
            "amount": "Сумма",
            "notes": "Примечания",
        }
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, readonly: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        source_filter = Q(archived_at__isnull=True)
        if self.instance and self.instance.funding_source_id:
            source_filter |= Q(pk=self.instance.funding_source_id)
        self.fields["funding_source"].queryset = FundingSource.all_objects.filter(
            source_filter
        ).order_by("name")

        if readonly:
            for field in self.fields.values():
                field.disabled = True


class BaseExpenseFundingSplitFormSet(BaseInlineFormSet):
    def clean(self):
        seen_source_ids: set[int] = set()
        split_total = Decimal("0")
        for form in self.forms:
            if not hasattr(form, "cleaned_data") or not form.cleaned_data:
                continue
            if form.cleaned_data.get("DELETE"):
                continue
            funding_source = form.cleaned_data.get("funding_source")
            amount = form.cleaned_data.get("amount")
            if funding_source:
                if funding_source.pk in seen_source_ids:
                    raise forms.ValidationError(
                        "Один источник финансирования нельзя указать дважды в одном расходе."
                    )
                seen_source_ids.add(funding_source.pk)
            if amount:
                split_total += amount
        self.split_total = split_total
        super().clean()


ExpenseFundingSplitFormSet = inlineformset_factory(
    CenterExpense,
    ExpenseFundingSplit,
    form=ExpenseFundingSplitForm,
    formset=BaseExpenseFundingSplitFormSet,
    fields=("funding_source", "amount", "notes"),
    extra=2,
    can_delete=True,
)


class EquipmentAssetForm(forms.ModelForm):
    class Meta:
        model = EquipmentAsset
        fields = (
            "name",
            "asset_type",
            "inventory_number",
            "purchase_date",
            "purchase_expense",
            "total_amount",
            "status",
            "location",
            "responsible_staff",
            "notes",
        )
        labels = {
            "name": "Название",
            "asset_type": "Тип",
            "inventory_number": "Инвентарный номер",
            "purchase_date": "Дата покупки",
            "purchase_expense": "Расход покупки",
            "total_amount": "Стоимость",
            "status": "Статус",
            "location": "Местонахождение",
            "responsible_staff": "Ответственный",
            "notes": "Примечания",
        }
        widgets = {
            "purchase_date": DATE_INPUT,
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        expense_filter = Q(
            category__expense_type=CenterExpenseCategory.ExpenseType.EQUIPMENT
        ) & ~Q(status=CenterExpense.Status.CANCELLED)
        if self.instance and self.instance.purchase_expense_id:
            expense_filter |= Q(pk=self.instance.purchase_expense_id)
        self.fields["purchase_expense"].queryset = (
            CenterExpense.objects.select_related("category")
            .filter(expense_filter)
            .order_by("-expense_date", "title")
        )
        self.fields["purchase_expense"].required = False

        staff_filter = Q(archived_at__isnull=True)
        if self.instance and self.instance.responsible_staff_id:
            staff_filter |= Q(pk=self.instance.responsible_staff_id)
        self.fields["responsible_staff"].queryset = StaffMember.all_objects.filter(
            staff_filter
        ).order_by("full_name")
        self.fields["responsible_staff"].required = False


class ContractTemplateForm(forms.ModelForm):
    class Meta:
        model = ContractTemplate
        fields = ("template_type", "title", "version", "file", "is_active", "notes")
        labels = {
            "template_type": "Тип шаблона",
            "title": "Название",
            "version": "Версия",
            "file": "Файл шаблона",
            "is_active": "Активен",
            "notes": "Примечания",
        }
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["file"].help_text = (
            "Поддерживается только .docx. Старые .doc из локальных образцов "
            "сначала конвертируйте в .docx."
        )

    def clean_file(self):
        upload = self.cleaned_data.get("file")
        if isinstance(upload, UploadedFile) and not upload.name.lower().endswith(".docx"):
            raise forms.ValidationError(
                "Загрузите шаблон в формате .docx. Файлы .doc нужно сначала "
                "конвертировать в .docx."
            )
        return upload


class DonationContractForm(forms.ModelForm):
    class Meta:
        model = DonationContract
        fields = (
            "counterparty",
            "funding_source",
            "contract_type",
            "number",
            "signed_on",
            "valid_from",
            "valid_until",
            "amount_limit",
            "status",
            "template",
            "document",
            "notes",
        )
        labels = {
            "counterparty": "Контрагент",
            "funding_source": "Источник финансирования",
            "contract_type": "Тип договора",
            "number": "Номер",
            "signed_on": "Дата подписания",
            "valid_from": "Действует с",
            "valid_until": "Действует до",
            "amount_limit": "Лимит суммы",
            "status": "Статус",
            "template": "Шаблон",
            "document": "Файл договора",
            "notes": "Примечания",
        }
        widgets = {
            "signed_on": DATE_INPUT,
            "valid_from": DATE_INPUT,
            "valid_until": DATE_INPUT,
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        counterparty_filter = Q(archived_at__isnull=True)
        if self.instance and self.instance.counterparty_id:
            counterparty_filter |= Q(pk=self.instance.counterparty_id)
        self.fields["counterparty"].queryset = Counterparty.all_objects.filter(
            counterparty_filter
        ).order_by("name")

        funding_filter = Q(archived_at__isnull=True)
        if self.instance and self.instance.funding_source_id:
            funding_filter |= Q(pk=self.instance.funding_source_id)
        self.fields["funding_source"].queryset = FundingSource.all_objects.filter(
            funding_filter
        ).order_by("name")

        template_filter = Q(
            template_type__in=ContractTemplate.donation_contract_template_types(),
            is_active=True,
        )
        if self.instance and self.instance.template_id:
            template_filter |= Q(pk=self.instance.template_id)
        self.fields["template"].queryset = ContractTemplate.objects.filter(
            template_filter
        ).order_by("template_type", "title", "version")
        self.fields["template"].required = False

        counterparty_id = self._selected_counterparty_id()
        document_filter = Q(category=Document.Category.CONTRACT) & ~Q(
            target_type=Document.TargetType.RECIPIENT
        )
        if counterparty_id:
            document_filter &= Q(
                target_type=Document.TargetType.COUNTERPARTY, counterparty_id=counterparty_id
            ) | Q(
                target_type__in=[
                    Document.TargetType.CENTER,
                    Document.TargetType.CONTRACT,
                    Document.TargetType.OTHER,
                ]
            )
        if self.instance and self.instance.document_id:
            document_filter |= Q(pk=self.instance.document_id)
        self.fields["document"].queryset = Document.objects.filter(document_filter).order_by(
            "-created_at"
        )
        self.fields["document"].required = False

    def _selected_counterparty_id(self) -> int | None:
        if self.is_bound:
            try:
                return int(self.data.get(self.add_prefix("counterparty")) or "")
            except (TypeError, ValueError):
                return None
        if self.instance and self.instance.counterparty_id:
            return self.instance.counterparty_id
        return None


class ServiceContractForm(forms.ModelForm):
    class Meta:
        model = ServiceContract
        fields = (
            "child",
            "representative_link",
            "funding_source",
            "certificate",
            "contract_type",
            "number",
            "signed_on",
            "valid_from",
            "valid_until",
            "status",
            "template",
            "document",
            "notes",
        )
        labels = {
            "child": "Получатель",
            "representative_link": "Подписант",
            "funding_source": "Источник финансирования",
            "certificate": "Сертификат",
            "contract_type": "Тип договора",
            "number": "Номер",
            "signed_on": "Дата подписания",
            "valid_from": "Действует с",
            "valid_until": "Действует до",
            "status": "Статус",
            "template": "Шаблон",
            "document": "Файл договора",
            "notes": "Примечания",
        }
        widgets = {
            "signed_on": DATE_INPUT,
            "valid_from": DATE_INPUT,
            "valid_until": DATE_INPUT,
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        child_id = self._selected_child_id()

        child_filter = Q(archived_at__isnull=True)
        if self.instance and self.instance.child_id:
            child_filter |= Q(pk=self.instance.child_id)
        self.fields["child"].queryset = Child.all_objects.filter(child_filter).order_by(
            "last_name", "first_name"
        )

        representative_filter = Q(signs_contract=True)
        if child_id:
            representative_filter &= Q(child_id=child_id)
        if self.instance and self.instance.representative_link_id:
            representative_filter |= Q(pk=self.instance.representative_link_id)
        self.fields["representative_link"].queryset = (
            RecipientRepresentative.objects.select_related("child", "representative")
            .filter(representative_filter)
            .order_by(
                "child__last_name",
                "child__first_name",
                "representative__last_name",
                "representative__first_name",
            )
        )

        funding_filter = Q(archived_at__isnull=True)
        if self.instance and self.instance.funding_source_id:
            funding_filter |= Q(pk=self.instance.funding_source_id)
        self.fields["funding_source"].queryset = FundingSource.all_objects.filter(
            funding_filter
        ).order_by("name")
        self.fields["funding_source"].required = False

        certificate_filter = Q()
        if child_id:
            certificate_filter &= Q(child_id=child_id)
        else:
            certificate_filter &= Q(pk__isnull=True)
        if self.instance and self.instance.certificate_id:
            certificate_filter |= Q(pk=self.instance.certificate_id)
        self.fields["certificate"].queryset = Certificate.objects.filter(
            certificate_filter
        ).order_by("-created_at")
        self.fields["certificate"].required = False

        template_filter = Q(
            template_type__in=ContractTemplate.service_contract_template_types(),
            is_active=True,
        )
        if self.instance and self.instance.template_id:
            template_filter |= Q(pk=self.instance.template_id)
        self.fields["template"].queryset = ContractTemplate.objects.filter(
            template_filter
        ).order_by("template_type", "title", "version")
        self.fields["template"].required = False

        document_filter = Q(
            category=Document.Category.CONTRACT,
            target_type=Document.TargetType.RECIPIENT,
        )
        if child_id:
            document_filter &= Q(child_id=child_id)
        if self.instance and self.instance.document_id:
            document_filter |= Q(pk=self.instance.document_id)
        self.fields["document"].queryset = Document.objects.filter(document_filter).order_by(
            "-created_at"
        )
        self.fields["document"].required = False

    def _selected_child_id(self) -> int | None:
        if self.is_bound:
            try:
                return int(self.data.get(self.add_prefix("child")) or "")
            except (TypeError, ValueError):
                return None
        if self.instance and self.instance.child_id:
            return self.instance.child_id
        return None


class OrganizationServiceContractForm(forms.ModelForm):
    class Meta:
        model = OrganizationServiceContract
        fields = (
            "counterparty",
            "funding_source",
            "contract_type",
            "number",
            "signed_on",
            "valid_from",
            "valid_until",
            "status",
            "template",
            "document",
            "notes",
        )
        labels = {
            "counterparty": "Организация",
            "funding_source": "Источник финансирования",
            "contract_type": "Тип договора",
            "number": "Номер",
            "signed_on": "Дата подписания",
            "valid_from": "Действует с",
            "valid_until": "Действует до",
            "status": "Статус",
            "template": "Шаблон",
            "document": "Файл договора",
            "notes": "Примечания",
        }
        widgets = {
            "signed_on": DATE_INPUT,
            "valid_from": DATE_INPUT,
            "valid_until": DATE_INPUT,
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        counterparty_filter = Q(archived_at__isnull=True)
        if self.instance and self.instance.counterparty_id:
            counterparty_filter |= Q(pk=self.instance.counterparty_id)
        self.fields["counterparty"].queryset = Counterparty.all_objects.filter(
            counterparty_filter
        ).order_by("name")

        funding_filter = Q(archived_at__isnull=True)
        if self.instance and self.instance.funding_source_id:
            funding_filter |= Q(pk=self.instance.funding_source_id)
        self.fields["funding_source"].queryset = FundingSource.all_objects.filter(
            funding_filter
        ).order_by("name")
        self.fields["funding_source"].required = False

        template_filter = Q(
            template_type__in=ContractTemplate.organization_service_contract_template_types(),
            is_active=True,
        )
        if self.instance and self.instance.template_id:
            template_filter |= Q(pk=self.instance.template_id)
        self.fields["template"].queryset = ContractTemplate.objects.filter(
            template_filter
        ).order_by("template_type", "title", "version")
        self.fields["template"].required = False

        counterparty_id = self._selected_counterparty_id()
        document_filter = Q(category=Document.Category.CONTRACT) & ~Q(
            target_type=Document.TargetType.RECIPIENT
        )
        if counterparty_id:
            document_filter &= Q(
                target_type=Document.TargetType.COUNTERPARTY,
                counterparty_id=counterparty_id,
            ) | Q(
                target_type__in=[
                    Document.TargetType.CENTER,
                    Document.TargetType.CONTRACT,
                    Document.TargetType.OTHER,
                ]
            )
        if self.instance and self.instance.document_id:
            document_filter |= Q(pk=self.instance.document_id)
        self.fields["document"].queryset = Document.objects.filter(document_filter).order_by(
            "-created_at"
        )
        self.fields["document"].required = False

    def _selected_counterparty_id(self) -> int | None:
        if self.is_bound:
            try:
                return int(self.data.get(self.add_prefix("counterparty")) or "")
            except (TypeError, ValueError):
                return None
        if self.instance and self.instance.counterparty_id:
            return self.instance.counterparty_id
        return None


class ContractActForm(forms.ModelForm):
    class Meta:
        model = ContractAct
        fields = (
            "act_kind",
            "service_contract",
            "organization_contract",
            "number",
            "act_on",
            "period_from",
            "period_until",
            "amount",
            "status",
            "template",
            "document",
            "notes",
        )
        labels = {
            "act_kind": "Тип акта",
            "service_contract": "Договор с получателем",
            "organization_contract": "B2B-договор",
            "number": "Номер акта",
            "act_on": "Дата акта",
            "period_from": "Период с",
            "period_until": "Период по",
            "amount": "Сумма акта",
            "status": "Статус",
            "template": "Шаблон",
            "document": "Файл акта",
            "notes": "Примечания",
        }
        widgets = {
            "act_on": DATE_INPUT,
            "period_from": DATE_INPUT,
            "period_until": DATE_INPUT,
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["act_kind"].initial = self.instance.act_kind or ContractAct.ActKind.SERVICE
        self.fields["service_contract"].queryset = (
            ServiceContract.objects.select_related("child", "representative_link__representative")
            .prefetch_related("service_lines__service")
            .order_by("-signed_on", "-created_at")
        )
        self.fields["service_contract"].required = False
        self.fields["organization_contract"].queryset = (
            OrganizationServiceContract.objects.select_related("counterparty", "funding_source")
            .prefetch_related("service_lines__service")
            .order_by("-signed_on", "-created_at")
        )
        self.fields["organization_contract"].required = False

        template_filter = Q(
            template_type__in=ContractTemplate.act_template_types(),
            is_active=True,
        )
        if self.instance and self.instance.template_id:
            template_filter |= Q(pk=self.instance.template_id)
        self.fields["template"].queryset = ContractTemplate.objects.filter(
            template_filter
        ).order_by("template_type", "title", "version")
        self.fields["template"].required = False

        self.fields["document"].queryset = self._document_queryset()
        self.fields["document"].required = False
        self.fields["document"].help_text = (
            "Если оставить пустым, Word создаст новый документ акта для выбранного договора."
        )

    def _selected_act_kind(self) -> str:
        if self.is_bound:
            return self.data.get(self.add_prefix("act_kind")) or ""
        if self.instance and self.instance.act_kind:
            return self.instance.act_kind
        return ContractAct.ActKind.SERVICE

    def _selected_service_contract_id(self) -> int | None:
        if self.is_bound:
            try:
                return int(self.data.get(self.add_prefix("service_contract")) or "")
            except (TypeError, ValueError):
                return None
        if self.instance and self.instance.service_contract_id:
            return self.instance.service_contract_id
        return None

    def _selected_organization_contract_id(self) -> int | None:
        if self.is_bound:
            try:
                return int(self.data.get(self.add_prefix("organization_contract")) or "")
            except (TypeError, ValueError):
                return None
        if self.instance and self.instance.organization_contract_id:
            return self.instance.organization_contract_id
        return None

    def _document_queryset(self):
        document_filter = Q(category=Document.Category.ACT)
        act_kind = self._selected_act_kind()
        if act_kind == ContractAct.ActKind.SERVICE:
            service_contract_id = self._selected_service_contract_id()
            if service_contract_id:
                try:
                    contract = ServiceContract.objects.only("child_id").get(
                        pk=service_contract_id
                    )
                except ServiceContract.DoesNotExist:
                    document_filter &= Q(pk__isnull=True)
                else:
                    document_filter &= Q(
                        target_type=Document.TargetType.RECIPIENT,
                        child_id=contract.child_id,
                    )
            else:
                document_filter &= Q(pk__isnull=True)
        elif act_kind == ContractAct.ActKind.ORGANIZATION_SERVICE:
            organization_contract_id = self._selected_organization_contract_id()
            if organization_contract_id:
                try:
                    contract = OrganizationServiceContract.objects.only(
                        "counterparty_id"
                    ).get(pk=organization_contract_id)
                except OrganizationServiceContract.DoesNotExist:
                    document_filter &= Q(pk__isnull=True)
                else:
                    document_filter &= Q(
                        target_type=Document.TargetType.COUNTERPARTY,
                        counterparty_id=contract.counterparty_id,
                    ) | Q(
                        target_type__in=[
                            Document.TargetType.CENTER,
                            Document.TargetType.CONTRACT,
                            Document.TargetType.OTHER,
                        ],
                    )
            else:
                document_filter &= Q(pk__isnull=True)
        else:
            document_filter &= Q(pk__isnull=True)
        if self.instance and self.instance.document_id:
            document_filter |= Q(pk=self.instance.document_id)
        return Document.objects.filter(document_filter).order_by("-created_at")


class ServiceContractLineForm(forms.ModelForm):
    meaningful_empty_check_fields = (
        "service",
        "service_name",
        "quantity",
        "unit_price",
        "starts_on",
        "ends_on",
        "notes",
    )

    class Meta:
        model = ServiceContractLine
        fields = (
            "sort_order",
            "service",
            "service_name",
            "quantity",
            "unit",
            "unit_price",
            "starts_on",
            "ends_on",
            "notes",
        )
        labels = {
            "sort_order": "Порядок",
            "service": "Услуга",
            "service_name": "Наименование в договоре",
            "quantity": "Количество",
            "unit": "Ед.",
            "unit_price": "Цена",
            "starts_on": "Период с",
            "ends_on": "Период по",
            "notes": "Примечания",
        }
        widgets = {
            "starts_on": DATE_INPUT,
            "ends_on": DATE_INPUT,
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        service_filter = Q(is_active=True)
        if self.instance and self.instance.service_id:
            service_filter |= Q(pk=self.instance.service_id)
        self.fields["service"].queryset = Service.all_objects.filter(service_filter).order_by(
            "name"
        )
        self.fields["service_name"].required = False

    def has_changed(self) -> bool:
        base_changed = super().has_changed()
        if not base_changed:
            return False
        if self.instance and self.instance.pk:
            return True
        return any(
            (self.data.get(self.add_prefix(field)) or "").strip()
            for field in self.meaningful_empty_check_fields
        )


class OrganizationServiceContractLineForm(forms.ModelForm):
    meaningful_empty_check_fields = (
        "service",
        "service_name",
        "quantity",
        "unit_price",
        "starts_on",
        "ends_on",
        "notes",
    )

    class Meta:
        model = OrganizationServiceContractLine
        fields = (
            "sort_order",
            "service",
            "service_name",
            "quantity",
            "unit",
            "unit_price",
            "starts_on",
            "ends_on",
            "notes",
        )
        labels = {
            "sort_order": "Порядок",
            "service": "Услуга",
            "service_name": "Наименование в договоре",
            "quantity": "Количество",
            "unit": "Ед.",
            "unit_price": "Цена",
            "starts_on": "Период с",
            "ends_on": "Период по",
            "notes": "Примечания",
        }
        widgets = {
            "starts_on": DATE_INPUT,
            "ends_on": DATE_INPUT,
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        service_filter = Q(is_active=True)
        if self.instance and self.instance.service_id:
            service_filter |= Q(pk=self.instance.service_id)
        self.fields["service"].queryset = Service.all_objects.filter(service_filter).order_by(
            "name"
        )
        self.fields["service_name"].required = False

    def has_changed(self) -> bool:
        base_changed = super().has_changed()
        if not base_changed:
            return False
        if self.instance and self.instance.pk:
            return True
        return any(
            (self.data.get(self.add_prefix(field)) or "").strip()
            for field in self.meaningful_empty_check_fields
        )


ServiceContractLineFormSet = inlineformset_factory(
    ServiceContract,
    ServiceContractLine,
    form=ServiceContractLineForm,
    fields=(
        "sort_order",
        "service",
        "service_name",
        "quantity",
        "unit",
        "unit_price",
        "starts_on",
        "ends_on",
        "notes",
    ),
    extra=2,
    can_delete=True,
)

OrganizationServiceContractLineFormSet = inlineformset_factory(
    OrganizationServiceContract,
    OrganizationServiceContractLine,
    form=OrganizationServiceContractLineForm,
    fields=(
        "sort_order",
        "service",
        "service_name",
        "quantity",
        "unit",
        "unit_price",
        "starts_on",
        "ends_on",
        "notes",
    ),
    extra=2,
    can_delete=True,
)


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


class FundingServiceQuotaQuickForm(forms.ModelForm):
    class Meta:
        model = FundingServiceQuota
        fields = (
            "funding_source",
            "service",
            "planned_sessions",
            "starts_on",
            "ends_on",
            "note",
        )
        labels = {
            "funding_source": "Источник финансирования",
            "service": "Услуга",
            "planned_sessions": "План занятий",
            "starts_on": "Действует с",
            "ends_on": "Действует по",
            "note": "Примечание",
        }
        widgets = {
            "starts_on": DATE_INPUT,
            "ends_on": DATE_INPUT,
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["funding_source"].queryset = FundingSource.objects.filter(
            archived_at__isnull=True
        ).order_by("name")
        self.fields["service"].queryset = Service.objects.filter(is_active=True).order_by("name")

    def clean(self):
        cleaned = super().clean()
        date_from = cleaned.get("starts_on")
        date_to = cleaned.get("ends_on")
        if date_from and date_to and date_to < date_from:
            raise forms.ValidationError("Дата окончания не может быть раньше даты начала.")
        return cleaned


class FundingStaffAllocationQuickForm(forms.ModelForm):
    class Meta:
        model = FundingStaffAllocation
        fields = (
            "service_quota",
            "funding_source",
            "service",
            "staff_member",
            "allocated_sessions",
            "session_pay_amount",
            "starts_on",
            "ends_on",
            "note",
        )
        labels = {
            "service_quota": "Квота услуги",
            "funding_source": "Источник финансирования",
            "service": "Услуга",
            "staff_member": "Специалист",
            "allocated_sessions": "Количество занятий",
            "session_pay_amount": "Ставка специалисту за занятие",
            "starts_on": "Действует с",
            "ends_on": "Действует по",
            "note": "Примечание",
        }
        help_texts = {
            "service_quota": (
                "Можно выбрать существующую квоту услуги или оставить пустым и задать "
                "источник + услугу вручную."
            ),
            "session_pay_amount": "Заполните, если для гранта нужна отдельная ставка специалисту.",
        }
        widgets = {
            "starts_on": DATE_INPUT,
            "ends_on": DATE_INPUT,
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service_quota"].queryset = FundingServiceQuota.objects.select_related(
            "funding_source", "service"
        ).order_by("funding_source__name", "service__name", "starts_on")
        self.fields["service_quota"].required = False
        self.fields["funding_source"].queryset = FundingSource.objects.filter(
            archived_at__isnull=True
        ).order_by("name")
        self.fields["funding_source"].required = False
        self.fields["service"].queryset = Service.objects.filter(is_active=True).order_by("name")
        self.fields["service"].required = False
        self.fields["staff_member"].queryset = StaffMember.objects.filter(
            status=StaffMember.Status.ACTIVE
        ).order_by("full_name")

    def clean(self):
        cleaned = super().clean()
        service_quota = cleaned.get("service_quota")
        funding_source = cleaned.get("funding_source")
        service = cleaned.get("service")
        allocated_sessions = cleaned.get("allocated_sessions") or 0
        date_from = cleaned.get("starts_on")
        date_to = cleaned.get("ends_on")

        if service_quota:
            cleaned["funding_source"] = service_quota.funding_source
            cleaned["service"] = service_quota.service
            already_allocated = (
                FundingStaffAllocation.objects.filter(service_quota=service_quota)
                .exclude(pk=self.instance.pk)
                .aggregate(total=Sum("allocated_sessions"))["total"]
                or 0
            )
            if already_allocated + allocated_sessions > service_quota.planned_sessions:
                self.add_error(
                    "allocated_sessions",
                    (
                        "Количество превышает общий план квоты. "
                        f"Уже распределено: {already_allocated}, план: {service_quota.planned_sessions}."
                    ),
                )
        else:
            if not funding_source:
                self.add_error("funding_source", "Укажите источник финансирования.")
            if not service:
                self.add_error("service", "Укажите услугу.")
        if date_from and date_to and date_to < date_from:
            raise forms.ValidationError("Дата окончания не может быть раньше даты начала.")
        return cleaned


class GrantRecipientAllocationQuickForm(forms.ModelForm):
    class Meta:
        model = GrantRecipientAllocation
        fields = (
            "funding_source",
            "child",
            "service",
            "allocated_sessions",
            "balance_account",
            "valid_from",
            "valid_until",
            "note",
        )
        labels = {
            "funding_source": "Источник финансирования",
            "child": "Получатель",
            "service": "Услуга",
            "allocated_sessions": "Количество занятий",
            "balance_account": "Счет баланса",
            "valid_from": "Действует с",
            "valid_until": "Действует по",
            "note": "Примечание",
        }
        help_texts = {
            "balance_account": (
                "Можно оставить пустым: система создаст счет в занятиях для выбранного "
                "получателя, источника и услуги."
            ),
        }
        widgets = {
            "valid_from": DATE_INPUT,
            "valid_until": DATE_INPUT,
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["funding_source"].queryset = FundingSource.objects.filter(
            archived_at__isnull=True
        ).order_by("name")
        self.fields["child"].queryset = Child.objects.order_by("last_name", "first_name")
        self.fields["service"].queryset = Service.objects.filter(is_active=True).order_by("name")
        self.fields["balance_account"].queryset = (
            BalanceAccount.objects.select_related("child", "funding_source", "service")
            .filter(unit=BalanceAccount.Unit.SESSIONS, status=BalanceAccount.Status.ACTIVE)
            .order_by("child__last_name", "funding_source__name", "service__name")
        )
        self.fields["balance_account"].required = False

    def clean(self):
        cleaned = super().clean()
        funding_source = cleaned.get("funding_source")
        child = cleaned.get("child")
        service = cleaned.get("service")
        account = cleaned.get("balance_account")
        valid_from = cleaned.get("valid_from")
        valid_until = cleaned.get("valid_until")
        if valid_from and valid_until and valid_until < valid_from:
            self.add_error("valid_until", "Дата окончания не может быть раньше даты начала.")
        if account:
            if child and account.child_id != child.id:
                self.add_error("balance_account", "Счет должен принадлежать выбранному получателю.")
            if funding_source and account.funding_source_id != funding_source.id:
                self.add_error(
                    "balance_account",
                    "Счет должен относиться к выбранному источнику финансирования.",
                )
            if account.unit != BalanceAccount.Unit.SESSIONS:
                self.add_error("balance_account", "Выберите счет в занятиях.")
            if service and not account.can_pay_for(service):
                self.add_error("balance_account", "Счет не подходит для выбранной услуги.")
        return cleaned

    def save(self, commit=True):
        allocation = super().save(commit=False)
        if commit and not allocation.balance_account_id:
            allocation.balance_account = BalanceAccount.objects.create(
                child=allocation.child,
                funding_source=allocation.funding_source,
                unit=BalanceAccount.Unit.SESSIONS,
                service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
                service=allocation.service,
                initial_amount=Decimal(allocation.allocated_sessions),
                valid_from=allocation.valid_from,
                valid_until=allocation.valid_until,
                notes="\n".join(
                    part
                    for part in [
                        f"Создано из грантового выделения: {allocation.allocated_sessions} зан.",
                        allocation.note,
                    ]
                    if part
                ),
            )
        if commit:
            allocation.save()
            self.save_m2m()
        return allocation


class RecipientImportPreviewForm(forms.Form):
    file = forms.FileField(
        label="Файл Excel/CSV",
        help_text=(
            "Поддерживаются .xlsx, .csv и .tsv. На этом шаге система только проверяет файл "
            "и ничего не записывает в базу."
        ),
    )

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        name = uploaded.name.lower()
        if not name.endswith((".xlsx", ".csv", ".tsv")):
            raise forms.ValidationError("Загрузите файл .xlsx, .csv или .tsv.")
        if uploaded.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Файл слишком большой. Ограничение: 5 МБ.")
        return uploaded


class ContractImportPreviewForm(forms.Form):
    import_type = forms.ChoiceField(
        label="Что проверяем",
        choices=CONTRACT_IMPORT_TYPE_CHOICES,
        initial="expenses",
    )
    file = forms.FileField(
        label="Файл Excel/CSV",
        help_text=(
            "Поддерживаются .xlsx, .csv и .tsv. Экран только проверяет строки "
            "и ничего не записывает в базу."
        ),
    )

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        name = uploaded.name.lower()
        if not name.endswith((".xlsx", ".csv", ".tsv")):
            raise forms.ValidationError("Загрузите файл .xlsx, .csv или .tsv.")
        if uploaded.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Файл слишком большой. Ограничение: 5 МБ.")
        return uploaded
