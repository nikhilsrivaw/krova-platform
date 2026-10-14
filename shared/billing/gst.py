"""
GST arithmetic and checks for invoices. Pure functions - no database, no network - so every rupee and every
rule can be tested. Integer paise throughout.

The seller is Aqirox Technology Private Limited (shared/billing/supplier.py), registered in Uttar Pradesh.
A buyer in the same state pays CGST + SGST (9% + 9%); a buyer in another state pays IGST (18%).
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

GST_PCT = 18
IST = ZoneInfo("Asia/Kolkata")

# GST state / union-territory codes (the first two digits of a GSTIN).
STATES: dict[str, str] = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory",
}

_GSTIN_SHAPE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_check_char(first14: str) -> str:
    """The 15th character of a GSTIN, from the first 14 (the GSTN checksum, mod 36)."""
    total = 0
    for i, ch in enumerate(first14):
        value = _CHARSET.index(ch) * (1 if i % 2 == 0 else 2)
        total += value // 36 + value % 36
    return _CHARSET[(36 - total % 36) % 36]


def valid_gstin(gstin: str) -> bool:
    g = (gstin or "").strip().upper()
    return bool(_GSTIN_SHAPE.match(g)) and g[:2] in STATES and gstin_check_char(g[:14]) == g[14]


def state_name(code: str) -> str:
    return STATES.get(code, "")


def split_gst(total_paise: int, *, intra_state: bool) -> tuple[int, int, int, int]:
    """
    A GST-inclusive total split into (taxable, cgst, sgst, igst) paise.

    Same state: CGST and SGST are equal halves, so the GST is rounded to an even number of paise (a normal
    plan price already is). Another state: all of it is IGST.
    """
    gst = round(total_paise * GST_PCT / (100 + GST_PCT))
    if intra_state and gst % 2:
        gst += 1
    taxable = total_paise - gst
    if intra_state:
        return taxable, gst // 2, gst // 2, 0
    return taxable, 0, 0, gst


def financial_year(when: datetime) -> str:
    """'2627' for any date from 1 April 2026 to 31 March 2027 (India time). Invoice series restart each year."""
    d = when.astimezone(IST)
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


_ONES = ("Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen Fifteen "
         "Sixteen Seventeen Eighteen Nineteen").split()
_TENS = "_ _ Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()


def _below_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else "")


def _below_thousand(n: int) -> str:
    if n < 100:
        return _below_hundred(n)
    rest = f" {_below_hundred(n % 100)}" if n % 100 else ""
    return f"{_ONES[n // 100]} Hundred{rest}"


def rupees_in_words(paise: int) -> str:
    """'Rupees Three Thousand Five Hundred Thirty-Eight and Eighty-Two Paise only' (Indian number system)."""
    rupees, ps = divmod(paise, 100)
    parts: list[str] = []
    for unit, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
        if rupees >= unit:
            parts.append(f"{_below_thousand(rupees // unit) if unit == 1000 else _below_hundred(rupees // unit)} {name}")
            rupees %= unit
    if rupees or not parts:
        parts.append(_below_thousand(rupees))
    text = "Rupees " + " ".join(parts)
    if ps:
        text += f" and {_below_hundred(ps)} Paise"
    return text + " only"
