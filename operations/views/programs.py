from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from operations.forms import (
    GroupProgramJoinForm,
    GroupProgramSeriesForm,
    ProgramBlockForm,
    ProgramBlockScheduleWizardForm,
    ProgramFundsTransferForm,
    TreatmentProgramForm,
)
from operations.models import (
    AppointmentSeries,
    BalanceAccount,
    Child,
    ProgramBlock,
    TreatmentProgram,
)
from operations.services import billing as billing_svc, program_series, program_wizard

from ._common import admin_required, is_admin_user


def _form_value(form, field_name: str):
    value = form[field_name].value()
    if value in (None, ""):
        return None
    return value


def _form_choice_label(form, field_name: str, fallback: str) -> str:
    value = _form_value(form, field_name)
    if value is None:
        return fallback
    choices = {str(key): str(label) for key, label in form.fields[field_name].choices}
    return choices.get(str(value), fallback)


def _form_model_label(form, field_name: str, fallback: str) -> str:
    value = _form_value(form, field_name)
    if value is None:
        return fallback
    queryset = getattr(form.fields[field_name], "queryset", None)
    if queryset is None:
        return str(value)
    obj = queryset.filter(pk=value).first()
    return str(obj) if obj else fallback


def _wizard_bool_value(form, field_name: str) -> bool:
    value = _form_value(form, field_name)
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "on", "true", "yes"}


def program_block_wizard_summary_items(block: ProgramBlock, form, preview=None) -> list[dict]:
    planned_remaining = max(block.planned_sessions - block.scheduled_count, 0)
    funded_remaining = (
        preview.funded_remaining if preview else program_wizard.funded_sessions_remaining(block)
    )
    funded_label = "не ограничено"
    if funded_remaining is not None:
        funded_label = str(funded_remaining)
    requested = _form_value(form, "requested_count") or planned_remaining or 1
    return [
        {
            "label": "Каскад",
            "value": f"{block.number}. {block.title}",
            "hint": str(block.program.child),
        },
        {
            "label": "План",
            "value": f"{block.scheduled_count} / {block.planned_sessions}",
            "hint": f"осталось по плану: {planned_remaining}",
        },
        {
            "label": "Подбор",
            "value": f"запрошено: {requested}",
            "hint": f"доступно по оплате: {funded_label}",
        },
        {
            "label": "Специалист и кабинет",
            "value": _form_model_label(form, "staff_member", "автоподбор специалиста"),
            "hint": _form_model_label(form, "room", "автоподбор кабинета"),
        },
        {
            "label": "Создавать как",
            "value": _form_choice_label(form, "appointment_status", "Предложено / на согласование"),
            "hint": "бронь сверх оплаты включена"
            if _wizard_bool_value(form, "allow_unpaid_reserve")
            else "в пределах доступной оплаты",
        },
    ]


def program_block_wizard_attention_items(block: ProgramBlock, form, preview=None) -> list[dict]:
    items = []
    funded_remaining = (
        preview.funded_remaining if preview else program_wizard.funded_sessions_remaining(block)
    )
    if not block.balance_account_id:
        items.append(
            {
                "tone": "info",
                "title": "Счет оплаты не выбран",
                "text": (
                    "Без счета обычное планирование не создаст окна. "
                    "Для временной брони включите бронь сверх оплаты и укажите основание."
                ),
            }
        )
    elif funded_remaining == 0 and not _wizard_bool_value(form, "allow_unpaid_reserve"):
        items.append(
            {
                "tone": "warning",
                "title": "Нет доступной оплаты",
                "text": "Включите осознанную бронь сверх оплаты или пополните счет каскада.",
            }
        )
    if _wizard_bool_value(form, "allow_unpaid_reserve"):
        items.append(
            {
                "tone": "warning",
                "title": "Включена бронь сверх оплаты",
                "text": "Созданные занятия могут потребовать отдельного решения администратора по оплате.",
            }
        )
    if _wizard_bool_value(form, "allow_outside_availability"):
        items.append(
            {
                "tone": "warning",
                "title": "Разрешен подбор вне графика",
                "text": "Проверьте согласование со специалистом перед созданием занятий.",
            }
        )
    if preview and preview.limited_by_balance:
        items.append(
            {
                "tone": "warning",
                "title": "Количество ограничено оплатой",
                "text": f"Можно создать {preview.allowed_count} из {preview.requested_count}.",
            }
        )
    if preview and preview.missing_count:
        items.append(
            {
                "tone": "info",
                "title": "Не хватило свободных окон",
                "text": f"Не найдено окон: {preview.missing_count}. Расширьте период или время поиска.",
            }
        )
    if form.non_field_errors():
        items.append(
            {
                "tone": "danger",
                "title": "Мастер не готов к созданию",
                "text": "Проверьте параметры подбора и ошибки формы.",
            }
        )
    return items


