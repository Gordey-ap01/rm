from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render
from django.urls import reverse

from operations.forms import ContractImportPreviewForm, RecipientImportPreviewForm
from operations.services.import_preview import (
    FINANCE_CONTRACT_IMPORT_SPECS,
    ImportPreview,
    preview_finance_contract_import,
    preview_recipient_import,
)

from ._common import is_admin_user


def recipient_import_summary_items(preview: ImportPreview | None) -> list[dict[str, str]]:
    if preview is None:
        return [
            {
                "label": "Файл",
                "value": "не выбран",
                "hint": "поддерживаются .xlsx, .csv, .tsv",
            },
            {
                "label": "Режим",
                "value": "preview",
                "hint": "без записи в базу",
            },
            {
                "label": "Лимит",
                "value": "200",
                "hint": "строк показываются в предпросмотре",
            },
        ]
    return [
        {
            "label": "Строк",
            "value": str(preview.total_rows),
            "hint": preview.filename,
        },
        {
            "label": "Готово",
            "value": str(preview.valid_count),
            "hint": "строк без ошибок",
        },
        {
            "label": "Ошибки",
            "value": str(preview.invalid_count),
            "hint": "требуют правки файла",
        },
        {
            "label": "Предупреждения",
            "value": str(preview.warning_count),
            "hint": "проверьте дубли и совпадения",
        },
    ]


def recipient_import_next_action(preview: ImportPreview | None) -> dict[str, str]:
    if preview is None:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Загрузить Excel или CSV",
            "detail": "Система только проверит файл и не создаст записи.",
            "href": "#import-upload",
        }
    if preview.missing_required_headers:
        return {
            "tone": "danger",
            "label": "Следующее действие",
            "title": "Исправить обязательные колонки",
            "detail": ", ".join(preview.missing_required_headers),
            "href": "#import-columns",
        }
    if preview.invalid_count:
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Исправить строки с ошибками",
            "detail": f"Ошибочных строк: {preview.invalid_count}. После правки загрузите файл заново.",
            "href": "#import-rows",
        }
    if preview.valid_count:
        return {
            "tone": "success",
            "label": "Следующее действие",
            "title": "Файл готов к будущему импорту",
            "detail": "Запись в базу отключена: проверьте строки и сохраните файл как эталон.",
            "href": "#import-rows",
        }
    return {
        "tone": "info",
        "label": "Следующее действие",
        "title": "Добавить строки данных",
        "detail": "В файле найдены заголовки, но нет получателей для проверки.",
        "href": "#import-upload",
    }


def contract_import_summary_items(preview: ImportPreview | None) -> list[dict[str, str]]:
    if preview is None:
        return [
            {
                "label": "Файл",
                "value": "не выбран",
                "hint": "поддерживаются .xlsx, .csv, .tsv",
            },
            {
                "label": "Режим",
                "value": "preview",
                "hint": "без записи в базу",
            },
            {
                "label": "Типы",
                "value": "4",
                "hint": "контрагенты, расходы, договоры",
            },
            {
                "label": "Лимит",
                "value": "200",
                "hint": "строк показываются в предпросмотре",
            },
        ]
    return [
        {
            "label": "Строк",
            "value": str(preview.total_rows),
            "hint": preview.filename,
        },
        {
            "label": "Готово",
            "value": str(preview.valid_count),
            "hint": "строк без ошибок",
        },
        {
            "label": "Ошибки",
            "value": str(preview.invalid_count),
            "hint": "требуют правки файла",
        },
        {
            "label": "Предупреждения",
            "value": str(preview.warning_count),
            "hint": "проверьте справочники",
        },
    ]


def contract_import_next_action(preview: ImportPreview | None) -> dict[str, str]:
    if preview is None:
        return {
            "tone": "info",
            "label": "Следующее действие",
            "title": "Выбрать файл и тип проверки",
            "detail": "Система проверит строки и не создаст записи.",
            "href": "#import-upload",
        }
    if preview.missing_required_headers:
        return {
            "tone": "danger",
            "label": "Следующее действие",
            "title": "Исправить обязательные колонки",
            "detail": ", ".join(preview.missing_required_headers),
            "href": "#import-columns",
        }
    if preview.invalid_count:
        return {
            "tone": "warning",
            "label": "Следующее действие",
            "title": "Исправить строки с ошибками",
            "detail": f"Ошибочных строк: {preview.invalid_count}. Запись в БД выключена.",
            "href": "#import-rows",
        }
    if preview.valid_count:
        return {
            "tone": "success",
            "label": "Следующее действие",
            "title": "Файл проходит preview",
            "detail": "Реальный импорт останется отдельным утвержденным срезом.",
            "href": "#import-rows",
        }
    return {
        "tone": "info",
        "label": "Следующее действие",
        "title": "Добавить строки данных",
        "detail": "В файле найдены заголовки, но нет строк для проверки.",
        "href": "#import-upload",
    }


def contract_import_display_rows(preview: ImportPreview | None) -> list[dict[str, object]]:
    if preview is None:
        return []
    rows: list[dict[str, object]] = []
    for row in preview.rows:
        rows.append(
            {
                "row_number": row.row_number,
                "errors": row.errors,
                "warnings": row.warnings,
                "cells": [
                    {
                        "label": column.label,
                        "value": row.values.get(column.key, ""),
                    }
                    for column in preview.columns
                ],
            }
        )
    return rows


@login_required
@user_passes_test(is_admin_user)
def recipient_import_preview(request):
    preview = None
    if request.method == "POST":
        form = RecipientImportPreviewForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                preview = preview_recipient_import(form.cleaned_data["file"])
            except (ValueError, OSError) as exc:
                form.add_error("file", str(exc))
            else:
                if preview.invalid_count:
                    messages.warning(
                        request,
                        f"Файл разобран, но есть строки с ошибками: {preview.invalid_count}.",
                    )
                else:
                    messages.success(
                        request,
                        f"Файл разобран. Готово к будущему импорту строк: {preview.valid_count}.",
                    )
    else:
        form = RecipientImportPreviewForm()

    return render(
        request,
        "operations/recipient_import_preview.html",
        {
            "form": form,
            "preview": preview,
            "import_summary_items": recipient_import_summary_items(preview),
            "import_next_action": recipient_import_next_action(preview),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def contract_import_preview(request):
    preview = None
    selected_type = request.POST.get("import_type", "expenses")
    if request.method == "POST":
        form = ContractImportPreviewForm(request.POST, request.FILES)
        if form.is_valid():
            selected_type = form.cleaned_data["import_type"]
            try:
                preview = preview_finance_contract_import(
                    form.cleaned_data["file"],
                    selected_type,
                )
            except (ValueError, OSError) as exc:
                form.add_error("file", str(exc))
            else:
                if preview.invalid_count:
                    messages.warning(
                        request,
                        f"Файл разобран, но есть строки с ошибками: {preview.invalid_count}.",
                    )
                else:
                    messages.success(
                        request,
                        f"Файл разобран. Готово к будущему импорту строк: {preview.valid_count}.",
                    )
    else:
        form = ContractImportPreviewForm()

    return render(
        request,
        "operations/contract_import_preview.html",
        {
            "form": form,
            "preview": preview,
            "selected_type": selected_type,
            "selected_spec": FINANCE_CONTRACT_IMPORT_SPECS.get(selected_type),
            "contract_import_types": FINANCE_CONTRACT_IMPORT_SPECS.values(),
            "import_summary_items": contract_import_summary_items(preview),
            "import_next_action": contract_import_next_action(preview),
            "import_display_rows": contract_import_display_rows(preview),
            "contract_list_url": reverse("contract_list"),
        },
    )
