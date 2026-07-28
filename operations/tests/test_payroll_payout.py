from datetime import datetime, time, timedelta
from decimal import Decimal
from queue import Queue
from threading import Barrier, Thread
from unittest import skipUnless

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    PayrollAccrual,
    PayrollPayout,
    PayrollSheet,
    PayrollSheetLifecycleEvent,
    PayrollSheetLine,
    Service,
    StaffCompensationRule,
    StaffMember,
)
from operations.services import payroll as payroll_svc


def _approved_payroll_sheet(*, staff: StaffMember, service: Service, director: User) -> PayrollSheet:
    work_date = timezone.localdate()
    starts_at = timezone.make_aware(
        datetime.combine(work_date, time(10, 0)), timezone.get_current_timezone()
    )
    accrual = PayrollAccrual.objects.create(
        dedupe_key=f"payroll-payout:{staff.pk}:{work_date.isoformat()}",
        staff_member=staff,
        service=service,
        work_date=work_date,
        starts_at_snapshot=starts_at,
        ends_at_snapshot=starts_at + timedelta(minutes=30),
        duration_minutes=30,
        rate_type_snapshot=StaffCompensationRule.RateType.PER_SESSION,
        rate_amount_snapshot=Decimal("500.00"),
        session_scope_snapshot=StaffCompensationRule.SessionScope.ALL,
        group_pay_policy_snapshot=StaffCompensationRule.GroupPayPolicy.PER_SESSION,
        charged_participants_count_snapshot=1,
        pay_units_snapshot=1,
        amount=Decimal("500.00"),
        status=PayrollAccrual.Status.APPROVED,
        approved_by=director,
        approved_at=timezone.now(),
    )
    sheet = PayrollSheet.objects.create(
        staff_member=staff,
        date_from=work_date,
        date_to=work_date,
        status=PayrollSheet.Status.APPROVED,
        total_amount=Decimal("500.00"),
        approved_by=director,
        approved_at=timezone.now(),
    )
    PayrollSheetLine.objects.create(
        payroll_sheet=sheet,
        payroll_accrual=accrual,
        accrual_kind_snapshot=PayrollAccrual.AccrualKind.APPOINTMENT,
        service=service,
        work_date=work_date,
        line_label=service.name,
        duration_minutes=30,
        amount=Decimal("500.00"),
    )
    return sheet


