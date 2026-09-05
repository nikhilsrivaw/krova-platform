"""
Exporting settled payments as a Tally-importable XML file.

Scope, deliberately narrow for a first version: payments received
(Commitment kind=payment, direction=they_owe, status=met) become Receipt
Vouchers, nothing else. Not outstanding balances, not expenses - those need
their own voucher/ledger treatment and their own decision about what a CA
actually wants, not assumed here.

Tally cannot import a plain CSV - only this specific
ENVELOPE/HEADER/BODY/IMPORTDATA XML shape, verified against Tally's own
integration docs and third-party Tally-XML tooling, not guessed. Built with
xml.etree rather than string formatting so special characters in a
patient's name or a commitment's description (an "&" is a real name
character, not a hypothetical) are escaped correctly - a hand-formatted
string here would silently produce a file Tally refuses to import.

Every patient becomes their own ledger under Sundry Debtors, auto-created
in the same file via a LEDGER master message - a business using this for
the first time has no ledgers in Tally for its patients yet, so the export
must create them, not just reference them. Money is assumed to have landed
in a single generic "Cash" ledger (Tally's own default under Cash-in-hand,
present in every fresh company) since Krova does not know which specific
bank account an individual UPI/cash payment actually reached - the business
or their CA re-maps this ledger in Tally afterwards if the money actually
went to a bank account.

Import this file into Tally only once per date range - Tally's own
vouchers have no natural dedupe key, so importing the same range twice
creates duplicate transactions.
"""

import xml.etree.ElementTree as ET
from datetime import datetime

from shared.db.models import Commitment, Customer

CASH_LEDGER = "Cash"
PARTY_LEDGER_GROUP = "Sundry Debtors"


def _patient_ledger_name(customer: Customer) -> str:
    # Disambiguates same-name patients within one business - ledger names
    # must be unique per Tally company, display names are not.
    short_id = str(customer.id)[:8]
    name = customer.display_name or "Patient"
    return f"{name} ({short_id})"


def _ledger_master_message(name: str) -> ET.Element:
    message = ET.Element("TALLYMESSAGE")
    message.set("xmlns:UDF", "TallyUDF")
    ledger = ET.SubElement(message, "LEDGER")
    ledger.set("NAME", name)
    ledger.set("ACTION", "Create")
    ET.SubElement(ledger, "NAME").text = name
    ET.SubElement(ledger, "PARENT").text = PARTY_LEDGER_GROUP
    ET.SubElement(ledger, "OPENINGBALANCE").text = "0"
    return message


def _receipt_voucher_message(
    *, party_ledger: str, amount_rupees: str, when: datetime, narration: str, voucher_number: int
) -> ET.Element:
    message = ET.Element("TALLYMESSAGE")
    message.set("xmlns:UDF", "TallyUDF")
    voucher = ET.SubElement(message, "VOUCHER")
    voucher.set("VCHTYPE", "Receipt")
    voucher.set("ACTION", "Create")

    ET.SubElement(voucher, "DATE").text = when.strftime("%Y%m%d")
    ET.SubElement(voucher, "NARRATION").text = narration
    ET.SubElement(voucher, "VOUCHERTYPENAME").text = "Receipt"
    ET.SubElement(voucher, "VOUCHERNUMBER").text = str(voucher_number)
    ET.SubElement(voucher, "PARTYLEDGERNAME").text = party_ledger

    cash_entry = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
    ET.SubElement(cash_entry, "LEDGERNAME").text = CASH_LEDGER
    ET.SubElement(cash_entry, "ISDEEMEDPOSITIVE").text = "Yes"
    ET.SubElement(cash_entry, "AMOUNT").text = amount_rupees

    party_entry = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
    ET.SubElement(party_entry, "LEDGERNAME").text = party_ledger
    ET.SubElement(party_entry, "ISDEEMEDPOSITIVE").text = "No"
    ET.SubElement(party_entry, "AMOUNT").text = f"-{amount_rupees}"

    return message


def build_tally_receipts_xml(commitments: list[Commitment], customers: dict, /) -> bytes:
    """
    commitments: settled payment commitments to export - filtering (kind,
    status, direction, date range) is the caller's responsibility, this
    function only serialises what it's given.
    customers: {customer_id: Customer}, must contain every commitment's
    customer_id - a missing one is a caller bug, not handled quietly here.
    """
    envelope = ET.Element("ENVELOPE")
    header = ET.SubElement(envelope, "HEADER")
    ET.SubElement(header, "TALLYREQUEST").text = "Import Data"

    body = ET.SubElement(envelope, "BODY")
    import_data = ET.SubElement(body, "IMPORTDATA")
    request_desc = ET.SubElement(import_data, "REQUESTDESC")
    ET.SubElement(request_desc, "REPORTNAME").text = "Vouchers"
    request_data = ET.SubElement(import_data, "REQUESTDATA")

    # Ledger masters first, one per distinct patient - a voucher naming a
    # ledger Tally has not been told about yet fails to import.
    seen_ledgers: set[str] = set()
    for commitment in commitments:
        customer = customers[commitment.customer_id]
        ledger_name = _patient_ledger_name(customer)
        if ledger_name in seen_ledgers:
            continue
        seen_ledgers.add(ledger_name)
        request_data.append(_ledger_master_message(ledger_name))

    for i, commitment in enumerate(commitments, start=1):
        customer = customers[commitment.customer_id]
        amount_rupees = f"{(commitment.amount_paise or 0) / 100:.2f}"
        when = commitment.resolved_at or commitment.due_at or commitment.created_at
        request_data.append(
            _receipt_voucher_message(
                party_ledger=_patient_ledger_name(customer),
                amount_rupees=amount_rupees,
                when=when,
                narration=commitment.description,
                voucher_number=i,
            )
        )

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(envelope, encoding="utf-8")
