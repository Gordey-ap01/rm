"""Safe preview import for recipients and representatives."""

from __future__ import annotations

import csv
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO
from pathlib import PurePosixPath
from xml.etree import ElementTree

from operations.models import Child

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


def preview_recipient_import(uploaded_file) -> ImportPreview:
    filename = getattr(uploaded_file, "name", "import")
    data = uploaded_file.read()
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
    )
