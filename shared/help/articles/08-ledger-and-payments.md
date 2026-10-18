# Commitment Ledger, payments and quotations

## Commitment Ledger
Ledger page. The AI reads chats and calls and writes down promises: "I'll pay on Friday" (owed to you), "we'll deliver tomorrow" (you promised). Each has the amount, due date and the exact message it came from (Evidence).
- Views: All Promises, Overdue, Owed to You, You Promised, Needs Review.
- Needs Review: promises the AI was less sure about. They are left out of totals until you Confirm or Reject them.
- Resolve a promise: Mark Met or Mark Missed.
- Record payment received: closes the promise once the full amount has arrived. A customer saying "I paid" is not recorded automatically - only you, a paid payment link, or a verified WhatsApp payment can.
- Export for Tally: settled payments as a file your CA can import.
- Unpaid invoices from your accounting software: Settings → Leads & payments → Unpaid invoices (import). Upload CSV/Excel with customer, invoice_number, due_date, amount.

## Payment links (Razorpay)
Connect your own Razorpay: Settings → Leads & payments → Payment links. In Razorpay: Account & Settings → API Keys → Generate Key, paste both. Money goes straight to your Razorpay account.
- Send a link from the Ledger (Request Payment), a booking or a queue token. It is marked paid by itself once Razorpay confirms.
- Outside the 24-hour window, a link needs an approved template whose two variables are the amount and the link.
- Automations can send a link when a payment is due or overdue, or right after a booking.
- Refunds are made from your Razorpay dashboard.

## Payment with a booking
Settings → Leads & payments → Payment with a booking (shown when you take bookings or tokens): only the token (pay at the counter), an advance online and the rest at the counter, or the full fee online.

## Quotations
Quotations page (when Quotations is on): add each quote you sent. KROVA tells your team when one starts going quiet - it never messages the buyer on its own. Mark quotes Won or Lost; see open value and win rate.
