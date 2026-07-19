"""Safe preview import for recipients and representatives."""

from __future__ import annotations

import csv
import hashlib
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from pathlib import PurePosixPath
from xml.etree import ElementTree

from django.db.models import Q

from operations.models import (
    CenterExpense,
    CenterExpenseCategory,
    Certificate,
    Child,
    ContractTemplate,
    Counterparty,
    DonationContract,
    FundingSource,
    ImportBatch,
    ImportBatchRow,
    RecipientRepresentative,
    ServiceContract,
)

MAX_IMPORT_ROWS = 200
XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass(frozen=True)
class ImportColumn:
    key: str
    label: str
    required: bool = False


@dataclass(frozen=True)
class ImportRowPreview:
    row_number: int
    values: dict[str, str]
    errors: list[str]
    warnings: list[str]

    @property
    def is_valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class ImportPreview:
    filename: str
    columns: list[ImportColumn]
    mapped_headers: dict[str, str]
    missing_required_headers: list[str]
    rows: list[ImportRowPreview]
    total_rows: int
    truncated: bool = False
    source_sha256: str = ""

    @property
    def valid_count(self) -> int:
        return sum(1 for row in self.rows if row.is_valid)

    @property
    def invalid_count(self) -> int:
        return sum(1 for row in self.rows if not row.is_valid)

    @property
    def warning_count(self) -> int:
        return sum(1 for row in self.rows if row.warnings)

    @property
    def column_rows(self) -> list[dict[str, str | bool]]:
        return [
            {
                "label": column.label,
                "required": column.required,
                "header": self.mapped_headers.get(column.key, ""),
            }
            for column in self.columns
        ]


@dataclass(frozen=True)
class ImportSpec:
    key: str
    label: str
    columns: list[ImportColumn]
    aliases: dict[str, list[str]]


RECIPIENT_COLUMNS = [
    ImportColumn("recipient_last_name", "Фамилия получателя", required=True),
    ImportColumn("recipient_first_name", "Имя получателя", required=True),
    ImportColumn("recipient_middle_name", "Отчество получателя"),
    ImportColumn("birth_date", "Дата рождения"),
    ImportColumn("representative_last_name", "Фамилия представителя"),
    ImportColumn("representative_first_name", "Имя представителя"),
    ImportColumn("representative_phone", "Телефон представителя"),
    ImportColumn("representative_email", "Email представителя"),
    ImportColumn("representative_relationship", "Кем приходится"),
    ImportColumn("is_primary", "Основной представитель"),
    ImportColumn("receives_schedule", "Получает расписание"),
    ImportColumn("notes", "Примечание"),
]


ALIASES = {
    "recipient_last_name": [
        "фамилия получателя",
        "фамилия ребенка",
        "фамилия ребёнка",
        "получатель фамилия",
        "ребенок фамилия",
        "ребёнок фамилия",
        "child_last_name",
        "recipient_last_name",
    ],
    "recipient_first_name": [
        "имя получателя",
        "имя ребенка",
        "имя ребёнка",
        "получатель имя",
        "ребенок имя",
        "ребёнок имя",
        "child_first_name",
        "recipient_first_name",
    ],
    "recipient_middle_name": [
        "отчество получателя",
        "отчество ребенка",
        "отчество ребёнка",
        "child_middle_name",
        "recipient_middle_name",
    ],
    "birth_date": [
        "дата рождения",
        "др",
        "дата рожд",
        "birth_date",
        "date_of_birth",
    ],
    "representative_last_name": [
        "фамилия представителя",
        "фамилия родителя",
        "представитель фамилия",
        "родитель фамилия",
        "representative_last_name",
        "parent_last_name",
    ],
    "representative_first_name": [
        "имя представителя",
        "имя родителя",
        "представитель имя",
        "родитель имя",
        "representative_first_name",
        "parent_first_name",
    ],
    "representative_phone": [
        "телефон представителя",
        "телефон родителя",
        "телефон",
        "representative_phone",
        "parent_phone",
        "phone",
    ],
    "representative_email": [
        "email представителя",
        "почта представителя",
        "email родителя",
        "почта родителя",
        "email",
        "representative_email",
        "parent_email",
    ],
    "representative_relationship": [
        "кем приходится",
        "родство",
        "тип представителя",
        "relationship",
        "representative_relationship",
    ],
    "is_primary": [
        "основной представитель",
        "подписант договора",
        "основной",
        "is_primary",
    ],
    "receives_schedule": [
        "получает расписание",
        "отправлять расписание",
        "рассылка расписания",
        "receives_schedule",
    ],
    "notes": ["примечание", "комментарий", "notes", "note"],
}


