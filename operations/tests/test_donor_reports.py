"""Immutable donor-report snapshot contracts."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from queue import Queue
from threading import Barrier, Event, Thread
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import (
    DatabaseError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentParticipant,
    BalanceAccount,
    CenterExpense,
    CenterExpenseCategory,
    Child,
    Counterparty,
    DonorReport,
    DonorReportSnapshot,
    ExpenseFundingSplit,
    FundingPayrollBudget,
    FundingSource,
    GrantFixedCompensation,
    GrantRecipientAllocation,
    LedgerEntry,
    Service,
    StaffMember,
)
from operations.services import (
    donor_reports as donor_reports_svc,
    grant_compensation as grant_compensation_svc,
    payroll as payroll_svc,
)
from operations.views import reports as donor_report_views


class DonorReportSnapshotTests(TransactionTestCase):
    def setUp(self):
        self.director = User.objects.create_superuser(
            "donor-report-director",
            password="x",
        )
        self.admin = User.objects.create_user(
            "donor-report-admin",
            password="x",
            is_staff=True,
        )
        self.funding = FundingSource.objects.create(
            name="Грант безопасного снимка",
            source_type=FundingSource.SourceType.GRANT,
        )
        self.counterparty = Counterparty.objects.create(
            name="Фонд добрых программ",
            counterparty_type=Counterparty.CounterpartyType.FOUNDATION,
        )
        self.period_from = date(2026, 1, 1)
        self.period_to = date(2026, 3, 31)

    def _close(
        self,
        *,
        counterparty: Counterparty | None = None,
        expected_snapshot_id: int | None = None,
        reason: str = "Закрыта квартальная сверка проекта.",
    ) -> DonorReportSnapshot:
        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=counterparty,
            date_from=self.period_from,
            date_to=self.period_to,
            expected_snapshot_id=expected_snapshot_id,
        )
        return donor_reports_svc.close_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=counterparty,
            date_from=self.period_from,
            date_to=self.period_to,
            actor=self.director,
            reason=reason,
            expected_review_token=review.review_token,
            expected_snapshot_id=expected_snapshot_id,
        )

    def test_director_closes_first_snapshot_with_canonical_hashes(self):
        snapshot = self._close(counterparty=self.counterparty)

        snapshot.report.refresh_from_db()
        self.assertEqual(snapshot.snapshot_number, 1)
        self.assertEqual(snapshot.event_type, DonorReportSnapshot.EventType.CLOSED)
        self.assertEqual(snapshot.report.current_snapshot, snapshot)
        self.assertEqual(
            snapshot.snapshot_schema_version,
            donor_reports_svc.SNAPSHOT_SCHEMA_VERSION,
        )
        self.assertEqual(
            snapshot.canonicalizer_version,
            donor_reports_svc.CANONICALIZER_VERSION,
        )
        self.assertEqual(
            snapshot.payload_sha256,
            donor_reports_svc.canonical_sha256(snapshot.payload),
        )
        self.assertEqual(
            snapshot.evidence_manifest_sha256,
            donor_reports_svc.canonical_sha256(snapshot.evidence_manifest),
        )

    def test_private_donor_identity_and_forbidden_keys_do_not_enter_payload(self):
        secret_name = "Секретное ФИО донора"
        recipient = Child.objects.create(
            last_name="Секретная",
            first_name="Получательница",
            phone="+79991112233",
            email="recipient@example.test",
            diagnosis="Чувствительные медицинские сведения",
            notes="Скрытый комментарий",
        )
        service = Service.objects.create(
            name="Безопасная услуга",
            code="DONOR-PRIVACY",
        )
        account = BalanceAccount.objects.create(
            child=recipient,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=service,
            initial_amount=Decimal("10"),
            notes="Номер счета SECRET-ACCOUNT-42",
        )
        GrantRecipientAllocation.objects.create(
            funding_source=self.funding,
            child=recipient,
            service=service,
            allocated_sessions=10,
            balance_account=account,
            valid_from=self.period_from,
            valid_until=self.period_to,
            note="Скрытое основание выделения",
        )
        private_donor = Counterparty.objects.create(
            name=secret_name,
            counterparty_type=Counterparty.CounterpartyType.INDIVIDUAL,
            phone="+79990000000",
            email="secret@example.test",
            bank_details="Счет 40702810900000000000",
            notes="Не публиковать",
        )

        snapshot = self._close(counterparty=private_donor)
        payload_text = donor_reports_svc.canonical_json_bytes(snapshot.payload).decode()

        self.assertNotIn(secret_name, payload_text)
        self.assertNotIn("+79990000000", payload_text)
        self.assertNotIn("secret@example.test", payload_text)
        self.assertNotIn("40702810900000000000", payload_text)
        self.assertNotIn(recipient.full_name, payload_text)
        self.assertNotIn("+79991112233", payload_text)
        self.assertNotIn("recipient@example.test", payload_text)
        self.assertNotIn("Чувствительные медицинские сведения", payload_text)
        self.assertNotIn("SECRET-ACCOUNT-42", payload_text)
        self.assertNotIn(self.funding.name, payload_text)
        self.assertNotIn(service.name, payload_text)
        self.assertEqual(snapshot.payload["report"]["donor"]["ref"], "DON-001")
        self.assertEqual(snapshot.payload["report"]["donor"]["type"], "individual")
        self.assertNotIn("data_as_of", snapshot.payload)
        self.assertEqual(
            snapshot.payload["recipient_allocations"][0]["recipient_ref"],
            "RCP-001",
        )
        recipient_alias = next(
            row
            for row in snapshot.evidence_manifest["aliases"]
            if row["kind"] == "recipient"
        )
        self.assertEqual(recipient_alias["source_pk"], recipient.pk)
        self.assertEqual(
            donor_reports_svc._payload_forbidden_key_paths(snapshot.payload),
            [],
        )

    def test_correction_preserves_history_and_requires_changed_payload(self):
        first = self._close(counterparty=self.counterparty)

        with self.assertRaisesMessage(ValidationError, "Данные отчета не изменились"):
            self._close(
                counterparty=self.counterparty,
                expected_snapshot_id=first.pk,
                reason="Повторное закрытие без изменений.",
            )

        self.funding.source_type = FundingSource.SourceType.SPONSOR
        self.funding.save(update_fields=["source_type", "updated_at"])
        second = self._close(
            counterparty=self.counterparty,
            expected_snapshot_id=first.pk,
            reason="Исправлено название источника.",
        )

        first.refresh_from_db()
        second.report.refresh_from_db()
        self.assertEqual(second.snapshot_number, 2)
        self.assertEqual(second.event_type, DonorReportSnapshot.EventType.CORRECTED)
        self.assertEqual(second.supersedes, first)
        self.assertEqual(second.report.current_snapshot, second)
        self.assertNotEqual(first.payload_sha256, second.payload_sha256)
        self.assertEqual(DonorReportSnapshot.objects.count(), 2)

    def test_stale_expected_pointer_is_rejected(self):
        first = self._close(counterparty=self.counterparty)
        self.funding.source_type = FundingSource.SourceType.SPONSOR
        self.funding.save(update_fields=["source_type", "updated_at"])
        second = self._close(
            counterparty=self.counterparty,
            expected_snapshot_id=first.pk,
            reason="Создана исправляющая версия.",
        )
        self.funding.source_type = FundingSource.SourceType.CHARITY_FUND
        self.funding.save(update_fields=["source_type", "updated_at"])

        with self.assertRaisesMessage(ValidationError, "Цепочка отчета изменилась"):
            self._close(
                counterparty=self.counterparty,
                expected_snapshot_id=first.pk,
                reason="Устаревшая попытка исправления.",
            )

        second.report.refresh_from_db()
        self.assertEqual(second.report.current_snapshot, second)

    def test_administrator_cannot_close_but_can_read_and_export(self):
        with self.assertRaises(PermissionDenied):
            donor_reports_svc.close_donor_report_snapshot(
                funding_source_id=self.funding.pk,
                counterparty=self.counterparty,
                date_from=self.period_from,
                date_to=self.period_to,
                actor=self.admin,
                reason="Попытка администратора закрыть отчет.",
                expected_review_token="a" * 64,
            )

        self.client.force_login(self.admin)
        denied = self.client.post(
            reverse("donor_report_snapshot_close"),
            {
                "funding_source": self.funding.pk,
                "counterparty": self.counterparty.pk,
                "date_from": self.period_from.isoformat(),
                "date_to": self.period_to.isoformat(),
                "reason": "Попытка администратора закрыть отчет.",
            },
        )
        self.assertEqual(denied.status_code, 403)

        snapshot = self._close(counterparty=self.counterparty)
        detail = self.client.get(
            reverse("donor_report_snapshot_detail", args=[snapshot.pk])
        )
        export = self.client.get(
            reverse("donor_report_snapshot_json", args=[snapshot.pk])
        )

        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Снимок неизменяем")
        self.assertNotContains(detail, "evidence_manifest")
        self.assertEqual(detail["Cache-Control"], "private, no-store")
        self.assertEqual(
            detail["X-Data-Classification"],
            "internal-confidential",
        )
        self.assertContains(detail, "Построить preview исправления")
        self.assertEqual(export.status_code, 200)
        self.assertEqual(export["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(
            export["Content-Disposition"],
            (
                f'attachment; filename="donor_report_{snapshot.report_id}'
                f'_v{snapshot.snapshot_number}.json"'
            ),
        )
        self.assertEqual(export["X-Content-Type-Options"], "nosniff")
        self.assertEqual(export["Cache-Control"], "private, no-store")
        self.assertEqual(
            export["X-Data-Classification"],
            "internal-pseudonymized-report",
        )
        self.assertEqual(
            export.content,
            donor_reports_svc.canonical_json_bytes(snapshot.payload),
        )
        self.assertNotContains(export, "evidence_manifest")

    def test_snapshot_integrity_status_covers_payload_and_evidence_manifest(self):
        snapshot = self._close(counterparty=self.counterparty)
        self.assertTrue(
            donor_report_views._donor_report_snapshot_hashes_valid(snapshot)
        )

        snapshot.evidence_manifest = deepcopy(snapshot.evidence_manifest)
        snapshot.evidence_manifest["unexpected"] = "tampered"
        self.assertFalse(
            donor_report_views._donor_report_snapshot_hashes_valid(snapshot)
        )

        snapshot.refresh_from_db()
        snapshot.payload = deepcopy(snapshot.payload)
        snapshot.payload["report"]["funding_source"]["type"] = "tampered"
        self.assertFalse(
            donor_report_views._donor_report_snapshot_hashes_valid(snapshot)
        )

    def test_snapshot_export_fails_closed_when_integrity_check_fails(self):
        snapshot = self._close(counterparty=self.counterparty)
        self.client.force_login(self.admin)

        with patch.object(
            donor_report_views,
            "_donor_report_snapshot_hashes_valid",
            return_value=False,
        ):
            detail = self.client.get(
                reverse("donor_report_snapshot_detail", args=[snapshot.pk])
            )
            export = self.client.get(
                reverse("donor_report_snapshot_json", args=[snapshot.pk])
            )

        self.assertContains(detail, "Нарушена")
        self.assertEqual(export.status_code, 409)
        self.assertEqual(export["Cache-Control"], "private, no-store")
        self.assertEqual(export["X-Content-Type-Options"], "nosniff")
        self.assertEqual(
            export["X-Data-Classification"],
            "internal-confidential",
        )
        self.assertNotIn("Content-Disposition", export)

    def test_director_closes_from_grant_report_ui(self):
        self.client.force_login(self.director)
        review_response = self.client.post(
            reverse("donor_report_snapshot_review"),
            {
                "funding_source": self.funding.pk,
                "counterparty": self.counterparty.pk,
                "date_from": self.period_from.isoformat(),
                "date_to": self.period_to.isoformat(),
            },
        )
        self.assertEqual(review_response.status_code, 200)
        self.assertContains(review_response, "Preflight пройден")
        self.assertFalse(DonorReportSnapshot.objects.exists())
        review = review_response.context["review"]
        response = self.client.post(
            reverse("donor_report_snapshot_close"),
            {
                "funding_source": self.funding.pk,
                "counterparty": self.counterparty.pk,
                "date_from": self.period_from.isoformat(),
                "date_to": self.period_to.isoformat(),
                "expected_review_token": review.review_token,
                "reason": "Закрытие через интерфейс руководителя.",
            },
        )

        snapshot = DonorReportSnapshot.objects.get()
        self.assertRedirects(
            response,
            reverse("donor_report_snapshot_detail", args=[snapshot.pk]),
        )
        detail = self.client.get(response.url)
        self.assertContains(detail, "DON-001")
        self.assertContains(detail, "Проверить исправляющую версию")

    def test_snapshot_and_root_reject_direct_mutation_or_delete(self):
        snapshot = self._close(counterparty=self.counterparty)
        snapshot.reason = "Перезапись"
        with self.assertRaisesMessage(ValidationError, "неизменяем"):
            snapshot.save()
        with self.assertRaisesMessage(ValidationError, "нельзя удалить"):
            snapshot.delete()

        report = snapshot.report
        report.date_to = date(2026, 4, 1)
        with self.assertRaisesMessage(ValidationError, "нельзя изменять напрямую"):
            report.save()
        with self.assertRaisesMessage(ValidationError, "нельзя удалить"):
            report.delete()

    def test_canonical_json_is_stable(self):
        left = {"b": ["2", "1"], "a": {"amount": Decimal("10.00")}}
        right = {"a": {"amount": Decimal("10.00")}, "b": ["2", "1"]}

        self.assertEqual(
            donor_reports_svc.canonical_json_bytes(left),
            donor_reports_svc.canonical_json_bytes(right),
        )
        self.assertIn(b'"amount":"10.00"', donor_reports_svc.canonical_json_bytes(left))
        self.assertEqual(
            donor_reports_svc.canonical_json_bytes({"text": "e\u0301"}),
            donor_reports_svc.canonical_json_bytes({"text": "é"}),
        )
        with self.assertRaisesMessage(TypeError, "colliding Unicode-normalized keys"):
            donor_reports_svc.canonical_json_bytes({"e\u0301": 1, "é": 2})

    def test_payload_uses_exact_allowlist(self):
        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=self.counterparty,
            date_from=self.period_from,
            date_to=self.period_to,
        )

        self.assertEqual(
            set(review.payload),
            {
                "schema_version",
                "report",
                "balances",
                "quotas",
                "recipient_allocations",
                "payroll",
                "expenses",
                "integrity",
            },
        )
        self.assertEqual(
            set(review.payload["report"]),
            {"report_kind", "period", "funding_source", "donor"},
        )
        self.assertNotIn(self.funding.name, str(review.payload))
        self.assertNotIn(self.counterparty.name, str(review.payload))

        invalid = dict(review.payload)
        invalid["unsafe_name"] = "ФИО"
        with self.assertRaisesMessage(ValidationError, "нарушен allowlist"):
            donor_reports_svc._validate_payload_allowlist(invalid)

    def test_payload_value_allowlist_rejects_free_text_refs_and_enums(self):
        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=self.counterparty,
            date_from=self.period_from,
            date_to=self.period_to,
        )
        invalid_ref = deepcopy(review.payload)
        invalid_ref["report"]["funding_source"]["ref"] = "Секретное ФИО"
        with self.assertRaisesMessage(ValidationError, "локальный псевдоним"):
            donor_reports_svc._validate_payload_values(invalid_ref)

        invalid_enum = deepcopy(review.payload)
        invalid_enum["report"]["funding_source"]["type"] = "secret free text"
        with self.assertRaisesMessage(ValidationError, "разрешенный справочник"):
            donor_reports_svc._validate_payload_values(invalid_enum)

    def test_snapshot_validators_reject_wrong_array_types_and_unstable_order(self):
        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=self.counterparty,
            date_from=self.period_from,
            date_to=self.period_to,
        )
        invalid_payload = deepcopy(review.payload)
        invalid_payload["balances"] = 1
        with self.assertRaisesMessage(ValidationError, "ожидается JSON-массив"):
            donor_reports_svc._validate_payload_values(invalid_payload)

        invalid_evidence = deepcopy(review.evidence_manifest)
        invalid_evidence["sources"].reverse()
        with self.assertRaisesMessage(ValidationError, "каноническом порядке"):
            donor_reports_svc._validate_evidence_manifest(invalid_evidence)

    def test_payload_validator_enforces_period_and_fixed_scope_semantics(self):
        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=self.counterparty,
            date_from=self.period_from,
            date_to=self.period_to,
        )
        invalid_period = deepcopy(review.payload)
        invalid_period["report"]["period"] = {
            "from": "2026-03-01",
            "to": "2026-02-01",
        }
        with self.assertRaisesMessage(ValidationError, "раньше даты начала"):
            donor_reports_svc._validate_payload_values(invalid_period)

        fixed_row = {
            "fixed_ref": "FIX-001",
            "budget_ref": "BUD-001",
            "staff_ref": "SPC-001",
            "compensation_scope": GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
            "service": {
                "ref": "SVC-001",
                "category": Service.Category.SPEECH,
            },
            "period": {
                "from": "2026-02-01",
                "to": "2026-02-28",
            },
            "accrual_on": "2026-02-28",
            "amount": "100.00",
            "lifecycle_status": GrantFixedCompensation.LifecycleStatus.ACTIVE,
        }
        invalid_project_role = deepcopy(review.payload)
        invalid_project_role["payroll"]["fixed_positions"] = [fixed_row]
        with self.assertRaisesMessage(ValidationError, "услуга должна отсутствовать"):
            donor_reports_svc._validate_payload_values(invalid_project_role)

        invalid_service_delivery = deepcopy(invalid_project_role)
        invalid_service_delivery["payroll"]["fixed_positions"][0][
            "compensation_scope"
        ] = GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
        invalid_service_delivery["payroll"]["fixed_positions"][0]["service"] = None
        with self.assertRaisesMessage(ValidationError, "услуга обязательна"):
            donor_reports_svc._validate_payload_values(invalid_service_delivery)

    def test_model_rejects_matching_hash_payload_or_evidence_with_free_text(self):
        snapshot = self._close(counterparty=self.counterparty)
        payload = deepcopy(snapshot.payload)
        payload["report"]["funding_source"]["ref"] = "ФИО в псевдониме"
        snapshot.payload = payload
        snapshot.payload_sha256 = donor_reports_svc.canonical_sha256(payload)
        evidence = deepcopy(snapshot.evidence_manifest)
        evidence["payload_sha256"] = snapshot.payload_sha256
        snapshot.evidence_manifest = evidence
        snapshot.evidence_manifest_sha256 = donor_reports_svc.canonical_sha256(
            evidence
        )
        with self.assertRaisesMessage(ValidationError, "локальный псевдоним"):
            snapshot.full_clean()

        snapshot.refresh_from_db()
        evidence = deepcopy(snapshot.evidence_manifest)
        evidence["sources"][0]["notes"] = "Секретные медицинские сведения"
        snapshot.evidence_manifest = evidence
        snapshot.evidence_manifest_sha256 = donor_reports_svc.canonical_sha256(
            evidence
        )
        with self.assertRaisesMessage(ValidationError, "exact allowlist"):
            snapshot.full_clean()

    def test_multisource_expense_uses_full_split_total_for_preflight(self):
        second_funding = FundingSource.objects.create(
            name="Второй источник",
            source_type=FundingSource.SourceType.PERSONAL,
        )
        category = CenterExpenseCategory.objects.create(
            name="Секретное название категории",
            expense_type=CenterExpenseCategory.ExpenseType.EQUIPMENT,
        )
        expense = CenterExpense.objects.create(
            expense_date=date(2026, 2, 15),
            category=category,
            title="Секретное название расхода",
            description="Не должно попасть в отчет",
            total_amount=Decimal("100.00"),
            status=CenterExpense.Status.APPROVED,
            approved_by=self.director,
        )
        ExpenseFundingSplit.objects.create(
            expense=expense,
            funding_source=self.funding,
            amount=Decimal("40.00"),
        )
        second_split = ExpenseFundingSplit.objects.create(
            expense=expense,
            funding_source=second_funding,
            amount=Decimal("60.00"),
        )

        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=self.period_from,
            date_to=self.period_to,
        )

        self.assertEqual(review.payload["expenses"][0]["amount"], "40.00")
        self.assertNotIn(category.name, str(review.payload))
        self.assertNotIn(expense.title, str(review.payload))
        expense_source_kinds = {
            row["kind"] for row in review.evidence_manifest["sources"]
        }
        self.assertTrue(
            {"center_expense", "expense_category", "expense_split"}.issubset(
                expense_source_kinds
            )
        )
        original_payload_hash = review.payload_sha256
        original_evidence_hash = review.evidence_manifest_sha256
        category.name = "Переименованная скрытая категория"
        category.save(update_fields=["name", "updated_at"])
        renamed_review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=self.period_from,
            date_to=self.period_to,
        )
        self.assertEqual(renamed_review.payload_sha256, original_payload_hash)
        self.assertEqual(
            renamed_review.evidence_manifest_sha256,
            original_evidence_hash,
        )
        second_split.amount = Decimal("50.00")
        second_split.save(update_fields=["amount", "updated_at"])
        with self.assertRaisesMessage(
            ValidationError,
            "grant_expense_funding_unbalanced",
        ):
            donor_reports_svc.review_donor_report_snapshot(
                funding_source_id=self.funding.pk,
                counterparty=None,
                date_from=self.period_from,
                date_to=self.period_to,
            )

    def test_fixed_project_role_is_complete_and_privacy_safe(self):
        secret_staff_name = "Секретное ФИО координатора"
        secret_assignment = "Секретное название проектной роли"
        staff = StaffMember.objects.create(full_name=secret_staff_name)
        budget = grant_compensation_svc.create_payroll_budget(
            funding_source=self.funding,
            starts_on=self.period_from,
            ends_on=self.period_to,
            planned_amount=Decimal("10000.00"),
            enforcement_mode=FundingPayrollBudget.EnforcementMode.HARD,
            note="Скрытое примечание бюджета",
            actor=self.director,
            reason="Утвержден бюджет безопасного отчета.",
        )
        fixed = grant_compensation_svc.create_fixed_compensation(
            payroll_budget=budget,
            staff_member=staff,
            compensation_scope=GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
            service=None,
            assignment_label=secret_assignment,
            period_from=self.period_from,
            period_to=self.period_to,
            accrual_on=self.period_to,
            amount=Decimal("5000.00"),
            note="Скрытое примечание позиции",
            actor=self.director,
            reason="Утверждена фиксированная проектная роль.",
        )

        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=self.period_from,
            date_to=self.period_to,
        )

        fixed_row = review.payload["payroll"]["fixed_positions"][0]
        self.assertEqual(fixed_row["fixed_ref"], "FIX-001")
        self.assertEqual(fixed_row["budget_ref"], "BUD-001")
        self.assertEqual(fixed_row["staff_ref"], "SPC-001")
        self.assertIsNone(fixed_row["service"])
        self.assertEqual(fixed_row["amount"], "5000.00")
        payload_text = donor_reports_svc.canonical_json_bytes(review.payload).decode()
        self.assertNotIn(secret_staff_name, payload_text)
        self.assertNotIn(secret_assignment, payload_text)
        self.assertNotIn("Скрытое примечание", payload_text)
        preflight = {
            row["code"]: row["result"]
            for row in review.evidence_manifest["preflight"]
        }
        self.assertEqual(preflight["grant_compensation_integrity"], "passed")
        self.assertEqual(preflight["grant_hard_payroll_budget_exceeded"], "passed")
        self.assertEqual(
            review.evidence_manifest["source_sets"]["payroll_line_ids"],
            [],
        )
        fixed_source = next(
            row
            for row in review.evidence_manifest["sources"]
            if row["kind"] == "fixed_position"
        )
        self.assertEqual(fixed_source["source_pk"], fixed.pk)
        self.assertEqual(fixed_source["revision_pk"], fixed.current_revision_id)

    def test_budget_usage_evidence_includes_lines_outside_report_period(self):
        staff = StaffMember.objects.create(full_name="Сотрудник годового бюджета")
        budget = grant_compensation_svc.create_payroll_budget(
            funding_source=self.funding,
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
            planned_amount=Decimal("10000.00"),
            enforcement_mode=FundingPayrollBudget.EnforcementMode.HARD,
            note="Годовой бюджет",
            actor=self.director,
            reason="Утвержден годовой бюджет.",
        )
        grant_compensation_svc.create_fixed_compensation(
            payroll_budget=budget,
            staff_member=staff,
            compensation_scope=GrantFixedCompensation.CompensationScope.PROJECT_ROLE,
            service=None,
            assignment_label="Координатор апреля",
            period_from=date(2026, 4, 1),
            period_to=date(2026, 4, 30),
            accrual_on=date(2026, 4, 30),
            amount=Decimal("5000.00"),
            note="",
            actor=self.director,
            reason="Утверждена апрельская проектная роль.",
        )
        sheet = payroll_svc.create_payroll_sheet_for_staff(
            staff,
            date_from=date(2026, 4, 30),
            date_to=date(2026, 4, 30),
            actor=self.director,
        )
        payroll_svc.approve_payroll_sheet(sheet, actor=self.director)
        line = sheet.lines.get()

        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=self.period_from,
            date_to=self.period_to,
        )

        budget_row = review.payload["payroll"]["budgets"][0]
        self.assertEqual(budget_row["consumed_amount"], "5000.00")
        self.assertEqual(review.payload["payroll"]["accrual_totals"], [])
        source_sets = review.evidence_manifest["source_sets"]
        self.assertEqual(source_sets["payroll_line_ids"], [line.pk])
        self.assertEqual(source_sets["payroll_sheet_ids"], [sheet.pk])
        source_kinds = {
            row["kind"] for row in review.evidence_manifest["sources"]
        }
        self.assertIn("payroll_sheet_line", source_kinds)
        self.assertIn("payroll_sheet", source_kinds)

    def test_recipient_aliases_are_stable_and_removed_aliases_stay_reserved(self):
        service = Service.objects.create(name="Услуга псевдонимов", code="DONOR-ALIAS")
        first_child = Child.objects.create(last_name="Первый", first_name="Получатель")
        first_account = BalanceAccount.objects.create(
            child=first_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=service,
            initial_amount=Decimal("4"),
        )
        first_allocation = GrantRecipientAllocation.objects.create(
            funding_source=self.funding,
            child=first_child,
            service=service,
            allocated_sessions=4,
            balance_account=first_account,
            valid_from=self.period_from,
            valid_until=self.period_to,
        )
        first = self._close(counterparty=self.counterparty)
        self.assertEqual(
            first.payload["recipient_allocations"][0]["recipient_ref"],
            "RCP-001",
        )

        first_allocation.delete()
        second_child = Child.objects.create(last_name="Второй", first_name="Получатель")
        second_account = BalanceAccount.objects.create(
            child=second_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=service,
            initial_amount=Decimal("5"),
        )
        GrantRecipientAllocation.objects.create(
            funding_source=self.funding,
            child=second_child,
            service=service,
            allocated_sessions=5,
            balance_account=second_account,
            valid_from=self.period_from,
            valid_until=self.period_to,
        )
        second = self._close(
            counterparty=self.counterparty,
            expected_snapshot_id=first.pk,
            reason="Получатель проекта заменен с сохранением истории псевдонимов.",
        )

        self.assertEqual(
            second.payload["recipient_allocations"][0]["recipient_ref"],
            "RCP-002",
        )
        recipient_aliases = {
            row["ref"]: row["active"]
            for row in second.evidence_manifest["aliases"]
            if row["kind"] == "recipient"
        }
        self.assertEqual(
            recipient_aliases,
            {"RCP-001": False, "RCP-002": True},
        )

    def test_scoped_financial_integrity_blocks_charge_without_account(self):
        child = Child.objects.create(last_name="Контроль", first_name="Финансовый")
        service = Service.objects.create(name="Контрольная услуга", code="DONOR-FIN")
        account = BalanceAccount.objects.create(
            child=child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=service,
            initial_amount=Decimal("3"),
        )
        GrantRecipientAllocation.objects.create(
            funding_source=self.funding,
            child=child,
            service=service,
            allocated_sessions=3,
            balance_account=account,
            valid_from=self.period_from,
            valid_until=self.period_to,
        )
        staff = StaffMember.objects.create(full_name="Специалист финансового контроля")
        starts_at = timezone.make_aware(datetime(2026, 2, 10, 10, 0))
        appointment = Appointment(
            child=child,
            staff_member=staff,
            service=service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            status=Appointment.Status.COMPLETED,
        )
        appointment.save(validate_schedule=False)
        appointment.participants.update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=None,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "participant_charge_without_account",
        ):
            donor_reports_svc.review_donor_report_snapshot(
                funding_source_id=self.funding.pk,
                counterparty=None,
                date_from=self.period_from,
                date_to=self.period_to,
            )

    def test_scoped_financial_integrity_uses_allocation_date_at_appointment(self):
        child = Child.objects.create(last_name="Вне", first_name="Периода")
        service = Service.objects.create(
            name="Услуга с поздним выделением",
            code="DONOR-FIN-DATE",
        )
        account = BalanceAccount.objects.create(
            child=child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=service,
            initial_amount=Decimal("3"),
        )
        GrantRecipientAllocation.objects.create(
            funding_source=self.funding,
            child=child,
            service=service,
            allocated_sessions=3,
            balance_account=account,
            valid_from=date(2026, 3, 1),
            valid_until=self.period_to,
        )
        staff = StaffMember.objects.create(full_name="Специалист до выделения")
        starts_at = timezone.make_aware(datetime(2026, 2, 10, 10, 0))
        appointment = Appointment(
            child=child,
            staff_member=staff,
            service=service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            status=Appointment.Status.COMPLETED,
        )
        appointment.save(validate_schedule=False)
        appointment.participants.update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=None,
        )

        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=self.period_from,
            date_to=self.period_to,
        )

        preflight = {
            row["code"]: row["result"]
            for row in review.evidence_manifest["preflight"]
        }
        self.assertEqual(preflight["financial_integrity"], "passed")

    def test_group_secondary_grant_participant_is_in_financial_preflight(self):
        primary_child = Child.objects.create(
            last_name="Личный",
            first_name="Участник",
        )
        grant_child = Child.objects.create(
            last_name="Грантовый",
            first_name="Участник",
        )
        service = Service.objects.create(
            name="Групповая грантовая услуга",
            code="DONOR-GROUP-GRANT",
        )
        account = BalanceAccount.objects.create(
            child=grant_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=service,
            initial_amount=Decimal("4"),
        )
        GrantRecipientAllocation.objects.create(
            funding_source=self.funding,
            child=grant_child,
            service=service,
            allocated_sessions=4,
            balance_account=account,
            valid_from=self.period_from,
            valid_until=self.period_to,
        )
        staff = StaffMember.objects.create(full_name="Ведущий группы")
        starts_at = timezone.make_aware(datetime(2026, 2, 12, 10, 0))
        appointment = Appointment(
            child=primary_child,
            staff_member=staff,
            service=service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
            session_type=Appointment.SessionType.GROUP,
            status=Appointment.Status.COMPLETED,
        )
        appointment.save(validate_schedule=False)
        secondary = AppointmentParticipant.objects.create(
            appointment=appointment,
            child=grant_child,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        AppointmentParticipant.objects.filter(pk=secondary.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=None,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "participant_charge_without_account",
        ):
            donor_reports_svc.review_donor_report_snapshot(
                funding_source_id=self.funding.pk,
                counterparty=None,
                date_from=self.period_from,
                date_to=self.period_to,
            )

    def test_non_grant_group_participant_does_not_block_grant_preflight(self):
        grant_child = Child.objects.create(
            last_name="Грантовый",
            first_name="Основной",
        )
        other_child = Child.objects.create(
            last_name="Личный",
            first_name="Дополнительный",
        )
        service = Service.objects.create(
            name="Смешанная групповая услуга",
            code="DONOR-GROUP-MIXED",
        )
        account = BalanceAccount.objects.create(
            child=grant_child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=service,
            initial_amount=Decimal("4"),
        )
        GrantRecipientAllocation.objects.create(
            funding_source=self.funding,
            child=grant_child,
            service=service,
            allocated_sessions=4,
            balance_account=account,
            valid_from=self.period_from,
            valid_until=self.period_to,
        )
        staff = StaffMember.objects.create(full_name="Ведущий смешанной группы")
        starts_at = timezone.make_aware(datetime(2026, 2, 13, 10, 0))
        appointment = Appointment(
            child=grant_child,
            staff_member=staff,
            service=service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=45),
            session_type=Appointment.SessionType.GROUP,
            status=Appointment.Status.COMPLETED,
        )
        appointment.save(validate_schedule=False)
        primary = appointment.participants.get(child=grant_child)
        AppointmentParticipant.objects.filter(pk=primary.pk).update(
            billing_decision=Appointment.BillingDecision.DO_NOT_CHARGE,
            billing_account=account,
        )
        secondary = AppointmentParticipant.objects.create(
            appointment=appointment,
            child=other_child,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            appointment_status=appointment.status,
        )
        AppointmentParticipant.objects.filter(pk=secondary.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=None,
        )

        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=self.period_from,
            date_to=self.period_to,
        )

        preflight = {
            row["code"]: row["result"]
            for row in review.evidence_manifest["preflight"]
        }
        self.assertEqual(preflight["financial_integrity"], "passed")
        evidence_kinds = {
            row["kind"] for row in review.evidence_manifest["sources"]
        }
        self.assertTrue(
            {
                "appointment",
                "appointment_participant",
                "service",
            }.issubset(evidence_kinds)
        )

    def test_ledger_evidence_uses_same_effective_date_as_report(self):
        child = Child.objects.create(last_name="Ledger", first_name="Evidence")
        service = Service.objects.create(
            name="Услуга effective date",
            code="DONOR-LEDGER-EFFECTIVE",
        )
        account = BalanceAccount.objects.create(
            child=child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=service,
            initial_amount=Decimal("3"),
        )
        GrantRecipientAllocation.objects.create(
            funding_source=self.funding,
            child=child,
            service=service,
            allocated_sessions=3,
            balance_account=account,
            valid_from=self.period_from,
            valid_until=self.period_to,
        )
        staff = StaffMember.objects.create(full_name="Специалист ledger evidence")
        starts_at = timezone.make_aware(datetime(2026, 2, 14, 10, 0))
        appointment = Appointment(
            child=child,
            staff_member=staff,
            service=service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            status=Appointment.Status.COMPLETED,
        )
        appointment.save(validate_schedule=False)
        participant = appointment.participants.get()
        AppointmentParticipant.objects.filter(pk=participant.pk).update(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=account,
        )
        ledger = LedgerEntry.objects.create(
            account=account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=Decimal("-1"),
            appointment=appointment,
            appointment_participant=participant,
            created_by=self.admin,
            reason="Проводка создана после отчетного периода.",
        )
        LedgerEntry.objects.filter(pk=ledger.pk).update(
            created_at=timezone.make_aware(datetime(2026, 4, 10, 10, 0))
        )

        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=self.period_from,
            date_to=self.period_to,
        )

        sessions = next(
            row
            for row in review.payload["balances"]
            if row["unit"] == BalanceAccount.Unit.SESSIONS
        )
        self.assertEqual(sessions["outflows"], "1.00")
        ledger_source = next(
            row
            for row in review.evidence_manifest["sources"]
            if row["kind"] == "ledger_entry"
        )
        self.assertEqual(ledger_source["source_pk"], ledger.pk)

    def test_administrator_preview_is_no_store_and_has_no_close_action(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("donor_report_snapshot_review"),
            {
                "funding_source": self.funding.pk,
                "counterparty": self.counterparty.pk,
                "date_from": self.period_from.isoformat(),
                "date_to": self.period_to.isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(
            response["X-Data-Classification"],
            "internal-pseudonymized-report",
        )
        self.assertIsNone(response.context["close_form"])
        self.assertNotContains(response, "Закрыть проверенный снимок")

    def test_grant_report_preview_resolves_current_snapshot_when_pointer_is_omitted(self):
        snapshot = self._close(counterparty=self.counterparty)
        self.funding.source_type = FundingSource.SourceType.SPONSOR
        self.funding.save(update_fields=["source_type", "updated_at"])
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("donor_report_snapshot_review"),
            {
                "funding_source": self.funding.pk,
                "counterparty": self.counterparty.pk,
                "date_from": self.period_from.isoformat(),
                "date_to": self.period_to.isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["review"].expected_snapshot_id, snapshot.pk)
        self.assertIsNone(response.context["close_form"])

    def test_live_grant_report_and_csv_are_marked_as_internal_personal_data(self):
        self.client.force_login(self.admin)
        params = {
            "funding": self.funding.pk,
            "date_from": self.period_from.isoformat(),
            "date_to": self.period_to.isoformat(),
        }

        page = self.client.get(reverse("grant_report"), params)
        export = self.client.get(reverse("grant_report"), {**params, "csv": "1"})

        self.assertEqual(page.status_code, 200)
        self.assertEqual(page["Cache-Control"], "private, no-store")
        self.assertEqual(
            page["X-Data-Classification"],
            "internal-personal-data",
        )
        self.assertContains(page, "Внутренний отчет содержит персональные данные")
        self.assertEqual(export.status_code, 200)
        self.assertEqual(export["Cache-Control"], "private, no-store")
        self.assertEqual(
            export["X-Data-Classification"],
            "internal-personal-data",
        )

    def test_specialist_cannot_review_read_or_export_snapshot(self):
        snapshot = self._close(counterparty=self.counterparty)
        specialist_user = User.objects.create_user(
            "donor-report-specialist",
            password="x",
        )
        StaffMember.objects.create(
            user=specialist_user,
            full_name="Специалист без доступа к отчету",
        )
        self.client.force_login(specialist_user)

        review = self.client.post(
            reverse("donor_report_snapshot_review"),
            {
                "funding_source": self.funding.pk,
                "date_from": self.period_from.isoformat(),
                "date_to": self.period_to.isoformat(),
            },
        )
        detail = self.client.get(
            reverse("donor_report_snapshot_detail", args=[snapshot.pk])
        )
        export = self.client.get(
            reverse("donor_report_snapshot_json", args=[snapshot.pk])
        )

        self.assertEqual(review.status_code, 403)
        self.assertEqual(detail.status_code, 403)
        self.assertEqual(export.status_code, 403)

    def test_full_clean_detects_payload_hash_tampering(self):
        snapshot = self._close(counterparty=self.counterparty)
        snapshot.payload["report"]["funding_source"]["type"] = "tampered"

        with self.assertRaisesMessage(ValidationError, "Хеш не соответствует payload"):
            snapshot.full_clean()

    def test_close_rejects_data_changed_after_review(self):
        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=self.counterparty,
            date_from=self.period_from,
            date_to=self.period_to,
        )
        self.funding.source_type = FundingSource.SourceType.SPONSOR
        self.funding.save(update_fields=["source_type", "updated_at"])

        with self.assertRaisesMessage(ValidationError, "Данные изменились после проверки"):
            donor_reports_svc.close_donor_report_snapshot(
                funding_source_id=self.funding.pk,
                counterparty=self.counterparty,
                date_from=self.period_from,
                date_to=self.period_to,
                actor=self.director,
                reason="Попытка закрыть устаревший preview.",
                expected_review_token=review.review_token,
            )

        self.assertFalse(DonorReport.objects.exists())
        self.assertFalse(DonorReportSnapshot.objects.exists())


@skipUnless(connection.vendor == "postgresql", "PostgreSQL trigger contract")
class DonorReportPostgreSQLTriggerTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.director = User.objects.create_superuser(
            "donor-trigger-director",
            password="x",
        )
        self.funding = FundingSource.objects.create(
            name="Грант PostgreSQL guard",
            source_type=FundingSource.SourceType.GRANT,
        )

    def test_deferred_guard_rejects_root_without_terminal_snapshot(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            DonorReport.objects.create(
                funding_source=self.funding,
                date_from=date(2026, 1, 1),
                date_to=date(2026, 1, 31),
            )

    def test_database_rejects_snapshot_update_and_root_delete(self):
        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )
        snapshot = donor_reports_svc.close_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
            actor=self.director,
            reason="Закрыт PostgreSQL снимок.",
            expected_review_token=review.review_token,
        )

        with self.assertRaises(DatabaseError):
            DonorReportSnapshot.objects.filter(pk=snapshot.pk).update(
                reason="Попытка изменить"
            )
        with self.assertRaises(DatabaseError):
            DonorReport.objects.filter(pk=snapshot.report_id).delete()

    def test_database_rejects_noop_correction_even_via_bulk_create(self):
        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )
        first = donor_reports_svc.close_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
            actor=self.director,
            reason="Закрыт исходный снимок.",
            expected_review_token=review.review_token,
        )
        now = timezone.now()
        duplicate = DonorReportSnapshot(
            report=first.report,
            snapshot_number=2,
            event_type=DonorReportSnapshot.EventType.CORRECTED,
            snapshot_schema_version=first.snapshot_schema_version,
            canonicalizer_version=first.canonicalizer_version,
            payload=first.payload,
            evidence_manifest=first.evidence_manifest,
            payload_sha256=first.payload_sha256,
            evidence_manifest_sha256=first.evidence_manifest_sha256,
            data_as_of=now,
            actor=self.director,
            actor_role_snapshot=DonorReportSnapshot.ActorRole.DIRECTOR,
            reason="Попытка создать исправление без изменения.",
            closed_at=now,
            supersedes=first,
        )

        with self.assertRaises(DatabaseError):
            DonorReportSnapshot.objects.bulk_create([duplicate])

        self.assertEqual(first.report.snapshots.count(), 1)

    def test_database_recomputes_hashes_and_rejects_forbidden_payload_keys(self):
        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )
        first = donor_reports_svc.close_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
            actor=self.director,
            reason="Закрыт исходный снимок для проверки БД.",
            expected_review_token=review.review_token,
        )

        def correction(
            *,
            payload: dict,
            evidence_manifest: dict,
            payload_sha256: str,
            evidence_manifest_sha256: str,
        ) -> DonorReportSnapshot:
            now = timezone.now()
            return DonorReportSnapshot(
                report=first.report,
                snapshot_number=2,
                event_type=DonorReportSnapshot.EventType.CORRECTED,
                snapshot_schema_version=first.snapshot_schema_version,
                canonicalizer_version=first.canonicalizer_version,
                payload=payload,
                evidence_manifest=evidence_manifest,
                payload_sha256=payload_sha256,
                evidence_manifest_sha256=evidence_manifest_sha256,
                data_as_of=now,
                actor=self.director,
                actor_role_snapshot=DonorReportSnapshot.ActorRole.DIRECTOR,
                reason="Попытка обойти проверку снимка через bulk_create.",
                closed_at=now,
                supersedes=first,
            )

        fake_hash_payload = deepcopy(first.payload)
        fake_hash_payload["report"]["funding_source"]["type"] = "sponsor"
        fake_hash_evidence = deepcopy(first.evidence_manifest)
        fake_hash_evidence["payload_sha256"] = "0" * 64
        with self.assertRaisesMessage(
            DatabaseError,
            "hash verification failed",
        ), transaction.atomic():
            fake_hash_snapshot = DonorReportSnapshot.objects.bulk_create(
                [
                    correction(
                        payload=fake_hash_payload,
                        evidence_manifest=fake_hash_evidence,
                        payload_sha256="0" * 64,
                        evidence_manifest_sha256="0" * 64,
                    )
                ]
            )[0]
            DonorReport.objects.filter(pk=first.report_id).update(
                current_snapshot=fake_hash_snapshot
            )

        pii_payload = deepcopy(first.payload)
        pii_payload["integrity"]["warnings"].append(
            {
                "code": "recipient_full_name",
                "severity": "info",
                "count": 1,
            }
        )
        pii_payload_sha256 = donor_reports_svc.canonical_sha256(pii_payload)
        pii_evidence = deepcopy(first.evidence_manifest)
        pii_evidence["payload_sha256"] = pii_payload_sha256
        pii_evidence_sha256 = donor_reports_svc.canonical_sha256(pii_evidence)
        with self.assertRaisesMessage(
            DatabaseError,
            "forbidden JSON key or value",
        ), transaction.atomic():
            pii_snapshot = DonorReportSnapshot.objects.bulk_create(
                [
                    correction(
                        payload=pii_payload,
                        evidence_manifest=pii_evidence,
                        payload_sha256=pii_payload_sha256,
                        evidence_manifest_sha256=pii_evidence_sha256,
                    )
                ]
            )[0]
            DonorReport.objects.filter(pk=first.report_id).update(
                current_snapshot=pii_snapshot
            )

        self.assertEqual(first.report.snapshots.count(), 1)

    def test_database_timestamp_is_used_for_data_and_closure(self):
        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=date(2026, 2, 1),
            date_to=date(2026, 2, 28),
        )
        with patch.object(
            donor_reports_svc.timezone,
            "now",
            return_value=datetime(2000, 1, 1, tzinfo=UTC),
        ):
            snapshot = donor_reports_svc.close_donor_report_snapshot(
                funding_source_id=self.funding.pk,
                counterparty=None,
                date_from=date(2026, 2, 1),
                date_to=date(2026, 2, 28),
                actor=self.director,
                reason="Проверка единого времени PostgreSQL.",
                expected_review_token=review.review_token,
            )

        self.assertEqual(snapshot.closed_at, snapshot.data_as_of)
        self.assertGreater(snapshot.closed_at.year, 2020)

    def test_concurrent_ledger_transaction_is_wholly_next_snapshot(self):
        child = Child.objects.create(last_name="MVCC", first_name="Ledger")
        account = BalanceAccount.objects.create(
            child=child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("0"),
        )
        period_from = date(2026, 3, 1)
        period_to = date(2026, 3, 31)
        initial_review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=period_from,
            date_to=period_to,
        )
        snapshot_started = Event()
        writer_committed = Event()
        results: Queue[tuple[str, object]] = Queue()
        original_data_as_of = donor_reports_svc._database_data_as_of

        def delayed_data_as_of():
            snapshot_started.set()
            if not writer_committed.wait(timeout=20):
                raise TimeoutError("Ledger writer did not commit in time.")
            return original_data_as_of()

        def close_worker() -> None:
            close_old_connections()
            try:
                actor = User.objects.get(pk=self.director.pk)
                with patch.object(
                    donor_reports_svc,
                    "_database_data_as_of",
                    side_effect=delayed_data_as_of,
                ):
                    snapshot = donor_reports_svc.close_donor_report_snapshot(
                        funding_source_id=self.funding.pk,
                        counterparty=None,
                        date_from=period_from,
                        date_to=period_to,
                        actor=actor,
                        reason="MVCC снимок до конкурентной проводки.",
                        expected_review_token=initial_review.review_token,
                    )
                results.put(("close", snapshot.pk))
            except Exception as exc:
                results.put(("close_error", exc))
            finally:
                connections.close_all()

        def writer_worker() -> None:
            close_old_connections()
            try:
                if not snapshot_started.wait(timeout=20):
                    raise TimeoutError("Donor snapshot did not start in time.")
                with transaction.atomic():
                    locked_account = BalanceAccount.objects.get(pk=account.pk)
                    entries = LedgerEntry.objects.bulk_create(
                        [
                            LedgerEntry(
                                account=locked_account,
                                entry_type=LedgerEntry.EntryType.CREDIT,
                                amount=Decimal("1.00"),
                                reason="Первая часть конкурентного транша.",
                            ),
                            LedgerEntry(
                                account=locked_account,
                                entry_type=LedgerEntry.EntryType.CREDIT,
                                amount=Decimal("2.00"),
                                reason="Вторая часть конкурентного транша.",
                            ),
                        ]
                    )
                    LedgerEntry.objects.filter(
                        pk__in=[entry.pk for entry in entries]
                    ).update(
                        created_at=timezone.make_aware(
                            datetime(2026, 3, 15, 12, 0)
                        )
                    )
                results.put(("writer", "committed"))
            except Exception as exc:
                results.put(("writer_error", exc))
            finally:
                writer_committed.set()
                connections.close_all()

        close_thread = Thread(target=close_worker)
        writer_thread = Thread(target=writer_worker)
        close_thread.start()
        writer_thread.start()
        close_thread.join(timeout=40)
        writer_thread.join(timeout=40)

        self.assertFalse(close_thread.is_alive())
        self.assertFalse(writer_thread.is_alive())
        outcomes = dict(
            results.get_nowait()
            for _index in range(results.qsize())
        )
        self.assertNotIn("close_error", outcomes)
        self.assertNotIn("writer_error", outcomes)
        first = DonorReportSnapshot.objects.get(pk=outcomes["close"])
        first_sessions = next(
            row
            for row in first.payload["balances"]
            if row["unit"] == BalanceAccount.Unit.SESSIONS
        )
        self.assertEqual(first_sessions["inflows"], "0.00")

        correction_review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=period_from,
            date_to=period_to,
            expected_snapshot_id=first.pk,
        )
        second = donor_reports_svc.close_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=period_from,
            date_to=period_to,
            actor=self.director,
            reason="Следующий снимок включает конкурентный транш целиком.",
            expected_review_token=correction_review.review_token,
            expected_snapshot_id=first.pk,
        )
        second_sessions = next(
            row
            for row in second.payload["balances"]
            if row["unit"] == BalanceAccount.Unit.SESSIONS
        )
        self.assertEqual(second_sessions["inflows"], "3.00")

    def test_concurrent_first_close_creates_one_terminal_snapshot(self):
        barrier = Barrier(2)
        results: Queue[tuple[str, object]] = Queue()
        review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=date(2026, 2, 1),
            date_to=date(2026, 2, 28),
        )

        def worker() -> None:
            close_old_connections()
            try:
                actor = User.objects.get(pk=self.director.pk)
                barrier.wait(timeout=10)
                snapshot = donor_reports_svc.close_donor_report_snapshot(
                    funding_source_id=self.funding.pk,
                    counterparty=None,
                    date_from=date(2026, 2, 1),
                    date_to=date(2026, 2, 28),
                    actor=actor,
                    reason="Параллельное закрытие грантового отчета.",
                    expected_review_token=review.review_token,
                )
                results.put(("ok", snapshot.pk))
            except Exception as exc:
                results.put(("error", exc))
            finally:
                connections.close_all()

        threads = [Thread(target=worker), Thread(target=worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        outcomes = [results.get_nowait(), results.get_nowait()]
        successes = [value for status, value in outcomes if status == "ok"]
        errors = [value for status, value in outcomes if status == "error"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValidationError)
        self.assertIn("Отчет уже закрыт", str(errors[0]))

        report = DonorReport.objects.get(
            funding_source=self.funding,
            counterparty=None,
            date_from=date(2026, 2, 1),
            date_to=date(2026, 2, 28),
        )
        self.assertEqual(report.snapshots.count(), 1)
        self.assertEqual(report.current_snapshot_id, successes[0])

    def test_concurrent_corrections_create_one_direct_successor(self):
        first_review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=date(2026, 4, 1),
            date_to=date(2026, 4, 30),
        )
        first = donor_reports_svc.close_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=date(2026, 4, 1),
            date_to=date(2026, 4, 30),
            actor=self.director,
            reason="Исходный снимок перед конкурентным исправлением.",
            expected_review_token=first_review.review_token,
        )
        FundingSource.objects.filter(pk=self.funding.pk).update(
            source_type=FundingSource.SourceType.SPONSOR
        )
        correction_review = donor_reports_svc.review_donor_report_snapshot(
            funding_source_id=self.funding.pk,
            counterparty=None,
            date_from=date(2026, 4, 1),
            date_to=date(2026, 4, 30),
            expected_snapshot_id=first.pk,
        )
        barrier = Barrier(2)
        results: Queue[tuple[str, object]] = Queue()

        def worker() -> None:
            close_old_connections()
            try:
                actor = User.objects.get(pk=self.director.pk)
                barrier.wait(timeout=10)
                snapshot = donor_reports_svc.close_donor_report_snapshot(
                    funding_source_id=self.funding.pk,
                    counterparty=None,
                    date_from=date(2026, 4, 1),
                    date_to=date(2026, 4, 30),
                    actor=actor,
                    reason="Конкурентная исправляющая версия.",
                    expected_review_token=correction_review.review_token,
                    expected_snapshot_id=first.pk,
                )
                results.put(("ok", snapshot.pk))
            except Exception as exc:
                results.put(("error", exc))
            finally:
                connections.close_all()

        threads = [Thread(target=worker), Thread(target=worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        outcomes = [results.get_nowait(), results.get_nowait()]
        successes = [value for status, value in outcomes if status == "ok"]
        errors = [value for status, value in outcomes if status == "error"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValidationError)
        first.report.refresh_from_db()
        self.assertEqual(first.report.snapshots.count(), 2)
        self.assertEqual(first.report.current_snapshot_id, successes[0])
