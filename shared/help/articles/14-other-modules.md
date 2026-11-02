# Other pages

## Escalations
Every time the AI couldn't answer something on its own. (A customer who asks to talk to a person goes to Talk to team instead.) Open ones alert whoever is on staff if nobody acknowledges them for 15 minutes. Press "I have got this", reply to the customer (WhatsApp, Instagram or call), then Resolve with a short note (only your team sees it). You can build an automation for escalations like one you are looking at.

## Feedback (owner and admins)
Sidebar → Feedback. KROVA asks customers how it went and keeps what they say.
- Switch it on and choose what to ask about automatically: after a booking or appointment, after a walk-in token is done, after an order is delivered, after a chat is marked resolved. Only the moments your business has are shown - booking needs Bookings, token needs the walk-in queue, order needs Orders (Settings → Features); switch one on and it appears, with your earlier tick kept. Choose when the first ask goes (right away, 30 minutes to the next day after it is over), and how often one customer may be asked (at most once in 3, 7, 14 or 30 days, or no limit). Only visits, orders and chats from the last 3 days are asked about.
- Rounds: KROVA keeps asking until the customer answers, up to 5 rounds. Each round is WhatsApp or a call, and from round 2 you choose the gap: "if no answer, after" 1 hour to 1 week. Example: WhatsApp first, a call after 1 day, WhatsApp again after 2 days. It stops the moment they answer. If a round cannot go out (template not approved, voice wallet empty, calls blocked) the next round runs straight away. After the last round KROVA waits (2 days after a WhatsApp, 3 hours after a call) and then marks it "No answer after every round". Calls are paid from your voice wallet.
- Asking hours: "Only ask between" (default 9 AM to 8 PM, your business time). A round that falls outside waits for the next morning.
- Add customers to ask: for things that happened outside KROVA (a bus ticket sold on another app, a walk-in sale, a job done on site). Three ways on the Feedback page:
  - Upload a file: CSV or Excel (.xlsx), up to 2,000 customers and 2 MB. Old .xls files: open and save as .xlsx first. "Download a sample file" gives the layout.
  - Paste a list: copy rows from Excel or Google Sheets, or type one per line, separated by commas, tabs, semicolons or |.
  - One customer: name, phone, what to ask about, date and optional time.
  - Columns can be in any order and use your own names: name (Passenger, Customer), phone (Mobile, Contact, WhatsApp), what (Route, Trip, Service, Product, Item), date (Travel date, Visit date, Delivery date), time, and booking id (PNR, Ticket, Order id). Without a header row the order is name, phone, what, date. Dates like 02/11/2026 (day first), 2 Nov 2026 or 2026-11-02 all work.
  - They are asked on the same rounds, starting the chosen time after the date you gave (or now, if that has passed). Things older than 30 days, customers already on the list waiting to be asked, customers actually asked within your "at most once in" days, customers marked private, wrong phone numbers and the same row twice are skipped - the result shows each skipped row and why. A request that never went out (Feedback was off, or it was skipped) does not count as asked. If Feedback is switched off they wait until you switch it on.
  - "What" is the thing, not a question: write "order", "AC repair" or "Delhi to Jaipur bus". If someone types "how was the product?" KROVA keeps just "product". The form shows the message the customer will get.
  - If "what" has no date in it, the date is added ("Delhi to Jaipur bus on 2 Nov") because Meta only treats a message naming a specific visit or order as utility.
  - Your own software can send them too, with an API key from Settings: POST /public-api/feedback with {"customers": [{"name", "phone", "about", "date", "ref"}]}.
- WhatsApp needs the "feedback_request" template approved by Meta. Press "Send the templates to Meta for approval" on the Feedback page (usually a few minutes, at most a day). It names the visit or order ("How was your visit on 2 Nov with Sharma Clinic?"), which is what lets Meta treat it as a utility message (cheaper than marketing) - never add an offer to it. Until it is approved, WhatsApp rounds are skipped.
- On WhatsApp the customer taps Great, Okay or Not good (saved as 5, 3 or 1). After Okay or Not good they are asked what went wrong, and the next message they type is saved as their comment.
- On a call the AI asks for a rating from 1 to 5 and whether anything could be better; the answers are saved after the call. A call nobody answers counts as no answer and the next round runs. These calls run through an automatic "Feedback calls (automatic)" call campaign and survey script on the Voice page - leave them as they are.
- A rating of 1 or 2 goes to Talk to team as "Unhappy - rated 1/5 for their visit on 2 Nov", so whoever is free calls them; their comment is added to it.
- Review link: tick "Send happy customers (4-5) your review link" and paste any review page (Google, redBus, Justdial, your own site); leave it empty to use the Google review link from Settings. On WhatsApp it is in the thank-you reply. After a happy call it goes as a separate WhatsApp message, the "feedback_review_link" template - this one is a marketing template (charged at the marketing rate, about ₹0.86) and needs Meta approval too. Healthcare may not ask for reviews. With Feedback on, the old review request after every visit stops - only happy customers are asked.
- The page shows the last 30 days: average rating, how many answered, how many are still waiting for a round, happy and unhappy counts, a 1-5 breakdown, and every answer with its comment, how many times they were asked and whether they came from your list (filter: all answers, unhappy, happy, with a comment, still waiting).

