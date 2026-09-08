"""Review and save future composition without changing materialized appointments."""

from dataclasses import asdict

from django.contrib import messages
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from operations.forms_series_composition import SeriesCompositionForm, SeriesCompositionStaffFormSet
from operations.models import Appointment, AppointmentSeries, AppointmentSeriesStaffAssignment
from operations.services import series_revisions

from ._common import admin_required

COMPOSITION_PREVIEW_SALT = "operations.series-composition-preview.v1"
COMPOSITION_PREVIEW_MAX_AGE = 30 * 60


def composition_access(series, *, user):
    try:
        series_revisions.assert_future_composition_editable(series, actor=user)
    except ValidationError as exc:
        reason = " ".join(exc.messages)
    except PermissionDenied as exc:
        reason = str(exc)
    else:
        reason = ""
    return {
        "allowed": not reason, "reason": reason,
        "url": reverse("appointment_series_composition", args=[series.pk]),
    }


def _confirmation_payload(series, user, form, staff_formset):
    return {
        "series_id": series.pk, "actor_id": user.pk,
        "expected_revision_id": form.cleaned_data["expected_revision_id"],
        "effective_from": form.cleaned_data["effective_from"].isoformat(),
        "reason": form.cleaned_data["reason"],
        "participants": [asdict(item) for item in form.participant_inputs()],
        "staff": [asdict(item) for item in staff_formset.staff_inputs()],
    }


def _preview(series, form, staff_formset):
    appointments = Appointment.objects.filter(
        series=series, starts_at__date__gte=form.cleaned_data["effective_from"]
    ).order_by("starts_at", "pk")
    roles = dict(AppointmentSeriesStaffAssignment.Role.choices)
    return {
        "effective_from": form.cleaned_data["effective_from"],
        "reason": form.cleaned_data["reason"],
        "participants": [
            {"child": block.program.child, "program_block": block, "billing_account": block.balance_account}
            for block in form.selected_blocks()
        ],
        "staff": [
            {**row, "role_label": roles[row["role"]]}
            for row in staff_formset.selected_rows()
        ],
        "existing_appointments": list(appointments[:10]),
        "existing_appointments_count": appointments.count(),
    }


@admin_required
@require_http_methods(["GET", "POST"])
def appointment_series_composition(request, series_id):
    series = get_object_or_404(
        AppointmentSeries.objects.select_related("service", "room", "current_revision").prefetch_related(
            "current_revision__participants", "current_revision__staff_assignments",
        ),
        pk=series_id,
    )
    access = composition_access(series, user=request.user)
    context = {"series": series, "composition_access": access, "preview": None}
    if not access["allowed"]:
        status = 403 if request.method == "POST" else 200
        if request.method == "POST" and series.current_revision_id:
            try:
                expected = int(request.POST.get("expected_revision_id", ""))
            except (ValueError, TypeError):
                expected = 0
            if expected > 0 and expected != series.current_revision_id:
                context["composition_access"] = {
                    **access,
                    "reason": "Состав серии уже изменился. Откройте карточку и проверьте актуальную редакцию.",
                }
                status = 409
        return render(
            request, "operations/appointment_series_composition.html", context,
            status=status,
        )
    assignments = list(series.current_revision.staff_assignments.all())
    initial_staff = [
        {
            "staff_member": assignment.staff_member_id, "role": assignment.role,
            "override_availability": assignment.override_availability,
            "override_reason": assignment.override_reason,
        }
        for assignment in assignments
    ]
    data = request.POST if request.method == "POST" else None
    form = SeriesCompositionForm(data, series=series)
    staff_formset = SeriesCompositionStaffFormSet(
        data, prefix="staff", series=series, initial=initial_staff,
        form_kwargs={"current_staff_ids": [item.staff_member_id for item in assignments]},
    )
    context.update(form=form, staff_formset=staff_formset)
    status = 200
    if request.method == "POST":
        form_valid = form.is_valid()
        staff_valid = staff_formset.is_valid()
        expected = form.cleaned_data.get("expected_revision_id")
        if expected and expected != series.current_revision_id:
            form.add_error(None, "Состав серии уже изменился. Откройте редактор заново и проверьте актуальные данные.")
            status = 409
        elif form_valid and staff_valid:
            action = request.POST.get("action")
            payload = _confirmation_payload(series, request.user, form, staff_formset)
            if action == "preview":
                context["preview"] = _preview(series, form, staff_formset)
                form.data = form.data.copy()
                form.data["preview_token"] = signing.dumps(payload, salt=COMPOSITION_PREVIEW_SALT, compress=True)
            elif action == "apply":
                try:
                    reviewed = signing.loads(
                        form.cleaned_data["preview_token"], salt=COMPOSITION_PREVIEW_SALT,
                        max_age=COMPOSITION_PREVIEW_MAX_AGE,
                    )
                    if reviewed != payload:
                        raise signing.BadSignature("Preview differs from submitted composition")
                except signing.BadSignature:
                    form.add_error(None, "Сначала просмотрите эти изменения. Подтверждение отсутствует, устарело или данные изменились.")
                    status = 400
                else:
                    try:
                        revision = series_revisions.revise_future_composition(
                            series, expected_revision_id=expected,
                            effective_from=form.cleaned_data["effective_from"],
                            participants=form.participant_inputs(), staff_assignments=staff_formset.staff_inputs(),
                            actor=request.user, reason=form.cleaned_data["reason"],
                        )
                    except series_revisions.SeriesRevisionMismatch as exc:
                        form.add_error(None, " ".join(exc.messages))
                        status = 409
                    except ValidationError as exc:
                        form.add_error(None, " ".join(exc.messages))
                    except PermissionDenied as exc:
                        form.add_error(None, str(exc))
                        status = 403
                    else:
                        messages.success(
                            request,
                            f"Редакция №{revision.revision_number} сохранена. "
                            "Уже созданные занятия не изменены; новые занятия автоматически не создавались.",
                        )
                        return redirect("appointment_series_detail", series_id=series.pk)
            else:
                form.add_error(None, "Выберите предварительный просмотр или сохранение редакции.")
                status = 400
    return render(request, "operations/appointment_series_composition.html", context, status=status)
