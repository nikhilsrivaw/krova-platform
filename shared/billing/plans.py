"""
What KROVA charges, and how a charge is built up. Pure arithmetic - no database,
no network - so every rupee can be checked by a test.

Plans are monthly, priced WITHOUT GST; 18% GST is added on top and shown
separately. Voice is paid from the rupee wallet: each call at its real cost plus
25%, and a flat rent per phone number. A wallet top-up adds GST and the payment
gateway's fee on top, so the wallet receives exactly the amount chosen.
"""

import math

GST_PCT = 18

# Paise, before GST.
PLAN_PRICES_PAISE: dict[str, int] = {
    "starter": 2_999_00,
    "pro": 5_999_00,
    "scale": 9_999_00,
}
PLAN_LABELS = {"starter": "Starter", "pro": "Pro", "scale": "Scale"}

NUMBER_RENT_PAISE = 220_00      # per phone number, per month, taken from the wallet
CALL_MARKUP_PCT = 25            # on top of what a call really costs KROVA

MIN_TOPUP_PAISE = 100_00
MAX_TOPUP_PAISE = 50_000_00
LOW_BALANCE_PAISE = 100_00


def gst_of(base_paise: int) -> int:
    return round(base_paise * GST_PCT / 100)


def plan_charge(plan: str) -> tuple[int, int, int]:
    """(base, gst, total) in paise for one month of this plan."""
    if plan not in PLAN_PRICES_PAISE:
        raise ValueError(f"Unknown plan: {plan}")
    base = PLAN_PRICES_PAISE[plan]
    gst = gst_of(base)
    return base, gst, base + gst


def topup_charge(credit_paise: int, fee_pct: float) -> tuple[int, int, int, int]:
    """
    (credit, gst, fee, total) in paise for adding `credit_paise` to the wallet.

    GST is charged on the credit; the gateway fee (already including its own GST,
    see Settings.payu_fee_pct) is charged on what PayU actually moves, credit + GST.
    """
    if not MIN_TOPUP_PAISE <= credit_paise <= MAX_TOPUP_PAISE:
        raise ValueError("Top-up must be between Rs 100 and Rs 50,000")
    gst = gst_of(credit_paise)
    fee = math.ceil((credit_paise + gst) * fee_pct / 100)
    return credit_paise, gst, fee, credit_paise + gst + fee


def call_charge(cost_paise: int) -> int:
    """What the wallet is charged for usage that cost KROVA `cost_paise`: cost + 25%, rounded up."""
    return math.ceil(cost_paise * (100 + CALL_MARKUP_PCT) / 100)


def rupees(paise: int) -> str:
    """'3538.82' - the form PayU wants amounts in."""
    return f"{paise / 100:.2f}"
