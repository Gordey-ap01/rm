"""Input forms for a future composition, separate from appointment creation."""

from datetime import timedelta

from django import forms
from django.db.models import Q
from django.forms import BaseFormSet, formset_factory
from django.utils import timezone

from operations.models import (
    AppointmentSeriesStaffAssignment,
    ProgramBlock,
    StaffMember,
    TreatmentProgram,
)
from operations.services.series_revisions import SeriesParticipantInput, SeriesStaffInput


class ProgramBlockChoices(forms.ModelMultipleChoiceField):
    def label_from_instance(self, block):
        return (
            f"{block.program.child} / {block.program.title} / {block.number}. {block.title} "
            f"({block.program.get_status_display()}, {block.get_status_display()})"
        )


class SeriesCompositionForm(forms.Form):
    expected_revision_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    preview_token = forms.CharField(required=False, widget=forms.HiddenInput, max_length=200000)
    effective_from = forms.DateField(
        label="Применять новый состав с",
        input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "class": "form-control"}),
    )
    reason = forms.CharField(
        label="Основание изменения", min_length=5, max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
    )
    program_blocks = ProgramBlockChoices(
        label="Каскады получателей", queryset=ProgramBlock.objects.none(),
        widget=forms.SelectMultiple(attrs={"size": 8, "data-searchable": "off", "class": "form-select"}),
        help_text="Выберите по одному каскаду для каждого получателя. Счет оплаты берется из каскада.",
    )

    def __init__(self, *args, series, **kwargs):
        self.series = series
        self.memberships = list(series.current_revision.participants.all())
        super().__init__(*args, **kwargs)
        current_blocks = [item.program_block_id for item in self.memberships if item.program_block_id]
        eligible = Q(
            service_id=series.service_id, program__status=TreatmentProgram.Status.ACTIVE
        ) & ~Q(status__in=[ProgramBlock.Status.COMPLETED, ProgramBlock.Status.CANCELLED])
        self.fields["program_blocks"].queryset = (
            ProgramBlock.objects.select_related(
                "program__child", "service", "balance_account__funding_source",
                "balance_account__service", "balance_account__child",
            )
            .filter(eligible | Q(pk__in=current_blocks))
            .order_by("program__child__last_name", "program__child__first_name", "number", "pk")
        )
        first_day = max(timezone.localdate(), series.current_revision.effective_from) + timedelta(days=1)
        self.initial.setdefault("expected_revision_id", series.current_revision_id)
        self.initial.setdefault("effective_from", first_day)
        self.initial.setdefault("program_blocks", current_blocks)
        self.fields["effective_from"].widget.attrs.update(
            min=first_day.isoformat(), max=series.end_date.isoformat()
        )

    def clean(self):
        cleaned = super().clean()
        effective_from = cleaned.get("effective_from")
        if effective_from and not (
            max(timezone.localdate(), self.series.current_revision.effective_from)
            < effective_from <= self.series.end_date
        ):
            self.add_error("effective_from", "Выберите будущую дату после начала текущей редакции, в периоде серии.")
        blocks = list(cleaned.get("program_blocks") or [])
        if blocks:
            if len({block.program.child_id for block in blocks}) != len(blocks):
                self.add_error("program_blocks", "Выберите только один каскад для каждого получателя.")
            if self.series.session_type == "individual" and len(blocks) != 1:
                self.add_error("program_blocks", "В индивидуальной серии нужен один получатель.")
            if self.series.session_type == "group" and len(blocks) < 2:
                self.add_error("program_blocks", "В групповой серии нужны минимум два получателя.")
            if self.series.room and self.series.room.limit_recipient_count and len(blocks) > self.series.room.max_recipient_count:
                self.add_error("program_blocks", "Число получателей превышает вместимость кабинета.")
            for block in blocks:
                if (
                    block.service_id != self.series.service_id
                    or block.program.status != TreatmentProgram.Status.ACTIVE
                    or block.status in {ProgramBlock.Status.COMPLETED, ProgramBlock.Status.CANCELLED}
                ):
                    self.add_error("program_blocks", "Нужны доступные каскады той же услуги из активных программ.")
                    break
        return cleaned

    def selected_blocks(self):
        # A replacement cascade for an existing child keeps the child's position.
        positions = {item.child_id: item.position for item in self.memberships}
        blocks = list(self.cleaned_data["program_blocks"])
        return sorted(blocks, key=lambda block: (positions.get(block.program.child_id, float("inf")), block.pk))

    def participant_inputs(self):
        return [
            SeriesParticipantInput(
                child_id=block.program.child_id, program_block_id=block.pk,
                billing_account_id=block.balance_account_id, position=position,
            )
            for position, block in enumerate(self.selected_blocks(), 1)
        ]


