"""Receipt rendering from the frozen snapshot (PDF); the HTML version is a template."""

import io
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


def money(paise):
    value = (Decimal(paise) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"Rs {value:,.2f}"


def render_pdf(document, member_name):
    snap = document.snapshot
    lib = snap["library"]
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    pdf.setTitle(f"Receipt {document.pk}")
    width, height = A4
    y = height - 20 * mm
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(20 * mm, y, lib["name"])
    pdf.setFont("Helvetica", 9)
    for line in (lib.get("address"), lib.get("phone"), lib.get("email")):
        if line:
            y -= 5 * mm
            pdf.drawString(20 * mm, y, line)
    y -= 10 * mm
    pdf.setFont("Helvetica", 10)
    pdf.drawString(20 * mm, y, f"Member: {snap['membership_no']}  {member_name}")
    pdf.drawRightString(width - 20 * mm, y, f"Date: {date.fromisoformat(snap['date']):%d %b %Y}")
    for item in snap["lines"]:
        y -= 12 * mm
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(20 * mm, y, f"{item['receipt_no']}   {item['label']}")
        pdf.drawRightString(width - 20 * mm, y, money(item["amount_paise"]))
        pdf.setFont("Helvetica", 9)
        for key, value in item.get("detail", {}).items():
            y -= 5 * mm
            pdf.drawString(24 * mm, y, f"{key.replace('_', ' ').capitalize()}: {value}")
        for pay in item["payments"]:
            y -= 5 * mm
            ref = f"  ref {pay['reference']}" if pay["reference"] else ""
            pdf.drawString(
                24 * mm,
                y,
                f"{pay['method'].upper()} {money(pay['amount_paise'])}  paid {pay['paid_on']}{ref}",
            )
        y -= 4 * mm
        pdf.setFont("Helvetica", 8)
        pdf.drawString(24 * mm, y, f"Transaction ID {item['internal_txn_id']}")
        if y < 40 * mm:
            pdf.showPage()
            y = height - 20 * mm
    if snap.get("notes"):
        y -= 8 * mm
        pdf.setFont("Helvetica", 9)
        pdf.drawString(20 * mm, y, snap["notes"][:110])
    if lib.get("footer"):
        pdf.setFont("Helvetica-Oblique", 8)
        pdf.drawString(20 * mm, 15 * mm, lib["footer"][:120])
    pdf.showPage()
    pdf.save()
    return buf.getvalue()
