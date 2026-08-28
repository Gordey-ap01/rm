from datetime import datetime, time, timedelta
from decimal import Decimal
from queue import Queue
from threading import Barrier, Thread
from unittest import skipUnless
from uuid import uuid4

from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.utils import timezone

from operations.models import (
    Appointment,
    BalanceAccount,
    Child,
    FundingSource,
    LedgerEntry,
    ProgramBlock,
    Service,
    StaffMember,
    TreatmentProgram,
)
from operations.services import (
    billing as billing_svc,
    financial_integrity as financial_integrity_svc,
)


class BillingDecisionPostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.child = Child.objects.create(last_name="Billing", first_name="Concurrent")
        self.staff = StaffMember.objects.create(full_name="Billing Concurrent Staff")
        self.service = Service.objects.create(
            name="Billing concurrent service",
            code="BILLING-CONCURRENT",
            default_duration_minutes=30,
        )
        self.funding = FundingSource.objects.create(
            name="Billing concurrent funding",
            transfer_policy=FundingSource.TransferPolicy.WITHIN_CHILD,
        )
        self.account = BalanceAccount.objects.create(
            child=self.child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("5"),
        )
        starts_at = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=30), time(10, 0)),
            timezone.get_current_timezone(),
        )
        self.appointment = Appointment.objects.create(
            child=self.child,
            staff_member=self.staff,
            service=self.service,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентное списание проверяется только на PostgreSQL.",
    )
    def test_concurrent_charge_keeps_one_active_debit_and_one_financial_fact(self):
        barrier = Barrier(2)
        outcomes = Queue()

        def charge() -> None:
            close_old_connections()
            try:
                appointment = Appointment.objects.get(pk=self.appointment.pk)
                account = BalanceAccount.objects.get(pk=self.account.pk)
                barrier.wait(timeout=10)
                billing_svc.apply_decision(
                    appointment,
                    decision=Appointment.BillingDecision.CHARGE,
                    account=account,
                    amount=Decimal("-1"),
                )
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put("charged")
            finally:
                connection.close()

        threads = [Thread(target=charge), Thread(target=charge)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertEqual(results.count("charged"), 2, results)
        active_debits = LedgerEntry.objects.filter(
            appointment=self.appointment,
            entry_type=LedgerEntry.EntryType.DEBIT,
        )
        self.assertEqual(active_debits.count(), 1)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal("4"))
        self.assertEqual(financial_integrity_svc.audit_appointments([self.appointment]), [])

    @skipUnless(
        connection.vendor == "postgresql",
        "Гонка отмены списания и переноса средств проверяется только на PostgreSQL.",
    )
    def test_do_not_charge_and_transfer_follow_block_account_ledger_order(self):
        target_account = BalanceAccount.objects.create(
            child=self.child,
            funding_source=self.funding,
            unit=BalanceAccount.Unit.SESSIONS,
            service_scope=BalanceAccount.ServiceScope.ANY,
            initial_amount=Decimal("0"),
        )
        program = TreatmentProgram.objects.create(
            child=self.child,
            title="Billing concurrent program",
            status=TreatmentProgram.Status.ACTIVE,
        )
        block = ProgramBlock.objects.create(
            program=program,
            number=1,
            title="Billing concurrent block",
            service=self.service,
            staff_member=self.staff,
            planned_sessions=5,
            balance_account=target_account,
        )
        self.appointment.program_block = block
        self.appointment.billing_account = self.account
        self.appointment.save(
            update_fields=["program_block", "billing_account", "updated_at"]
        )
        billing_svc.apply_decision(
            self.appointment,
            decision=Appointment.BillingDecision.CHARGE,
            account=self.account,
            amount=Decimal("-1"),
        )
        barrier = Barrier(2)
        outcomes = Queue()

        def do_not_charge() -> None:
            close_old_connections()
            try:
                appointment = Appointment.objects.get(pk=self.appointment.pk)
                barrier.wait(timeout=10)
                billing_svc.apply_decision(
                    appointment,
                    decision=Appointment.BillingDecision.DO_NOT_CHARGE,
                    reason="Concurrent cancellation of the debit.",
                )
                outcomes.put("do_not_charge")
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        def transfer() -> None:
            close_old_connections()
            try:
                source = BalanceAccount.objects.get(pk=self.account.pk)
                target = BalanceAccount.objects.get(pk=target_account.pk)
                current_block = ProgramBlock.objects.get(pk=block.pk)
                barrier.wait(timeout=10)
                billing_svc.record_balance_transfer(
                    from_account=source,
                    to_account=target,
                    amount=Decimal("1"),
                    reason="Concurrent transfer after billing decision.",
                    program_block=current_block,
                    idempotency_key=uuid4(),
                )
                outcomes.put("transfer")
            except BaseException as exc:
                outcomes.put(exc)
            finally:
                connection.close()

        threads = [Thread(target=do_not_charge), Thread(target=transfer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertCountEqual(results, ["do_not_charge", "transfer"])
        self.account.refresh_from_db()
        target_account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal("4"))
        self.assertEqual(target_account.current_balance, Decimal("1"))
        self.assertFalse(
            LedgerEntry.objects.filter(
                appointment=self.appointment,
                entry_type=LedgerEntry.EntryType.DEBIT,
            ).exists()
        )
