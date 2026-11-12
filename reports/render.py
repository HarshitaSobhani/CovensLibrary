"""Turn a Report into CSV, XLSX or PDF bytes, and format cells for the screen.

Every text cell is escaped against spreadsheet formula injection (R-30): a value that starts with
= + - @ (or a tab / carriage return) gets a leading apostrophe, in CSV and XLSX alike. Money is
rounded once, half-up, from the exact amount (D-28e)."""

import csv
import io
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from xml.sax.saxutils import escape as xml_escape

from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from core.spreadsheet import (  # noqa: F401 - re-exported for the report modules and tests
    PHONE_RE,
    safe_text,
    xlsx_clean,
    xlsx_text,
)

from .core import DATE, DATETIME, INT, MONEY, NUMBER, PERCENT, TEXT


def to_decimal(paise, places="0.01"):
    value = Fraction(paise)
    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
        Decimal(places), rounding=ROUND_HALF_UP
    )


def money_decimal(paise):
    return to_decimal(Fraction(paise) / 100)


def display(value, kind):
    """The text shown on screen, in a PDF and in CSV (CSV money has no symbol)."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value  # a label such as "Total" in a non-text column
    if kind == MONEY:
        return f"{money_decimal(value):,.2f}"
    if kind == DATE:
        return value.strftime("%d %b %Y")
    if kind == DATETIME:
        return timezone.localtime(value).strftime("%d %b %Y %H:%M")
    if kind == INT:
        return str(value)
    if kind == NUMBER:
        return f"{to_decimal(value, '0.1')}"
    if kind == PERCENT:
        return f"{to_decimal(value, '0.1')}%"
    return str(value)


def _cells(section, row, escape):
    out = []
    for column, value in zip(section.columns, row, strict=True):
        text = display(value, column.kind)
        out.append(safe_text(text) if escape and column.kind == TEXT else text)
    return out


def rows_for_screen(section):
    body = [_cells(section, r, escape=False) for r in section.rows]
    total = _cells(section, section.total_row, escape=False) if section.total_row else None
    return body, total


# ------------------------------------------------------------------ CSV
def to_csv(report):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([safe_text(report.title)])
    writer.writerow([safe_text(report.subtitle)])
    for note in report.notes:
        writer.writerow([safe_text(note)])
    for section in report.sections:
        writer.writerow([])
        writer.writerow([safe_text(section.title)])
        if section.note:
            writer.writerow([safe_text(section.note)])
        writer.writerow([safe_text(c.label) for c in section.columns])
        for row in section.rows:
            writer.writerow(_cells(section, row, escape=True))
        if section.total_row:
            writer.writerow(_cells(section, section.total_row, escape=True))
    return ("﻿" + buf.getvalue()).encode("utf-8")


# ------------------------------------------------------------------ XLSX
def _sheet_title(title, used):
    clean = "".join(ch for ch in xlsx_clean(title) if ch not in "[]:*?/\\")[:28].strip() or "Sheet"
    name, n = clean, 2
    while name in used:
        name = f"{clean[:25]}-{n}"
        n += 1
    used.add(name)
    return name


def _xlsx_value(value, kind):
    if value is None or value == "":
        return None
    if isinstance(value, str) and kind != TEXT:
        return xlsx_text(value)
    if kind == MONEY:
        return float(money_decimal(value))
    if kind == INT:
        return int(value)
    if kind in (NUMBER, PERCENT):
        return float(to_decimal(value, "0.1"))
    if kind == DATE:
        return value if isinstance(value, date) else None
    if kind == DATETIME:
        return (
            timezone.localtime(value).replace(tzinfo=None) if isinstance(value, datetime) else None
        )
    return xlsx_text(value)


def _write_cell(ws, row, col, value, kind):
    cell = ws.cell(row=row, column=col)
    cell.value = _xlsx_value(value, kind)
    if isinstance(cell.value, str):
        cell.data_type = "s"  # never a formula, even if escaping were bypassed
    if kind == MONEY:
        cell.number_format = "#,##0.00"
    elif kind == DATE:
        cell.number_format = "DD MMM YYYY"
    elif kind == DATETIME:
        cell.number_format = "DD MMM YYYY HH:MM"
    elif kind == PERCENT:
        cell.number_format = '0.0"%"'
    elif kind == NUMBER:
        cell.number_format = "0.0"


def to_xlsx(report):
    wb = Workbook()
    wb.remove(wb.active)
    used = set()
    for section in report.sections or []:
        ws = wb.create_sheet(_sheet_title(section.title, used))
        ws.cell(row=1, column=1, value=xlsx_text(report.title)).font = Font(bold=True)
        ws.cell(row=2, column=1, value=xlsx_text(report.subtitle))
        ws.cell(row=3, column=1, value=xlsx_text(section.title))
        for i, column in enumerate(section.columns, start=1):
            ws.cell(row=5, column=i, value=xlsx_text(column.label)).font = Font(bold=True)
        r = 6
        for row in section.rows:
            for i, (column, value) in enumerate(zip(section.columns, row, strict=True), start=1):
                _write_cell(ws, r, i, value, column.kind)
            r += 1
        if section.total_row:
            for i, (column, value) in enumerate(
                zip(section.columns, section.total_row, strict=True), start=1
            ):
                _write_cell(ws, r, i, value, column.kind)
                ws.cell(row=r, column=i).font = Font(bold=True)
            r += 1
        for n, note in enumerate(
            report.notes + ([section.note] if section.note else []), start=r + 1
        ):
            ws.cell(row=n, column=1, value=xlsx_text(note)).data_type = "s"
        for i in range(1, len(section.columns) + 1):
            ws.column_dimensions[get_column_letter(i)].width = 22
    if not wb.sheetnames:
        wb.create_sheet("Report")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ PDF
def to_pdf(report):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=7.5, leading=9)

    def para(text, style=small):
        return Paragraph(xml_escape(str(text)), style)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=report.title,
    )
    story = [para(report.title, styles["Title"]), para(report.subtitle, styles["BodyText"])]
    for section in report.sections:
        story += [Spacer(1, 4 * mm), para(section.title, styles["Heading3"])]
        if section.note:
            story.append(para(section.note))
        data = [[para(c.label, small) for c in section.columns]]
        for row in section.rows:
            data.append([para(v) for v in _cells(section, row, escape=False)])
        if section.total_row:
            data.append([para(v) for v in _cells(section, section.total_row, escape=False)])
        if len(data) == 1:
            story.append(para("No records."))
            continue
        table = Table(data, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e2d6")),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(table)
    story.append(Spacer(1, 4 * mm))
    for note in report.notes:
        story.append(para(note))
    doc.build(story)
    return buf.getvalue()