def program_block_wizard_context(block: ProgramBlock, form, preview, cancel_url: str) -> dict:
    return {
        "block": block,
        "form": form,
        "preview": preview,
        "cancel_url": cancel_url,
        "wizard_summary_items": program_block_wizard_summary_items(block, form, preview),
        "wizard_attention_items": program_block_wizard_attention_items(block, form, preview),
    }


def _transfer_form_account(form, field_name: str, fallback=None):
    value = _form_value(form, field_name)
    if value is None:
        return fallback
    queryset = getattr(form.fields[field_name], "queryset", None)
    if queryset is None:
        return fallback
    return queryset.filter(pk=value).first() or fallback


def _transfer_operation_kind(form) -> str:
    return _form_value(form, "operation_kind") or "direct"


def _transfer_decimal_value(form, field_name: str) -> Decimal | None:
    value = _form_value(form, field_name)
    if value is None:
        return None
    try:
        amount = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount > 0 else None


def _transfer_amount_value(block: ProgramBlock, form) -> Decimal | None:
    if _transfer_operation_kind(form) == "money_to_sessions":
        sessions = _transfer_decimal_value(form, "sessions")
        price = block.service.default_price
        if sessions is None or not price or price <= 0:
            return None
        return sessions * price
    return _transfer_decimal_value(form, "amount")


def _transfer_credit_value(form) -> Decimal | None:
    field_name = "sessions" if _transfer_operation_kind(form) == "money_to_sessions" else "amount"
    return _transfer_decimal_value(form, field_name)


def _transfer_balance_label(account: BalanceAccount | None) -> str:
    if account is None:
        return "не выбран"
    return f"{account.current_balance} {account.get_unit_display()}"


def _transfer_estimated_sessions(
    block: ProgramBlock,
    target_account: BalanceAccount | None,
    amount: Decimal | None,
) -> int | None:
    if target_account is None or amount is None:
        return None
    if target_account.unit == BalanceAccount.Unit.SESSIONS:
        return int(amount.to_integral_value(rounding=ROUND_FLOOR))
    price = block.service.default_price
    if not price or price <= 0:
        return None
    return int((amount / price).to_integral_value(rounding=ROUND_FLOOR))


