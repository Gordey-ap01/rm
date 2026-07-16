from __future__ import annotations

from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from operations.models import Child, DonationContract, ServiceContract

STYLES = getSampleStyleSheet()
STYLES.add(
    ParagraphStyle(
        "Title_ru",
        parent=STYLES["Title"],
        fontName="Helvetica",
        fontSize=16,
        leading=20,
        spaceAfter=12,
        alignment=1,
    )
)
STYLES.add(ParagraphStyle("Body_ru", parent=STYLES["Normal"], fontName="Helvetica", fontSize=10, leading=14))
STYLES.add(
    ParagraphStyle("Small_ru", parent=STYLES["Normal"], fontName="Helvetica", fontSize=8, leading=10)
)
STYLES.add(
    ParagraphStyle(
        "Right_ru",
        parent=STYLES["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        alignment=2,
    )
)


def _build_contract_story(child: Child) -> list[Any]:
    today = timezone.localdate()
    parent = child.primary_parent
    parent_name = parent.full_name if parent else "______________________"
    story: list[Any] = []

    story.append(Paragraph("ДОГОВОР", STYLES["Title_ru"]))
    story.append(Paragraph(f"{'на оказание реабилитационных услуг'}", STYLES["Body_ru"]))
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph(f"г. Владивосток &nbsp;&nbsp; {today:%d}.{today:%m}.{today:%Y}", STYLES["Right_ru"]))
    story.append(Spacer(1, 8 * mm))

    data = [
        ["1.", "Реабилитационный центр (далее — Центр)"],
        ["2.", f"Представитель: {parent_name}"],
        ["3.", f"Получатель услуг: {child.full_name}"],
        ["", f"Дата рождения: {child.birth_date.isoformat() if child.birth_date else '_______________'}"],
        ["4.", "Настоящий договор вступает в силу с даты подписания."],
        ["5.", "Срок действия договора: 1 (один) календарный месяц."],
        ["6.", "Стоимость услуг определяется согласно действующему прейскуранту Центра."],
        [
            "7.",
            "Оплата производится ежемесячно на основании выставленного счёта не позднее 10 числа текущего месяца.",
        ],
        ["8.", "Центр обязуется:"],
        ["", "8.1. Обеспечить проведение занятий в соответствии с утверждённым расписанием."],
        ["", "8.2. Информировать представителя об изменениях в расписании."],
        ["9.", "Представитель обязуется:"],
        ["", "9.1. Своевременно информировать об отмене занятия не менее чем за 24 часа."],
        ["", "9.2. Обеспечить присутствие получателя на занятиях."],
        [
            "10.",
            "Центр не несёт ответственности за невозможность проведения занятий по вине представителя или получателя услуг.",
        ],
        ["11.", "Договор может быть расторгнут досрочно по письменному заявлению любой из сторон."],
        ["12.", "Споры разрешаются путём переговоров, при недостижении согласия — в судебном порядке."],
    ]

    table = Table(data, colWidths=[15 * mm, 160 * mm])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("LEADING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 12 * mm))

    story.append(Paragraph("Подписи сторон:", STYLES["Body_ru"]))
    story.append(Spacer(1, 4 * mm))
    story.append(
        Paragraph(
            "Представитель: ______________________ &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; /_______________/",
            STYLES["Body_ru"],
        )
    )
    story.append(Spacer(1, 3 * mm))
    story.append(
        Paragraph(
            "Центр: &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; ______________________ &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; /_______________/",
            STYLES["Body_ru"],
        )
    )
    return story


def _safe(value: object, fallback: str = "_______________") -> str:
    if value is None or value == "":
        return fallback
    return escape(str(value))


def _contract_template_label(template) -> str:
    if template is None:
        return "Базовый системный шаблон"
    version = f" v{template.version}" if template.version else ""
    return f"{template.title}{version}"


def _build_service_contract_story(contract: ServiceContract) -> list[Any]:
    story: list[Any] = []
    child = contract.child
    signer = contract.representative_link.representative
    signed_on = contract.signed_on or timezone.localdate()
    number = contract.number or "б/н"

    story.append(Paragraph("ДОГОВОР", STYLES["Title_ru"]))
    story.append(Paragraph("на оказание реабилитационных услуг", STYLES["Body_ru"]))
    story.append(Paragraph(f"Шаблон: {_safe(_contract_template_label(contract.template), '')}", STYLES["Small_ru"]))
    story.append(Spacer(1, 6 * mm))
    story.append(
        Paragraph(
            f"№ {_safe(number)} &nbsp;&nbsp; г. Владивосток &nbsp;&nbsp; {signed_on:%d.%m.%Y}",
            STYLES["Right_ru"],
        )
    )
    story.append(Spacer(1, 8 * mm))

    data = [
        ["1.", "Реабилитационный центр (далее — Центр)"],
        ["2.", f"Представитель: {_safe(signer.full_name)}"],
        ["3.", f"Получатель услуг: {_safe(child.full_name)}"],
        ["", f"Дата рождения: {_safe(child.birth_date.isoformat() if child.birth_date else '')}"],
        ["4.", f"Тип договора: {_safe(contract.get_contract_type_display())}"],
        ["5.", f"Статус реестра: {_safe(contract.get_status_display())}"],
        [
            "6.",
            (
                "Срок действия: "
                f"{_safe(contract.valid_from.strftime('%d.%m.%Y') if contract.valid_from else '')} - "
                f"{_safe(contract.valid_until.strftime('%d.%m.%Y') if contract.valid_until else '')}"
            ),
        ],
        ["7.", "Стоимость услуг определяется согласно действующему прейскуранту Центра."],
        ["8.", "Центр обязуется проводить занятия в соответствии с утверждённым расписанием."],
        ["9.", "Представитель обязуется своевременно информировать Центр об отменах и изменениях."],
        ["10.", "Детальные юридические условия уточняются в утвержденном шаблоне договора."],
    ]
    table = Table(data, colWidths=[15 * mm, 160 * mm])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("LEADING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 12 * mm))
    story.append(Paragraph("Подписи сторон:", STYLES["Body_ru"]))
    story.append(Spacer(1, 4 * mm))
    story.append(
        Paragraph(
            "Представитель: ______________________ &nbsp;&nbsp;&nbsp;&nbsp; /_______________/",
            STYLES["Body_ru"],
        )
    )
    story.append(Spacer(1, 3 * mm))
    story.append(
        Paragraph(
            "Центр: ______________________________ &nbsp;&nbsp;&nbsp;&nbsp; /_______________/",
            STYLES["Body_ru"],
        )
    )
    return story


def _build_donation_contract_story(contract: DonationContract) -> list[Any]:
    story: list[Any] = []
    signed_on = contract.signed_on or timezone.localdate()
    number = contract.number or "б/н"
    amount = f"{contract.amount_limit:,.2f}".replace(",", " ").replace(".", ",") if contract.amount_limit else "без лимита"

    story.append(Paragraph("ДОГОВОР ПОЖЕРТВОВАНИЯ", STYLES["Title_ru"]))
    story.append(Paragraph(f"Шаблон: {_safe(_contract_template_label(contract.template), '')}", STYLES["Small_ru"]))
    story.append(Spacer(1, 6 * mm))
    story.append(
        Paragraph(
            f"№ {_safe(number)} &nbsp;&nbsp; г. Владивосток &nbsp;&nbsp; {signed_on:%d.%m.%Y}",
            STYLES["Right_ru"],
        )
    )
    story.append(Spacer(1, 8 * mm))

    data = [
        ["1.", f"Жертвователь/спонсор: {_safe(contract.counterparty.name)}"],
        ["2.", f"Источник финансирования: {_safe(contract.funding_source.name)}"],
        ["3.", f"Тип договора: {_safe(contract.get_contract_type_display())}"],
        ["4.", f"Статус реестра: {_safe(contract.get_status_display())}"],
        ["5.", f"Лимит суммы: {_safe(amount)}"],
        [
            "6.",
            (
                "Срок действия: "
                f"{_safe(contract.valid_from.strftime('%d.%m.%Y') if contract.valid_from else '')} - "
                f"{_safe(contract.valid_until.strftime('%d.%m.%Y') if contract.valid_until else '')}"
            ),
        ],
        ["7.", "Договор не создает автоматические платежи или балансы получателей."],
        ["8.", "Отчетность по использованию средств ведется по источнику финансирования."],
        ["9.", "Детальные юридические условия уточняются в утвержденном шаблоне договора."],
    ]
    table = Table(data, colWidths=[15 * mm, 160 * mm])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("LEADING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 12 * mm))
    story.append(Paragraph("Подписи сторон:", STYLES["Body_ru"]))
    story.append(Spacer(1, 4 * mm))
    story.append(
        Paragraph(
            "Жертвователь/спонсор: ______________ &nbsp;&nbsp;&nbsp;&nbsp; /_______________/",
            STYLES["Body_ru"],
        )
    )
    story.append(Spacer(1, 3 * mm))
    story.append(
        Paragraph(
            "Центр: ______________________________ &nbsp;&nbsp;&nbsp;&nbsp; /_______________/",
            STYLES["Body_ru"],
        )
    )
    return story


def _build_pdf(story: list[Any]) -> BytesIO:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )
    doc.build(story)
    buf.seek(0)
    return buf


def contract_pdf(child: Child) -> BytesIO:
    return _build_pdf(_build_contract_story(child))


def service_contract_pdf(contract: ServiceContract) -> BytesIO:
    return _build_pdf(_build_service_contract_story(contract))


def donation_contract_pdf(contract: DonationContract) -> BytesIO:
    return _build_pdf(_build_donation_contract_story(contract))