class PayrollPayoutLifecycleTests(TestCase):
    def setUp(self):
        self.director = User.objects.create_superuser("payout-director", password="x")
        self.operator = User.objects.create_user("payout-operator", password="x", is_staff=True)
        self.staff = StaffMember.objects.create(full_name="Payroll payout specialist")
        self.service = Service.objects.create(
            name="Payroll payout service",
            code="PAYROLL-PAYOUT",
            default_duration_minutes=30,
        )

    def _approved_sheet(self) -> PayrollSheet:
        return _approved_payroll_sheet(
            staff=self.staff,
            service=self.service,
            director=self.director,
        )

    def test_only_director_can_send_and_record_payout(self):
        sheet = self._approved_sheet()

        with self.assertRaises(PermissionDenied):
            payroll_svc.send_payroll_sheet(sheet, note="Передать в оплату", actor=self.operator)

        sheet.refresh_from_db()
        self.assertEqual(sheet.status, PayrollSheet.Status.APPROVED)
        self.assertFalse(PayrollSheetLifecycleEvent.objects.exists())

        payroll_svc.send_payroll_sheet(sheet, note="Передать в оплату", actor=self.director)
        with self.assertRaises(PermissionDenied):
            payroll_svc.record_payroll_payout(
                sheet,
                amount=Decimal("500.00"),
                method=PayrollPayout.Method.BANK_TRANSFER,
                paid_at=timezone.localdate(),
                actor=self.operator,
            )

        sheet.refresh_from_db()
        self.assertEqual(sheet.status, PayrollSheet.Status.SENT)
        event = PayrollSheetLifecycleEvent.objects.get()
        self.assertEqual(event.event_type, PayrollSheetLifecycleEvent.EventType.SENT)
        self.assertEqual(event.actor, self.director)
        self.assertEqual(event.actor_role_snapshot, "director")

    def test_full_payout_is_atomic_and_cannot_be_recorded_twice(self):
        sheet = self._approved_sheet()
        payroll_svc.send_payroll_sheet(sheet, note="Проверено руководителем", actor=self.director)

        payout = payroll_svc.record_payroll_payout(
            sheet,
            amount=Decimal("500.00"),
            method=PayrollPayout.Method.BANK_TRANSFER,
            paid_at=timezone.localdate(),
            reference="PAY-2026-001",
            note="Перевод выполнен.",
            actor=self.director,
        )

        sheet.refresh_from_db()
        accrual = PayrollAccrual.objects.get()
        self.assertEqual(sheet.status, PayrollSheet.Status.PAID)
        self.assertEqual(accrual.status, PayrollAccrual.Status.PAID)
        self.assertEqual(payout.amount, sheet.total_amount)
        self.assertEqual(PayrollPayout.objects.count(), 1)
        self.assertEqual(PayrollSheetLifecycleEvent.objects.count(), 2)
        self.assertTrue(
            PayrollSheetLifecycleEvent.objects.filter(
                event_type=PayrollSheetLifecycleEvent.EventType.PAID
            ).exists()
        )

        with self.assertRaises(ValueError):
            payroll_svc.record_payroll_payout(
                sheet,
                amount=Decimal("500.00"),
                method=PayrollPayout.Method.BANK_TRANSFER,
                paid_at=timezone.localdate(),
                actor=self.director,
            )
        self.assertEqual(PayrollPayout.objects.count(), 1)

    def test_payout_requires_sent_status_and_exact_total(self):
        sheet = self._approved_sheet()
        with self.assertRaises(ValueError):
            payroll_svc.record_payroll_payout(
                sheet,
                amount=Decimal("500.00"),
                method=PayrollPayout.Method.CASH,
                paid_at=timezone.localdate(),
                actor=self.director,
            )

        payroll_svc.send_payroll_sheet(sheet, note="Проверено руководителем", actor=self.director)
        with self.assertRaises(ValueError):
            payroll_svc.record_payroll_payout(
                sheet,
                amount=Decimal("499.99"),
                method=PayrollPayout.Method.CASH,
                paid_at=timezone.localdate(),
                actor=self.director,
            )

        sheet.refresh_from_db()
        self.assertEqual(sheet.status, PayrollSheet.Status.SENT)
        self.assertFalse(PayrollPayout.objects.exists())
        self.assertEqual(PayrollAccrual.objects.get().status, PayrollAccrual.Status.APPROVED)

    def test_payout_and_lifecycle_history_are_immutable_and_protect_sheet(self):
        sheet = self._approved_sheet()
        payroll_svc.send_payroll_sheet(sheet, note="Проверено руководителем", actor=self.director)
        payout = payroll_svc.record_payroll_payout(
            sheet,
            amount=Decimal("500.00"),
            method=PayrollPayout.Method.CASH,
            paid_at=timezone.localdate(),
            actor=self.director,
        )
        payout.reference = "changed"
        with self.assertRaises(ValidationError):
            payout.save()

        event = PayrollSheetLifecycleEvent.objects.get(
            event_type=PayrollSheetLifecycleEvent.EventType.SENT
        )
        event.note = "Изменено"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ProtectedError):
            sheet.delete()

    def test_operator_sees_history_but_cannot_send_or_record_payout(self):
        sheet = self._approved_sheet()
        detail_url = reverse("payroll_sheet_detail", args=[sheet.pk])

        self.client.force_login(self.operator)
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="action" value="send"')
        self.assertNotContains(response, 'name="action" value="record_payout"')
        self.assertEqual(
            self.client.post(detail_url, {"action": "send", "note": "Передать в оплату"}).status_code,
            403,
        )

        self.client.force_login(self.director)
        self.assertRedirects(
            self.client.post(detail_url, {"action": "send", "note": "Передать в оплату"}),
            detail_url,
        )
        response = self.client.get(detail_url)
        self.assertContains(response, 'name="action" value="record_payout"')
        self.assertContains(response, "Передан в выплату")
        self.assertRedirects(
            self.client.post(
                detail_url,
                {
                    "action": "record_payout",
                    "amount": "500.00",
                    "method": PayrollPayout.Method.BANK_TRANSFER,
                    "paid_at": timezone.localdate().isoformat(),
                    "reference": "PAY-2026-UI",
                    "note": "Перевод выполнен.",
                },
            ),
            detail_url,
        )
        response = self.client.get(detail_url)
        self.assertContains(response, "PAY-2026-UI")
        self.assertContains(response, "Выплата отмечена")


class PayrollPayoutPostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.director = User.objects.create_superuser("payout-pg-director", password="x")
        self.staff = StaffMember.objects.create(full_name="Payroll payout concurrent specialist")
        self.service = Service.objects.create(
            name="Payroll payout concurrent service",
            code="PAYROLL-PAYOUT-CONCURRENT",
            default_duration_minutes=30,
        )
        self.sheet = _approved_payroll_sheet(
            staff=self.staff,
            service=self.service,
            director=self.director,
        )
        payroll_svc.send_payroll_sheet(
            self.sheet,
            note="Проверено руководителем",
            actor=self.director,
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "Конкурентная выплата проверяется только на PostgreSQL.",
    )
    def test_concurrent_payout_recording_creates_one_payout(self):
        barrier = Barrier(2)
        outcomes = Queue()

        def record() -> None:
            close_old_connections()
            try:
                sheet = PayrollSheet.objects.get(pk=self.sheet.pk)
                director = User.objects.get(pk=self.director.pk)
                barrier.wait(timeout=10)
                payroll_svc.record_payroll_payout(
                    sheet,
                    amount=Decimal("500.00"),
                    method=PayrollPayout.Method.BANK_TRANSFER,
                    paid_at=timezone.localdate(),
                    actor=director,
                )
            except ValueError:
                outcomes.put("rejected")
            except BaseException as exc:
                outcomes.put(exc)
            else:
                outcomes.put("paid")
            finally:
                connection.close()

        threads = [Thread(target=record), Thread(target=record)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        results = [outcomes.get_nowait() for _ in range(2)]
        self.assertCountEqual(results, ["paid", "rejected"])
        self.assertEqual(PayrollPayout.objects.filter(payroll_sheet=self.sheet).count(), 1)
        self.assertEqual(
            PayrollSheetLifecycleEvent.objects.filter(
                payroll_sheet=self.sheet,
                event_type=PayrollSheetLifecycleEvent.EventType.PAID,
            ).count(),
            1,
        )
        self.sheet.refresh_from_db()
        self.assertEqual(self.sheet.status, PayrollSheet.Status.PAID)
