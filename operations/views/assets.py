"""Equipment and significant inventory registry."""

from __future__ import annotations

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import EquipmentAssetForm
from operations.models import EquipmentAsset

from ._common import is_admin_user


def _format_money(amount: Decimal | None) -> str:
    if amount is None:
        return "не указана"
    value = amount.quantize(Decimal("0.01"))
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _asset_queryset(request) -> list[EquipmentAsset]:
    queryset = EquipmentAsset.objects.select_related(
        "purchase_expense",
        "purchase_expense__category",
        "responsible_staff",
    )
    status = request.GET.get("status", "")
    if status in {choice[0] for choice in EquipmentAsset.Status.choices}:
        queryset = queryset.filter(status=status)

    asset_type = request.GET.get("asset_type", "")
    if asset_type in {choice[0] for choice in EquipmentAsset.AssetType.choices}:
        queryset = queryset.filter(asset_type=asset_type)

    query = request.GET.get("q", "").strip()
    if query:
        queryset = queryset.filter(
            Q(name__icontains=query)
            | Q(inventory_number__icontains=query)
            | Q(location__icontains=query)
            | Q(responsible_staff__full_name__icontains=query)
            | Q(purchase_expense__title__icontains=query)
        )

    assets = list(queryset.order_by("status", "asset_type", "name")[:300])
    for asset in assets:
        asset.ui_amount_display = _format_money(asset.total_amount)
        asset.ui_purchase_expense_url = (
            reverse("center_expense_edit", args=[asset.purchase_expense_id])
            if asset.purchase_expense_id
            else ""
        )
    return assets


def asset_summary_items(assets: list[EquipmentAsset]) -> list[dict[str, str]]:
    active_count = sum(1 for asset in assets if asset.status == EquipmentAsset.Status.ACTIVE)
    repair_count = sum(1 for asset in assets if asset.status == EquipmentAsset.Status.IN_REPAIR)
    written_off_count = sum(
        1
        for asset in assets
        if asset.status in {EquipmentAsset.Status.WRITTEN_OFF, EquipmentAsset.Status.LOST}
    )
    linked_count = sum(1 for asset in assets if asset.purchase_expense_id)
    total_amount = sum(
        (asset.total_amount for asset in assets if asset.total_amount is not None),
        Decimal("0"),
    )
    return [
        {
            "label": "Активов",
            "value": str(len(assets)),
            "hint": f"в работе: {active_count}",
        },
        {
            "label": "Стоимость",
            "value": _format_money(total_amount),
            "hint": "по текущему фильтру",
        },
        {
            "label": "В ремонте",
            "value": str(repair_count),
            "hint": "требуют контроля",
        },
        {
            "label": "Списано/утеряно",
            "value": str(written_off_count),
            "hint": f"с расходом покупки: {linked_count}",
        },
    ]


def asset_next_action(assets: list[EquipmentAsset]) -> dict[str, str]:
    if not assets:
        return {
            "tone": "info",
            "label": "Следующий шаг",
            "title": "Добавить первый актив",
            "detail": "Свяжите оборудование с расходом покупки, если расход уже внесен в реестр.",
            "href": reverse("equipment_asset_create"),
        }
    unlinked_count = sum(1 for asset in assets if not asset.purchase_expense_id)
    if unlinked_count:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Проверить связь с расходами",
            "detail": f"Активов без расхода покупки: {unlinked_count}. Их можно оставить без связи, если стоимость неизвестна.",
            "href": "#equipment-asset-list",
        }
    return {
        "tone": "success",
        "label": "Следующий шаг",
        "title": "Реестр связан с расходами",
        "detail": "Списание или архивирование актива не удаляет расход покупки.",
        "href": "#equipment-asset-list",
    }


def asset_form_control_items(asset: EquipmentAsset | None) -> list[dict[str, str]]:
    if asset and asset.pk:
        status_detail = f"Текущий статус: {asset.get_status_display()}."
    else:
        status_detail = "Новый актив будет создан в реестре оборудования."
    return [
        {
            "title": "Расход покупки",
            "detail": "Связать можно только расход категории оборудования. Сам расход не изменяется и не удаляется.",
        },
        {
            "title": "Инвентарный номер",
            "detail": "Если номер заполнен, он должен быть уникальным во всем реестре.",
        },
        {
            "title": "Статус актива",
            "detail": status_detail,
        },
        {
            "title": "Без финансовых проводок",
            "detail": "Реестр оборудования не создает оплаты, списания занятий или ledger-записи.",
        },
    ]


@login_required
@user_passes_test(is_admin_user)
def equipment_asset_list(request):
    assets = _asset_queryset(request)
    return render(
        request,
        "operations/equipment_asset_list.html",
        {
            "assets": assets,
            "asset_summary_items": asset_summary_items(assets),
            "asset_next_action": asset_next_action(assets),
            "status_choices": EquipmentAsset.Status.choices,
            "asset_type_choices": EquipmentAsset.AssetType.choices,
            "filters": {
                "q": request.GET.get("q", "").strip(),
                "status": request.GET.get("status", ""),
                "asset_type": request.GET.get("asset_type", ""),
            },
        },
    )


@login_required
@user_passes_test(is_admin_user)
def equipment_asset_create(request):
    if request.method == "POST":
        form = EquipmentAssetForm(request.POST)
        if form.is_valid():
            asset = form.save()
            messages.success(request, "Актив добавлен в реестр оборудования.")
            return redirect("equipment_asset_edit", pk=asset.pk)
    else:
        form = EquipmentAssetForm()

    return render(
        request,
        "operations/equipment_asset_form.html",
        {
            "title": "Добавить оборудование",
            "subtitle": "Реестр оборудования и значимого инвентаря.",
            "asset": None,
            "form": form,
            "cancel_url": reverse("equipment_asset_list"),
            "asset_form_control_items": asset_form_control_items(None),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def equipment_asset_edit(request, pk: int):
    asset = get_object_or_404(
        EquipmentAsset.objects.select_related(
            "purchase_expense",
            "purchase_expense__category",
            "responsible_staff",
        ),
        pk=pk,
    )
    if request.method == "POST":
        form = EquipmentAssetForm(request.POST, instance=asset)
        if form.is_valid():
            form.save()
            messages.success(request, "Актив обновлен.")
            return redirect("equipment_asset_list")
    else:
        form = EquipmentAssetForm(instance=asset)

    return render(
        request,
        "operations/equipment_asset_form.html",
        {
            "title": "Редактировать оборудование",
            "subtitle": str(asset),
            "asset": asset,
            "form": form,
            "cancel_url": reverse("equipment_asset_list"),
            "asset_form_control_items": asset_form_control_items(asset),
        },
    )
