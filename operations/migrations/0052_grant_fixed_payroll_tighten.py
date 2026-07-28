from django.db import migrations, models


def install_postgresql_payroll_event_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        CREATE FUNCTION operations_block_payroll_lifecycle_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'payroll lifecycle events are immutable'
                USING DETAIL = TG_TABLE_NAME || '.' || OLD.id::text;
        END;
        $$
        """
    )
    schema_editor.execute(
        """
        CREATE TRIGGER operations_payroll_lifecycle_event_immutable
        BEFORE UPDATE OR DELETE ON operations_payrollsheetlifecycleevent
        FOR EACH ROW
        EXECUTE FUNCTION operations_block_payroll_lifecycle_event_mutation()
        """
    )


def remove_postgresql_payroll_event_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        DROP TRIGGER IF EXISTS operations_payroll_lifecycle_event_immutable
        ON operations_payrollsheetlifecycleevent
        """
    )
    schema_editor.execute(
        "DROP FUNCTION IF EXISTS operations_block_payroll_lifecycle_event_mutation()"
    )


def assert_legacy_payroll_is_tightenable(apps, schema_editor):
    alias = schema_editor.connection.alias
    PayrollAccrual = apps.get_model("operations", "PayrollAccrual")
    PayrollSheetLine = apps.get_model("operations", "PayrollSheetLine")
    PayrollSheetLifecycleEvent = apps.get_model(
        "operations",
        "PayrollSheetLifecycleEvent",
    )

    invalid_accruals = (
        PayrollAccrual.objects.using(alias)
        .exclude(accrual_kind="appointment")
        .count()
    )
    invalid_accruals += (
        PayrollAccrual.objects.using(alias)
        .filter(
            models.Q(service__isnull=True)
            | models.Q(starts_at_snapshot__isnull=True)
            | models.Q(ends_at_snapshot__isnull=True)
            | models.Q(duration_minutes__isnull=True)
            | models.Q(rate_type_snapshot__isnull=True)
            | models.Q(rate_amount_snapshot__isnull=True)
            | models.Q(session_scope_snapshot__isnull=True)
            | models.Q(group_pay_policy_snapshot__isnull=True)
            | models.Q(charged_participants_count_snapshot__isnull=True)
            | models.Q(pay_units_snapshot__isnull=True)
            | models.Q(grant_fixed_compensation_revision__isnull=False)
        )
        .count()
    )
    if invalid_accruals:
        raise RuntimeError(
            "59A-2 strict preflight: legacy payroll accruals are incomplete "
            f"({invalid_accruals} invalid rows)."
        )

    invalid_lines = (
        PayrollSheetLine.objects.using(alias)
        .exclude(accrual_kind_snapshot="appointment")
        .count()
    )
    invalid_lines += (
        PayrollSheetLine.objects.using(alias)
        .filter(
            models.Q(service__isnull=True)
            | models.Q(duration_minutes__isnull=True)
            | models.Q(line_label__isnull=True)
            | models.Q(line_label="")
        )
        .count()
    )
    if invalid_lines:
        raise RuntimeError(
            "59A-2 strict preflight: legacy payroll sheet lines are incomplete "
            f"({invalid_lines} invalid rows)."
        )

    invalid_events = PayrollSheetLifecycleEvent.objects.using(alias).filter(
        budget_overage_amount__isnull=True,
    )
    if invalid_events.exists():
        raise RuntimeError(
            "59A-2 strict preflight: lifecycle events are missing budget snapshots."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0051_grant_fixed_payroll_backfill"),
    ]

    operations = [
        migrations.RunPython(
            assert_legacy_payroll_is_tightenable,
            migrations.RunPython.noop,
        ),
        migrations.AlterModelOptions(
            name="payrollsheetline",
            options={
                "ordering": ["work_date", "line_label", "pk"],
                "verbose_name": "строка расчетного листа",
                "verbose_name_plural": "строки расчетных листов",
            },
        ),
        migrations.AlterField(
            model_name="payrollaccrual",
            name="accrual_kind",
            field=models.CharField(
                choices=[
                    ("appointment", "За занятие"),
                    ("grant_fixed", "Фиксированная грантовая оплата"),
                ],
                default="appointment",
                max_length=30,
                verbose_name="вид начисления",
            ),
        ),
        migrations.AlterField(
            model_name="payrollsheetline",
            name="accrual_kind_snapshot",
            field=models.CharField(
                choices=[
                    ("appointment", "За занятие"),
                    ("grant_fixed", "Фиксированная грантовая оплата"),
                ],
                max_length=30,
                verbose_name="вид начисления",
            ),
        ),
        migrations.AlterField(
            model_name="payrollsheetline",
            name="line_label",
            field=models.CharField(
                max_length=200,
                verbose_name="название строки",
            ),
        ),
        migrations.AlterField(
            model_name="payrollsheetlifecycleevent",
            name="budget_overage_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=14,
                verbose_name="превышение бюджета",
            ),
        ),
        migrations.AlterField(
            model_name="payrollsheetlifecycleevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("approved", "Утвержден"),
                    ("sent", "Передан в выплату"),
                    ("paid", "Выплата зафиксирована"),
                ],
                max_length=30,
                verbose_name="тип события",
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollaccrual",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        accrual_kind="appointment",
                        grant_fixed_compensation_revision__isnull=True,
                        service__isnull=False,
                        starts_at_snapshot__isnull=False,
                        ends_at_snapshot__isnull=False,
                        duration_minutes__isnull=False,
                        rate_type_snapshot__isnull=False,
                        rate_amount_snapshot__isnull=False,
                        session_scope_snapshot__isnull=False,
                        group_pay_policy_snapshot__isnull=False,
                        charged_participants_count_snapshot__isnull=False,
                        pay_units_snapshot__isnull=False,
                    )
                    | models.Q(
                        accrual_kind="grant_fixed",
                        grant_fixed_compensation_revision__isnull=False,
                        staff_assignment__isnull=True,
                        appointment__isnull=True,
                        appointment_participant__isnull=True,
                        ledger_entry__isnull=True,
                        service__isnull=True,
                        funding_source__isnull=False,
                        pay_rule__isnull=True,
                        grant_allocation_revision__isnull=True,
                        period_from_snapshot__isnull=False,
                        period_to_snapshot__isnull=False,
                        starts_at_snapshot__isnull=True,
                        ends_at_snapshot__isnull=True,
                        duration_minutes__isnull=True,
                        rate_type_snapshot__isnull=True,
                        rate_amount_snapshot__isnull=True,
                        session_scope_snapshot__isnull=True,
                        group_pay_policy_snapshot__isnull=True,
                        charged_participants_count_snapshot__isnull=True,
                        pay_units_snapshot__isnull=True,
                    )
                ),
                name="payroll_accrual_kind_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollaccrual",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(accrual_kind="grant_fixed")
                    | models.Q(
                        period_to_snapshot__gte=models.F("period_from_snapshot")
                    )
                ),
                name="payroll_accrual_fixed_period_order",
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollaccrual",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(accrual_kind="grant_fixed")
                    | models.Q(amount__gt=0)
                ),
                name="payroll_accrual_fixed_amount_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollsheetline",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        accrual_kind_snapshot="appointment",
                        service__isnull=False,
                        duration_minutes__isnull=False,
                    )
                    | models.Q(
                        accrual_kind_snapshot="grant_fixed",
                        service__isnull=True,
                        funding_source__isnull=False,
                        period_from_snapshot__isnull=False,
                        period_to_snapshot__isnull=False,
                        duration_minutes__isnull=True,
                    )
                ),
                name="payroll_sheet_line_kind_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollsheetline",
            constraint=models.CheckConstraint(
                condition=~models.Q(line_label=""),
                name="payroll_sheet_line_label_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollsheetline",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(accrual_kind_snapshot="grant_fixed")
                    | models.Q(
                        period_to_snapshot__gte=models.F("period_from_snapshot")
                    )
                ),
                name="payroll_sheet_line_fixed_period_order",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="payrollsheetlifecycleevent",
            name="payroll_lifecycle_event_valid_transition",
        ),
        migrations.RemoveConstraint(
            model_name="payrollsheetlifecycleevent",
            name="payroll_sent_event_note_required",
        ),
        migrations.AddConstraint(
            model_name="payrollsheetlifecycleevent",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        event_type="approved",
                        status_from="draft",
                        status_to="approved",
                        actor_role_snapshot="director",
                    )
                    | models.Q(
                        event_type="sent",
                        status_from="approved",
                        status_to="sent",
                        actor_role_snapshot="director",
                    )
                    | models.Q(
                        event_type="paid",
                        status_from="sent",
                        status_to="paid",
                        actor_role_snapshot="director",
                    )
                ),
                name="payroll_lifecycle_event_valid_transition",
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollsheetlifecycleevent",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(event_type="sent")
                    | ~models.Q(note="")
                ),
                name="payroll_lifecycle_sent_note_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollsheetlifecycleevent",
            constraint=models.CheckConstraint(
                condition=models.Q(budget_overage_amount__gte=0),
                name="payroll_lifecycle_overage_non_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollsheetlifecycleevent",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(event_type="approved")
                    | models.Q(
                        payroll_budget_revision__isnull=True,
                        budget_overage_amount=0,
                    )
                ),
                name="payroll_lifecycle_budget_fields_event",
            ),
        ),
        migrations.AddConstraint(
            model_name="payrollsheetlifecycleevent",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(budget_overage_amount=0)
                    | (
                        models.Q(
                            event_type="approved",
                            payroll_budget_revision__isnull=False,
                        )
                        & ~models.Q(note="")
                    )
                ),
                name="payroll_lifecycle_overage_reason",
            ),
        ),
        migrations.RunPython(
            install_postgresql_payroll_event_guard,
            remove_postgresql_payroll_event_guard,
        ),
    ]
