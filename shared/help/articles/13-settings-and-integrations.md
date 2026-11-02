# Settings and integrations

Settings has five tabs: Business, Channels, Calls & queue, Leads & payments, Integrations. Owner and admins only.

## Business tab
- Business name, owner name, Google Review Link (set it to ask customers for a review after a completed visit; blank turns it off).
- Emergencies (healthcare businesses only): when a customer writes or says something like chest pain, can't breathe, fainted, heavy bleeding or an accident - in English, Hinglish or Hindi - KROVA answers at once, before anything else and whatever your Autonomy setting: "Please call 112 right now, or go to the nearest emergency room. I've alerted our team." On WhatsApp, Instagram, the website chat and calls. Your team gets an urgent alert on Escalations, and a call is put through to your team if anyone can take it. Add your own "Emergency desk number" here and it is said after 112. Ordinary questions ("emergency shift kab hai", "khoon ki jaanch") are not treated as emergencies.
- What kind of business is this: decides your starting modules, how the AI talks and what it must never promise. Changing it updates your menu and the AI's rules; features you added yourself stay.
- Features: switch modules on or off (see the list of features below). Some need a one-time setup after switching on.
- Price list & payments (on from the start for "You sell products" businesses; off for every other type until you switch it on): the Fees / Price list on the Knowledge page, ticking tests on tokens and bookings, taking an order in a chat, and the payment link for WhatsApp orders. Needs Razorpay connected (Leads & payments tab) to send links; Cash on delivery works without it. Razorpay keeps about 2% plus GST on that fee from each paid link, UPI included - check your own Razorpay plan.
- Agent Autonomy Level: Observe, Draft (recommended), Conditional, Act.
- What the AI can do for customers (owner and admins): tick, for Calls, WhatsApp and Instagram, where the AI may book an appointment, meeting or viewing; cancel a booking; move a booking to another time; give or cancel a walk-in queue token; take an order from the Catalogue; cancel an unpaid order. Booking, tokens, cancelling and moving are on everywhere by default; orders are off until you tick them. Each needs its module on in Features: Bookings for booking, cancelling and moving; the walk-in queue for tokens; Orders for cancelling an order; Orders and Price list & payments both for taking an order. Until they are on, the row is greyed out and the AI never does it, even if it was ticked before.
  - Every change is confirmed first: the AI repeats it back ("Confirm: cancel your booking on Sun 2 Nov, 5:00 PM?") and does it only if the customer's next message is a clear yes. Then it says what happened.
  - Only the customer's own booking, token or order can be changed. A booking can only be moved to a time that is really free. Orders are priced from your Catalogue page.
  - "Last time a booking can be cancelled or moved": closer than this to the booking (1 hour to 2 days, or any time), the AI hands it to your team.
  - You get a notification for every change the AI made (phone app: "Bookings or orders the AI changed"). An order the AI took shows "Taken by KROVA AI" in its note.
  - Anything not ticked, or that cannot be done (a paid or dispatched order, a time that is not free), goes to your team as an escalation.
  - Website chat only books; any change asked there goes to your team, because a visitor's number is not verified.
- Phone Login: link a phone number to also sign in with a code read out on a call (no SMS).

## Channels tab
WhatsApp, Instagram, voice and Gmail connections; WhatsApp Account Health, Business Profile, number verification and two-step PIN; Click-to-WhatsApp ad attribution (reports purchases back to Meta for ads that led to a chat - needs your Dataset ID from Events Manager).

## Calls & queue tab
- Escalation due time: if an escalation stays open this many hours, the owner gets an SMS (blank = off).
- Outbound number series: 080 / 022 for service calls, 140 for promotional. Campaigns are blocked unless their purpose matches.
- Walk-in Queue settings.

## Leads & payments tab
Connect lead sources (Justdial, IndiaMART, portals, email forwarding), add leads by hand or by file, import unpaid invoices, Razorpay payment links, payment with a booking.

## Integrations tab
- Google Calendar: every booking and token appears on your own calendar.
- Webhooks: send booking events to your own endpoint (Zapier, your CRM). Copy the signing secret when shown - it is shown once.
- API Keys: let your own systems call KROVA (ask a question, check availability, create a booking). Shown once on creation.
- Download your data: customers and conversations as CSV.
- Photo → Product match (with Orders & products): match a customer's photo against your Meta catalog.
- For software products: GitHub issues, outbound email, Stripe dunning.
Your plan sets how many API keys and webhooks you can have.
