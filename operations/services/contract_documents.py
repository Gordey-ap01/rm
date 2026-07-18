"""Generate editable contract documents from structured contract records."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from typing import Protocol

from django.core.files.base import ContentFile
from django.utils import timezone
from docx import Document as WordDocument
from docx.document import Document as WordDocumentType

from operations.models import Document, DonationContract, ServiceContract

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_TEMPLATE_BYTES = 5 * 1024 * 1024
PLACEHOLDER_BLANK = "_______________"


class ContractDocumentError(RuntimeError):
    """Raised when a stored Word template cannot be used safely."""


class _ContractWithNumber(Protocol):
    pk: int | None
    number: str


@dataclass(frozen=True)
class PlaceholderGroup:
    title: str
    placeholders: tuple[str, ...]


@dataclass(frozen=True)
class GeneratedContractFile:
    payload: BytesIO
    filename: str
    document: Document | None = None


PLACEHOLDER_GROUPS = (
    PlaceholderGroup(
        "Центр",
        (
            "center.full_name",
            "center.short_name",
            "center.director_full_name",
            "center.director_short_name",
            "center.authority_basis",
            "center.license_number",
            "center.license_date",
            "center.license_authority",
            "center.ogrn",
            "center.inn",
            "center.kpp",
            "center.legal_address",
            "center.location_address",
            "center.phone",
            "center.email",
            "center.site",
            "center.bank_name",
            "center.bank_bik",
            "center.bank_account",
            "center.bank_corr_account",
        ),
    ),
    PlaceholderGroup(
        "Договор",
        (
            "contract.number",
            "contract.signed_on",
            "contract.valid_from",
            "contract.valid_until",
            "contract.validity",
            "contract.status",
            "contract.type",
            "contract.template",
            "contract.city",
            "contract.amount",
            "contract.amount_words",
            "contract.monthly_amount",
            "contract.payment_due_days",
        ),
    ),
    PlaceholderGroup(
        "Получатель",
        (
            "child.full_name",
            "child.birth_date",
            "child.phone",
            "child.email",
            "child.address",
        ),
    ),
    PlaceholderGroup(
        "Представитель",
        (
            "representative.full_name",
            "representative.relationship",
            "representative.phone",
            "representative.phone_alt",
            "representative.email",
            "representative.passport_series",
            "representative.passport_number",
            "representative.passport_issued_by",
            "representative.passport_issued_on",
            "representative.registration_address",
            "representative.signs_contract",
            "representative.receives_schedule",
            "representative.is_payer",
        ),
    ),
    PlaceholderGroup(
        "Контрагент",
        (
            "counterparty.name",
            "counterparty.type",
            "counterparty.inn",
            "counterparty.kpp",
            "counterparty.ogrn",
            "counterparty.legal_address",
            "counterparty.postal_address",
            "counterparty.bank_details",
            "counterparty.contact_person",
            "counterparty.phone",
            "counterparty.email",
            "counterparty.signer_full_name",
            "counterparty.signer_position",
            "counterparty.authority_basis",
            "counterparty.bank_name",
            "counterparty.bank_bik",
            "counterparty.bank_account",
            "counterparty.bank_corr_account",
        ),
    ),
    PlaceholderGroup(
        "Финансирование",
        (
            "funding_source.name",
            "funding_source.type",
            "funding_source.starts_on",
            "funding_source.ends_on",
            "funding_source.transfer_policy",
            "funding_source.project_name",
        ),
    ),
    PlaceholderGroup(
        "Пожертвование",
        (
            "donation.amount_limit",
            "donation.amount",
            "donation.monthly_amount",
            "donation.periodicity",
        ),
    ),
    PlaceholderGroup(
        "Спецификация услуг",
        (
            "service_spec.rows",
            "service_spec.service_name",
            "service_spec.quantity",
            "service_spec.unit",
            "service_spec.hours",
            "service_spec.price",
            "service_spec.amount",
            "service_spec.period",
        ),
    ),
    PlaceholderGroup(
        "Сертификат",
        (
            "certificate.type",
            "certificate.number",
            "certificate.total_amount",
            "certificate.remaining_amount",
            "certificate.valid_from",
            "certificate.valid_until",
            "certificate.payer_name",
        ),
    ),
)


def placeholder_reference_groups() -> list[PlaceholderGroup]:
    return list(PLACEHOLDER_GROUPS)


def _empty_placeholder_values() -> dict[str, str]:
    return {
        placeholder: PLACEHOLDER_BLANK
        for group in PLACEHOLDER_GROUPS
        for placeholder in group.placeholders
    }


def _safe_filename(prefix: str, contract: _ContractWithNumber) -> str:
    source = contract.number or str(contract.pk or "")
    suffix = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in source).strip("_")
    return f"{prefix}_{suffix or 'draft'}.docx"


def _date_label(value) -> str:
    if not value:
        return PLACEHOLDER_BLANK
    return value.strftime("%d.%m.%Y")


def _money_label(value: Decimal | None) -> str:
    if value is None:
        return "без лимита"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _text(value: object, fallback: str = PLACEHOLDER_BLANK) -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def _bool_label(value: bool | None) -> str:
    if value is None:
        return PLACEHOLDER_BLANK
    return "да" if value else "нет"


def _validity_label(valid_from, valid_until) -> str:
    if valid_from and valid_until:
        return f"{_date_label(valid_from)} - {_date_label(valid_until)}"
    if valid_from:
        return f"с {_date_label(valid_from)}"
    if valid_until:
        return f"до {_date_label(valid_until)}"
    return PLACEHOLDER_BLANK


def _template_label(template) -> str:
    if template is None:
        return "Базовый системный шаблон"
    version = f" v{template.version}" if template.version else ""
    return f"{template.title}{version}"


def service_contract_placeholders(contract: ServiceContract) -> dict[str, str]:
    signer_link = contract.representative_link
    signer = signer_link.representative
    values = _empty_placeholder_values()
    values.update(
        {
            "contract.number": _text(contract.number, "б/н"),
            "contract.signed_on": _date_label(contract.signed_on),
            "contract.valid_from": _date_label(contract.valid_from),
            "contract.valid_until": _date_label(contract.valid_until),
            "contract.validity": _validity_label(contract.valid_from, contract.valid_until),
            "contract.status": contract.get_status_display(),
            "contract.type": contract.get_contract_type_display(),
            "contract.template": _template_label(contract.template),
            "child.full_name": contract.child.full_name,
            "child.birth_date": _date_label(contract.child.birth_date),
            "child.phone": _text(contract.child.phone),
            "child.email": _text(contract.child.email),
            "representative.full_name": signer.full_name,
            "representative.relationship": signer_link.get_relationship_type_display(),
            "representative.phone": _text(signer.phone),
            "representative.phone_alt": _text(signer.phone_alt),
            "representative.email": _text(signer.email),
            "representative.signs_contract": _bool_label(signer_link.signs_contract),
            "representative.receives_schedule": _bool_label(signer_link.receives_schedule),
            "representative.is_payer": _bool_label(signer_link.is_payer),
        }
    )
    return values


def donation_contract_placeholders(contract: DonationContract) -> dict[str, str]:
    counterparty = contract.counterparty
    funding_source = contract.funding_source
    values = _empty_placeholder_values()
    values.update(
        {
            "contract.number": _text(contract.number, "б/н"),
            "contract.signed_on": _date_label(contract.signed_on),
            "contract.valid_from": _date_label(contract.valid_from),
            "contract.valid_until": _date_label(contract.valid_until),
            "contract.validity": _validity_label(contract.valid_from, contract.valid_until),
            "contract.status": contract.get_status_display(),
            "contract.type": contract.get_contract_type_display(),
            "contract.template": _template_label(contract.template),
            "counterparty.name": counterparty.name,
            "counterparty.type": counterparty.get_counterparty_type_display(),
            "counterparty.inn": _text(counterparty.inn, ""),
            "counterparty.kpp": _text(counterparty.kpp, ""),
            "counterparty.ogrn": _text(counterparty.ogrn, ""),
            "counterparty.legal_address": _text(counterparty.legal_address, ""),
            "counterparty.postal_address": _text(counterparty.postal_address, ""),
            "counterparty.bank_details": _text(counterparty.bank_details, ""),
            "counterparty.contact_person": _text(counterparty.contact_person),
            "counterparty.phone": _text(counterparty.phone),
            "counterparty.email": _text(counterparty.email),
            "funding_source.name": funding_source.name,
            "funding_source.type": funding_source.get_source_type_display(),
            "funding_source.starts_on": _date_label(funding_source.starts_on),
            "funding_source.ends_on": _date_label(funding_source.ends_on),
            "funding_source.transfer_policy": funding_source.get_transfer_policy_display(),
            "donation.amount_limit": _money_label(contract.amount_limit),
            "donation.amount": _money_label(contract.amount_limit),
        }
    )
    return values


def _replace_placeholders(text: str, values: dict[str, str]) -> str:
    result = text
    for key, value in values.items():
        result = result.replace("{{ " + key + " }}", value)
        result = result.replace("{{" + key + "}}", value)
    return result


def _replace_in_paragraph(paragraph, values: dict[str, str]) -> None:
    current = paragraph.text
    updated = _replace_placeholders(current, values)
    if updated == current:
        return
    if not paragraph.runs:
        paragraph.add_run(updated)
        return
    paragraph.runs[0].text = updated
    for run in paragraph.runs[1:]:
        run.text = ""


def _replace_in_table(table, values: dict[str, str]) -> None:
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                _replace_in_paragraph(paragraph, values)
            for nested_table in cell.tables:
                _replace_in_table(nested_table, values)


def _replace_in_document(document: WordDocumentType, values: dict[str, str]) -> None:
    for paragraph in document.paragraphs:
        _replace_in_paragraph(paragraph, values)
    for table in document.tables:
        _replace_in_table(table, values)
    for section in document.sections:
        for part in (section.header, section.footer):
            for paragraph in part.paragraphs:
                _replace_in_paragraph(paragraph, values)
            for table in part.tables:
                _replace_in_table(table, values)


def _load_template_document(contract) -> WordDocumentType | None:
    template = contract.template
    if template is None or not template.file:
        return None
    if template.file.size > MAX_TEMPLATE_BYTES:
        raise ContractDocumentError("Файл шаблона больше 5 МБ. Загрузите более компактный .docx.")
    try:
        template.file.open("rb")
        try:
            return WordDocument(template.file)
        finally:
            template.file.close()
    except Exception as exc:
        raise ContractDocumentError("Не удалось прочитать .docx шаблон договора.") from exc


def _service_fallback_document(contract: ServiceContract) -> WordDocumentType:
    values = service_contract_placeholders(contract)
    document = WordDocument()
    document.add_heading("Договор на оказание реабилитационных услуг", level=1)
    document.add_paragraph(f"№ {values['contract.number']} от {values['contract.signed_on']}")
    document.add_paragraph(f"Шаблон: {values['contract.template']}")

    table = document.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for label, value in [
        ("Получатель", values["child.full_name"]),
        ("Дата рождения", values["child.birth_date"]),
        ("Подписант", values["representative.full_name"]),
        ("Тип договора", values["contract.type"]),
        ("Статус реестра", values["contract.status"]),
        ("Срок действия", f"{values['contract.valid_from']} - {values['contract.valid_until']}"),
    ]:
        row = table.add_row()
        row.cells[0].text = label
        row.cells[1].text = value

    document.add_paragraph(
        "Стоимость услуг определяется действующим прейскурантом центра. "
        "Расписание, переносы и отмены ведутся в операционной системе центра."
    )
    document.add_paragraph("Подписи сторон:")
    document.add_paragraph("Представитель: ______________________ /_______________/")
    document.add_paragraph("Центр: ______________________________ /_______________/")
    return document


def _donation_fallback_document(contract: DonationContract) -> WordDocumentType:
    values = donation_contract_placeholders(contract)
    document = WordDocument()
    document.add_heading("Договор пожертвования", level=1)
    document.add_paragraph(f"№ {values['contract.number']} от {values['contract.signed_on']}")
    document.add_paragraph(f"Шаблон: {values['contract.template']}")

    table = document.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for label, value in [
        ("Жертвователь/спонсор", values["counterparty.name"]),
        ("ИНН", values["counterparty.inn"]),
        ("Источник финансирования", values["funding_source.name"]),
        ("Тип договора", values["contract.type"]),
        ("Статус реестра", values["contract.status"]),
        ("Лимит суммы", values["donation.amount_limit"]),
        ("Срок действия", f"{values['contract.valid_from']} - {values['contract.valid_until']}"),
    ]:
        row = table.add_row()
        row.cells[0].text = label
        row.cells[1].text = value

    document.add_paragraph(
        "Этот документ не создает автоматические платежи, балансы получателей, "
        "ledger-записи или начисления специалистам."
    )
    document.add_paragraph("Подписи сторон:")
    document.add_paragraph("Жертвователь/спонсор: ______________ /_______________/")
    document.add_paragraph("Центр: _____________________________ /_______________/")
    return document


def _render_document(document: WordDocumentType) -> BytesIO:
    payload = BytesIO()
    document.save(payload)
    payload.seek(0)
    return payload


def render_service_contract_docx(contract: ServiceContract) -> GeneratedContractFile:
    document = _load_template_document(contract)
    if document is None:
        document = _service_fallback_document(contract)
    else:
        _replace_in_document(document, service_contract_placeholders(contract))
    return GeneratedContractFile(
        payload=_render_document(document),
        filename=_safe_filename("service_contract", contract),
    )


def render_donation_contract_docx(contract: DonationContract) -> GeneratedContractFile:
    document = _load_template_document(contract)
    if document is None:
        document = _donation_fallback_document(contract)
    else:
        _replace_in_document(document, donation_contract_placeholders(contract))
    return GeneratedContractFile(
        payload=_render_document(document),
        filename=_safe_filename("donation_contract", contract),
    )


def save_service_contract_docx(contract: ServiceContract, *, actor=None) -> GeneratedContractFile:
    generated = render_service_contract_docx(contract)
    document = contract.document
    if document is not None:
        if document.child_id != contract.child_id:
            raise ContractDocumentError(
                "Связанный документ относится к другому получателю. Исправьте карточку договора."
            )
        if document.category != Document.Category.CONTRACT:
            raise ContractDocumentError(
                "Связанный документ должен быть категорией договора. Исправьте карточку договора."
            )
    else:
        document = Document(
            child=contract.child,
            category=Document.Category.CONTRACT,
        )

    document.title = _service_document_title(contract)
    document.issued_on = contract.signed_on or timezone.localdate()
    document.expires_on = contract.valid_until
    if actor is not None and getattr(actor, "is_authenticated", False):
        document.uploaded_by = actor
    document.note = "Сформирован автоматически из карточки договора и Word-шаблона."
    document.file.save(generated.filename, ContentFile(generated.payload.getvalue()), save=False)
    document.full_clean()
    document.save()

    if contract.document_id != document.pk:
        contract.document = document
        contract.save(update_fields=["document", "updated_at"])

    generated.payload.seek(0)
    return GeneratedContractFile(
        payload=generated.payload,
        filename=generated.filename,
        document=document,
    )


def _service_document_title(contract: ServiceContract) -> str:
    number = contract.number or "б/н"
    title = f"Договор {number} — {contract.child.full_name}"
    return title[:200]