def program_block_transfer_summary_items(block: ProgramBlock, form) -> list[dict[str, str]]:
    source_account = _transfer_form_account(form, "from_account")
    target_account = _transfer_form_account(form, "to_account", block.balance_account)
    operation_kind = _transfer_operation_kind(form)
    amount_from = _transfer_amount_value(block, form)
    amount_to = _transfer_credit_value(form)
    estimated = _transfer_estimated_sessions(block, target_account, amount_to)

    source_after = source_account.current_balance - amount_from if source_account and amount_from else None
    target_after = target_account.current_balance + amount_to if target_account and amount_to else None
    estimated_hint = (
        f"примерно занятий после переноса: {estimated}"
        if estimated is not None
        else "количество занятий будет рассчитано после выбора суммы"
    )
    return [
        {
            "label": "Каскад",
            "value": f"{block.number}. {block.title}",
            "hint": str(block.program.child),
        },
        {
            "label": "Услуга",
            "value": str(block.service),
            "hint": str(block.staff_member) if block.staff_member else "специалист не выбран",
        },
        {
            "label": "Счет каскада",
            "value": str(target_account) if target_account else "не выбран",
            "hint": f"сейчас: {_transfer_balance_label(target_account)}",
        },
        {
            "label": "Откуда переносим",
            "value": str(source_account) if source_account else "выберите счет",
            "hint": (
                f"после переноса: {source_after} {source_account.get_unit_display()}"
                if source_after is not None
                else "исходный счет и остаток"
            ),
        },
        {
            "label": "Куда придет",
            "value": str(target_account) if target_account else "выберите счет",
            "hint": (
                f"после переноса: {target_after} {target_account.get_unit_display()}"
                if target_after is not None
                else "целевой счет каскада"
            ),
        },
        {
            "label": "Вид операции",
            "value": "Рубли в занятия" if operation_kind == "money_to_sessions" else "Прямой перенос",
            "hint": (
                f"курс: {block.service.default_price} руб. за занятие"
                if operation_kind == "money_to_sessions" and block.service.default_price
                else "единицы исходного и целевого счета совпадают"
            ),
        },
        {
            "label": "Объем операции",
            "value": (
                f"списать: {amount_from}" if amount_from is not None else "не указан"
            ),
            "hint": (
                f"зачислить: {amount_to}; {estimated_hint}"
                if amount_to is not None
                else estimated_hint
            ),
        },
    ]


def program_block_transfer_control_items(block: ProgramBlock, form) -> list[dict[str, str]]:
    source_account = _transfer_form_account(form, "from_account")
    target_account = _transfer_form_account(form, "to_account", block.balance_account)
    operation_kind = _transfer_operation_kind(form)
    amount = _transfer_amount_value(block, form)
    items = [
        {
            "tone": "info",
            "title": "Ledger сохранит две операции",
            "text": "С исходного счета будет создан debit, на целевой счет - credit с одним основанием.",
        }
    ]
    if form.errors:
        items.append(
            {
                "tone": "danger",
                "title": "Есть ошибки формы",
                "text": "Проверьте вид операции, счета, объем и основание переноса.",
            }
        )
    if not block.balance_account_id:
        items.append(
            {
                "tone": "warning",
                "title": "У каскада нет текущего счета",
                "text": "После успешного переноса выбранный целевой счет будет привязан к каскаду.",
            }
        )
    if not form.fields["from_account"].queryset.exists():
        items.append(
            {
                "tone": "warning",
                "title": "Нет доступного исходного счета",
                "text": "Для переноса нужен другой активный счет получателя из того же источника.",
            }
        )
    if (
        operation_kind == "direct"
        and source_account
        and target_account
        and source_account.unit != target_account.unit
    ):
        items.append(
            {
                "tone": "danger",
                "title": "Разные единицы счетов",
                "text": "Выберите конвертацию, если нужно перевести рубли в занятия.",
            }
        )
    if source_account and amount and amount > source_account.current_balance:
        items.append(
            {
                "tone": "warning",
                "title": "Сумма больше доступного остатка",
                "text": "Уменьшите сумму или сначала пополните исходный счет.",
            }
        )
    if operation_kind == "money_to_sessions":
        items.append(
            {
                "tone": "info",
                "title": "Курс будет сохранен",
                "text": "Система сама рассчитает рубли по цене услуги и сохранит курс в истории операции.",
            }
        )
    return items


