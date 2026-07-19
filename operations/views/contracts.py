"""Contract templates and contract registries."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.forms import (
    ContractActForm,
    ContractTemplateForm,
    DonationContractForm,
    OrganizationServiceContractForm,
    OrganizationServiceContractLineFormSet,
    ServiceContractForm,
    ServiceContractLineFormSet,
    SignedArchiveUploadForm,
)
from operations.models import (
    ContractAct,
    ContractActSignedFile,
    ContractLegalSnapshot,
    ContractSignedFile,
    ContractTemplate,
    DonationContract,
    OrganizationServiceContract,
    ServiceContract,
)
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


def _uploaded_signed_file_or_error(request):
    if not request.FILES:
        return None, ""
    form = SignedArchiveUploadForm(request.POST, request.FILES)
    if form.is_valid():
        return form.cleaned_data["signed_file"], ""
    return None, " ".join(error for errors in form.errors.values() for error in errors)


def _validity_label(valid_from: date | None, valid_until: date | None) -> str:
    if valid_from and valid_until:
        return f"{_format_date(valid_from)} - {_format_date(valid_until)}"
    if valid_from:
        return f"с {_format_date(valid_from)}"
    if valid_until:
        return f"до {_format_date(valid_until)}"
    return "срок не указан"


def _legal_snapshot_label(contract) -> str:
    if not contract.document_id:
        return ""
    try:
        snapshot = contract.document.contract_legal_snapshot
    except ContractLegalSnapshot.DoesNotExist:
        return ""
    return "реквизиты зафиксированы: " + timezone.localtime(snapshot.updated_at).strftime(
        "%d.%m.%Y %H:%M"
    )


def _active_signed_files_prefetch() -> Prefetch:
    return Prefetch(
        "signed_files",
        queryset=ContractSignedFile.objects.filter(
            status=ContractSignedFile.Status.ACTIVE,
        ).order_by("-signed_on", "-created_at"),
        to_attr="ui_active_signed_files",
    )


def _active_act_signed_files_prefetch() -> Prefetch:
    return Prefetch(
        "signed_files",
        queryset=ContractActSignedFile.objects.filter(
            status=ContractActSignedFile.Status.ACTIVE,
        ).order_by("-signed_on", "-created_at"),
        to_attr="ui_active_signed_files",
    )


def _attach_signed_file_ui(contract, *, archive_url_name: str) -> None:
    signed_files = getattr(contract, "ui_active_signed_files", [])
    signed_file = signed_files[0] if signed_files else None
    contract.ui_signed_file = signed_file
    contract.ui_signed_file_url = (
        reverse("contract_signed_file_download", args=[signed_file.pk]) if signed_file else ""
    )
    contract.ui_archive_signed_url = (
        reverse(archive_url_name, args=[contract.pk])
        if contract.document_id and contract.ui_legal_snapshot
        else ""
    )


def _attach_act_signed_file_ui(act: ContractAct) -> None:
    signed_files = getattr(act, "ui_active_signed_files", [])
    signed_file = signed_files[0] if signed_files else None
    act.ui_signed_file = signed_file
    act.ui_signed_file_url = (
        reverse("contract_act_signed_file_download", args=[signed_file.pk])
        if signed_file
        else ""
    )
    act.ui_archive_signed_url = (
        reverse("contract_act_archive_signed", args=[act.pk])
        if act.document_id and act.act_snapshot
        else ""
    )


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
        "document__contract_legal_snapshot",
    ).prefetch_related(_active_signed_files_prefetch())
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
        contract.ui_legal_snapshot = _legal_snapshot_label(contract)
        _attach_signed_file_ui(contract, archive_url_name="donation_contract_archive_signed")
    return contracts


def _service_queryset(filters: dict[str, str]) -> list[ServiceContract]:
    queryset = ServiceContract.objects.select_related(
        "child",
        "representative_link__representative",
        "funding_source",
        "certificate",
        "certificate__funding_source",
        "certificate__payer_representative__representative",
        "template",
        "document",
        "document__contract_legal_snapshot",
    ).prefetch_related("service_lines__service")
    queryset = queryset.prefetch_related(_active_signed_files_prefetch())
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
            | Q(funding_source__name__icontains=query)
            | Q(certificate__number__icontains=query)
            | Q(service_lines__service_name__icontains=query)
            | Q(service_lines__service__name__icontains=query)
            | Q(template__title__icontains=query)
            | Q(document__title__icontains=query)
        ).distinct()
    contracts = list(queryset.order_by("-signed_on", "-created_at")[:300])
    for contract in contracts:
        contract.ui_signed_on = _format_date(contract.signed_on)
        contract.ui_validity = _validity_label(contract.valid_from, contract.valid_until)
        contract.ui_pdf_url = reverse("service_contract_pdf", args=[contract.pk])
        contract.ui_word_url = reverse("service_contract_word", args=[contract.pk])
        contract.ui_legal_snapshot = _legal_snapshot_label(contract)
        contract.ui_spec_summary = contract.service_lines_summary or "спецификация не заполнена"
        contract.ui_amount = _format_money(contract.service_lines_total_amount)
        _attach_signed_file_ui(contract, archive_url_name="service_contract_archive_signed")
    return contracts


def _organization_queryset(filters: dict[str, str]) -> list[OrganizationServiceContract]:
    queryset = OrganizationServiceContract.objects.select_related(
        "counterparty",
        "funding_source",
        "template",
        "document",
        "document__contract_legal_snapshot",
    ).prefetch_related("service_lines__service")
    queryset = queryset.prefetch_related(_active_signed_files_prefetch())
    if filters["kind"] not in {"", "organization"}:
        return []
    if filters["status"] in {choice[0] for choice in OrganizationServiceContract.Status.choices}:
        queryset = queryset.filter(status=filters["status"])
    query = filters["q"]
    if query:
        queryset = queryset.filter(
            Q(number__icontains=query)
            | Q(notes__icontains=query)
            | Q(counterparty__name__icontains=query)
            | Q(funding_source__name__icontains=query)
            | Q(service_lines__service_name__icontains=query)
            | Q(service_lines__service__name__icontains=query)
            | Q(template__title__icontains=query)
            | Q(document__title__icontains=query)
        ).distinct()
    contracts = list(queryset.order_by("-signed_on", "-created_at")[:300])
    for contract in contracts:
        contract.ui_signed_on = _format_date(contract.signed_on)
        contract.ui_validity = _validity_label(contract.valid_from, contract.valid_until)
        contract.ui_pdf_url = reverse("organization_service_contract_pdf", args=[contract.pk])
        contract.ui_word_url = reverse("organization_service_contract_word", args=[contract.pk])
        contract.ui_legal_snapshot = _legal_snapshot_label(contract)
        contract.ui_spec_summary = contract.service_lines_summary or "спецификация не заполнена"
        contract.ui_amount = _format_money(contract.service_lines_total_amount)
        _attach_signed_file_ui(
            contract,
            archive_url_name="organization_service_contract_archive_signed",
        )
    return contracts


def _act_queryset(filters: dict[str, str]) -> list[ContractAct]:
    queryset = ContractAct.objects.select_related(
        "service_contract__child",
        "service_contract__representative_link__representative",
        "organization_contract__counterparty",
        "template",
        "document",
    ).prefetch_related(_active_act_signed_files_prefetch())
    if filters["kind"] not in {"", "act"}:
        return []
    if filters["status"] in {choice[0] for choice in ContractAct.Status.choices}:
        queryset = queryset.filter(status=filters["status"])
    query = filters["q"]
    if query:
        queryset = queryset.filter(
            Q(number__icontains=query)
            | Q(notes__icontains=query)
            | Q(service_contract__number__icontains=query)
            | Q(service_contract__child__last_name__icontains=query)
            | Q(service_contract__child__first_name__icontains=query)
            | Q(organization_contract__number__icontains=query)
            | Q(organization_contract__counterparty__name__icontains=query)
            | Q(template__title__icontains=query)
            | Q(document__title__icontains=query)
        ).distinct()
    acts = list(queryset.order_by("-act_on", "-created_at")[:300])
    for act in acts:
        act.ui_act_on = _format_date(act.act_on)
        act.ui_period = _validity_label(act.period_from, act.period_until)
        act.ui_amount = _format_money(act.amount)
        act.ui_word_url = reverse("contract_act_word", args=[act.pk])
        act.ui_target = act.target_label
        _attach_act_signed_file_ui(act)
    return acts


def contract_summary_items(
    *,
    templates: list[ContractTemplate],
    donation_contracts: list[DonationContract],
    service_contracts: list[ServiceContract],
    organization_contracts: list[OrganizationServiceContract],
    contract_acts: list[ContractAct],
) -> list[dict[str, str]]:
    contracts = [*donation_contracts, *service_contracts, *organization_contracts]
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
        {
            "label": "Актов",
            "value": str(len(contract_acts)),
            "hint": "без финансовых проводок",
        },
    ]


def contract_next_action(
    *,
    templates: list[ContractTemplate],
    donation_contracts: list[DonationContract],
    service_contracts: list[ServiceContract],
    organization_contracts: list[OrganizationServiceContract],
    contract_acts: list[ContractAct],
) -> dict[str, str]:
    contracts = [*donation_contracts, *service_contracts, *organization_contracts]
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
    if kind == "organization":
        return [
            *common,
            {
                "title": "Организация",
                "detail": "B2B-договор подписывается с контрагентом и не требует получателя или представителя.",
            },
            {
                "title": "Спецификация",
                "detail": "Строки описывают юридический план услуг организации и не создают платежей, занятий или актов.",
            },
            {
                "title": "Документ контрагента",
                "detail": "Word сохраняется как документ выбранной организации; документ получателя использовать нельзя.",
            },
        ]
    if kind == "act":
        return [
            {
                "title": "Один договор-основание",
                "detail": "Выберите договор с получателем или B2B-договор в соответствии с типом акта.",
            },
            {
                "title": "Период и сумма",
                "detail": "Акт хранит период и сумму юридического документа, но не создает платежи и списания.",
            },
            {
                "title": "Файл акта",
                "detail": "Word сохраняется как отдельный документ категории акта у получателя или организации.",
            },
        ]
    return [
        *common,
        {
            "title": "Подписант",
            "detail": "Подписант берется из представителей выбранного получателя с флажком подписания договора.",
        },
        {
            "title": "Спецификация",
            "detail": "Строки договора описывают юридический план услуг и не создают списаний по балансу.",
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
    organization_contracts = _organization_queryset(filters)
    contract_acts = _act_queryset(filters)
    return render(
        request,
        "operations/contract_list.html",
        {
            "contract_templates": templates,
            "donation_contracts": donation_contracts,
            "service_contracts": service_contracts,
            "organization_contracts": organization_contracts,
            "contract_acts": contract_acts,
            "contract_summary_items": contract_summary_items(
                templates=templates,
                donation_contracts=donation_contracts,
                service_contracts=service_contracts,
                organization_contracts=organization_contracts,
                contract_acts=contract_acts,
            ),
            "contract_next_action": contract_next_action(
                templates=templates,
                donation_contracts=donation_contracts,
                service_contracts=service_contracts,
                organization_contracts=organization_contracts,
                contract_acts=contract_acts,
            ),
            "kind_choices": [
                ("", "Все"),
                ("service", "С получателями"),
                ("organization", "Организации"),
                ("donation", "Пожертвования"),
                ("act", "Акты"),
                ("template", "Шаблоны"),
            ],
            "status_choices": (
                *ServiceContract.Status.choices,
                (ContractAct.Status.ISSUED, ContractAct.Status.ISSUED.label),
                (ContractAct.Status.SIGNED, ContractAct.Status.SIGNED.label),
            ),
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
    contract = ServiceContract()
    if request.method == "POST":
        form = ServiceContractForm(request.POST, instance=contract)
        line_formset = ServiceContractLineFormSet(
            request.POST,
            instance=contract,
            prefix="service_lines",
        )
        form_valid = form.is_valid()
        line_formset_valid = line_formset.is_valid()
        if form_valid and line_formset_valid:
            with transaction.atomic():
                contract = form.save()
                line_formset.instance = contract
                line_formset.save()
            messages.success(request, "Договор с получателем добавлен.")
            return redirect("service_contract_edit", pk=contract.pk)
    else:
        form = ServiceContractForm(instance=contract)
        line_formset = ServiceContractLineFormSet(instance=contract, prefix="service_lines")
    return render(
        request,
        "operations/contract_form.html",
        {
            "title": "Добавить договор с получателем",
            "subtitle": "Структурная запись договора и подписанта.",
            "form": form,
            "service_line_formset": line_formset,
            "cancel_url": reverse("contract_list"),
            "control_items": contract_form_control_items("service"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def service_contract_edit(request, pk: int):
    contract = get_object_or_404(
        ServiceContract.objects.select_related(
            "child",
            "representative_link__representative",
            "funding_source",
            "certificate",
            "certificate__funding_source",
            "certificate__payer_representative__representative",
        ).prefetch_related("service_lines__service"),
        pk=pk,
    )
    if request.method == "POST":
        form = ServiceContractForm(request.POST, instance=contract)
        line_formset = ServiceContractLineFormSet(
            request.POST,
            instance=contract,
            prefix="service_lines",
        )
        form_valid = form.is_valid()
        line_formset_valid = line_formset.is_valid()
        if form_valid and line_formset_valid:
            with transaction.atomic():
                form.save()
                line_formset.save()
            messages.success(request, "Договор с получателем обновлен.")
            return redirect("contract_list")
    else:
        form = ServiceContractForm(instance=contract)
        line_formset = ServiceContractLineFormSet(instance=contract, prefix="service_lines")
    return render(
        request,
        "operations/contract_form.html",
        {
            "title": "Редактировать договор с получателем",
            "subtitle": str(contract),
            "form": form,
            "service_line_formset": line_formset,
            "cancel_url": reverse("contract_list"),
            "control_items": contract_form_control_items("service"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def organization_service_contract_create(request):
    contract = OrganizationServiceContract()
    if request.method == "POST":
        form = OrganizationServiceContractForm(request.POST, instance=contract)
        line_formset = OrganizationServiceContractLineFormSet(
            request.POST,
            instance=contract,
            prefix="service_lines",
        )
        form_valid = form.is_valid()
        line_formset_valid = line_formset.is_valid()
        if form_valid and line_formset_valid:
            with transaction.atomic():
                contract = form.save()
                line_formset.instance = contract
                line_formset.save()
            messages.success(request, "B2B-договор услуг добавлен.")
            return redirect("organization_service_contract_edit", pk=contract.pk)
    else:
        form = OrganizationServiceContractForm(instance=contract)
        line_formset = OrganizationServiceContractLineFormSet(
            instance=contract,
            prefix="service_lines",
        )
    return render(
        request,
        "operations/contract_form.html",
        {
            "title": "Добавить B2B-договор услуг",
            "subtitle": "Структурная запись договора с организацией и спецификацией услуг.",
            "form": form,
            "service_line_formset": line_formset,
            "cancel_url": reverse("contract_list"),
            "control_items": contract_form_control_items("organization"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def organization_service_contract_edit(request, pk: int):
    contract = get_object_or_404(
        OrganizationServiceContract.objects.select_related(
            "counterparty",
            "funding_source",
        ).prefetch_related("service_lines__service"),
        pk=pk,
    )
    if request.method == "POST":
        form = OrganizationServiceContractForm(request.POST, instance=contract)
        line_formset = OrganizationServiceContractLineFormSet(
            request.POST,
            instance=contract,
            prefix="service_lines",
        )
        form_valid = form.is_valid()
        line_formset_valid = line_formset.is_valid()
        if form_valid and line_formset_valid:
            with transaction.atomic():
                form.save()
                line_formset.save()
            messages.success(request, "B2B-договор услуг обновлен.")
            return redirect("contract_list")
    else:
        form = OrganizationServiceContractForm(instance=contract)
        line_formset = OrganizationServiceContractLineFormSet(
            instance=contract,
            prefix="service_lines",
        )
    return render(
        request,
        "operations/contract_form.html",
        {
            "title": "Редактировать B2B-договор услуг",
            "subtitle": str(contract),
            "form": form,
            "service_line_formset": line_formset,
            "cancel_url": reverse("contract_list"),
            "control_items": contract_form_control_items("organization"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def contract_act_create(request):
    act = ContractAct()
    if request.method == "POST":
        form = ContractActForm(request.POST, instance=act)
        if form.is_valid():
            act = form.save()
            messages.success(request, "Акт оказанных услуг добавлен.")
            return redirect("contract_act_edit", pk=act.pk)
    else:
        form = ContractActForm(instance=act)
    return render(
        request,
        "operations/contract_form.html",
        {
            "title": "Добавить акт",
            "subtitle": "Юридический документ за период без автоматических финансовых проводок.",
            "form": form,
            "form_panel_title": "Параметры акта",
            "form_panel_subtitle": (
                "Акт хранится отдельно от договора. Word можно сформировать после сохранения."
            ),
            "cancel_url": reverse("contract_list"),
            "control_items": contract_form_control_items("act"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def contract_act_edit(request, pk: int):
    act = get_object_or_404(
        ContractAct.objects.select_related(
            "service_contract__child",
            "organization_contract__counterparty",
        ),
        pk=pk,
    )
    if request.method == "POST":
        form = ContractActForm(request.POST, instance=act)
        if form.is_valid():
            form.save()
            messages.success(request, "Акт оказанных услуг обновлен.")
            return redirect("contract_list")
    else:
        form = ContractActForm(instance=act)
    return render(
        request,
        "operations/contract_form.html",
        {
            "title": "Редактировать акт",
            "subtitle": str(act),
            "form": form,
            "form_panel_title": "Параметры акта",
            "form_panel_subtitle": (
                "Акт хранится отдельно от договора. Word можно сформировать после сохранения."
            ),
            "cancel_url": reverse("contract_list"),
            "control_items": contract_form_control_items("act"),
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
        DonationContract.objects.select_related(
            "counterparty",
            "funding_source",
            "template",
            "document",
        ),
        pk=pk,
    )
    if request.method != "POST":
        messages.warning(request, "Сформируйте Word-файл кнопкой в реестре договоров.")
        return redirect("contract_list")
    try:
        generated = contract_doc_svc.save_donation_contract_docx(contract, actor=request.user)
    except contract_doc_svc.ContractDocumentError as exc:
        messages.error(request, str(exc))
        return redirect("contract_list")
    return _docx_response(generated)


@login_required
@user_passes_test(is_admin_user)
def donation_contract_archive_signed(request, pk: int):
    contract = get_object_or_404(
        DonationContract.objects.select_related(
            "counterparty",
            "funding_source",
            "document",
            "document__contract_legal_snapshot",
        ),
        pk=pk,
    )
    if request.method != "POST":
        messages.warning(request, "Архив подписанного файла создается кнопкой в реестре.")
        return redirect("contract_list")
    uploaded_file, upload_error = _uploaded_signed_file_or_error(request)
    if upload_error:
        messages.error(request, upload_error)
        return redirect("contract_list")
    try:
        if uploaded_file is not None:
            signed_file = contract_doc_svc.archive_donation_contract_uploaded_signed_file(
                contract,
                uploaded_file,
                actor=request.user,
            )
        else:
            signed_file = contract_doc_svc.archive_donation_contract_signed_file(
                contract,
                actor=request.user,
            )
    except contract_doc_svc.ContractDocumentError as exc:
        messages.error(request, str(exc))
        return redirect("contract_list")
    messages.success(
        request,
        f"Подписанный файл договора пожертвования сохранен в архиве: "
        f"{signed_file.file_sha256[:12]}...",
    )
    return redirect("contract_list")


@login_required
@user_passes_test(is_admin_user)
def service_contract_pdf(request, pk: int):
    contract = get_object_or_404(
        ServiceContract.objects.select_related(
            "child",
            "representative_link__representative",
            "funding_source",
            "certificate",
            "certificate__funding_source",
            "certificate__payer_representative__representative",
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
            "funding_source",
            "certificate",
            "certificate__funding_source",
            "certificate__payer_representative__representative",
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


@login_required
@user_passes_test(is_admin_user)
def organization_service_contract_pdf(request, pk: int):
    contract = get_object_or_404(
        OrganizationServiceContract.objects.select_related(
            "counterparty",
            "funding_source",
            "template",
        ).prefetch_related("service_lines__service"),
        pk=pk,
    )
    filename = _pdf_filename("organization_service_contract", contract.number, contract.pk)
    return FileResponse(
        pdf_svc.organization_service_contract_pdf(contract),
        as_attachment=True,
        filename=filename,
        content_type="application/pdf",
    )


@login_required
@user_passes_test(is_admin_user)
def organization_service_contract_word(request, pk: int):
    contract = get_object_or_404(
        OrganizationServiceContract.objects.select_related(
            "counterparty",
            "funding_source",
            "template",
            "document",
        ).prefetch_related("service_lines__service"),
        pk=pk,
    )
    if request.method != "POST":
        messages.warning(request, "Сформируйте Word-файл кнопкой в реестре договоров.")
        return redirect("contract_list")
    try:
        generated = contract_doc_svc.save_organization_service_contract_docx(
            contract,
            actor=request.user,
        )
    except contract_doc_svc.ContractDocumentError as exc:
        messages.error(request, str(exc))
        return redirect("contract_list")
    return _docx_response(generated)


@login_required
@user_passes_test(is_admin_user)
def contract_act_word(request, pk: int):
    act = get_object_or_404(
        ContractAct.objects.select_related(
            "service_contract__child",
            "service_contract__representative_link__representative",
            "service_contract__funding_source",
            "service_contract__template",
            "service_contract__document",
            "service_contract__certificate",
            "organization_contract__counterparty",
            "organization_contract__funding_source",
            "organization_contract__template",
            "organization_contract__document",
            "template",
            "document",
        ).prefetch_related(
            "service_contract__service_lines__service",
            "organization_contract__service_lines__service",
        ),
        pk=pk,
    )
    if request.method != "POST":
        messages.warning(request, "Сформируйте Word-файл кнопкой в реестре договоров.")
        return redirect("contract_list")
    try:
        generated = contract_doc_svc.save_contract_act_docx(act, actor=request.user)
    except contract_doc_svc.ContractDocumentError as exc:
        messages.error(request, str(exc))
        return redirect("contract_list")
    return _docx_response(generated)


@login_required
@user_passes_test(is_admin_user)
def contract_act_archive_signed(request, pk: int):
    act = get_object_or_404(
        ContractAct.objects.select_related(
            "service_contract__child",
            "organization_contract__counterparty",
            "document",
        ),
        pk=pk,
    )
    if request.method != "POST":
        messages.warning(request, "Архив подписанного акта создается кнопкой в реестре.")
        return redirect("contract_list")
    uploaded_file, upload_error = _uploaded_signed_file_or_error(request)
    if upload_error:
        messages.error(request, upload_error)
        return redirect("contract_list")
    try:
        if uploaded_file is not None:
            signed_file = contract_doc_svc.archive_contract_act_uploaded_signed_file(
                act,
                uploaded_file,
                actor=request.user,
            )
        else:
            signed_file = contract_doc_svc.archive_contract_act_signed_file(
                act,
                actor=request.user,
            )
    except contract_doc_svc.ContractDocumentError as exc:
        messages.error(request, str(exc))
        return redirect("contract_list")
    messages.success(
        request,
        f"Подписанный файл акта сохранен в архиве: {signed_file.file_sha256[:12]}...",
    )
    return redirect("contract_list")


@login_required
@user_passes_test(is_admin_user)
def service_contract_archive_signed(request, pk: int):
    contract = get_object_or_404(
        ServiceContract.objects.select_related(
            "child",
            "representative_link__representative",
            "document",
            "document__contract_legal_snapshot",
        ),
        pk=pk,
    )
    if request.method != "POST":
        messages.warning(request, "Архив подписанного файла создается кнопкой в реестре.")
        return redirect("contract_list")
    uploaded_file, upload_error = _uploaded_signed_file_or_error(request)
    if upload_error:
        messages.error(request, upload_error)
        return redirect("contract_list")
    try:
        if uploaded_file is not None:
            signed_file = contract_doc_svc.archive_service_contract_uploaded_signed_file(
                contract,
                uploaded_file,
                actor=request.user,
            )
        else:
            signed_file = contract_doc_svc.archive_service_contract_signed_file(
                contract,
                actor=request.user,
            )
    except contract_doc_svc.ContractDocumentError as exc:
        messages.error(request, str(exc))
        return redirect("contract_list")
    messages.success(
        request,
        f"Подписанный файл договора с получателем сохранен в архиве: "
        f"{signed_file.file_sha256[:12]}...",
    )
    return redirect("contract_list")


@login_required
@user_passes_test(is_admin_user)
def organization_service_contract_archive_signed(request, pk: int):
    contract = get_object_or_404(
        OrganizationServiceContract.objects.select_related(
            "counterparty",
            "document",
            "document__contract_legal_snapshot",
        ),
        pk=pk,
    )
    if request.method != "POST":
        messages.warning(request, "Архив подписанного файла создается кнопкой в реестре.")
        return redirect("contract_list")
    uploaded_file, upload_error = _uploaded_signed_file_or_error(request)
    if upload_error:
        messages.error(request, upload_error)
        return redirect("contract_list")
    try:
        if uploaded_file is not None:
            signed_file = contract_doc_svc.archive_organization_service_contract_uploaded_signed_file(
                contract,
                uploaded_file,
                actor=request.user,
            )
        else:
            signed_file = contract_doc_svc.archive_organization_service_contract_signed_file(
                contract,
                actor=request.user,
            )
    except contract_doc_svc.ContractDocumentError as exc:
        messages.error(request, str(exc))
        return redirect("contract_list")
    messages.success(
        request,
        f"Подписанный файл B2B-договора услуг сохранен в архиве: "
        f"{signed_file.file_sha256[:12]}...",
    )
    return redirect("contract_list")


@login_required
@user_passes_test(is_admin_user)
def contract_signed_file_download(request, pk: int):
    signed_file = get_object_or_404(ContractSignedFile, pk=pk)
    if not signed_file.file:
        raise Http404("Архивный файл не найден.")
    try:
        signed_file.file.open("rb")
    except OSError as exc:
        raise Http404("Архивный файл не найден.") from exc
    return FileResponse(
        signed_file.file,
        as_attachment=True,
        filename=signed_file.original_filename,
        content_type=signed_file.content_type or "application/octet-stream",
    )


@login_required
@user_passes_test(is_admin_user)
def contract_act_signed_file_download(request, pk: int):
    signed_file = get_object_or_404(ContractActSignedFile, pk=pk)
    if not signed_file.file:
        raise Http404("Архивный файл акта не найден.")
    try:
        signed_file.file.open("rb")
    except OSError as exc:
        raise Http404("Архивный файл акта не найден.") from exc
    return FileResponse(
        signed_file.file,
        as_attachment=True,
        filename=signed_file.original_filename,
        content_type=signed_file.content_type or "application/octet-stream",
    )
