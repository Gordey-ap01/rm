"""Operator navigation over current and historical series membership."""

from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.shortcuts import render
from django.views.decorators.http import require_GET

from operations.forms_series_registry import SeriesRegistryFilterForm
from operations.models import (
    AppointmentSeries,
    AppointmentSeriesParticipant,
    AppointmentSeriesRevisionParticipant,
)

from ._common import admin_required


def _filter_membership(series, *, recipient, program):
    if not recipient and not program:
        return series
    membership_filters = {}
    legacy_filters = {}
    if recipient:
        membership_filters["child_id"] = recipient.pk
        legacy_filters["child_id"] = recipient.pk
    if program:
        membership_filters["program_block__program_id"] = program.pk
        legacy_filters["program_block__program_id"] = program.pk

    # Exists keeps one row per series and applies both filters to the same
    # membership, including an earlier immutable composition revision.
    return series.alias(
        registry_default_match=Exists(
            AppointmentSeriesParticipant.objects.filter(
                series_id=OuterRef("pk"), **membership_filters
            )
        ),
        registry_revision_match=Exists(
            AppointmentSeriesRevisionParticipant.objects.filter(
                revision__series_id=OuterRef("pk"), **membership_filters
            )
        ),
    ).filter(
        Q(**legacy_filters)
        | Q(registry_default_match=True)
        | Q(registry_revision_match=True)
    )


def _registry_row(series):
    revision = series.current_revision
    if revision:
        memberships = revision.registry_participants
    else:
        memberships = series.registry_default_participants
    participants = [
        {"child": item.child, "program_block": item.program_block}
        for item in memberships
    ]
    if not revision and not participants:
        participants = [{"child": series.child, "program_block": series.program_block}]
    return {
        "series": series,
        "participants": participants,
        "composition_revision": revision,
    }


@admin_required
@require_GET
def appointment_series_list(request):
    form = SeriesRegistryFilterForm(request.GET)
    filters_valid = form.is_valid()
    filters = form.cleaned_data if filters_valid else {}
    series = AppointmentSeries.objects.all()
    if filters_valid:
        series = _filter_membership(
            series, recipient=filters.get("recipient"), program=filters.get("program")
        )
        if filters.get("status"):
            series = series.filter(status=filters["status"])
        if filters.get("date_from"):
            series = series.filter(end_date__gte=filters["date_from"])
        if filters.get("date_to"):
            series = series.filter(start_date__lte=filters["date_to"])
    else:
        series = series.none()

    series = series.select_related(
        "service", "child", "program_block__program", "current_revision"
    ).prefetch_related(
        Prefetch(
            "default_participants",
            queryset=AppointmentSeriesParticipant.objects.select_related(
                "child", "program_block__program"
            ).order_by("position", "pk"),
            to_attr="registry_default_participants",
        ),
        Prefetch(
            "current_revision__participants",
            queryset=AppointmentSeriesRevisionParticipant.objects.select_related(
                "child", "program_block__program"
            ).order_by("position", "pk"),
            to_attr="registry_participants",
        ),
    ).order_by("-start_date", "-pk")
    page = Paginator(series, 25).get_page(request.GET.get("page"))
    query = request.GET.copy()
    for key in list(query):
        if key not in form.fields:
            query.pop(key)
    return render(
        request,
        "operations/appointment_series_list.html",
        {
            "filter_form": form,
            "filters_valid": filters_valid,
            "page_obj": page,
            "registry_rows": [_registry_row(item) for item in page],
            "pagination_query": query.urlencode(),
            "selected_recipient": filters.get("recipient"),
            "selected_program": filters.get("program"),
        },
    )
