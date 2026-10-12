"""
Is PayU wired up correctly? Run this on the server before taking the first real payment.

    docker compose -f docker-compose.prod.yml exec app python -m scripts.payu_selfcheck
    docker compose -f docker-compose.prod.yml exec app python -m scripts.payu_selfcheck --form 10
    docker compose -f docker-compose.prod.yml exec app python -m scripts.payu_selfcheck --txn <TXNID>

With no arguments it checks, and prints PASS / FAIL for each:
  1. the settings (keys, environment, the two public URLs PayU is told about)
  2. that PayU accepts our key, salt and hash (asks PayU about a transaction that cannot exist; PayU
     answers "not found" only if it trusted the signature)

--form <rupees>   writes payu_checkout_test.html: a signed Rs <rupees> subscription checkout (a card /
                  UPI AutoPay mandate, like a real plan) that you open in a browser and pay in PayU's test
                  mode. Nothing is saved on our side; it exists so you can watch PayU's own pages.
--txn <id>        asks PayU what it holds for a transaction id, and prints it.

Reads nothing from, and writes nothing to, the database.
"""

import asyncio
import json
import sys
import uuid
from datetime import date, timedelta
from html import escape

from shared.billing import payu, service
from shared.config.settings import settings

OK, BAD = "PASS", "FAIL"


def line(status: str, text: str) -> None:
    print(f"  [{status}] {text}")


def check_settings() -> bool:
    print("\n1. Settings")
    ok = True
    line(OK if settings.payu_key else BAD, f"PAYU_KEY is {'set' if settings.payu_key else 'EMPTY'}")
    line(OK if settings.payu_salt else BAD, f"PAYU_SALT is {'set' if settings.payu_salt else 'EMPTY'}")
    ok &= bool(settings.payu_key and settings.payu_salt)
    live = payu._live()
    line(OK, f"PAYU_ENV = {settings.payu_env!r} -> using {'PRODUCTION (real money)' if live else 'the TEST environment'}")
    line(OK, f"checkout posts to {payu.checkout_url()}")
    base = settings.public_base_url.rstrip("/")
    line(OK if base.startswith("https://") else BAD, f"PUBLIC_BASE_URL = {base or 'EMPTY'} (must be https and reachable by PayU)")
    ok &= base.startswith("https://")
    front = settings.frontend_base_url.rstrip("/")
    line(OK if front.startswith("https://") else BAD, f"FRONTEND_BASE_URL = {front} (where customers land after paying)")
    print(f"       return URL  (surl/furl): {service.return_url()}")
    print(f"       webhook URL (give PayU): {base}/api/v1/billing/payu/webhook")
    cutoff = settings.billing_enforced_from.strip()
    line(OK if cutoff else BAD, f"BILLING_ENFORCED_FROM = {cutoff or 'EMPTY - nobody is asked to pay yet'}")
    line(OK, f"gateway fee added to wallet top-ups: {settings.payu_fee_pct}%")
    return ok


async def check_payu_accepts_us() -> bool:
    print("\n2. Does PayU accept our key, salt and signature?")
    if not payu.enabled():
        line(BAD, "skipped - no keys")
        return False
    probe = "KRCHECK" + uuid.uuid4().hex[:12]
    try:
        body = await payu._call("verify_payment", probe)
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic: show whatever went wrong
        line(BAD, f"could not reach PayU: {exc}")
        return False
    text = json.dumps(body)[:300]
    if "invalid hash" in text.lower():
        line(BAD, f"PayU says the hash is invalid - the key or salt is wrong for this environment: {text}")
        return False
    entry = (body.get("transaction_details") or {}).get(probe)
    if entry is not None or "transaction_details" in body:
        line(OK, "PayU accepted the signature and answered in the shape our code reads")
        print(f"       PayU said: {text}")
        return True
    line(BAD, f"PayU answered, but not in the shape our code expects: {text}")
    return False


def write_form(rupees: int) -> None:
    amount = rupees * 100
    txnid = service.new_txnid("KT")
    start = date.today() + timedelta(days=31)
    fields = payu.checkout_fields(
        txnid=txnid, amount_paise=amount, productinfo="KROVA sandbox check", firstname="Test", email="test@krova.space",
        phone="9999999999", return_url=service.return_url(), udf1="selfcheck",
        si_details=payu.si_details_json(amount_paise=amount, start=start, end=date(start.year + 5, start.month, 28)),
    )
    inputs = "\n".join(f'<input type="hidden" name="{escape(k)}" value="{escape(v, quote=True)}">' for k, v in fields.items())
    html = (
        f'<!doctype html><meta charset="utf-8"><title>PayU test checkout</title>'
        f'<p>Posting a Rs {rupees} test subscription to PayU ({"production" if payu._live() else "test"}). '
        f"Transaction id: <b>{txnid}</b></p>"
        f'<form id="f" method="post" action="{payu.checkout_url()}">{inputs}</form>'
        f'<script>document.getElementById("f").submit()</script>'
    )
    with open("payu_checkout_test.html", "w", encoding="utf8") as handle:
        handle.write(html)
    print(f"\nWrote payu_checkout_test.html  (transaction id {txnid})")
    print("Open it in a browser, pay with PayU's test card/UPI, then check the result with:")
    print(f"  python -m scripts.payu_selfcheck --txn {txnid}")
    if payu._live():
        print("WARNING: PAYU_ENV is production - this would charge real money.")


async def show_txn(txnid: str) -> None:
    print(f"\nWhat PayU holds for {txnid}:")
    details = await payu.verify_payment(txnid)
    print("  nothing (PayU has no such transaction)" if details is None else json.dumps(details, indent=2)[:2000])


def main() -> None:
    args = sys.argv[1:]
    if "--form" in args:
        try:
            write_form(int(args[args.index("--form") + 1]))
        except payu.PayUError as exc:
            print(f"Cannot build the checkout: {exc} Set PAYU_KEY and PAYU_SALT first.")
            sys.exit(1)
        return
    if "--txn" in args:
        asyncio.run(show_txn(args[args.index("--txn") + 1]))
        return
    ok = check_settings()
    ok &= asyncio.run(check_payu_accepts_us())
    print("\n" + ("All checks passed." if ok else "Some checks FAILED - fix them before taking payments."))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
