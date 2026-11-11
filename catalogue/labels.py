import io
from decimal import Decimal

from reportlab.graphics.barcode import code128
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from core.errors import RuleViolation

from .models import LabelLayout


def default_layout():
    layout = LabelLayout.objects.filter(is_default=True).first()
    if layout is None:
        layout, _ = LabelLayout.objects.get_or_create(
            name="Default A4 sheet", defaults={"is_default": not LabelLayout.objects.exists()}
        )
    return layout


def validate_layout(layout):
    width = layout.margin_mm * 2 + layout.label_width_mm * layout.columns
    height = layout.margin_mm * 2 + layout.label_height_mm * layout.rows
    if width > layout.page_width_mm or height > layout.page_height_mm:
        raise RuleViolation("layout_too_big", "The label grid does not fit on the page.")


def render_labels_pdf(copies, layout):
    """Printable Code 128 label sheet; label size and grid come from the owner's layout."""
    validate_layout(layout)
    per_page = layout.columns * layout.rows
    buf = io.BytesIO()
    page_w, page_h = float(layout.page_width_mm) * mm, float(layout.page_height_mm) * mm
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    lw, lh = float(layout.label_width_mm) * mm, float(layout.label_height_mm) * mm
    margin = float(layout.margin_mm) * mm
    for index, copy in enumerate(copies):
        slot = index % per_page
        if index and slot == 0:
            c.showPage()
        col, row = slot % layout.columns, slot // layout.columns
        x = margin + col * lw
        y = page_h - margin - (row + 1) * lh
        title = copy.edition.title[:40]
        c.setFont("Helvetica", 7)
        c.drawString(x + 2 * mm, y + lh - 4 * mm, title)
        bar = code128.Code128(copy.barcode, barHeight=lh - 11 * mm, barWidth=0.35 * mm)
        bar.drawOn(c, x + 2 * mm, y + 5 * mm)
        c.setFont("Helvetica", 7)
        c.drawString(x + 2 * mm, y + 1.5 * mm, copy.copy_code)
    c.save()
    return buf.getvalue()


def page_count(n_labels, layout):
    per_page = layout.columns * layout.rows
    return max(1, -(-n_labels // per_page)) if n_labels else 0


__all__ = ["default_layout", "render_labels_pdf", "validate_layout", "page_count", "Decimal"]
