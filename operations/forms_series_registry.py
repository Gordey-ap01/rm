"""Validated, read-only filters for the appointment series registry."""

from django import forms

from operations.models import AppointmentSeries, Child, TreatmentProgram


class SeriesRegistryFilterForm(forms.Form):
    recipient = forms.ModelChoiceField(
        label="Получатель",
        queryset=Child.objects.order_by("last_name", "first_name", "pk"),
        required=False,
        empty_label="Все получатели",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    program = forms.ModelChoiceField(
        label="Программа",
        queryset=TreatmentProgram.objects.select_related("child").order_by(
            "child__last_name", "child__first_name", "title", "pk"
        ),
        required=False,
        empty_label="Все программы",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    status = forms.ChoiceField(
        label="Статус серии",
        choices=[("", "Все статусы"), *AppointmentSeries.Status.choices],
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    date_from = forms.DateField(
        label="Период с",
        required=False,
        input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "class": "form-control"}),
    )
    date_to = forms.DateField(
        label="Период по",
        required=False,
        input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "class": "form-control"}),
    )

    def clean(self):
        cleaned = super().clean()
        recipient, program = cleaned.get("recipient"), cleaned.get("program")
        if recipient and program and program.child_id != recipient.pk:
            self.add_error("program", "Программа должна принадлежать выбранному получателю.")
        date_from, date_to = cleaned.get("date_from"), cleaned.get("date_to")
        if date_from and date_to and date_to < date_from:
            self.add_error("date_to", "Дата окончания не может быть раньше даты начала.")
        return cleaned