## Signals
What's worth knowing from conversations: complaints, churn risk, praise, competitor mentions, and depending on your business, feature requests, bugs, overdue follow-ups, uncollected reports, refunds owed, delivery risk. Dismiss, or "Create an automation" for signals like it.

## Orders and Catalogue
Orders page (Orders & products feature): orders from your Shopify store, so the AI can answer "where is my order". Connect Shopify with a webhook (Shopify Admin → Settings → Notifications → Webhooks, topics orders/create, orders/updated, orders/cancelled, plus checkouts/create and checkouts/update for abandoned carts) and paste its secret. Connect Shiprocket (email and password) to check delivery status and catch failed delivery attempts. COD orders can be confirmed on WhatsApp.
Catalogue page: what you sell - import from your WhatsApp catalogue, upload a CSV, or add by hand.

## Taking an order in a chat
The AI can also take an order itself on calls, WhatsApp or Instagram, or cancel an unpaid one, once you tick it in Settings → What the AI can do for customers. It names the products from your Catalogue page, repeats the order and total back, and places it only after the customer says yes - with a payment link on WhatsApp or as cash on delivery.
Conversations → open a chat → "Take order" (also inside the customer panel, and in the phone app chat: "Take an order"). Needs two features on (Settings → Features): Orders & products, and Price list & payments - both are on from the start for "You sell products" businesses - plus products with a price on the Catalogue page.
- Find a product, press Add, set how many with - and +. The total shows as you go. Prices come from your Catalogue page; you can't type a different price.
- Choose "Send payment link" or "Cash on delivery", add a note if you like (size, address, gift wrap), and place the order.
- Payment link: a Razorpay link for the total goes on WhatsApp, naming what was ordered. The order is made only if the link could be made. When the customer pays, the order turns "paid" by itself, your order automations run, and if the customer came from a Meta ad, Meta is told about the purchase. Money recorded by hand on the Ledger for it does the same.
- Cash on delivery: the order is saved as confirmed, so KROVA's COD confirmation message and call don't go to this customer.
- The order appears on the Orders page with a number like WA-1A2B3C.
- If the customer hasn't written in 24 hours, WhatsApp needs an approved template with two variables (amount, link) - you're asked to pick one.
- A team member who isn't allowed to take payments (Team page) only sees Cash on delivery.
- The customer gets one WhatsApp message with each item and its price, the total, and your return and refund policy from the Business Card ("Cancel, return and refund"), with the payment link below it. For Cash on delivery the same summary goes on its own, if they wrote in the last 24 hours. Fill in that Business Card field - Indian e-commerce rules expect buyers to be told the price breakdown and the return policy.
- What you may not sell this way: WhatsApp's rules don't allow medicines, medical devices, test kits, thermometers or supplements to be sold in a chat, whatever licence you hold, and selling them can get your number restricted. Fees for services (a consultation, a scan, a test done at your lab) are fine.
- Razorpay keeps its fee from each paid link - about 2% plus GST on that fee, UPI included (check your Razorpay plan). Cash on delivery has no gateway fee.
- An order that came as a WhatsApp cart (or a chat order whose link was lost): on the Orders page, an unpaid order shows "Payment link" - it sends a link for the order's total, and the order turns paid by itself once paid. Shopify and WooCommerce orders don't get one; they are paid through their own store.

## Properties
Your listings (price, status, RERA number). The AI quotes price and availability only from here.

## Claims and Cases
Claims: track a warranty or insurance claim until it closes; customers can be updated when its status changes. Cases: one per client with status and next hearing; the AI answers only from what you record.

## Analytics
AI performance (approval rate, edits, rejections - whether it is safe to move from Draft to Act mode), response speed, the trust report, team performance, receivables aging and promise-keeping.

## Mobile app
KROVA has a mobile app view with "Ask KROVA" (owner and admins), where you can ask anything about your own business in Hindi, Hinglish or English and get an answer from KROVA's records: money owed and received, a customer by name or number (their messages, promises, bookings, orders, calls, notes), bookings, walk-in tokens, orders, leads, quotations, calls, escalations, signals, campaigns, AI replies, the team's work and the voice wallet - for today, last week, last month or any dates you name. For example "aaj kya pending hai?", "pichhle mahine business mein kya hua?", "Sharma ji ka kya scene hai?". It never guesses: what KROVA doesn't record (like expenses or profit) it says it doesn't track.
