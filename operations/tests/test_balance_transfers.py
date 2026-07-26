"""Regression and PostgreSQL concurrency tests for persisted balance transfers."""

from __future__ import annotations

from decimal import Decimal
from queue import Queue
from threading import Barrier, Thread
from unittest import skipUnless
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase

from operations.models import (
    BalanceAccount,
    BalanceTransfer,
    Child,
    FundingSource,
    LedgerEntry,
    ProgramBlock,
    Service,
    TreatmentProgram,
)
from operations.services import billing as billing_svc, program_wizard as program_wizard_svc


class BalanceTransferServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("balance-transfer-admin", password="x", is_staff=True)
        self.child = Child.objects.create(last_name="Transfer", first_name="Recipient")
        self.service = Service.objects.create(
            name="Balance transfer service",
            code="BALANCE-TRANSFER",
            default_duration_minutes=30,
            default_price=Decimal("500.00"),
        )
        self.funding = FundingSource.objects.create(
            name="Balance transfer funding",
            source_type=FundingSource.SourceType.GRANT,
            transfer_policy=FundingSource.TransferPolicy.WITHIN_CHILD,
        )
        self.sessions_source = BalanceAccount.objects.create(
            child=self.child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("10.00"),
        )
        self.sessions_target = BalanceAccount.objects.create(
            child=self.child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("0.00"),
        )
        self.money_source = BalanceAccount.objects.create(
            child=self.child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.MONEY,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("2000.00"),
        )
        self.program = TreatmentProgram.objects.create(
            child=self.child,
            title="Balance transfer program",
            status=TreatmentProgram.Status.ACTIVE,
        )
        self.block = ProgramBlock.objects.create(
            program=self.program,
            number=1,
            title="Balance transfer block",
            service=self.service,
            planned_sessions=6,
            balance_account=self.sessions_target,
        )

    def test_direct_transfer_persists_one_immutable_fact_and_two_linked_entries(self):
        transfer = billing_svc.record_balance_transfer(
            from_account=self.sessions_source,
            to_account=self.sessions_target,
            amount=Decimal("3.00"),
            reason="Move unused sessions to the cascade.",
            actor=self.user,
            program_block=self.block,
            idempotency_key=uuid4(),
        )

        entries = list(transfer.ledger_entries.order_by("transfer_side"))
        self.assertEqual(len(entries), 2)
        self.assertEqual(
            {(entry.transfer_side, entry.amount) for entry in entries},
            {
                (LedgerEntry.TransferSide.DEBIT, Decimal("-3.00")),
                (LedgerEntry.TransferSide.CREDIT, Decimal("3.00")),
            },
        )
        self.assertEqual(transfer.operation_kind, BalanceTransfer.OperationKind.DIRECT)
        self.assertIsNone(transfer.conversion_rate)
        self.sessions_source.refresh_from_db()
        self.sessions_target.refresh_from_db()
        self.assertEqual(self.sessions_source.current_balance, Decimal("7.00"))
        self.assertEqual(self.sessions_target.current_balance, Decimal("3.00"))

        transfer.reason = "Changed reason"
        with self.assertRaises(ValidationError):
            transfer.save()

    def test_money_to_sessions_snapshots_rate_and_updates_funded_capacity_not_plan(self):
        self.assertEqual(program_wizard_svc.funded_sessions_remaining(self.block), 0)
        key = uuid4()

        transfer = billing_svc.convert_money_to_sessions(
            from_account=self.money_source,
            to_account=self.sessions_target,
            program_block=self.block,
            sessions=Decimal("3"),
            reason="Convert grant money into sessions for the cascade.",
            actor=self.user,
            idempotency_key=key,
        )

        self.assertEqual(transfer.operation_kind, BalanceTransfer.OperationKind.MONEY_TO_SESSIONS)
        self.assertEqual(transfer.amount_from, Decimal("1500.00"))
        self.assertEqual(transfer.amount_to, Decimal("3.00"))
        self.assertEqual(transfer.conversion_rate, Decimal("500.00"))
        self.money_source.refresh_from_db()
        self.sessions_target.refresh_from_db()
        self.block.refresh_from_db()
        self.assertEqual(self.money_source.current_balance, Decimal("500.00"))
        self.assertEqual(self.sessions_target.current_balance, Decimal("3.00"))
        self.assertEqual(program_wizard_svc.funded_sessions_remaining(self.block), 3)
        self.assertEqual(self.block.planned_sessions, 6)

        self.service.default_price = Decimal("900.00")
        self.service.save(update_fields=["default_price", "updated_at"])
        transfer.refresh_from_db()
        self.assertEqual(transfer.conversion_rate, Decimal("500.00"))
        self.assertEqual(transfer.amount_from, Decimal("1500.00"))

        retried = billing_svc.convert_money_to_sessions(
            from_account=self.money_source,
            to_account=self.sessions_target,
            program_block=self.block,
            sessions=Decimal("3"),
            reason="Convert grant money into sessions for the cascade.",
            actor=self.user,
            idempotency_key=key,
        )
        self.assertEqual(retried.pk, transfer.pk)

    def test_rejects_target_account_different_from_assigned_block_account(self):
        alternate_target = BalanceAccount.objects.create(
            child=self.child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.SPECIFIC_SERVICE,
            service=self.service,
            initial_amount=Decimal("0.00"),
        )

        with self.assertRaisesMessage(ValueError, "должен совпадать со счётом выбранного каскада"):
            billing_svc.record_balance_transfer(
                from_account=self.sessions_source,
                to_account=alternate_target,
                amount=Decimal("1.00"),
                reason="Invalid target for this cascade.",
                actor=self.user,
                program_block=self.block,
                idempotency_key=uuid4(),
            )

    def test_transfer_binds_an_unfunded_block_to_its_target_account(self):
        self.block.balance_account = None
        self.block.save(update_fields=["balance_account", "updated_at"])

        billing_svc.record_balance_transfer(
            from_account=self.sessions_source,
            to_account=self.sessions_target,
            amount=Decimal("2.00"),
            reason="Fund the previously unbound cascade.",
            actor=self.user,
            program_block=self.block,
            idempotency_key=uuid4(),
        )

        self.block.refresh_from_db()
        self.assertEqual(self.block.balance_account_id, self.sessions_target.pk)
        self.assertEqual(program_wizard_svc.funded_sessions_remaining(self.block), 2)

    def test_money_to_sessions_rejects_fractional_sessions(self):
        with self.assertRaisesMessage(ValueError, "целое положительное"):
            billing_svc.convert_money_to_sessions(
                from_account=self.money_source,
                to_account=self.sessions_target,
                program_block=self.block,
                sessions=Decimal("1.50"),
                reason="Invalid fractional conversion.",
                actor=self.user,
            )

    def test_idempotency_key_returns_same_transfer_without_second_ledger_pair(self):
        key = uuid4()
        first = billing_svc.record_balance_transfer(
            from_account=self.sessions_source,
            to_account=self.sessions_target,
            amount=Decimal("4.00"),
            reason="Idempotent move.",
            actor=self.user,
            idempotency_key=key,
        )
        second = billing_svc.record_balance_transfer(
            from_account=self.sessions_source,
            to_account=self.sessions_target,
            amount=Decimal("4.00"),
            reason="Idempotent move.",
            actor=self.user,
            idempotency_key=key,
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(BalanceTransfer.objects.count(), 1)
        self.assertEqual(LedgerEntry.objects.filter(balance_transfer=first).count(), 2)
        self.sessions_source.refresh_from_db()
        self.sessions_target.refresh_from_db()
        self.assertEqual(self.sessions_source.current_balance, Decimal("6.00"))
        self.assertEqual(self.sessions_target.current_balance, Decimal("4.00"))

        with self.assertRaisesMessage(ValueError, "уже использован другой операцией"):
            billing_svc.record_balance_transfer(
                from_account=self.sessions_source,
                to_account=self.sessions_target,
                amount=Decimal("3.00"),
                reason="Idempotent move.",
                actor=self.user,
                idempotency_key=key,
            )

    def test_rejects_cross_funding_transfer_and_invalid_new_ledger_link(self):
        other_funding = FundingSource.objects.create(
            name="Other balance transfer funding",
            transfer_policy=FundingSource.TransferPolicy.WITHIN_CHILD,
        )
        other_target = BalanceAccount.objects.create(
            child=self.child,
            funding_source=other_funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("0.00"),
        )
        with self.assertRaisesMessage(ValueError, "одного источника"):
            billing_svc.record_balance_transfer(
                from_account=self.sessions_source,
                to_account=other_target,
                amount=Decimal("1.00"),
                reason="Invalid cross-funding move.",
                actor=self.user,
            )

        transfer = billing_svc.record_balance_transfer(
            from_account=self.sessions_source,
            to_account=self.sessions_target,
            amount=Decimal("1.00"),
            reason="Valid move before constraint test.",
            actor=self.user,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            LedgerEntry.objects.create(
                account=self.sessions_source,
                entry_type=LedgerEntry.EntryType.CREDIT,
                amount=Decimal("1.00"),
                balance_transfer=transfer,
                transfer_side=LedgerEntry.TransferSide.CREDIT,
            )

        debit = transfer.ledger_entries.get(transfer_side=LedgerEntry.TransferSide.DEBIT)
        with self.assertRaises(IntegrityError), transaction.atomic():
            LedgerEntry.objects.filter(pk=debit.pk).update(amount=Decimal("1.00"))

    def test_legacy_unlinked_transfer_entry_remains_valid(self):
        entry = LedgerEntry.objects.create(
            account=self.sessions_source,
            entry_type=LedgerEntry.EntryType.TRANSFER,
            amount=Decimal("-1.00"),
            reason="Historical transfer without persisted operation.",
        )
        self.assertIsNone(entry.balance_transfer_id)
        self.assertIsNone(entry.transfer_side)


class BalanceTransferPostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.child = Child.objects.create(last_name="Transfer", first_name="Concurrent")
        self.funding = FundingSource.objects.create(
            name="Concurrent transfer funding",
            transfer_policy=FundingSource.TransferPolicy.WITHIN_CHILD,
        )
        self.source = BalanceAccount.objects.create(
            child=self.child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("5.00"),
        )
        self.target = BalanceAccount.objects.create(
            child=self.child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("0.00"),
        )

    def _run_concurrent(self, *, key_factory):
        barrier = Barrier(2)
        outcomes = Queue()

        def transfer() -> None:
            close_old_connections()
            try:
                source = BalanceAccount.objects.get(pk=self.source.pk)
                target = BalanceAccount.objects.get(pk=self.target.pk)
                barrier.wait(timeout=10)
                persisted = billing_svc.record_balance_transfer(
                    from_account=source,
                    to_account=target,
                    amount=Decimal("4.00"),
                    reason="Concurrent persisted transfer.",
                    idempotency_key=key_factory(),
                )
            except ValueError:
                outcomes.put("rejected")
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put(persisted.pk)
            finally:
                connection.close()

        threads = [Thread(target=transfer), Thread(target=transfer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        return [outcomes.get_nowait() for _ in range(2)]

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентный перенос проверяется только на PostgreSQL.",
    )
    def test_concurrent_distinct_transfers_cannot_overspend_source(self):
        outcomes = self._run_concurrent(key_factory=uuid4)

        self.assertEqual(sum(isinstance(outcome, int) for outcome in outcomes), 1, outcomes)
        self.assertEqual(outcomes.count("rejected"), 1, outcomes)
        self.assertEqual(BalanceTransfer.objects.count(), 1)
        self.assertEqual(LedgerEntry.objects.filter(balance_transfer__isnull=False).count(), 2)
        self.source.refresh_from_db()
        self.target.refresh_from_db()
        self.assertEqual(self.source.current_balance, Decimal("1.00"))
        self.assertEqual(self.target.current_balance, Decimal("4.00"))

    @skipUnless(
        connection.vendor == "postgresql",
        "Идемпотентный конкурентный перенос проверяется только на PostgreSQL.",
    )
    def test_concurrent_same_key_creates_one_transfer_and_returns_same_fact(self):
        key = uuid4()
        outcomes = self._run_concurrent(key_factory=lambda: key)

        self.assertTrue(all(isinstance(outcome, int) for outcome in outcomes), outcomes)
        self.assertEqual(len(set(outcomes)), 1)
        self.assertEqual(BalanceTransfer.objects.count(), 1)
        self.assertEqual(LedgerEntry.objects.filter(balance_transfer__isnull=False).count(), 2)
