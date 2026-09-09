"""Forms for explicit pause and resume decisions on treatment programs."""

from uuid import uuid4

from django import forms


class TreatmentProgramLifecycleActionForm(forms.Form):
    operation_key = forms.UUIDField(widget=forms.HiddenInput)
    expected_event_id = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Основание решения",
        min_length=5,
        max_length=2000,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("operation_key", uuid4())

