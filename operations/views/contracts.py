"""Contract templates and contract registries."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.forms import ContractTemplateForm, DonationContractForm, ServiceContractForm
from operations.models import ContractTemplate, DonationContract, ServiceContract
from operations.services import contract_documents as contract_doc_svc, pdf as pdf_svc

from ._common import is_admin_user


def _format_money(amount: Decimal | None) -> str:
    if amount is None:
        return "без лимита"
    value = amount.quantize(Decimal("0.01"))
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _format_date(value: date | None) -> str:
    if value is None:
        return "не указана"
    return value.strftime("%d.%m.%Y")


def _pdf_filename(prefix: str, number: str, pk: int) -> str:
    safe_number = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in number).strip("_")
    suffix = safe_number or str(pk)
    return f"{prefix}_{suffix}.pdf"


def _docx_response(generated: contract_doc_svc.GeneratedContractFile) -> FileResponse:
    generated.payload.seek(0)
    return FileResponse(
        generated.payload,
        as_attachment=True,
        filename=generated.filename,
        content_type=contract_doc_svc.DOCX_CONTENT_TYPE,
    )


def _validity_label(valid_from: date | None, valid_until: date | None) -> str:
    if valid_from and valid_until:
        return f"{_format_date(valid_from)} - {_format_date(valid_until)}"
    if valid_from:
        return f"с {_format_date(valid_from)}"
    if valid_until:
        return f"до {_format_date(valid_until)}"
    return "срок не указан"


def _contract_filters(request) -> dict[str, str]:
    return {
        "q": request.GET.get("q", "").strip(),
        "kind": request.GET.get("kind", ""),
        "status": request.GET.get("status", ""),
    }


def _template_queryset(filters: dict[str, str]) -> list[ContractTemplate]:
    queryset = ContractTemplate.objects.all()
    if filters["kind"] not in {"", "template"}:
        return []
    query = filters["q"]
    if query:
        queryset = queryset.filter(
            Q(title__icontains=query) | Q(version__icontains=query) | Q(notes__icontains=query)
        )
    return list(queryset.order_by("template_type", "-is_active", "title", "version")[:200])


def _donation_queryset(filters: dict[str, str]) -> list[DonationContract]:
    queryset = DonationContract.objects.select_related(
        "counterparty",
        "funding_source",
        "template",
        "document",
    )
    if filters["kind"] not in {"", "donation"}:
        return []
    if filters["status"] in {choice[0] for choice in DonationContract.Status.choices}:
        queryset = queryset.filter(status=filters["status"])
    query = filters["q"]
    if query:
        queryset = queryset.filter(
            Q(number__icontains=query)
            | Q(notes__icontains=query)
            | Q(counterparty__name__icontains=query)
            | Q(funding_source__name__icontains=query)
            | Q(template__title__icontains=query)
            | Q(document__title__icontains=query)
        )
    contracts = list(queryset.order_by("-signed_on", "-created_at")[:300])
    for contract in contracts:
        contract.ui_amount_limit = _format_money(contract.amount_limit)
        contract.ui_signed_on = _format_date(contract.signed_on)
        contract.ui_validity = _validity_label(contract.valid_from, contract.valid_until)
        contract.ui_pdf_url = reverse("donation_contract_pdf", args=[contract.pk])
        contract.ui_word_url = reverse("donation_contract_word", args=[contract.pk])
    return contracts


def _service_queryset(filters: dict[str, str]) -> list[ServiceContract]:
    queryset = ServiceContract.objects.select_related(
        "child",
        "representative_link__representative",
        "template",
        "document",
    )
    if filters["kind"] not in {"", "service"}:
        return []
    if filters["status"] in {choice[0] for choice in ServiceContract.Status.choices}:
        queryset = queryset.filter(status=filters["status"])
    query = filters["q"]
    if query:
        queryset = queryset.filter(
            Q(number__icontains=query)
            | Q(notes__icontains=query)
            | Q(child__last_name__icontains=query)
            | Q(child__first_name__icontains=query)
            | Q(representative_link__representative__last_name__icontains=query)
            | Q(representative_link__representative__first_name__icontains=query)
            | Q(template__title__icontains=query)
            | Q(document__title__icontains=query)
        )
    contracts = list(queryset.order_by("-signed_on", "-created_at")[:300])
    for contract in contracts:
        contract.ui_signed_on = _format_date(contract.signed_on)
        contract.ui_validity = _validity_label(contract.valid_from, contract.valid_until)
        contract.ui_pdf_url = reverse("service_contract_pdf", args=[contract.pk])
        contract.ui_word_url = reverse("service_contract_word", args=[contract.pk])
    return contracts


def contract_summary_items(
    *,
    templates: list[ContractTemplate],
    donation_contracts: list[DonationContract],
    service_contracts: list[ServiceContract],
) -> list[dict[str, str]]:
    contracts = [*donation_contracts, *service_contracts]
    active_count = sum(1 for contract in contracts if contract.status == "active")
    draft_count = sum(1 for contract in contracts if contract.status == "draft")
    no_file_count = sum(1 for contract in contracts if not contract.document_id)
    active_templates = sum(1 for template in templates if template.is_active)
    return [
        {
            "label": "Договоров",
            "value": str(len(contracts)),
            "hint": f"активных: {active_count}",
        },
        {
            "label": "Черновиков",
            "value": str(draft_count),
            "hint": "можно вести без файла",
        },
        {
            "label": "Без файла",
            "value": str(no_file_count),
            "hint": "структурная запись уже есть",
        },
        {
            "label": "Шаблонов",
            "value": str(len(templates)),
            "hint": f"активных: {active_templates}",
        },
    ]


def contract_next_action(
    *,
    templates: list[ContractTemplate],
    donation_contracts: list[DonationContract],
    service_contracts: list[ServiceContract],
) -> dict[str, str]:
    contracts = [*donation_contracts, *service_contracts]
    if not any(template.is_active for template in templates):
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Добавить шаблон",
            "detail": "Договоры можно вести без шаблона, но шаблон нужен для юридического Word-файла.",
            "href": reverse("contract_template_create"),
        }
    if not contracts:
        return {
            "tone": "info",
            "label": "Следующий шаг",
            "title": "Создать первый договор",
            "detail": "Начните с договора с получателем или договора пожертвования.",
            "href": reverse("service_contract_create"),
        }
    without_documents = sum(1 for contract in contracts if not contract.document_id)
    if without_documents:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Проверить файлы договоров",
            "detail": f"Договоров без связанного файла: {without_documents}. Это допустимо для черновиков.",
            "href": "#contract-registry",
        }
    return {
        "tone": "success",
        "label": "Следующий шаг",
        "title": "Реестр заполнен",
        "detail": "Договоры имеют структурные поля и связи с файлами.",
        "href": "#contract-registry",
    }


def contract_form_control_items(kind: str) -> list[dict[str, str]]:
    common = [
        {
            "title": "Файл не обязателен",
            "detail": "Черновик договора можно создать без файла. Word можно сформировать позже из карточки договора.",
        },
        {
            "title": "Номер договора",
            "detail": "Если номер и дата подписания заполнены, пара должна быть уникальной внутри типа договора.",
        },
    ]
    if kind == "template":
        return [
            {
                "title": "Шаблон не договор",
                "detail": "Шаблон используется для Word-генерации, но не заменяет подписанный договор.",
            },
            {
                "title": "Версии",
                "detail": "Можно хранить несколько активных шаблонов одного типа для разных юридических сценариев.",
            },
        ]
    if kind == "donation":
        return [
            *common,
            {
                "title": "Источник финансирования",
                "detail": "Договор связывается с грантом, фондом или спонсором, но не создает деньги на балансах.",
            },
            {
                "title": "Лимит",
                "detail": "Лимит суммы можно оставить пустым, если договор не ограничен фиксированной суммой.",
            },
        ]
    return [
        *common,
        {
            "title": "Подписант",
            "detail": "Подписант берется из представителей выбранного получателя с флажком подписания договора.",
        },
        {
            "title": "Документ получателя",
            "detail": "Связанный файл договора должен относиться к тому же получателю.",
        },
    ]


@login_required
@user_passes_test(is_admin_user)
def contract_list(request):
    filters = _contract_filters(request)
    templates = _template_queryset(filters)
    donation_contracts = _donation_queryset(filters)
    service_contracts = _service_queryset(filters)
    return render(
        request,
        "operations/contract_list.html",
        {
            "contract_templates": templates,
            "donation_contracts": donation_contracts,
            "service_contracts": service_contracts,
            "contract_summary_items": contract_summary_items(
                templates=templates,
                donation_contracts=donation_contracts,
                service_contracts=service_contracts,
            ),
            "contract_next_action": contract_next_action(
                templates=templates,
                donation_contracts=donation_contracts,
                service_contracts=service_contracts,
            ),
            "kind_choices": [
                ("", "Все"),
                ("service", "С получателями"),
                ("donation", "Пожертвования"),
                ("template", "Шаблоны"),
            ],
            "status_choices": ServiceContract.Status.choices,
            "filters": filters,
            "today": timezone.localdate(),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def contract_template_create(request):
    if request.method == "POST":
        form = ContractTemplateForm(request.POST, request.FILES)
        if form.is_valid():
            template = form.save()
            messages.success(request, "Шаблон договора добавлен.")
            return redirect("contract_template_edit", pk=template.pk)
    else:
        form = ContractTemplateForm()
    return render(
        request,
        "operations/contract_form.html",
        {
            "title": "Добавить шаблон договора",
            "subtitle": "Версия шаблона для генерации Word-файлов.",
            "form": form,
            "cancel_url": reverse("contract_list"),
            "control_items": contract_form_control_items("template"),
            "placeholder_groups": contract_doc_svc.placeholder_reference_groups(),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def contract_template_edit(request, pk: int):
    template = get_object_or_404(ContractTemplate, pk=pk)
    if request.method == "POST":
        form = ContractTemplateForm(request.POST, request.FILES, instance=template)
        if form.is_valid():
            form.save()
            messages.success(request, "Шаблон договора обновлен.")
            return redirect("contract_list")
    else:
        form = ContractTemplateForm(instance=template)
    return render(
        request,
        "operations/contract_form.html",
        {
            "title": "Редактировать шаблон",
            "subtitle": str(template),
            "form": form,
            "cancel_url": reverse("contract_list"),
            "control_items": contract_form_control_items("template"),
            "placeholder_groups": contract_doc_svc.placeholder_reference_groups(),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def donation_contract_create(request):
    if request.method == "POST":
        form = DonationContractForm(request.POST)
        if form.is_valid():
            contract = form.save()
            messages.success(request, "Договор пожертвования добавлен.")
            return redirect("donation_contract_edit", pk=contract.pk)
    else:
        form = DonationContractForm()
    return render(
        request,
        "operations/contract_form.html",
        {
            "title": "Добавить договор пожертвования",
            "subtitle": "Связь договора с контрагентом и источником финансирования.",
            "form": form,
            "cancel_url": reverse("contract_list"),
            "control_items": contract_form_control_items("donation"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def donation_contract_edit(request, pk: int):
    contract = get_object_or_404(
        DonationContract.objects.select_related("counterparty", "funding_source"),
        pk=pk,
    )
    if request.method == "POST":
        form = DonationContractForm(request.POST, instance=contract)
        if form.is_valid():
            form.save()
            messages.success(request, "Договор пожертвования обновлен.")
            return redirect("contract_list")
    else:
        form = DonationContractForm(instance=contract)
    return render(
        request,
        "operations/contract_form.html",
        {
            "title": "Редактировать договор пожертвования",
            "subtitle": str(contract),
            "form": form,
            "cancel_url": reverse("contract_list"),
            "control_items": contract_form_control_items("donation"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def service_contract_create(request):
    if request.method == "POST":
        form = ServiceContractForm(request.POST)
        if form.is_valid():
            contract = form.save()
            messages.success(request, "Договор с получателем добавлен.")
            return redirect("service_contract_edit", pk=contract.pk)
    else:
        form = ServiceContractForm()
    return render(
        request,
        "operations/contract_form.html",
        {
            "title": "Добавить договор с получателем",
            "subtitle": "Структурная запись договора и подписанта.",
            "form": form,
            "cancel_url": reverse("contract_list"),
            "control_items": contract_form_control_items("service"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def service_contract_edit(request, pk: int):
    contract = get_object_or_404(
        ServiceContract.objects.select_related("child", "representative_link__representative"),
        pk=pk,
    )
    if request.method == "POST":
        form = ServiceContractForm(request.POST, instance=contract)
        if form.is_valid():
            form.save()
            messages.success(request, "Договор с получателем обновлен.")
            return redirect("contract_list")
    else:
        form = ServiceContractForm(instance=contract)
    return render(
        request,
        "operations/contract_form.html",
        {
            "title": "Редактировать договор с получателем",
            "subtitle": str(contract),
            "form": form,
            "cancel_url": reverse("contract_list"),
            "control_items": contract_form_control_items("service"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def donation_contract_pdf(request, pk: int):
    contract = get_object_or_404(
        DonationContract.objects.select_related("counterparty", "funding_source", "template"),
        pk=pk,
    )
    filename = _pdf_filename("donation_contract", contract.number, contract.pk)
    return FileResponse(
        pdf_svc.donation_contract_pdf(contract),
        as_attachment=True,
        filename=filename,
        content_type="application/pdf",
    )


@login_required
@user_passes_test(is_admin_user)
def donation_contract_word(request, pk: int):
    contract = get_object_or_404(
        DonationContract.objects.select_related("counterparty", "funding_source", "template"),
        pk=pk,
    )
    if request.method != "POST":
        messages.warning(request, "Сформируйте Word-файл кнопкой в реестре договоров.")
        return redirect("contract_list")
    try:
        generated = contract_doc_svc.render_donation_contract_docx(contract)
    except contract_doc_svc.ContractDocumentError as exc:
        messages.error(request, str(exc))
        return redirect("contract_list")
    return _docx_response(generated)


@login_required
@user_passes_test(is_admin_user)
def service_contract_pdf(request, pk: int):
    contract = get_object_or_404(
        ServiceContract.objects.select_related(
            "child",
            "representative_link__representative",
            "template",
        ),
        pk=pk,
    )
    filename = _pdf_filename("service_contract", contract.number, contract.pk)
    return FileResponse(
        pdf_svc.service_contract_pdf(contract),
        as_attachment=True,
        filename=filename,
        content_type="application/pdf",
    )


@login_required
@user_passes_test(is_admin_user)
def service_contract_word(request, pk: int):
    contract = get_object_or_404(
        ServiceContract.objects.select_related(
            "child",
            "representative_link__representative",
            "template",
            "document",
        ),
        pk=pk,
    )
    if request.method != "POST":
        messages.warning(request, "Сформируйте Word-файл кнопкой в реестре договоров.")
        return redirect("contract_list")
    try:
        generated = contract_doc_svc.save_service_contract_docx(contract, actor=request.user)
    except contract_doc_svc.ContractDocumentError as exc:
        messages.error(request, str(exc))
        return redirect("contract_list")
    return _docx_response(generated)