def _normalize_header(value: str) -> str:
    value = value.strip().lower().replace("ё", "е")
    value = re.sub(r"[_\-./]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value


ALIAS_TO_KEY = {
    _normalize_header(alias): key for key, aliases in ALIASES.items() for alias in aliases
}


COUNTERPARTY_IMPORT = "counterparties"
EXPENSE_IMPORT = "expenses"
DONATION_CONTRACT_IMPORT = "donation_contracts"
SERVICE_CONTRACT_IMPORT = "service_contracts"
CERTIFICATE_IMPORT = "certificates"

COUNTERPARTY_COLUMNS = [
    ImportColumn("name", "Контрагент", required=True),
    ImportColumn("counterparty_type", "Тип контрагента"),
    ImportColumn("inn", "ИНН"),
    ImportColumn("kpp", "КПП"),
    ImportColumn("ogrn", "ОГРН/ОГРНИП"),
    ImportColumn("contact_person", "Контактное лицо"),
    ImportColumn("phone", "Телефон"),
    ImportColumn("email", "Email"),
    ImportColumn("notes", "Примечание"),
]

EXPENSE_COLUMNS = [
    ImportColumn("expense_date", "Дата расхода", required=True),
    ImportColumn("category", "Категория", required=True),
    ImportColumn("title", "Название", required=True),
    ImportColumn("total_amount", "Сумма", required=True),
    ImportColumn("counterparty", "Контрагент"),
    ImportColumn("funding_source", "Источник финансирования"),
    ImportColumn("split_amount", "Сумма источника"),
    ImportColumn("status", "Статус"),
    ImportColumn("paid_at", "Дата оплаты"),
    ImportColumn("notes", "Примечание"),
]

DONATION_CONTRACT_COLUMNS = [
    ImportColumn("counterparty", "Контрагент", required=True),
    ImportColumn("funding_source", "Источник финансирования", required=True),
    ImportColumn("contract_type", "Тип договора"),
    ImportColumn("number", "Номер"),
    ImportColumn("signed_on", "Дата подписания"),
    ImportColumn("valid_from", "Действует с"),
    ImportColumn("valid_until", "Действует до"),
    ImportColumn("amount_limit", "Лимит суммы"),
    ImportColumn("status", "Статус"),
    ImportColumn("template", "Шаблон"),
    ImportColumn("notes", "Примечание"),
]

SERVICE_CONTRACT_COLUMNS = [
    ImportColumn("recipient_last_name", "Фамилия получателя", required=True),
    ImportColumn("recipient_first_name", "Имя получателя", required=True),
    ImportColumn("signer_last_name", "Фамилия подписанта", required=True),
    ImportColumn("signer_first_name", "Имя подписанта"),
    ImportColumn("signer_phone", "Телефон подписанта"),
    ImportColumn("contract_type", "Тип договора"),
    ImportColumn("number", "Номер"),
    ImportColumn("signed_on", "Дата подписания"),
    ImportColumn("valid_from", "Действует с"),
    ImportColumn("valid_until", "Действует до"),
    ImportColumn("status", "Статус"),
    ImportColumn("template", "Шаблон"),
    ImportColumn("document_title", "Документ"),
    ImportColumn("notes", "Примечание"),
]

CERTIFICATE_COLUMNS = [
    ImportColumn("recipient_last_name", "Фамилия получателя", required=True),
    ImportColumn("recipient_first_name", "Имя получателя", required=True),
    ImportColumn("recipient_middle_name", "Отчество получателя"),
    ImportColumn("birth_date", "Дата рождения"),
    ImportColumn("certificate_type", "Тип сертификата", required=True),
    ImportColumn("number", "Номер"),
    ImportColumn("total_amount", "Полная сумма", required=True),
    ImportColumn("remaining_amount", "Остаток", required=True),
    ImportColumn("valid_from", "Действует с"),
    ImportColumn("valid_until", "Действует до"),
    ImportColumn("funding_source", "Источник финансирования"),
    ImportColumn("payer_last_name", "Фамилия плательщика"),
    ImportColumn("payer_first_name", "Имя плательщика"),
    ImportColumn("payer_phone", "Телефон плательщика"),
    ImportColumn("payer_name", "Плательщик вручную"),
    ImportColumn("notes", "Примечание"),
]

COUNTERPARTY_ALIASES = {
    "name": ["контрагент", "наименование", "название", "name", "counterparty"],
    "counterparty_type": ["тип контрагента", "тип", "counterparty_type", "type"],
    "inn": ["инн", "inn"],
    "kpp": ["кпп", "kpp"],
    "ogrn": ["огрн", "огрнип", "ogrn"],
    "contact_person": ["контактное лицо", "контакт", "contact_person"],
    "phone": ["телефон", "phone"],
    "email": ["email", "почта"],
    "notes": ["примечание", "комментарий", "notes", "note"],
}

EXPENSE_ALIASES = {
    "expense_date": ["дата расхода", "дата", "expense_date", "date"],
    "category": ["категория", "категория расхода", "category"],
    "title": ["название", "расход", "title", "expense"],
    "total_amount": ["сумма", "сумма расхода", "total_amount", "amount"],
    "counterparty": ["контрагент", "поставщик", "counterparty", "vendor"],
    "funding_source": ["источник финансирования", "источник", "funding_source", "funding"],
    "split_amount": ["сумма источника", "сумма распределения", "split_amount"],
    "status": ["статус", "status"],
    "paid_at": ["дата оплаты", "оплачено", "paid_at"],
    "notes": ["примечание", "комментарий", "notes", "note"],
}

DONATION_CONTRACT_ALIASES = {
    "counterparty": ["контрагент", "донор", "спонсор", "counterparty", "donor"],
    "funding_source": ["источник финансирования", "источник", "funding_source", "funding"],
    "contract_type": ["тип договора", "тип", "contract_type"],
    "number": ["номер", "номер договора", "number"],
    "signed_on": ["дата подписания", "подписан", "signed_on"],
    "valid_from": ["действует с", "valid_from"],
    "valid_until": ["действует до", "valid_until"],
    "amount_limit": ["лимит суммы", "лимит", "amount_limit"],
    "status": ["статус", "status"],
    "template": ["шаблон", "template"],
    "notes": ["примечание", "комментарий", "notes", "note"],
}

SERVICE_CONTRACT_ALIASES = {
    "recipient_last_name": [
        "фамилия получателя",
        "фамилия ребенка",
        "фамилия ребёнка",
        "recipient_last_name",
    ],
    "recipient_first_name": [
        "имя получателя",
        "имя ребенка",
        "имя ребёнка",
        "recipient_first_name",
    ],
    "signer_last_name": ["фамилия подписанта", "подписант фамилия", "signer_last_name"],
    "signer_first_name": ["имя подписанта", "подписант имя", "signer_first_name"],
    "signer_phone": ["телефон подписанта", "телефон представителя", "signer_phone"],
    "contract_type": ["тип договора", "тип", "contract_type"],
    "number": ["номер", "номер договора", "number"],
    "signed_on": ["дата подписания", "подписан", "signed_on"],
    "valid_from": ["действует с", "valid_from"],
    "valid_until": ["действует до", "valid_until"],
    "status": ["статус", "status"],
    "template": ["шаблон", "template"],
    "document_title": ["документ", "файл договора", "document", "document_title"],
    "notes": ["примечание", "комментарий", "notes", "note"],
}

CERTIFICATE_ALIASES = {
    "recipient_last_name": [
        "фамилия получателя",
        "фамилия ребенка",
        "фамилия ребёнка",
        "recipient_last_name",
    ],
    "recipient_first_name": [
        "имя получателя",
        "имя ребенка",
        "имя ребёнка",
        "recipient_first_name",
    ],
    "recipient_middle_name": [
        "отчество получателя",
        "отчество ребенка",
        "отчество ребёнка",
        "recipient_middle_name",
    ],
    "birth_date": ["дата рождения", "др", "birth_date"],
    "certificate_type": [
        "тип сертификата",
        "тип",
        "сертификат",
        "certificate_type",
    ],
    "number": ["номер", "номер сертификата", "number"],
    "total_amount": ["полная сумма", "сумма", "total_amount", "amount"],
    "remaining_amount": ["остаток", "остаток сертификата", "remaining_amount", "remaining"],
    "valid_from": ["действует с", "valid_from"],
    "valid_until": ["действует до", "valid_until"],
    "funding_source": ["источник финансирования", "источник", "funding_source", "funding"],
    "payer_last_name": ["фамилия плательщика", "плательщик фамилия", "payer_last_name"],
    "payer_first_name": ["имя плательщика", "плательщик имя", "payer_first_name"],
    "payer_phone": ["телефон плательщика", "телефон", "payer_phone"],
    "payer_name": ["плательщик вручную", "плательщик", "payer_name"],
    "notes": ["примечание", "комментарий", "notes", "note"],
}

FINANCE_CONTRACT_IMPORT_SPECS = {
    COUNTERPARTY_IMPORT: ImportSpec(
        key=COUNTERPARTY_IMPORT,
        label="Контрагенты",
        columns=COUNTERPARTY_COLUMNS,
        aliases=COUNTERPARTY_ALIASES,
    ),
    EXPENSE_IMPORT: ImportSpec(
        key=EXPENSE_IMPORT,
        label="Расходы",
        columns=EXPENSE_COLUMNS,
        aliases=EXPENSE_ALIASES,
    ),
    DONATION_CONTRACT_IMPORT: ImportSpec(
        key=DONATION_CONTRACT_IMPORT,
        label="Договоры пожертвования",
        columns=DONATION_CONTRACT_COLUMNS,
        aliases=DONATION_CONTRACT_ALIASES,
    ),
    SERVICE_CONTRACT_IMPORT: ImportSpec(
        key=SERVICE_CONTRACT_IMPORT,
        label="Договоры с получателями",
        columns=SERVICE_CONTRACT_COLUMNS,
        aliases=SERVICE_CONTRACT_ALIASES,
    ),
    CERTIFICATE_IMPORT: ImportSpec(
        key=CERTIFICATE_IMPORT,
        label="Сертификаты",
        columns=CERTIFICATE_COLUMNS,
        aliases=CERTIFICATE_ALIASES,
    ),
}

FINANCE_CONTRACT_IMPORT_CHOICES = [
    (COUNTERPARTY_IMPORT, FINANCE_CONTRACT_IMPORT_SPECS[COUNTERPARTY_IMPORT].label),
    (EXPENSE_IMPORT, FINANCE_CONTRACT_IMPORT_SPECS[EXPENSE_IMPORT].label),
    (
        DONATION_CONTRACT_IMPORT,
        FINANCE_CONTRACT_IMPORT_SPECS[DONATION_CONTRACT_IMPORT].label,
    ),
    (
        SERVICE_CONTRACT_IMPORT,
        FINANCE_CONTRACT_IMPORT_SPECS[SERVICE_CONTRACT_IMPORT].label,
    ),
    (CERTIFICATE_IMPORT, FINANCE_CONTRACT_IMPORT_SPECS[CERTIFICATE_IMPORT].label),
]


def _read_csv_rows(data: bytes, filename: str) -> list[list[str]]:
    text = None
    for encoding in ("utf-8-sig", "cp1251", "utf-8"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = data.decode("utf-8", errors="replace")

    sample = text[:4096]
    delimiter = "\t" if filename.lower().endswith(".tsv") else ";"
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel()
        dialect.delimiter = delimiter
    return [list(row) for row in csv.reader(StringIO(text), dialect)]


def _column_index(cell_reference: str) -> int | None:
    letters = "".join(ch for ch in cell_reference if ch.isalpha())
    if not letters:
        return None
    value = 0
    for letter in letters.upper():
        value = value * 26 + ord(letter) - ord("A") + 1
    return value - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        data = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(data)
    strings: list[str] = []
    for item in root.findall(f".//{{{XLSX_MAIN_NS}}}si"):
        text_parts = [
            text_node.text or "" for text_node in item.findall(f".//{{{XLSX_MAIN_NS}}}t")
        ]
        strings.append("".join(text_parts))
    return strings


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    if "xl/workbook.xml" not in archive.namelist():
        return "xl/worksheets/sheet1.xml"
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    first_sheet = workbook.find(f".//{{{XLSX_MAIN_NS}}}sheet")
    if first_sheet is None:
        return "xl/worksheets/sheet1.xml"
    rel_id = first_sheet.attrib.get(f"{{{XLSX_REL_NS}}}id")
    if not rel_id or "xl/_rels/workbook.xml.rels" not in archive.namelist():
        return "xl/worksheets/sheet1.xml"

    rels_root = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for rel in rels_root.findall(f".//{{{PACKAGE_REL_NS}}}Relationship"):
        if rel.attrib.get("Id") != rel_id:
            continue
        target = rel.attrib.get("Target", "worksheets/sheet1.xml").lstrip("/")
        path = PurePosixPath(target)
        if path.parts and path.parts[0] == "xl":
            return str(path)
        return str(PurePosixPath("xl") / path)
    return "xl/worksheets/sheet1.xml"


def _cell_text(cell, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(
            text_node.text or "" for text_node in cell.findall(f".//{{{XLSX_MAIN_NS}}}t")
        )
    value_node = cell.find(f"{{{XLSX_MAIN_NS}}}v")
    if value_node is None or value_node.text is None:
        return ""
    raw = value_node.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (IndexError, ValueError):
            return raw
    return raw


def _read_xlsx_rows(data: bytes) -> list[list[str]]:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        shared = _shared_strings(archive)
        sheet_path = _first_sheet_path(archive)
        root = ElementTree.fromstring(archive.read(sheet_path))
        rows: list[list[str]] = []
        for row in root.findall(f".//{{{XLSX_MAIN_NS}}}sheetData/{{{XLSX_MAIN_NS}}}row"):
            values: list[str] = []
            for cell in row.findall(f"{{{XLSX_MAIN_NS}}}c"):
                index = _column_index(cell.attrib.get("r", "")) or len(values)
                while len(values) <= index:
                    values.append("")
                values[index] = _cell_text(cell, shared).strip()
            while values and not values[-1]:
                values.pop()
            rows.append(values)
        return rows


def _read_rows(data: bytes, filename: str) -> list[list[str]]:
    lower = filename.lower()
    if lower.endswith(".xlsx"):
        return _read_xlsx_rows(data)
    if lower.endswith((".csv", ".tsv")):
        return _read_csv_rows(data, filename)
    raise ValueError("Поддерживаются только файлы .xlsx, .csv или .tsv.")


def _parse_birth_date(value: str) -> tuple[date | None, str]:
    value = value.strip()
    if not value:
        return None, ""
    if re.fullmatch(r"\d+(\.0)?", value):
        serial = int(float(value))
        if 1 <= serial <= 80000:
            return date(1899, 12, 30) + timedelta(days=serial), ""
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(value, fmt).date(), ""
        except ValueError:
            continue
    return None, "Дата рождения должна быть в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД."


def _parse_date(value: str, label: str) -> tuple[date | None, str]:
    value = value.strip()
    if not value:
        return None, ""
    if re.fullmatch(r"\d+(\.0)?", value):
        serial = int(float(value))
        if 1 <= serial <= 80000:
            return date(1899, 12, 30) + timedelta(days=serial), ""
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(value, fmt).date(), ""
        except ValueError:
            continue
    return None, f"{label}: дата должна быть в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД."


def _parse_decimal(value: str, label: str) -> tuple[Decimal | None, str]:
    value = value.strip()
    if not value:
        return None, ""
    normalized = value.replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        amount = Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None, f"{label}: укажите число."
    return amount, ""


def _parse_bool(value: str) -> bool | None:
    normalized = _normalize_header(value)
    if not normalized:
        return None
    if normalized in {"1", "да", "д", "true", "yes", "y", "истина"}:
        return True
    if normalized in {"0", "нет", "н", "false", "no", "n", "ложь"}:
        return False
    return None


def _row_value(raw_row: list[str], index: int) -> str:
    return raw_row[index].strip() if index < len(raw_row) and raw_row[index] else ""


def _is_empty_row(row: list[str]) -> bool:
    return not any(cell.strip() for cell in row if cell)


def _choice_values_by_normalized_label(choices) -> dict[str, str]:
    values: dict[str, str] = {}
    for value, label in choices:
        values[_normalize_header(str(value))] = str(value)
        values[_normalize_header(str(label))] = str(value)
    return values


def _choice_error(value: str, choices, label: str) -> str:
    if not value:
        return ""
    allowed = _choice_values_by_normalized_label(choices)
    if _normalize_header(value) in allowed:
        return ""
    labels = ", ".join(choice_label for _, choice_label in choices)
    return f"{label}: допустимые значения - {labels}."


def _build_header_mapping(
    headers: list[str], aliases: dict[str, list[str]]
) -> tuple[dict[int, str], dict[str, str]]:
    alias_to_key = {
        _normalize_header(alias): key for key, key_aliases in aliases.items() for alias in key_aliases
    }
    index_to_key: dict[int, str] = {}
    mapped_headers: dict[str, str] = {}
    for index, header in enumerate(headers):
        key = alias_to_key.get(_normalize_header(header))
        if key and key not in mapped_headers:
            index_to_key[index] = key
            mapped_headers[key] = header
    return index_to_key, mapped_headers


def _validate_counterparty_row(values: dict[str, str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    type_error = _choice_error(
        values["counterparty_type"],
        Counterparty.CounterpartyType.choices,
        "Тип контрагента",
    )
    if type_error:
        errors.append(type_error)
    if values["email"] and "@" not in values["email"]:
        errors.append("Email выглядит некорректно.")
    if values["name"] and Counterparty.all_objects.filter(name__iexact=values["name"]).exists():
        warnings.append("Контрагент с таким названием уже есть в базе.")
    return errors, warnings


def _validate_expense_row(values: dict[str, str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    _, date_error = _parse_date(values["expense_date"], "Дата расхода")
    if date_error:
        errors.append(date_error)
    amount, amount_error = _parse_decimal(values["total_amount"], "Сумма")
    if amount_error:
        errors.append(amount_error)
    elif amount is not None and amount <= 0:
        errors.append("Сумма должна быть положительной.")
    paid_at = values["paid_at"]
    if paid_at:
        _, paid_at_error = _parse_date(paid_at, "Дата оплаты")
        if paid_at_error:
            errors.append(paid_at_error)
    if values["status"]:
        status_error = _choice_error(values["status"], CenterExpense.Status.choices, "Статус")
        if status_error:
            errors.append(status_error)
    if values["category"] and not CenterExpenseCategory.objects.filter(
        name__iexact=values["category"]
    ).exists():
        errors.append("Категория расхода не найдена в справочнике.")
    if values["counterparty"] and not Counterparty.all_objects.filter(
        name__iexact=values["counterparty"]
    ).exists():
        warnings.append("Контрагент не найден: перед импортом нужна карточка контрагента.")
    split_amount = values["split_amount"]
    if split_amount:
        split, split_error = _parse_decimal(split_amount, "Сумма источника")
        if split_error:
            errors.append(split_error)
        elif split is not None and split <= 0:
            errors.append("Сумма источника должна быть положительной.")
        if not values["funding_source"]:
            errors.append("Для суммы источника нужно указать источник финансирования.")
    if values["funding_source"] and not FundingSource.all_objects.filter(
        name__iexact=values["funding_source"]
    ).exists():
        errors.append("Источник финансирования не найден.")
    return errors, warnings


def _validate_donation_contract_row(values: dict[str, str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for key, label in (
        ("signed_on", "Дата подписания"),
        ("valid_from", "Действует с"),
        ("valid_until", "Действует до"),
    ):
        if values[key]:
            _, date_error = _parse_date(values[key], label)
            if date_error:
                errors.append(date_error)
    valid_from, _ = _parse_date(values["valid_from"], "Действует с")
    valid_until, _ = _parse_date(values["valid_until"], "Действует до")
    if valid_from and valid_until and valid_until < valid_from:
        errors.append("Дата окончания не может быть раньше даты начала.")
    amount_limit, amount_error = _parse_decimal(values["amount_limit"], "Лимит суммы")
    if amount_error:
        errors.append(amount_error)
    elif amount_limit is not None and amount_limit <= 0:
        errors.append("Лимит суммы должен быть положительным.")
    if values["contract_type"]:
        type_error = _choice_error(
            values["contract_type"],
            DonationContract.ContractType.choices,
            "Тип договора",
        )
        if type_error:
            errors.append(type_error)
    if values["status"]:
        status_error = _choice_error(values["status"], DonationContract.Status.choices, "Статус")
        if status_error:
            errors.append(status_error)
    if values["counterparty"] and not Counterparty.all_objects.filter(
        name__iexact=values["counterparty"]
    ).exists():
        errors.append("Контрагент договора не найден.")
    if values["funding_source"] and not FundingSource.all_objects.filter(
        name__iexact=values["funding_source"]
    ).exists():
        errors.append("Источник финансирования не найден.")
    if values["template"] and not ContractTemplate.objects.filter(
        title__iexact=values["template"]
    ).exists():
        warnings.append("Шаблон не найден: договор можно проверить без шаблона.")
    return errors, warnings


def _validate_service_contract_row(values: dict[str, str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for key, label in (
        ("signed_on", "Дата подписания"),
        ("valid_from", "Действует с"),
        ("valid_until", "Действует до"),
    ):
        if values[key]:
            _, date_error = _parse_date(values[key], label)
            if date_error:
                errors.append(date_error)
    valid_from, _ = _parse_date(values["valid_from"], "Действует с")
    valid_until, _ = _parse_date(values["valid_until"], "Действует до")
    if valid_from and valid_until and valid_until < valid_from:
        errors.append("Дата окончания не может быть раньше даты начала.")
    if values["contract_type"]:
        type_error = _choice_error(
            values["contract_type"],
            ServiceContract.ContractType.choices,
            "Тип договора",
        )
        if type_error:
            errors.append(type_error)
    if values["status"]:
        status_error = _choice_error(values["status"], ServiceContract.Status.choices, "Статус")
        if status_error:
            errors.append(status_error)

    child_queryset = Child.all_objects.filter(
        last_name__iexact=values["recipient_last_name"],
        first_name__iexact=values["recipient_first_name"],
    )
    child = child_queryset.first()
    if values["recipient_last_name"] and values["recipient_first_name"] and child is None:
        errors.append("Получатель не найден.")
    elif child_queryset.count() > 1:
        warnings.append("Найдено несколько получателей с такими ФИО; нужен ручной выбор.")

    if child and values["signer_last_name"]:
        signer_filter = Q(
            child=child,
            signs_contract=True,
            representative__last_name__iexact=values["signer_last_name"],
        )
        if values["signer_first_name"]:
            signer_filter &= Q(representative__first_name__iexact=values["signer_first_name"])
        if values["signer_phone"]:
            signer_filter &= Q(representative__phone__icontains=values["signer_phone"])
        signer_exists = RecipientRepresentative.objects.filter(signer_filter).exists()
        if not signer_exists:
            errors.append("Подписант договора не найден у выбранного получателя.")

    if values["template"] and not ContractTemplate.objects.filter(
        title__iexact=values["template"]
    ).exists():
        warnings.append("Шаблон не найден: договор можно проверить без шаблона.")
    if child and values["document_title"] and not child.documents.filter(
        title__iexact=values["document_title"]
    ).exists():
        warnings.append("Документ с таким названием не найден у получателя.")
    return errors, warnings


def _find_child_for_import(values: dict[str, str]) -> tuple[Child | None, int]:
    child_queryset = Child.all_objects.filter(
        last_name__iexact=values["recipient_last_name"],
        first_name__iexact=values["recipient_first_name"],
    )
    if values.get("recipient_middle_name"):
        child_queryset = child_queryset.filter(
            middle_name__iexact=values["recipient_middle_name"]
        )
    birth_date, _ = _parse_date(values.get("birth_date", ""), "Дата рождения")
    if birth_date:
        child_queryset = child_queryset.filter(birth_date=birth_date)
    return child_queryset.first(), child_queryset.count()


def _validate_certificate_row(values: dict[str, str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    birth_date = None
    if values["birth_date"]:
        birth_date, birth_error = _parse_date(values["birth_date"], "Дата рождения")
        if birth_error:
            errors.append(birth_error)

    for key, label in (
        ("valid_from", "Действует с"),
        ("valid_until", "Действует до"),
    ):
        if values[key]:
            _, date_error = _parse_date(values[key], label)
            if date_error:
                errors.append(date_error)
    valid_from, _ = _parse_date(values["valid_from"], "Действует с")
    valid_until, _ = _parse_date(values["valid_until"], "Действует до")
    if valid_from and valid_until and valid_until < valid_from:
        errors.append("Дата окончания не может быть раньше даты начала.")

    type_error = _choice_error(
        values["certificate_type"],
        Certificate.CertificateType.choices,
        "Тип сертификата",
    )
    if type_error:
        errors.append(type_error)

    total_amount, total_error = _parse_decimal(values["total_amount"], "Полная сумма")
    remaining_amount, remaining_error = _parse_decimal(values["remaining_amount"], "Остаток")
    if total_error:
        errors.append(total_error)
    elif total_amount is not None and total_amount < 0:
        errors.append("Полная сумма не может быть отрицательной.")
    if remaining_error:
        errors.append(remaining_error)
    elif remaining_amount is not None and remaining_amount < 0:
        errors.append("Остаток не может быть отрицательным.")
    if (
        total_amount is not None
        and remaining_amount is not None
        and remaining_amount > total_amount
    ):
        errors.append("Остаток не может быть больше полной суммы.")

    child = None
    child_count = 0
    if values["recipient_last_name"] and values["recipient_first_name"]:
        child, child_count = _find_child_for_import(values)
        if child is None:
            errors.append("Получатель не найден.")
        elif child_count > 1:
            warnings.append("Найдено несколько получателей с такими ФИО; нужен ручной выбор.")

    if values["funding_source"] and not FundingSource.all_objects.filter(
        name__iexact=values["funding_source"]
    ).exists():
        errors.append("Источник финансирования не найден.")

    if child and values["number"] and Certificate.objects.filter(
        child=child,
        number__iexact=values["number"],
    ).exists():
        warnings.append("Сертификат с таким номером уже есть у получателя.")

    payer_name_parts = [
        values["payer_last_name"],
        values["payer_first_name"],
        values["payer_phone"],
    ]
    if child and any(payer_name_parts):
        if not values["payer_last_name"]:
            errors.append("Для поиска представителя-плательщика нужна фамилия.")
        else:
            payer_filter = Q(
                child=child,
                representative__last_name__iexact=values["payer_last_name"],
            )
            if values["payer_first_name"]:
                payer_filter &= Q(representative__first_name__iexact=values["payer_first_name"])
            if values["payer_phone"]:
                payer_filter &= Q(representative__phone__icontains=values["payer_phone"])
            payer_links = RecipientRepresentative.objects.filter(payer_filter)
            if not payer_links.exists():
                errors.append("Представитель-плательщик не найден у выбранного получателя.")
            elif payer_links.count() > 1:
                warnings.append("Найдено несколько представителей-плательщиков; нужен ручной выбор.")
            elif not payer_links.first().is_payer:
                warnings.append("Представитель найден, но в карточке не отмечен как плательщик.")

    if child and birth_date and child.birth_date and child.birth_date != birth_date:
        warnings.append("Дата рождения в файле отличается от карточки получателя.")
    return errors, warnings


VALIDATORS = {
    COUNTERPARTY_IMPORT: _validate_counterparty_row,
    EXPENSE_IMPORT: _validate_expense_row,
    DONATION_CONTRACT_IMPORT: _validate_donation_contract_row,
    SERVICE_CONTRACT_IMPORT: _validate_service_contract_row,
    CERTIFICATE_IMPORT: _validate_certificate_row,
}


def preview_recipient_import(uploaded_file) -> ImportPreview:
    filename = getattr(uploaded_file, "name", "import")
    data = uploaded_file.read()
    source_sha256 = hashlib.sha256(data).hexdigest()
    raw_rows = _read_rows(data, filename)
    raw_rows = [row for row in raw_rows if not _is_empty_row(row)]
    columns = RECIPIENT_COLUMNS
    if not raw_rows:
        return ImportPreview(
            filename=filename,
            columns=columns,
            mapped_headers={},
            missing_required_headers=[column.label for column in columns if column.required],
            rows=[],
            total_rows=0,
            source_sha256=source_sha256,
        )

    headers = raw_rows[0]
    index_to_key: dict[int, str] = {}
    mapped_headers: dict[str, str] = {}
    for index, header in enumerate(headers):
        key = ALIAS_TO_KEY.get(_normalize_header(header))
        if key and key not in mapped_headers:
            index_to_key[index] = key
            mapped_headers[key] = header

    missing_required_headers = [
        column.label for column in columns if column.required and column.key not in mapped_headers
    ]
    column_by_key = {column.key: column for column in columns}
    rows: list[ImportRowPreview] = []
    seen_recipients: dict[tuple[str, str, str], int] = {}
    data_rows = raw_rows[1:]
    truncated = len(data_rows) > MAX_IMPORT_ROWS

    for offset, raw_row in enumerate(data_rows[:MAX_IMPORT_ROWS], start=2):
        values = {
            key: _row_value(raw_row, index)
            for index, key in index_to_key.items()
            if key in column_by_key
        }
        errors: list[str] = []
        warnings: list[str] = []
        for column in columns:
            values.setdefault(column.key, "")
            if column.required and not values[column.key]:
                errors.append(f"Не заполнено: {column.label}.")

        birth_date, birth_error = _parse_birth_date(values["birth_date"])
        if birth_error:
            errors.append(birth_error)
        if values["representative_email"] and "@" not in values["representative_email"]:
            errors.append("Email представителя выглядит некорректно.")
        for bool_key in ("is_primary", "receives_schedule"):
            if values[bool_key] and _parse_bool(values[bool_key]) is None:
                errors.append(f"{column_by_key[bool_key].label}: укажите да/нет.")

        last = values["recipient_last_name"].strip().lower()
        first = values["recipient_first_name"].strip().lower()
        birth_key = birth_date.isoformat() if birth_date else values["birth_date"].strip().lower()
        duplicate_key = (last, first, birth_key)
        if last and first:
            if duplicate_key in seen_recipients:
                warnings.append(f"Похожая строка уже есть в файле: {seen_recipients[duplicate_key]}.")
            else:
                seen_recipients[duplicate_key] = offset
            existing = Child.objects.filter(
                last_name__iexact=values["recipient_last_name"],
                first_name__iexact=values["recipient_first_name"],
            )
            if birth_date:
                existing = existing.filter(birth_date=birth_date)
            if existing.exists():
                warnings.append("Похожий получатель уже есть в базе.")

        rows.append(
            ImportRowPreview(
                row_number=offset,
                values=values,
                errors=errors,
                warnings=warnings,
            )
        )

    return ImportPreview(
        filename=filename,
        columns=columns,
        mapped_headers=mapped_headers,
        missing_required_headers=missing_required_headers,
        rows=rows,
        total_rows=len(data_rows),
        truncated=truncated,
        source_sha256=source_sha256,
    )


def preview_finance_contract_import(uploaded_file, import_type: str) -> ImportPreview:
    try:
        spec = FINANCE_CONTRACT_IMPORT_SPECS[import_type]
    except KeyError as exc:
        raise ValueError("Неизвестный тип предпросмотра импорта.") from exc

    filename = getattr(uploaded_file, "name", "import")
    data = uploaded_file.read()
    source_sha256 = hashlib.sha256(data).hexdigest()
    raw_rows = _read_rows(data, filename)
    raw_rows = [row for row in raw_rows if not _is_empty_row(row)]
    columns = spec.columns
    if not raw_rows:
        return ImportPreview(
            filename=filename,
            columns=columns,
            mapped_headers={},
            missing_required_headers=[column.label for column in columns if column.required],
            rows=[],
            total_rows=0,
            source_sha256=source_sha256,
        )

    index_to_key, mapped_headers = _build_header_mapping(raw_rows[0], spec.aliases)
    missing_required_headers = [
        column.label for column in columns if column.required and column.key not in mapped_headers
    ]
    column_by_key = {column.key: column for column in columns}
    validator = VALIDATORS[import_type]
    rows: list[ImportRowPreview] = []
    data_rows = raw_rows[1:]
    truncated = len(data_rows) > MAX_IMPORT_ROWS

    for offset, raw_row in enumerate(data_rows[:MAX_IMPORT_ROWS], start=2):
        values = {
            key: _row_value(raw_row, index)
            for index, key in index_to_key.items()
            if key in column_by_key
        }
        errors: list[str] = []
        warnings: list[str] = []
        for column in columns:
            values.setdefault(column.key, "")
            if column.required and not values[column.key]:
                errors.append(f"Не заполнено: {column.label}.")

        row_errors, row_warnings = validator(values)
        errors.extend(row_errors)
        warnings.extend(row_warnings)
        rows.append(
            ImportRowPreview(
                row_number=offset,
                values=values,
                errors=errors,
                warnings=warnings,
            )
        )

    return ImportPreview(
        filename=filename,
        columns=columns,
        mapped_headers=mapped_headers,
        missing_required_headers=missing_required_headers,
        rows=rows,
        total_rows=len(data_rows),
        truncated=truncated,
        source_sha256=source_sha256,
    )


PERSISTED_IMPORT_KINDS = {CERTIFICATE_IMPORT}


def _import_kind_for_batch(import_type: str) -> str:
    if import_type == CERTIFICATE_IMPORT:
        return ImportBatch.ImportKind.CERTIFICATES
    raise ValueError("Persisted preview is not enabled for this import type.")


def persist_import_preview_batch(
    preview: ImportPreview,
    import_type: str,
    *,
    uploaded_by=None,
) -> ImportBatch:
    if import_type not in PERSISTED_IMPORT_KINDS:
        raise ValueError("Persisted preview is not enabled for this import type.")
    user = uploaded_by if getattr(uploaded_by, "is_authenticated", False) else None
    batch = ImportBatch.objects.create(
        import_kind=_import_kind_for_batch(import_type),
        status=ImportBatch.Status.PREVIEWED,
        original_filename=preview.filename,
        source_sha256=preview.source_sha256,
        uploaded_by=user,
        total_rows=preview.total_rows,
        valid_rows=preview.valid_count,
        invalid_rows=preview.invalid_count,
        warning_rows=preview.warning_count,
        header_snapshot={
            "columns": [
                {
                    "key": column.key,
                    "label": column.label,
                    "required": column.required,
                }
                for column in preview.columns
            ],
            "mapped_headers": preview.mapped_headers,
            "missing_required_headers": preview.missing_required_headers,
            "truncated": preview.truncated,
        },
        error_summary={
            "missing_required_headers": preview.missing_required_headers,
            "invalid_rows": preview.invalid_count,
        },
    )
    ImportBatchRow.objects.bulk_create(
        [
            ImportBatchRow(
                batch=batch,
                row_number=row.row_number,
                status=(
                    ImportBatchRow.Status.INVALID
                    if row.errors
                    else ImportBatchRow.Status.VALID
                ),
                raw_values=row.values,
                normalized_values=row.values,
                errors=row.errors,
                warnings=row.warnings,
            )
            for row in preview.rows
        ]
    )
    return batch
