# Bookings, Calendar and the walk-in Queue

## Scheduling (appointments)
Needs the "Appointments & staff calendar" feature (Settings → Features). Scheduling page:
- Add staff (for example doctors) and their weekly hours and slot length. Every slot a customer can book comes from these hours - nothing else.
- Dates off: mark a day off and no one can book it. If bookings already exist that day you can cancel them; cancelling runs your "appointment cancelled" automations.
- Book for a customer: only real, open slots can be booked - the same check WhatsApp and voice go through.
Customers can book, cancel and reschedule in WhatsApp chats and on calls; the AI offers free slots.

## Calendar
Calendar page: every booking from calls, WhatsApp, the website and your team on one calendar (day, week, month). Open a booking to mark "They came" or "No-show", reschedule or cancel it, change its tests, or send a payment link. Cancelling frees the slot; the customer is not messaged automatically unless you have an automation for it. Marking came / no-show can trigger automations (thank-you, rebook).
Bookings can also go to your own Google Calendar (Settings → Integrations → Google Calendar).

## Walk-in Queue (tokens)
Turn it on in Settings → Calls & queue → Walk-in Queue, and name it what you call it (token, table...). Queue page:
- Open a shift before checking anyone in. Call Next, Done or Skip people as you serve them.
- Self-service kiosk: a link for a tablet at the front desk - people check themselves in, no login.
- Reception QR: print it for the door. Patients scan it, WhatsApp opens with a message typed, and they get a token (and payment link, if you take fees online) on WhatsApp.
- Warn when N are ahead: the customer gets a WhatsApp alert when their turn is near.

## Tests and fees on a token or booking
If "Price list & payments" is on (Settings → Features) and you keep a fee list (Knowledge & Gaps → Fees / Price list), a "What for" list appears when you check someone in, book a token from a chat or a customer's page, or make a booking. Tick the tests or services they came for; the total shows as you tick.
- The token or booking shows what was ticked and the total. Use "Add tests" or "Change" on it to fix it later.
- Payment link: leave the amount empty and the link asks for the ticked total. If nothing is ticked it uses the doctor's fee, then your default fee. A typed amount always wins.
- Once a payment link has been sent (or money received) for a token or booking, its tests can't be changed - that link is for the old amount.
- Tokens from the kiosk, the reception QR and the AI on WhatsApp don't have tests ticked; add them at the counter with "Add tests".
