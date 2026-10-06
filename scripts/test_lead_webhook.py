"""
Send sample leads to a KROVA lead webhook URL and check the answers.

No platform needs to be connected for this. It checks KROVA's side end to end:
a new lead is saved, the same lead sent again is a duplicate, and a lead with no
phone number is still accepted with HTTP 200.

Usage (copy the URL from Settings after generating it):
    python scripts/test_lead_webhook.py "https://api.krova.space/webhooks/leads/generic/<token>"

Run it against a test business, not a real customer list: each run creates a
customer with the test phone number.
"""

import argparse
import sys
import time

import httpx

TEST_PHONE = "9999900001"


def send(url: str, payload: dict) -> tuple[int, dict]:
    res = httpx.post(url, json=payload, timeout=20.0)
    try:
        body = res.json()
    except ValueError:
        body = {}
    return res.status_code, body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="the webhook URL generated in Settings")
    args = parser.parse_args()

    lead_id = f"TEST-{int(time.time())}"
    sample = {
        "lead_id": lead_id,
        "name": "KROVA Test Lead",
        "mobile": TEST_PHONE,
        "email": "test@example.com",
        "query": "Test enquiry from the webhook check",
    }
    no_phone = {"lead_id": f"{lead_id}-nophone", "name": "No Phone Lead", "query": "No number given"}

    checks = [
        ("new lead is saved", sample, 200, "received"),
        ("same lead again is a duplicate", sample, 200, "duplicate"),
        ("lead with no phone is accepted", no_phone, 200, "no_phone"),
    ]

    failed = 0
    for label, payload, want_code, want_status in checks:
        code, body = send(args.url, payload)
        ok = code == want_code and body.get("status") == want_status
        failed += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}: HTTP {code}, status={body.get('status')!r}")

    bad_code, _ = send(args.url.rsplit("/", 1)[0] + "/not-a-real-token", sample)
    ok = bad_code == 404
    failed += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] wrong token is refused: HTTP {bad_code}")

    print()
    print("Check the new rows in Settings > Recent leads. The first lead should show its phone and query.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
