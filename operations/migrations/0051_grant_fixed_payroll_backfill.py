from django.db import migrations


BATCH_SIZE = 1000


def _batched_ids(queryset):
    last_pk = 0
    while True:
        ids = list(
            queryset.filter(pk__gt=last_pk)
            .order_by("pk")
            .values_list("pk", flat=True)[:BATCH_SIZE]
        )
        if not ids:
            return
        yield ids
        last_pk = ids[-1]


def backfill_appointment_payroll(apps, schema_editor):
    alias = schema_editor.connection.alias
    PayrollAccrual = apps.get_model("operations", "PayrollAccrual")
    PayrollSheetLine = apps.get_model("operations", "PayrollSheetLine")
    PayrollSheetLifecycleEvent = apps.get_model(
        "operations",
        "PayrollSheetLifecycleEvent",
    )

    accruals = PayrollAccrual.objects.using(alias).filter(accrual_kind__isnull=True)
    for ids in _batched_ids(accruals):
        PayrollAccrual.objects.using(alias).filter(pk__in=ids).update(
            accrual_kind="appointment",
        )

    lines = PayrollSheetLine.objects.using(alias).filter(
        accrual_kind_snapshot__isnull=True,
    )
    for ids in _batched_ids(lines):
        batch = list(
            PayrollSheetLine.objects.using(alias)
            .filter(pk__in=ids)
            .select_related("service", "payroll_accrual")
            .order_by("pk")
        )
        for line in batch:
            line.accrual_kind_snapshot = "appointment"
            line.funding_source_id = line.payroll_accrual.funding_source_id
            line.payroll_budget_revision_id = (
                line.payroll_accrual.payroll_budget_revision_id
            )
            line.period_from_snapshot = line.payroll_accrual.period_from_snapshot
            line.period_to_snapshot = line.payroll_accrual.period_to_snapshot
            line.line_label = line.service.name
        PayrollSheetLine.objects.using(alias).bulk_update(
            batch,
            [
                "accrual_kind_snapshot",
                "funding_source",
                "payroll_budget_revision",
                "period_from_snapshot",
                "period_to_snapshot",
                "line_label",
            ],
            batch_size=BATCH_SIZE,
        )

    events = PayrollSheetLifecycleEvent.objects.using(alias).filter(
        budget_overage_amount__isnull=True,
    )
    for ids in _batched_ids(events):
        PayrollSheetLifecycleEvent.objects.using(alias).filter(pk__in=ids).update(
            budget_overage_amount=0,
        )


def reverse_appointment_payroll_backfill(apps, schema_editor):
    alias = schema_editor.connection.alias
    PayrollAccrual = apps.get_model("operations", "PayrollAccrual")
    PayrollSheetLine = apps.get_model("operations", "PayrollSheetLine")
    PayrollSheetLifecycleEvent = apps.get_model(
        "operations",
        "PayrollSheetLifecycleEvent",
    )

    PayrollAccrual.objects.using(alias).filter(accrual_kind="appointment").update(
        accrual_kind=None,
        payroll_budget_revision=None,
        period_from_snapshot=None,
        period_to_snapshot=None,
    )
    PayrollSheetLine.objects.using(alias).filter(
        accrual_kind_snapshot="appointment",
    ).update(
        accrual_kind_snapshot=None,
        funding_source=None,
        payroll_budget_revision=None,
        period_from_snapshot=None,
        period_to_snapshot=None,
        line_label=None,
    )
    PayrollSheetLifecycleEvent.objects.using(alias).update(
        payroll_budget_revision=None,
        budget_overage_amount=None,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0050_grant_fixed_payroll_expand"),
    ]

    operations = [
        migrations.RunPython(
            backfill_appointment_payroll,
            reverse_appointment_payroll_backfill,
        ),
    ]