def program_block_transfer_next_action(block: ProgramBlock, form) -> dict[str, str]:
    source_account = _transfer_form_account(form, "from_account")
    target_account = _transfer_form_account(form, "to_account", block.balance_account)
    operation_kind = _transfer_operation_kind(form)
    amount = _transfer_amount_value(block, form)
    if form.errors:
        return {
            "tone": "danger",
            "label": "Следующий шаг",
            "title": "Исправить ошибки",
            "detail": "Форма не готова к переносу средств.",
            "href": "#transfer-form",
        }
    if not source_account:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Выбрать исходный счет",
            "detail": "Нужен активный счет получателя, с которого можно списать средства.",
            "href": "#transfer-form",
        }
    if not target_account:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Выбрать счет каскада",
            "detail": "Без целевого счета перенос не сможет привязать средства к каскаду.",
            "href": "#transfer-form",
        }
    if amount is None:
        return {
            "tone": "info",
            "label": "Следующий шаг",
            "title": "Указать объем операции",
            "detail": (
                "Введите целое число занятий для конвертации."
                if operation_kind == "money_to_sessions"
                else "Введите сумму или количество в единицах выбранных счетов."
            ),
            "href": "#transfer-form",
        }
    if amount > source_account.current_balance:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Проверить остаток",
            "detail": "Сумма переноса больше доступного остатка исходного счета.",
            "href": "#transfer-form",
        }
    return {
        "tone": "success",
        "label": "Следующий шаг",
        "title": "Проверить и перенести",
        "detail": "После отправки будут созданы ledger-операции списания и пополнения.",
        "href": "#transfer-form",
    }


def program_block_transfer_context(block: ProgramBlock, form, cancel_url: str) -> dict:
    return {
        "block": block,
        "program_block": block,
        "form": form,
        "cancel_url": cancel_url,
        "transfer_summary_items": program_block_transfer_summary_items(block, form),
        "transfer_control_items": program_block_transfer_control_items(block, form),
        "transfer_next_action": program_block_transfer_next_action(block, form),
        "transfer_operation_kind": _transfer_operation_kind(form),
    }


def _program_control_items(child: Child | None = None) -> list[dict[str, str]]:
    recipient_detail = (
        f"Программа будет создана для получателя: {child.full_name}."
        if child
        else "Выберите получателя, для которого собирается программа занятий."
    )
    return [
        {
            "title": "Получатель",
            "detail": recipient_detail,
        },
        {
            "title": "Каскады и серии",
            "detail": (
                "После создания программы добавьте блоки: каждый блок задает услугу, план занятий, "
                "счет и дальнейшую нумерацию серии."
            ),
        },
        {
            "title": "Период программы",
            "detail": (
                "Даты помогают руководителю видеть рамки программы; расписание занятий создается отдельно "
                "через каскады."
            ),
        },
        {
            "title": "Консультация",
            "detail": "Связь с консультацией нужна как основание программы, если она уже была проведена.",
        },
    ]


def _program_block_control_items(program: TreatmentProgram, next_number: int) -> list[dict[str, str]]:
    return [
        {
            "title": "Номер каскада",
            "detail": (
                f"Следующий номер для этой программы: {next_number}. "
                "Номер используется в карточке получателя, расписании и отчетах."
            ),
        },
        {
            "title": "Услуга и специалист",
            "detail": (
                "Услуга определяет допустимые счета и будущие занятия; специалист может быть задан сразу "
                "или выбран позднее в мастере расписания."
            ),
        },
        {
            "title": "План и счет",
            "detail": (
                "План занятий задает объем каскада, а счет ограничивает создание занятий доступной оплатой "
                "или грантовой квотой получателя."
            ),
        },
        {
            "title": "Дальше расписание",
            "detail": (
                f"После сохранения блок появится в карточке {program.child}; затем можно открыть мастер "
                "подбора окон или перенос средств между счетами."
            ),
        },
    ]


@login_required
@user_passes_test(is_admin_user)
def program_create(request, child_id: int | None = None):
    child = get_object_or_404(Child, pk=child_id) if child_id else None
    if request.method == "POST":
        form = TreatmentProgramForm(request.POST, child=child)
        if form.is_valid():
            program = form.save()
            messages.success(request, "Программа занятий создана.")
            return redirect("recipient_detail", pk=program.child_id)
    else:
        form = TreatmentProgramForm(child=child)

    cancel_url = reverse("recipient_detail", args=[child.pk]) if child else reverse("recipient_list")
    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Создать программу занятий",
            "subtitle": child.full_name if child else "",
            "form_panel_title": "Параметры программы",
            "form_intro": (
                "Программа объединяет каскады занятий для одного получателя и служит рамкой для планирования."
            ),
            "control_title": "Контроль программы",
            "object_form_control_items": _program_control_items(child),
            "cancel_url": cancel_url,
        },
    )


