"""
Draws an invoice or receipt as a one-page A4 PDF (reportlab, pure Python, no system packages).

The fonts are DejaVu Sans (shared/billing/fonts), bundled because the standard PDF fonts have no rupee sign.
The page follows the approved design: the seller (Aqirox Technology Private Limited) is the issuer; KROVA
appears only as the product on the line item.
"""

import io
from datetime import datetime, timedelta
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from shared.billing import gst, supplier

_FONTS = Path(__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("Sans", str(_FONTS / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("Sans-Bold", str(_FONTS / "DejaVuSans-Bold.ttf")))

INK, DIM, LINE = HexColor("#16181d"), HexColor("#6b7280"), HexColor("#e5e7eb")
SOFT, TEAL, TEALSOFT = HexColor("#f6f7f9"), HexColor("#00A387"), HexColor("#e6f6f3")
GREEN, GREENSOFT = HexColor("#047857"), HexColor("#d1fae5")

W, H = A4
M = 40  # page margin, points

MODES = {"UPI": "UPI", "CC": "Credit card", "DC": "Debit card", "NB": "Net banking", "WALLET": "Wallet"}


def money(paise: int) -> str:
    """₹1,00,000.00 - Indian digit grouping."""
    rupees, ps = divmod(abs(paise), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{'-' if paise < 0 else ''}₹{s}.{ps:02d}"


def plain(paise: int) -> str:
    return money(paise)[1:]


def _day(when: datetime | None) -> str:
    return when.astimezone(gst.IST).strftime("%d %b %Y") if when else "-"


def paid_via(invoice) -> str:
    mode = MODES.get((invoice.payment_mode or "").upper(), "Online payment")
    if invoice.kind == "invoice":
        return "UPI AutoPay" if mode == "UPI" else f"{mode} (AutoPay)" if mode != "Online payment" else "AutoPay"
    return mode


def filename(invoice) -> str:
    return invoice.number.replace("/", "-") + ".pdf"


def _text(c, x, y, s, size=9, font="Sans", color=INK, align="l"):
    c.setFont(font, size)
    c.setFillColor(color)
    {"l": c.drawString, "r": c.drawRightString, "c": c.drawCentredString}[align](x, y, s)


def _wrap(c, x, y, s, width, size=8.6, font="Sans", color=DIM, lead=11.5) -> float:
    """Draw wrapped text; return the y below the last line."""
    for line in simpleSplit(s, font, size, width):
        _text(c, x, y, line, size, font, color)
        y -= lead
    return y


def render(invoice) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"{invoice.number} - {supplier.NAME}")
    c.setAuthor(supplier.NAME)
    is_invoice = invoice.kind == "invoice"
    right = W - M

    # ── header ──
    y = H - M - 12
    _text(c, M, y, supplier.NAME, 14.5, "Sans-Bold")
    y -= 14
    for line in supplier.ADDRESS_LINES:
        _text(c, M, y, line, 8.4, color=DIM)
        y -= 11.5
    _text(c, M, y, f"{supplier.EMAIL}  ·  {supplier.WEBSITE}", 8.4, color=DIM)
    y -= 13
    _text(c, M, y, f"GSTIN: {supplier.GSTIN}   ·   State: {supplier.STATE_NAME} ({supplier.STATE_CODE})", 8.4, "Sans-Bold")

    ty = H - M - 6
    _text(c, right, ty, "TAX INVOICE" if is_invoice else "PAYMENT RECEIPT", 17, "Sans-Bold", TEAL if is_invoice else HexColor("#374151"), "r")
    meta = [("Invoice no." if is_invoice else "Receipt no.", invoice.number), ("Date", _day(invoice.issued_at))]
    if is_invoice and invoice.service_from and invoice.service_to:
        # The stored end is when the next period starts; the service runs to the day before.
        last_day = invoice.service_to - timedelta(days=1)
        meta.append(("Service period", f"{_day(invoice.service_from)} - {_day(last_day)}"))
    my = ty - 17
    for k, v in meta:
        _text(c, right - c.stringWidth(v, "Sans-Bold", 8.6) - 12, my, k, 8.4, color=DIM, align="r")
        _text(c, right, my, v, 8.6, "Sans-Bold", align="r")
        my -= 12
    badge = "PAID" if is_invoice else "RECEIVED"
    bw = c.stringWidth(badge, "Sans-Bold", 7.6) + 16
    c.setFillColor(GREENSOFT)
    c.roundRect(right - bw, my - 8, bw, 14, 7, fill=1, stroke=0)
    _text(c, right - bw / 2, my - 4.5, badge, 7.6, "Sans-Bold", GREEN, "c")

    line_y = min(y, my - 8) - 12
    c.setStrokeColor(INK)
    c.setLineWidth(1.6)
    c.line(M, line_y, right, line_y)

    # ── parties ──
    y = line_y - 20
    col2 = M + (right - M) / 2 + 10
    _text(c, M, y, "BILLED TO" if is_invoice else "RECEIVED FROM", 6.8, "Sans-Bold", DIM)
    _text(c, col2, y, "SUPPLY DETAILS" if is_invoice else "FOR", 6.8, "Sans-Bold", DIM)
    ly = y - 14
    _text(c, M, ly, invoice.buyer_name, 10.5, "Sans-Bold")
    ly -= 13
    if invoice.buyer_address:
        ly = _wrap(c, M, ly, invoice.buyer_address, (right - M) / 2 - 10, 8.6, color=HexColor("#374151"))
    _text(c, M, ly, f"{gst.state_name(invoice.buyer_state_code)}, India", 8.6, color=HexColor("#374151"))
    ly -= 12
    if invoice.buyer_gstin:
        _text(c, M, ly, f"GSTIN: {invoice.buyer_gstin}", 8.6, "Sans-Bold")
        ly -= 12
    ry = y - 14
    if is_invoice:
        for k, v in (
            ("Place of supply", f"{gst.state_name(invoice.buyer_state_code)} ({invoice.buyer_state_code})"),
            ("Reverse charge", "No"),
            ("Customer email", invoice.buyer_email or "-"),
        ):
            _text(c, col2, ry, f"{k}:", 8.6, "Sans-Bold")
            _text(c, col2 + c.stringWidth(f"{k}: ", "Sans-Bold", 8.6), ry, v, 8.6, color=HexColor("#374151"))
            ry -= 12
    else:
        _text(c, col2, ry, "KROVA Voice Agent wallet", 9, "Sans-Bold")
        _text(c, col2, ry - 12, "Prepaid balance for phone-number rent and calls", 8.6, color=HexColor("#374151"))
        ry -= 24
    y = min(ly, ry) - 14

    # ── items ──
    if is_invoice:
        igst = invoice.igst_paise > 0
        cols = [("#", M + 6, "l"), ("DESCRIPTION", M + 26, "l"), ("TAXABLE VALUE", right - 218, "r"),
                ("IGST 18%" if igst else "CGST 9%", right - 150, "r")]
        if not igst:
            cols.append(("SGST 9%", right - 84, "r"))
        cols.append(("AMOUNT", right - 6, "r"))
        desc_width = (right - 218) - (M + 26) - 70
    else:
        cols = [("#", M + 6, "l"), ("DESCRIPTION", M + 26, "l"), ("AMOUNT", right - 6, "r")]
        desc_width = right - 120 - (M + 26)

    c.setFillColor(SOFT)
    c.rect(M, y - 15, right - M, 20, fill=1, stroke=0)
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(M, y + 5, right, y + 5)
    c.line(M, y - 15, right, y - 15)
    for label, x, al in cols:
        _text(c, x, y - 8.5, label, 6.6, "Sans-Bold", DIM, al)
    y -= 15

    for i, row in enumerate(invoice.lines, 1):
        ry = y - 15
        _text(c, M + 6, ry, str(i), 8.8)
        dy = _wrap(c, M + 26, ry, row["description"], desc_width, 9.4, "Sans-Bold", INK, 12)
        by = _wrap(c, M + 26, dy, row.get("detail", ""), desc_width, 8, color=DIM, lead=10.5)
        if is_invoice:
            _text(c, right - 218, ry, plain(invoice.taxable_paise), 8.8, align="r")
            if invoice.igst_paise > 0:
                _text(c, right - 150, ry, plain(invoice.igst_paise), 8.8, align="r")
            else:
                _text(c, right - 150, ry, plain(invoice.cgst_paise), 8.8, align="r")
                _text(c, right - 84, ry, plain(invoice.sgst_paise), 8.8, align="r")
            _text(c, right - 6, ry, plain(invoice.total_paise), 8.8, "Sans-Bold", align="r")
        else:
            _text(c, right - 6, ry, plain(row["amount_paise"]), 8.8, align="r")
        y = min(by, ry - 14) - 6
        c.setStrokeColor(LINE)
        c.line(M, y + 2, right, y + 2)

    # ── totals ──
    y -= 14
    _text(c, M, y, "AMOUNT IN WORDS", 6.8, "Sans-Bold", DIM)
    _wrap(c, M, y - 12, gst.rupees_in_words(invoice.total_paise) + ".", (right - M) / 2 - 20, 8.6, color=HexColor("#374151"))
    sx, sr = right - 190, right
    ty = y
    if is_invoice:
        rows = [("Taxable value", invoice.taxable_paise)]
        rows += [("IGST @ 18%", invoice.igst_paise)] if invoice.igst_paise > 0 else [
            ("CGST @ 9%", invoice.cgst_paise), ("SGST @ 9%", invoice.sgst_paise)]
        total_label = "Total"
    else:
        rows = [("Wallet credit", invoice.lines[0]["amount_paise"]), ("Payment gateway fee", invoice.fee_paise)]
        total_label = "Total paid"
    for label, value in rows:
        _text(c, sx, ty, label, 9)
        _text(c, sr, ty, money(value), 9, align="r")
        ty -= 14
    c.setStrokeColor(INK)
    c.setLineWidth(1.4)
    c.line(sx, ty + 6, sr, ty + 6)
    ty -= 8
    _text(c, sx, ty, total_label, 11.5, "Sans-Bold")
    _text(c, sr, ty, money(invoice.total_paise), 11.5, "Sans-Bold", TEAL, "r")
    y = ty - 24

    # ── payment tiles ──
    tiles = [("Paid via", paid_via(invoice)), ("Payment date", _day(invoice.issued_at)),
             ("Payment reference", invoice.payment_ref or "-")]
    if is_invoice and invoice.next_renewal:
        tiles.append(("Next renewal", _day(invoice.next_renewal)))
    c.setFillColor(SOFT)
    c.roundRect(M, y - 34, right - M, 42, 6, fill=1, stroke=0)
    tw = (right - M - 20) / len(tiles)
    for i, (k, v) in enumerate(tiles):
        x = M + 12 + i * tw
        _text(c, x, y - 6, k, 7.4, color=DIM)
        _text(c, x, y - 20, v, 8.8, "Sans-Bold")
    y -= 56

    # ── note ──
    note = (
        "Your plan renews every month on its own and you are told before each charge. Cancel any time from Billing in your KROVA account."
        if is_invoice else
        "This is a receipt for a wallet top-up and is not a tax invoice. No GST is charged on the wallet credit. Calls and number rent "
        "are taken from the wallet at cost; each call's cost is shown in your call log."
    )
    lines = simpleSplit(note, "Sans", 8.4, right - M - 30)
    bh = len(lines) * 11.5 + 14
    c.setFillColor(TEALSOFT)
    c.rect(M, y - bh + 8, right - M, bh, fill=1, stroke=0)
    c.setFillColor(TEAL)
    c.rect(M, y - bh + 8, 2.5, bh, fill=1, stroke=0)
    ny = y - 5
    for line in lines:
        _text(c, M + 14, ny, line, 8.4, color=HexColor("#134e4a"))
        ny -= 11.5
    y -= bh + 12

    terms = ["This is a computer-generated document and does not need a signature.",
             f"Questions: write to {supplier.EMAIL} with the {'invoice' if is_invoice else 'receipt'} number."]
    if is_invoice:
        terms.append("Subject to Gorakhpur jurisdiction.")
    for t in terms:
        _text(c, M + 4, y, f"•  {t}", 7.8, color=DIM)
        y -= 11

    # ── footer ──
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(M, 46, right, 46)
    _text(c, M, 34, supplier.NAME, 7.6, "Sans-Bold")
    _text(c, M + c.stringWidth(supplier.NAME, "Sans-Bold", 7.6) + 4, 34, "·  Registered office as above", 7.6, color=DIM)
    _text(c, right, 34, "KROVA · a product of Aqirox · krova.space", 7.6, color=DIM, align="r")

    c.showPage()
    c.save()
    return buf.getvalue()
