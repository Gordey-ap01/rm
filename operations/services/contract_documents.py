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


class ContractDocumentError(RuntimeError):
    """Raised when a stored Word template cannot be used safely."""


class _ContractWithNumber(Protocol):
    pk: int | None
    number: str


@dataclass(frozen=True)
class GeneratedContractFile:
    payload: BytesIO
    filename: str
    document: Document | None = None


def _safe_filename(prefix: str, contract: _ContractWithNumber) -> str:
    source = contract.number or str(contract.pk or "")
    suffix = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in source).strip("_")
    return f"{prefix}_{suffix or 'draft'}.docx"


def _date_label(value) -> str:
    if not value:
        return "_______________"
    return value.strftime("%d.%m.%Y")


def _money_label(value: Decimal | None) -> str:
    if value is None:
        return "без лимита"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _text(value: object, fallback: str = "_______________") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def _template_label(template) -> str:
    if template is None:
        return "Базовый системный шаблон"
    version = f" v{template.version}" if template.version else ""
    return f"{template.title}{version}"


def service_contract_placeholders(contract: ServiceContract) -> dict[str, str]:
    signer = contract.representative_link.representative
    return {
        "contract.number": _text(contract.number, "б/н"),
        "contract.signed_on": _date_label(contract.signed_on),
        "contract.valid_from": _date_label(contract.valid_from),
        "contract.valid_until": _date_label(contract.valid_until),
        "contract.status": contract.get_status_display(),
        "contract.type": contract.get_contract_type_display(),
        "contract.template": _template_label(contract.template),
        "child.full_name": contract.child.full_name,
        "child.birth_date": _date_label(contract.child.birth_date),
        "representative.full_name": signer.full_name,
    }


def donation_contract_placeholders(contract: DonationContract) -> dict[str, str]:
    counterparty = contract.counterparty
    return {
        "contract.number": _text(contract.number, "б/н"),
        "contract.signed_on": _date_label(contract.signed_on),
        "contract.valid_from": _date_label(contract.valid_from),
        "contract.valid_until": _date_label(contract.valid_until),
        "contract.status": contract.get_status_display(),
        "contract.type": contract.get_contract_type_display(),
        "contract.template": _template_label(contract.template),
        "counterparty.name": counterparty.name,
        "counterparty.inn": _text(counterparty.inn, ""),
        "counterparty.kpp": _text(counterparty.kpp, ""),
        "counterparty.ogrn": _text(counterparty.ogrn, ""),
        "counterparty.legal_address": _text(counterparty.legal_address, ""),
        "counterparty.postal_address": _text(counterparty.postal_address, ""),
        "counterparty.bank_details": _text(counterparty.bank_details, ""),
        "funding_source.name": contract.funding_source.name,
        "donation.amount_limit": _money_label(contract.amount_limit),
    }


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