def _staff_queryset(current_staff_ids):
    return StaffMember.all_objects.filter(
        (Q(archived_at__isnull=True) & ~Q(status=StaffMember.Status.INACTIVE))
        | Q(pk__in=current_staff_ids)
    ).order_by("full_name", "pk")


class SeriesCompositionStaffForm(forms.Form):
    staff_member = forms.ModelChoiceField(
        label="Специалист", queryset=StaffMember.objects.none(),
        widget=forms.Select(attrs={"class": "form-select", "data-searchable": "off"}),
    )
    role = forms.ChoiceField(
        label="Роль в серии", choices=[("", "Выберите роль"), *AppointmentSeriesStaffAssignment.Role.choices],
        widget=forms.Select(attrs={"class": "form-select", "data-searchable": "off"}),
    )
    override_availability = forms.BooleanField(label="Разрешить выход вне графика", required=False)
    override_reason = forms.CharField(
        label="Основание выхода вне графика", required=False, max_length=2000,
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
    )

    def __init__(self, *args, current_staff_ids=(), staff_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["staff_member"].queryset = _staff_queryset(current_staff_ids)
        if staff_choices is not None:
            self.fields["staff_member"].choices = staff_choices

    def clean(self):
        cleaned = super().clean()
        member = cleaned.get("staff_member")
        override = cleaned.get("override_availability", False)
        if member:
            if member.is_archived or member.status == StaffMember.Status.INACTIVE:
                self.add_error("staff_member", "Выберите действующего специалиста.")
            elif member.status != StaffMember.Status.ACTIVE and not override:
                self.add_error("override_availability", "Отпуск или больничный требует явного разрешения выхода вне графика.")
        if override and len(cleaned.get("override_reason", "")) < 5:
            self.add_error("override_reason", "Укажите основание (минимум 5 символов).")
        return cleaned


class BaseCompositionStaffFormSet(BaseFormSet):
    def __init__(self, *args, series, **kwargs):
        self.series = series
        form_kwargs = dict(kwargs.get("form_kwargs") or {})
        # Render the common directory once; ModelChoiceField still validates
        # submitted IDs against its queryset, including current eligibility.
        form_kwargs["staff_choices"] = [
            ("", "Выберите специалиста"),
            *[(member.pk, str(member)) for member in _staff_queryset(form_kwargs.get("current_staff_ids", ()))],
        ]
        kwargs["form_kwargs"] = form_kwargs
        super().__init__(*args, **kwargs)

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        rows = self.selected_rows()
        members = [row["staff_member"].pk for row in rows]
        if not rows or sum(row["role"] == "primary" for row in rows) != 1:
            raise forms.ValidationError("Нужен ровно один основной специалист.")
        if len(set(members)) != len(members):
            raise forms.ValidationError("Специалист не должен повторяться в составе.")
        if self.series.session_type == "individual" and len(rows) != 1:
            raise forms.ValidationError("В индивидуальной серии нужен один специалист.")
        if self.series.room and self.series.room.limit_staff_count and len(rows) > self.series.room.max_staff_count:
            raise forms.ValidationError("Число специалистов превышает вместимость кабинета.")

    def selected_rows(self):
        return [
            form.cleaned_data for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE")
        ]

    def staff_inputs(self):
        return [
            SeriesStaffInput(
                staff_member_id=row["staff_member"].pk, role=row["role"],
                override_availability=row["override_availability"], override_reason=row["override_reason"],
            )
            for row in self.selected_rows()
        ]


SeriesCompositionStaffFormSet = formset_factory(
    SeriesCompositionStaffForm, formset=BaseCompositionStaffFormSet,
    extra=1, can_delete=True, max_num=100, validate_max=True, absolute_max=100,
)
