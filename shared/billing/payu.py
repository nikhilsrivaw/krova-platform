"""
PayU (India) - building a checkout, and talking to its server APIs.

Checkout is a browser form POST to PayU's _payment endpoint, signed with a
SHA-512 hash of the fields and our salt (the salt never leaves the server).
A plan's first payment also carries `si=1` and `si_details`, which asks the
customer to approve a monthly mandate (card standing instruction / UPI AutoPay);
we then charge that mandate ourselves each month with `si_transaction`, after
telling PayU about it with `pre_debit_SI` at least 48 hours ahead.

We never trust what the browser brings back from PayU. Every result is checked
by asking PayU directly (`verify_payment`) and comparing the amount to our own
record - see shared/billing/service.py::settle_payment.

Built from PayU's public docs (docs.payu.in). Two details were not fully
spelled out there and MUST be confirmed in PayU's sandbox before going live:
the hash for `pre_debit_SI` (docs mention a second SHA-512 pass), and the exact
shape of verify_payment's response.
"""

import hashlib
import json
from datetime import date

import httpx

from shared.billing.plans import rupees
from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)


class PayUError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.payu_key and settings.payu_salt)


def _live() -> bool:
    return settings.payu_env.lower() in ("prod", "production", "live")


def checkout_url() -> str:
    return "https://secure.payu.in/_payment" if _live() else "https://test.payu.in/_payment"


def service_url() -> str:
    return (
        "https://info.payu.in/merchant/postservice.php?form=2"
        if _live()
        else "https://test.payu.in/merchant/postservice.php?form=2"
    )


def sha512(text: str) -> str:
    return hashlib.sha512(text.encode("utf-8")).hexdigest()


def request_hash(fields: dict, si_details: str | None) -> str:
    """key|txnid|amount|productinfo|firstname|email|udf1..udf5||||||[si_details|]salt"""
    parts = [
        settings.payu_key, fields["txnid"], fields["amount"], fields["productinfo"],
        fields["firstname"], fields["email"],
        fields.get("udf1", ""), fields.get("udf2", ""), fields.get("udf3", ""),
        fields.get("udf4", ""), fields.get("udf5", ""),
    ]
    text = "|".join(parts) + "||||||"
    if si_details is not None:
        text += si_details + "|"
    return sha512(text + settings.payu_salt)


def server_hash(command: str, var1: str) -> str:
    return sha512(f"{settings.payu_key}|{command}|{var1}|{settings.payu_salt}")


def si_details_json(*, amount_paise: int, start: date, end: date) -> str:
    """Monthly mandate for exactly one plan-month, GST included."""
    return json.dumps(
        {
            "billingAmount": rupees(amount_paise),
            "billingCurrency": "INR",
            "billingCycle": "MONTHLY",
            "billingInterval": 1,
            "paymentStartDate": start.isoformat(),
            "paymentEndDate": end.isoformat(),
        },
        separators=(",", ":"),
    )


def checkout_fields(
    *, txnid: str, amount_paise: int, productinfo: str, firstname: str, email: str, phone: str,
    return_url: str, udf1: str = "", si_details: str | None = None,
) -> dict:
    """Everything the browser must POST to PayU (the hash is included; the salt is not)."""
    if not enabled():
        raise PayUError("Payments are not switched on yet.")
    fields = {
        "key": settings.payu_key,
        "txnid": txnid,
        "amount": rupees(amount_paise),
        "productinfo": productinfo[:100],
        "firstname": (firstname or "Customer")[:60],
        "email": email[:50],
        "phone": (phone or "9999999999")[:50],
        "surl": return_url,
        "furl": return_url,
        "udf1": udf1,
    }
    if si_details is not None:
        fields["api_version"] = "7"
        fields["si"] = "1"
        fields["si_details"] = si_details
    fields["hash"] = request_hash(fields, si_details)
    return fields


async def _call(command: str, var1: str) -> dict:
    data = {"key": settings.payu_key, "command": command, "var1": var1, "hash": server_hash(command, var1)}
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(service_url(), data=data)
    try:
        return res.json()
    except ValueError as exc:
        raise PayUError(f"PayU answered with something unreadable (HTTP {res.status_code})") from exc


async def verify_payment(txnid: str) -> dict | None:
    """PayU's own record of this transaction, or None if it has none (yet)."""
    body = await _call("verify_payment", txnid)
    details = (body.get("transaction_details") or {}).get(txnid)
    if not isinstance(details, dict) or not details:
        return None
    return details


async def pre_debit(*, authpayuid: str, request_id: str, debit_date: date, amount_paise: int, invoice: str) -> dict:
    """Tell PayU a charge is coming (cards and UPI; at least 48 hours before)."""
    var1 = json.dumps(
        {
            "authpayuid": authpayuid, "requestId": request_id, "debitDate": debit_date.isoformat(),
            "amount": rupees(amount_paise), "invoiceDisplayNumber": invoice,
        },
        separators=(",", ":"),
    )
    return await _call("pre_debit_SI", var1)


async def charge_mandate(
    *, authpayuid: str, txnid: str, amount_paise: int, invoice: str, email: str = "", phone: str = ""
) -> dict:
    """Charge the monthly mandate. The final outcome comes from verify_payment / the webhook."""
    var1 = json.dumps(
        {
            "authpayuid": authpayuid, "amount": rupees(amount_paise), "txnid": txnid,
            "invoiceDisplayNumber": invoice, "phone": phone, "email": email,
        },
        separators=(",", ":"),
    )
    return await _call("si_transaction", var1)
