from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from operations.models import (
    Certificate,
    Child,
    Consent,
    ContractSignedFile,
    ContractTemplate,
    Counterparty,
    Document,
    DonationContract,
    FundingSource,
    LedgerEntry,
    OrganizationServiceContract,
    OrganizationServiceContractLine,
    ParentGuardian,
    Payment,
    RecipientRepresentative,
    Service,
    ServiceContract,
    ServiceContractLine,
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
        cls.service = Service.objects.create(
            name="Speech therapy",
            code="SPEECH",
            default_duration_minutes=30,
            default_price=Decimal("1500.00"),
        )
        cls.certificate = Certificate.objects.create(
            child=cls.child,
            certificate_type=Certificate.CertificateType.MATERNITY_CAPITAL,
            number="CERT-001",
            total_amount=Decimal("100000.00"),
            remaining_amount=Decimal("75000.00"),
        )
        cls.other_certificate = Certificate.objects.create(
            child=cls.other_child,
            certificate_type=Certificate.CertificateType.REGIONAL,
            number="CERT-OTHER",
            total_amount=Decimal("50000.00"),
            remaining_amount=Decimal("50000.00"),
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

    def test_donation_contract_accepts_project_template_family(self):
        project_template = ContractTemplate.objects.create(
            template_type=ContractTemplate.TemplateType.DONATION_PROJECT,
            title="Project donation template",
        )
        contract = DonationContract(
            counterparty=self.counterparty,
            funding_source=self.funding_source,
            template=project_template,
        )

        contract.full_clean()

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
            funding_source=self.funding_source,
            certificate=self.certificate,
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
        self.assertEqual(contract.funding_source, self.funding_source)
        self.assertEqual(contract.certificate, self.certificate)
        self.assertEqual(contract.document, self.contract_document)

    def test_service_contract_rejects_certificate_from_other_child(self):
        contract = ServiceContract(
            child=self.child,
            representative_link=self.signer_link,
            certificate=self.other_certificate,
        )

        with self.assertRaises(ValidationError) as raised:
            contract.full_clean()

        self.assertIn("certificate", raised.exception.message_dict)

    def test_service_contract_line_tracks_spec_without_financial_facts(self):
        ledger_count = LedgerEntry.objects.count()
        payment_count = Payment.objects.count()
        contract = ServiceContract.objects.create(
            child=self.child,
            representative_link=self.signer_link,
            funding_source=self.funding_source,
            number="S-SPEC",
            signed_on=self.today,
        )

        line = ServiceContractLine.objects.create(
            service_contract=contract,
            service=self.service,
            quantity=Decimal("10.00"),
            unit=ServiceContractLine.Unit.SESSION,
            unit_price=Decimal("1500.00"),
            starts_on=self.today,
            ends_on=self.today + timezone.timedelta(days=30),
            sort_order=1,
        )

        self.assertEqual(line.service_name, self.service.name)
        self.assertEqual(line.amount, Decimal("15000.00"))
        self.assertEqual(contract.service_lines_total_amount, Decimal("15000.00"))
        self.assertIn("Speech therapy", contract.service_lines_summary)
        self.assertEqual(LedgerEntry.objects.count(), ledger_count)
        self.assertEqual(Payment.objects.count(), payment_count)

    def test_service_contract_line_validates_quantity_price_and_dates(self):
        contract = ServiceContract.objects.create(
            child=self.child,
            representative_link=self.signer_link,
        )
        line = ServiceContractLine(
            service_contract=contract,
            service=self.service,
            quantity=Decimal("0.00"),
            unit_price=Decimal("-1.00"),
            starts_on=self.today,
            ends_on=self.today - timezone.timedelta(days=1),
        )

        with self.assertRaises(ValidationError) as raised:
            line.full_clean()

        self.assertIn("quantity", raised.exception.message_dict)
        self.assertIn("unit_price", raised.exception.message_dict)
        self.assertIn("ends_on", raised.exception.message_dict)

    def test_organization_service_contract_tracks_counterparty_spec_without_financial_facts(self):
        ledger_count = LedgerEntry.objects.count()
        payment_count = Payment.objects.count()
        template = ContractTemplate.objects.create(
            template_type=ContractTemplate.TemplateType.ORGANIZATION_SERVICE,
            title="Organization service template",
        )
        contract = OrganizationServiceContract.objects.create(
            counterparty=self.counterparty,
            funding_source=self.funding_source,
            contract_type=OrganizationServiceContract.ContractType.PROJECT,
            number="B2B-SPEC",
            signed_on=self.today,
            template=template,
        )

        line = OrganizationServiceContractLine.objects.create(
            organization_contract=contract,
            service=self.service,
            quantity=Decimal("12.00"),
            unit=OrganizationServiceContractLine.Unit.SESSION,
            unit_price=Decimal("500.00"),
            starts_on=self.today,
            ends_on=self.today + timezone.timedelta(days=60),
            sort_order=1,
        )

        self.assertEqual(line.service_name, self.service.name)
        self.assertEqual(line.amount, Decimal("6000.00"))
        self.assertEqual(contract.service_lines_total_amount, Decimal("6000.00"))
        self.assertIn("Speech therapy", contract.service_lines_summary)
        self.assertEqual(LedgerEntry.objects.count(), ledger_count)
        self.assertEqual(Payment.objects.count(), payment_count)

    def test_organization_service_contract_rejects_wrong_template_and_recipient_document(self):
        contract = OrganizationServiceContract(
            counterparty=self.counterparty,
            template=self.service_template,
            document=self.contract_document,
        )

        with self.assertRaises(ValidationError) as raised:
            contract.full_clean()

        self.assertIn("template", raised.exception.message_dict)
        self.assertIn("document", raised.exception.message_dict)

    def test_consent_rejects_wrong_signatory_template_and_document(self):
        consent_template = ContractTemplate.objects.create(
            template_type=ContractTemplate.TemplateType.DONATION_ONE_TIME,
            title="Wrong consent template",
        )
        other_consent_document = Document.objects.create(
            child=self.other_child,
            category=Document.Category.CONSENT,
            title="Other child consent",
            file="documents/other-consent.txt",
        )
        consent = Consent(
            child=self.child,
            consent_type=Consent.ConsentType.PHOTO_VIDEO,
            signatory_representative=self.other_signer_link,
            template=consent_template,
            document=other_consent_document,
            signed_on=self.today,
            expires_on=self.today + timezone.timedelta(days=30),
        )

        with self.assertRaises(ValidationError) as raised:
            consent.full_clean()

        self.assertIn("signatory_representative", raised.exception.message_dict)
        self.assertIn("template", raised.exception.message_dict)
        self.assertIn("document", raised.exception.message_dict)

    def test_consent_accepts_photo_video_template_and_recipient_document(self):
        template = ContractTemplate.objects.create(
            template_type=ContractTemplate.TemplateType.CONSENT_PHOTO_VIDEO,
            title="Photo consent template",
        )
        document = Document.objects.create(
            child=self.child,
            category=Document.Category.CONSENT,
            title="Photo consent file",
            file="documents/photo-consent.txt",
        )
        consent = Consent(
            child=self.child,
            consent_type=Consent.ConsentType.PHOTO_VIDEO,
            signatory_representative=self.signer_link,
            template=template,
            document=document,
            signed_on=self.today,
            expires_on=self.today + timezone.timedelta(days=30),
        )

        consent.full_clean()

    def test_service_contract_accepts_recipient_template_families(self):
        for template_type in (
            ContractTemplate.TemplateType.RECIPIENT_FREE_SERVICE,
            ContractTemplate.TemplateType.RECIPIENT_CARE,
            ContractTemplate.TemplateType.RECIPIENT_CERTIFICATE,
        ):
            with self.subTest(template_type=template_type):
                template = ContractTemplate.objects.create(
                    template_type=template_type,
                    title=f"Template {template_type}",
                )
                contract = ServiceContract(
                    child=self.child,
                    representative_link=self.signer_link,
                    template=template,
                )

                contract.full_clean()

    def test_service_contract_rejects_future_non_recipient_template_family(self):
        template = ContractTemplate.objects.create(
            template_type=ContractTemplate.TemplateType.ORGANIZATION_SERVICE,
            title="B2B template",
        )
        contract = ServiceContract(
            child=self.child,
            representative_link=self.signer_link,
            template=template,
        )

        with self.assertRaises(ValidationError) as raised:
            contract.full_clean()

        self.assertIn("template", raised.exception.message_dict)

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

    def test_contract_signed_file_requires_matching_contract_kind(self):
        contract = ServiceContract.objects.create(
            child=self.child,
            representative_link=self.signer_link,
            number="S-SIGNED-KIND",
            signed_on=self.today,
        )
        signed_file = ContractSignedFile(
            contract_kind=ContractSignedFile.ContractKind.DONATION,
            service_contract=contract,
            source_document=self.contract_document,
            file="contract_signed_files/service.docx",
            original_filename="service.docx",
            file_size=10,
            file_sha256="a" * 64,
            signed_on=self.today,
        )

        with self.assertRaises(ValidationError) as raised:
            signed_file.full_clean()

        self.assertIn("donation_contract", raised.exception.message_dict)
        self.assertIn("service_contract", raised.exception.message_dict)

    def test_contract_signed_file_is_immutable_after_creation(self):
        contract = ServiceContract.objects.create(
            child=self.child,
            representative_link=self.signer_link,
            number="S-SIGNED-IMMUTABLE",
            signed_on=self.today,
        )
        signed_file = ContractSignedFile.objects.create(
            contract_kind=ContractSignedFile.ContractKind.SERVICE,
            service_contract=contract,
            source_document=self.contract_document,
            file="contract_signed_files/service.docx",
            original_filename="service.docx",
            file_size=10,
            file_sha256="a" * 64,
            signed_on=self.today,
        )

        signed_file.original_filename = "changed.docx"

        with self.assertRaises(ValidationError):
            signed_file.save()