@login_required
@user_passes_test(is_admin_user)
def program_block_create(request, program_id: int):
    program = get_object_or_404(TreatmentProgram.objects.select_related("child"), pk=program_id)
    next_number = (program.blocks.order_by("-number").values_list("number", flat=True).first() or 0) + 1
    if request.method == "POST":
        form = ProgramBlockForm(request.POST, program=program)
        if form.is_valid():
            block = form.save()
            messages.success(request, "Блок программы создан.")
            return redirect("recipient_detail", pk=block.program.child_id)
    else:
        form = ProgramBlockForm(program=program, initial={"number": next_number})

    return render(
        request,
        "operations/object_form.html",
        {
            "form": form,
            "title": "Добавить блок программы",
            "subtitle": str(program),
            "form_panel_title": "Параметры каскада",
            "form_intro": (
                "Каскад задает услугу, план занятий, специалиста и счет, от которого мастер расписания "
                "проверяет доступную оплату."
            ),
            "control_title": "Контроль каскада",
            "object_form_control_items": _program_block_control_items(program, next_number),
            "cancel_url": reverse("recipient_detail", args=[program.child_id]),
        },
    )


def _program_block_or_404(block_id: int) -> ProgramBlock:
    return get_object_or_404(
        ProgramBlock.objects.select_related(
            "program",
            "program__child",
            "service",
            "staff_member",
            "balance_account",
            "balance_account__funding_source",
            "balance_account__service",
        ),
        pk=block_id,
    )


@login_required
@user_passes_test(is_admin_user)
def program_block_schedule_wizard(request, block_id: int):
    block = _program_block_or_404(block_id)
    preview = None

    if request.method == "POST":
        form = ProgramBlockScheduleWizardForm(request.POST, block=block)
        if form.is_valid():
            data = form.cleaned_data
            preview = program_wizard.suggest_program_block_slots(
                block,
                date_from=data["start_date"],
                date_to=data["end_date"],
                weekdays={int(value) for value in data["weekdays"]},
                time_from=data["time_from"],
                time_until=data["time_until"],
                duration_minutes=data["duration_minutes"],
                staff_member=data["staff_member"],
                room=data["room"],
                requested_count=data["requested_count"],
                allow_outside_availability=data["allow_outside_availability"],
                allow_unpaid_reserve=data["allow_unpaid_reserve"],
            )
            if request.POST.get("action") == "create":
                if not preview.slots:
                    messages.warning(request, "Мастер не нашёл подходящих окон. Расписание не создано.")
                else:
                    try:
                        result = program_wizard.create_schedule_from_preview(
                            preview,
                            status=data["appointment_status"],
                            actor=request.user,
                        )
                    except ValidationError as exc:
                        form.add_error(None, exc)
                        messages.warning(
                            request,
                            "За время согласования расписание изменилось. Нажмите «Подобрать окна» ещё раз.",
                        )
                    else:
                        messages.success(request, f"Создано занятий: {len(result.appointments)}.")
                        if preview.limited_by_balance:
                            messages.warning(
                                request,
                                "Количество было ограничено доступным балансом. Для продолжения пополните счёт "
                                "или включите бронь сверх оплаты.",
                            )
                        if preview.missing_count:
                            messages.warning(
                                request,
                                f"Не хватило свободных окон: {preview.missing_count}. Расширьте даты или время поиска.",
                            )
                        return redirect("recipient_detail", pk=block.program.child_id)
            elif not preview.slots:
                messages.warning(request, "Подходящих окон не найдено. Попробуйте расширить период, время или кабинет.")
    else:
        form = ProgramBlockScheduleWizardForm(block=block)

    return render(
        request,
        "operations/program_block_schedule_wizard.html",
        program_block_wizard_context(
            block,
            form,
            preview,
            reverse("recipient_detail", args=[block.program.child_id]),
        ),
    )


