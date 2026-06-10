from __future__ import annotations

from io import BytesIO
from typing import Any

from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from operations.models import Child

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


def contract_pdf(child: Child) -> BytesIO:
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm, topMargin=20*mm, bottomMargin=20*mm)
    doc.build(_build_contract_story(child))
    buf.seek(0)
    return buf
