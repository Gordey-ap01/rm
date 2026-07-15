from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from operations.models import (
    Child,
    ContractTemplate,
    Counterparty,
    Document,
    DonationContract,
    FundingSource,
    LedgerEntry,
    ParentGuardian,
    Payment,
    RecipientRepresentative,
    ServiceContract,
)


class ContractRegistryValidationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.counterparty = Counterparty.objects.create(
            name="Sponsor org",
            counterparty_type=Counterparty.CounterpartyType.SPONSOR,
        )
        cls.funding_source = FundingSource.objects.create(
            name="Sponsor source",
            source_type=FundingSource.SourceType.SPONSOR,
        )
        cls.parent = ParentGuardian.objects.create(
            last_name="Signer",
            first_name="Parent",
            phone="+7 900 000-00-01",
        )
        cls.child = Child.objects.create(
            last_name="Recipient",
            first_name="One",
            primary_parent=cls.parent,
        )
        cls.signer_link = RecipientRepresentative.objects.get(
            child=cls.child,
            representative=cls.parent,
        )
        cls.other_parent = ParentGuardian.objects.create(
            last_name="Other",
            first_name="Parent",
            phone="+7 900 000-00-02",
        )
        cls.other_child = Child.objects.create(
            last_name="Recipient",
            first_name="Two",
            primary_parent=cls.other_parent,
        )
        cls.other_signer_link = RecipientRepresentative.objects.get(
            child=cls.other_child,
            representative=cls.other_parent,
        )
        cls.service_template = ContractTemplate.objects.create(
            template_type=ContractTemplate.TemplateType.RECIPIENT_SERVICE,
            title="Service template",
            version="1",
        )
        cls.donation_template = ContractTemplate.objects.create(
            template_type=ContractTemplate.TemplateType.SPONSOR,
            title="Sponsor template",
            version="1",
        )
        cls.contract_document = Document.objects.create(
            child=cls.child,
            category=Document.Category.CONTRACT,
            title="Service contract file",
            file="documents/service-contract.txt",
        )
        cls.other_document = Document.objects.create(
            child=cls.other_child,
            category=Document.Category.CONTRACT,
            title="Other contract file",
            file="documents/other-contract.txt",
        )
        cls.medical_document = Document.objects.create(
            child=cls.child,
            category=Document.Category.MEDICAL_REPORT,
            title="Medical file",
            file="documents/medical.txt",
        )

    def test_donation_contract_can_exist_without_file_and_does_not_create_financial_facts(self):
        ledger_count = LedgerEntry.objects.count()
        payment_count = Payment.objects.count()

        contract = DonationContract.objects.create(
            counterparty=self.counterparty,
            funding_source=self.funding_source,
            contract_type=DonationContract.ContractType.PROJECT,
            number="D-001",
            signed_on=self.today,
            valid_from=self.today,
            amount_limit=Decimal("100000.00"),
            status=DonationContract.Status.ACTIVE,
            template=self.donation_template,
        )

        self.assertIsNone(contract.document_id)
        self.assertEqual(contract.funding_source, self.funding_source)
        self.assertEqual(LedgerEntry.objects.count(), ledger_count)
        self.assertEqual(Payment.objects.count(), payment_count)

    def test_donation_contract_rejects_wrong_template_and_non_contract_document(self):
        contract = DonationContract(
            counterparty=self.counterparty,
            funding_source=self.funding_source,
            template=self.service_template,
            document=self.medical_document,
        )

        with self.assertRaises(ValidationError) as raised:
            contract.full_clean()

        self.assertIn("template", raised.exception.message_dict)
        self.assertIn("document", raised.exception.message_dict)

    def test_contract_dates_and_amount_are_validated(self):
        contract = DonationContract(
            counterparty=self.counterparty,
            funding_source=self.funding_source,
            valid_from=self.today,
            valid_until=self.today - timezone.timedelta(days=1),
            amount_limit=Decimal("-1.00"),
        )

        with self.assertRaises(ValidationError) as raised:
            contract.full_clean()

        self.assertIn("valid_until", raised.exception.message_dict)
        self.assertIn("amount_limit", raised.exception.message_dict)

    def test_donation_contract_number_date_unique_within_type(self):
        DonationContract.objects.create(
            counterparty=self.counterparty,
            funding_source=self.funding_source,
            contract_type=DonationContract.ContractType.ONE_TIME,
            number="D-002",
            signed_on=self.today,
        )

        with self.assertRaises(IntegrityError):
            DonationContract.objects.create(
                counterparty=self.counterparty,
                funding_source=self.funding_source,
                contract_type=DonationContract.ContractType.ONE_TIME,
                number="D-002",
                signed_on=self.today,
            )

    def test_service_contract_links_child_signer_template_and_document(self):
        contract = ServiceContract.objects.create(
            child=self.child,
            representative_link=self.signer_link,
            contract_type=ServiceContract.ContractType.STANDARD,
            number="S-001",
            signed_on=self.today,
            valid_from=self.today,
            status=ServiceContract.Status.ACTIVE,
            template=self.service_template,
            document=self.contract_document,
        )

        self.assertEqual(contract.child, self.child)
        self.assertEqual(contract.representative_link, self.signer_link)
        self.assertEqual(contract.document, self.contract_document)

    def test_service_contract_rejects_signer_from_other_child(self):
        contract = ServiceContract(
            child=self.child,
            representative_link=self.other_signer_link,
        )

        with self.assertRaises(ValidationError) as raised:
            contract.full_clean()

        self.assertIn("representative_link", raised.exception.message_dict)

    def test_service_contract_rejects_non_signer_and_wrong_child_document(self):
        non_signer = RecipientRepresentative.objects.create(
            child=self.child,
            representative=self.other_parent,
            signs_contract=False,
            receives_schedule=True,
        )
        contract = ServiceContract(
            child=self.child,
            representative_link=non_signer,
            document=self.other_document,
        )

        with self.assertRaises(ValidationError) as raised:
            contract.full_clean()

        self.assertIn("representative_link", raised.exception.message_dict)
        self.assertIn("document", raised.exception.message_dict)