def _group_series_preview_from_form(form: GroupProgramSeriesForm):
    data = form.cleaned_data
    return program_series.preview_group_series(
        blocks=form.selected_blocks(),
        staff_members=form.selected_staff_members(),
        room=data["room"],
        title=data["title"],
        start_date=data["start_date"],
        end_date=data["end_date"],
        weekdays={int(value) for value in data["weekdays"]},
        start_time=data["start_time"],
        duration_minutes=data["duration_minutes"],
        default_appointment_status=data["default_appointment_status"],
        allow_unpaid_reserve=data["allow_unpaid_reserve"],
        allow_outside_availability=data["allow_outside_availability"],
        override_reason=data["override_reason"],
    )


@admin_required
def program_block_group_join(request, block_id: int):
    block = _program_block_or_404(block_id)
    preview = None
    selected_ids = {
        int(value)
        for value in request.POST.getlist("appointments")
        if str(value).isdigit()
    }
    if request.method == "POST":
        form = GroupProgramJoinForm(request.POST, block=block)
        if form.is_valid():
            data = form.cleaned_data
            try:
                preview = program_series.preview_group_joins(
                    block=block,
                    date_from=data["date_from"],
                    date_to=data["date_to"],
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                if request.POST.get("action") == "join":
                    selected = list(data["appointments"])
                    if not selected:
                        form.add_error("appointments", "Выберите хотя бы одно занятие.")
                    else:
                        try:
                            selected_preview = program_series.preview_group_joins(
                                block=block,
                                date_from=data["date_from"],
                                date_to=data["date_to"],
                                appointments=selected,
                            )
                            operation_exists = AppointmentSeries.objects.filter(
                                operation_key=data["operation_key"]
                            ).exists()
                            blocked = [
                                candidate
                                for candidate in selected_preview.candidates
                                if not candidate.ready
                            ]
                            available_count = min(
                                selected_preview.planned_remaining,
                                selected_preview.funded_remaining,
                            )
                            if blocked and not operation_exists:
                                reasons = "; ".join(
                                    candidate.reason for candidate in blocked[:3]
                                )
                                raise ValidationError(
                                    "Выбранные занятия больше не готовы: " + reasons
                                )
                            if len(selected) > available_count and not operation_exists:
                                raise ValidationError(
                                    "Выбрано больше занятий, чем доступно по плану и оплате."
                                )
                            result = program_series.join_program_block_to_groups(
                                block=block,
                                appointments=selected,
                                operation_key=data["operation_key"],
                                actor=request.user,
                            )
                        except ValidationError as exc:
                            form.add_error("appointments", exc)
                        else:
                            if result.joined_count:
                                messages.success(
                                    request,
                                    f"Получатель присоединен к занятиям: "
                                    f"{result.joined_count}. Пропущено: "
                                    f"{result.skipped_count}.",
                                )
                            elif result.skipped_count:
                                messages.warning(
                                    request,
                                    "Новые присоединения не созданы: выбранные занятия "
                                    "изменились или больше не готовы.",
                                )
                            if result.reused_series:
                                messages.info(
                                    request,
                                    "Повторный запрос распознан без создания дублей.",
                                )
                            return redirect(
                                "appointment_series_detail",
                                series_id=result.series.pk,
                            )
    else:
        form = GroupProgramJoinForm(block=block)
        try:
            preview = program_series.preview_group_joins(
                block=block,
                date_from=form.initial["date_from"],
                date_to=form.initial["date_to"],
            )
        except ValidationError as exc:
            messages.warning(request, "; ".join(exc.messages))

    return render(
        request,
        "operations/group_program_join_form.html",
        {
            "program_block": block,
            "form": form,
            "preview": preview,
            "selected_ids": selected_ids,
            "cancel_url": reverse("recipient_detail", args=[block.program.child_id]),
        },
    )


@admin_required
def program_block_group_series_create(request, block_id: int):
    block = _program_block_or_404(block_id)
    preview = None
    if request.method == "POST":
        form = GroupProgramSeriesForm(request.POST, block=block)
        if form.is_valid():
            try:
                preview = _group_series_preview_from_form(form)
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                if request.POST.get("action") == "create":
                    if not preview.ready_count:
                        messages.warning(
                            request,
                            "Нет ни одной даты, которую можно безопасно создать. Исправьте конфликты.",
                        )
                    else:
                        result = program_series.create_group_series(
                            preview,
                            operation_key=form.cleaned_data["operation_key"],
                            actor=request.user,
                        )
                        messages.success(
                            request,
                            f"Групповая серия создана. Занятий: {result.created_count}, "
                            f"пропущено дат: {result.skipped_count}.",
                        )
                        if result.reused_series:
                            messages.info(
                                request,
                                "Повторный запрос распознан: существующая серия использована без дублей.",
                            )
                        return redirect("appointment_series_detail", series_id=result.series.pk)
                elif not preview.ready_count:
                    messages.warning(
                        request,
                        "Все даты имеют ограничения. Причины показаны в предварительной проверке.",
                    )
    else:
        form = GroupProgramSeriesForm(block=block)

    return render(
        request,
        "operations/group_program_series_form.html",
        {
            "program_block": block,
            "form": form,
            "preview": preview,
            "cancel_url": reverse("recipient_detail", args=[block.program.child_id]),
        },
    )


@admin_required
def appointment_series_detail(request, series_id: int):
    series = get_object_or_404(
        AppointmentSeries.objects.select_related(
            "child",
            "service",
            "staff_member",
            "room",
            "program_block",
        ).prefetch_related(
            "default_participants__child",
            "default_participants__program_block__program",
            "default_participants__billing_account__funding_source",
            "default_staff_assignments__staff_member",
            "occurrences__appointment",
        ),
        pk=series_id,
    )
    return render(
        request,
        "operations/appointment_series_detail.html",
        {
            "series": series,
            "participants": series.default_participants.all(),
            "staff_assignments": series.default_staff_assignments.all(),
            "occurrences": series.occurrences.all(),
        },
    )


@admin_required
def program_block_transfer_funds(request, block_id: int):
    block = _program_block_or_404(block_id)

    if request.method == "POST":
        form = ProgramFundsTransferForm(request.POST, block=block)
        if form.is_valid():
            try:
                if form.cleaned_data["operation_kind"] == "money_to_sessions":
                    transfer = billing_svc.convert_money_to_sessions(
                        from_account=form.cleaned_data["from_account"],
                        to_account=form.cleaned_data["to_account"],
                        program_block=block,
                        sessions=form.cleaned_data["sessions"],
                        reason=form.cleaned_data["reason"],
                        actor=request.user,
                        idempotency_key=form.cleaned_data["idempotency_key"],
                    )
                else:
                    transfer = billing_svc.record_balance_transfer(
                        from_account=form.cleaned_data["from_account"],
                        to_account=form.cleaned_data["to_account"],
                        amount=form.cleaned_data["amount"],
                        reason=form.cleaned_data["reason"],
                        actor=request.user,
                        program_block=block,
                        idempotency_key=form.cleaned_data["idempotency_key"],
                    )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                block.refresh_from_db()
                funded_remaining = program_wizard.funded_sessions_remaining(block)
                funded_suffix = (
                    f" Доступно по оплате для каскада: {funded_remaining} зан."
                    if funded_remaining is not None
                    else ""
                )
                messages.success(
                    request,
                    f"Операция переноса #{transfer.pk} зафиксирована.{funded_suffix}",
                )
                return redirect("recipient_detail", pk=block.program.child_id)
    else:
        form = ProgramFundsTransferForm(block=block)

    return render(
        request,
        "operations/program_block_transfer_funds.html",
        program_block_transfer_context(
            block,
            form,
            reverse("recipient_detail", args=[block.program.child_id]),
        ),
    )
