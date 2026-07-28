import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0049_grant_fixed_compensation_plan"),
    ]

    operations = [
        migrations.AddField(
            model_name="payrollaccrual",
            name="accrual_kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("appointment", "За занятие"),
                    ("grant_fixed", "Фиксированная грантовая оплата"),
                ],
                max_length=30,
                null=True,
                verbose_name="вид начисления",
            ),
        ),
        migrations.AddField(
            model_name="payrollaccrual",
            name="grant_fixed_compensation_revision",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payroll_accruals",
                to="operations.grantfixedcompensationrevision",
                verbose_name="редакция фиксированной грантовой оплаты",
            ),
        ),
        migrations.AddField(
            model_name="payrollaccrual",
            name="payroll_budget_revision",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payroll_accruals",
                to="operations.fundingpayrollbudgetrevision",
                verbose_name="проверенная редакция бюджета оплаты труда",
            ),
        ),
        migrations.AddField(
            model_name="payrollaccrual",
            name="period_from_snapshot",
            field=models.DateField(
                blank=True,
                null=True,
                verbose_name="период начисления с",
            ),
        ),
        migrations.AddField(
            model_name="payrollaccrual",
            name="period_to_snapshot",
            field=models.DateField(
                blank=True,
                null=True,
                verbose_name="период начисления по",
            ),
        ),
        migrations.AlterField(
            model_name="payrollaccrual",
            name="service",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payroll_accruals",
                to="operations.service",
                verbose_name="услуга",
            ),
        ),
        migrations.AlterField(
            model_name="payrollaccrual",
            name="starts_at_snapshot",
            field=models.DateTimeField(blank=True, null=True, verbose_name="начало"),
        ),
        migrations.AlterField(
            model_name="payrollaccrual",
            name="ends_at_snapshot",
            field=models.DateTimeField(blank=True, null=True, verbose_name="окончание"),
        ),
        migrations.AlterField(
            model_name="payrollaccrual",
            name="duration_minutes",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                verbose_name="длительность, мин",
            ),
        ),
        migrations.AlterField(
            model_name="payrollaccrual",
            name="rate_type_snapshot",
            field=models.CharField(
                blank=True,
                choices=[
                    ("per_session", "За занятие"),
                    ("hourly", "За час"),
                ],
                max_length=30,
                null=True,
                verbose_name="тип ставки",
            ),
        ),
        migrations.AlterField(
            model_name="payrollaccrual",
            name="rate_amount_snapshot",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=12,
                null=True,
                verbose_name="ставка",
            ),
        ),
        migrations.AlterField(
            model_name="payrollaccrual",
            name="session_scope_snapshot",
            field=models.CharField(
                blank=True,
                choices=[
                    ("all", "Все занятия"),
                    ("individual", "Индивидуальные"),
                    ("group", "Групповые"),
                ],
                max_length=30,
                null=True,
                verbose_name="формат правила",
            ),
        ),
        migrations.AlterField(
            model_name="payrollaccrual",
            name="group_pay_policy_snapshot",
            field=models.CharField(
                blank=True,
                choices=[
                    ("per_session", "Один раз за группу"),
                    ("per_charged_participant", "По списанным участникам"),
                    ("fixed_group_amount", "Фиксированная сумма за группу"),
                ],
                max_length=40,
                null=True,
                verbose_name="начисление в группе",
            ),
        ),
        migrations.AlterField(
            model_name="payrollaccrual",
            name="charged_participants_count_snapshot",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                verbose_name="списано участников",
            ),
        ),
        migrations.AlterField(
            model_name="payrollaccrual",
            name="pay_units_snapshot",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                verbose_name="единиц начисления",
            ),
        ),
        migrations.AlterField(
            model_name="payrollaccrual",
            name="work_date",
            field=models.DateField(verbose_name="дата начисления"),
        ),
        migrations.AddField(
            model_name="payrollsheetline",
            name="accrual_kind_snapshot",
            field=models.CharField(
                blank=True,
                choices=[
                    ("appointment", "За занятие"),
                    ("grant_fixed", "Фиксированная грантовая оплата"),
                ],
                max_length=30,
                null=True,
                verbose_name="вид начисления",
            ),
        ),
        migrations.AddField(
            model_name="payrollsheetline",
            name="funding_source",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payroll_sheet_lines",
                to="operations.fundingsource",
                verbose_name="источник финансирования",
            ),
        ),
        migrations.AddField(
            model_name="payrollsheetline",
            name="payroll_budget_revision",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payroll_sheet_lines",
                to="operations.fundingpayrollbudgetrevision",
                verbose_name="проверенная редакция бюджета оплаты труда",
            ),
        ),
        migrations.AddField(
            model_name="payrollsheetline",
            name="period_from_snapshot",
            field=models.DateField(
                blank=True,
                null=True,
                verbose_name="период начисления с",
            ),
        ),
        migrations.AddField(
            model_name="payrollsheetline",
            name="period_to_snapshot",
            field=models.DateField(
                blank=True,
                null=True,
                verbose_name="период начисления по",
            ),
        ),
        migrations.AddField(
            model_name="payrollsheetline",
            name="line_label",
            field=models.CharField(
                blank=True,
                max_length=200,
                null=True,
                verbose_name="название строки",
            ),
        ),
        migrations.AlterField(
            model_name="payrollsheetline",
            name="service",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payroll_sheet_lines",
                to="operations.service",
                verbose_name="услуга",
            ),
        ),
        migrations.AlterField(
            model_name="payrollsheetline",
            name="duration_minutes",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                verbose_name="длительность, мин",
            ),
        ),
        migrations.AlterField(
            model_name="payrollsheetline",
            name="work_date",
            field=models.DateField(verbose_name="дата начисления"),
        ),
        migrations.AddField(
            model_name="payrollsheetlifecycleevent",
            name="payroll_budget_revision",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payroll_sheet_lifecycle_events",
                to="operations.fundingpayrollbudgetrevision",
                verbose_name="проверенная редакция бюджета оплаты труда",
            ),
        ),
        migrations.AddField(
            model_name="payrollsheetlifecycleevent",
            name="budget_overage_amount",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=14,
                null=True,
                verbose_name="превышение бюджета",
            ),
        ),
        migrations.AddIndex(
            model_name="payrollaccrual",
            index=models.Index(
                fields=["accrual_kind", "work_date", "status"],
                name="payroll_acc_kind_date_status",
            ),
        ),
        migrations.AddIndex(
            model_name="payrollaccrual",
            index=models.Index(
                fields=["payroll_budget_revision", "status"],
                name="payroll_acc_budget_status",
            ),
        ),
        migrations.AddIndex(
            model_name="payrollsheetline",
            index=models.Index(
                fields=["payroll_budget_revision", "payroll_sheet"],
                name="payroll_line_budget_sheet",
            ),
        ),
    ]
