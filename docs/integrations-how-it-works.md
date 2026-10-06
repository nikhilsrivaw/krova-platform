# How the integrations work

KROVA gets business data from outside tools in two ways:

1. **Leads**: a new enquiry arrives from a listing platform (Justdial, IndiaMART).
2. **Receivables**: a list of unpaid invoices arrives from accounting software (Zoho Books, or any CSV or Excel export).

Both end up in the Commitment Ledger, and the sections below explain each part.

---

## 1. Leads (Justdial, IndiaMART)

### What the business does
1. In KROVA settings, click **Generate URL**. The URL is shown once, so copy it.
2. Give that URL to the platform, so it sends new leads there:
   - **Justdial:** the URL goes to the Justdial account manager, who activates it on the Justdial side. There is no self-serve option.
   - **IndiaMART:** paste the URL in IndiaMART under Lead Manager, Import/Export Leads, Push API, as the Listener URL. Confirm with the OTP sent to the registered mobile. This needs an active IndiaMART paid plan.

### What happens when a lead arrives
1. The platform sends a `POST` with JSON to `/webhooks/<platform>/<token>`.
2. The token in the URL is the only check. KROVA stores only its hash, so a wrong token gets a 404.
3. A parser reads the platform's field names into one shape (name, phone, email, query, lead id).
4. If the lead id was seen before, the lead is marked **duplicate** and not saved again.
5. The phone number is normalised and matched to an existing customer, or a new customer is created.
6. The enquiry is saved as a CRM note on that customer.
7. The raw payload is stored with the lead, so a field mismatch can be fixed later from one row.

### Why a valid lead always gets HTTP 200
IndiaMART retries until it gets a 200, and turns off the integration if nothing is accepted for 48 hours. So a valid lead gets 200 even with no phone number. The lead is saved with status `no_phone`.

### Limits
- **No automatic WhatsApp to the lead.** A lead has never messaged the business, so the first message must be an approved WhatsApp template. None exists yet.
- **Justdial field names are assumed.** The Justdial payload has not been seen from a live account yet. The first real lead's raw payload should be checked.
- **Dedupe needs a lead id.** Without one, the same lead sent twice is stored twice.

---

## 2. Receivables (unpaid invoices)

### Option A: CSV or Excel upload (works with any software)
1. Export unpaid invoices from any accounting tool as CSV or `.xlsx`.
2. In KROVA settings, upload the file.
3. Tick **"full list"** only if the file contains every unpaid invoice. If it does, invoices missing from the file are marked paid. If it is a partial file, untick it, so real debts are not closed by mistake.

Accepted column names (case does not matter):
- Customer: `customer`, `customer name`, `party`, `party name`, `ledger name`, `name`
- Invoice number: `invoice number`, `invoice no`, `bill no`, `bill number`, `voucher no`
- Due date (optional): `due date`, `due on`. Formats: `YYYY-MM-DD` or `DD/MM/YYYY`
- Amount: `amount`, `balance`, `balance due`, `amount due`, `outstanding`, `closing balance`, `total`

Bad rows are skipped and reported with their line number. The good rows still import.

### Option B: Zoho Books (connected, currently hidden in the UI)
- Connected through Zoho's OAuth. The business logs in to Zoho and approves read-only access.
- Open invoices (unpaid, overdue, partially paid) are pulled every 24 hours.
- Invoices that leave the open list are marked paid. This also covers voided invoices, which is a known limit.
- The backend code exists, but it has not been tested against a live Zoho account.

### What happens to the rows (same for CSV and Zoho)
- Each invoice becomes one `they_owe` payment commitment.
- The invoice number is the key, per source. Importing the same invoice again updates it instead of duplicating it.
- Each import is recorded in **Recent imports** with its counts.

---

## What is verified and what is not

| Part | Status |
|---|---|
| CSV and Excel parsing, column aliases | Tested with unit tests |
| Lead parsers (Justdial, IndiaMART) | Tested with unit tests, using the documented IndiaMART field names |
| Database writes, dedupe, customer linking | Not tested end-to-end yet |
| Justdial live payload | Not seen yet, field names assumed |
| IndiaMART live Push | Not tested, needs a paid IndiaMART account |
| Zoho live connect and sync | Not tested, API host and pagination to confirm on first connect |

## Migrations to run
The database changes for all of the above are in `alembic/versions/`. The current head is `f5a6b7c8d9e0`. Run `alembic upgrade head` after deploying.
