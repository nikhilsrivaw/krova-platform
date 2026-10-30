# Other pages

## Escalations
Every time the AI couldn't handle something on its own and asked for a person. Open ones alert whoever is on staff if nobody acknowledges them for 15 minutes. Press "I have got this", reply to the customer (WhatsApp, Instagram or call), then Resolve with a short note (only your team sees it). You can build an automation for escalations like one you are looking at.

## Signals
What's worth knowing from conversations: complaints, churn risk, praise, competitor mentions, and depending on your business, feature requests, bugs, overdue follow-ups, uncollected reports, refunds owed, delivery risk. Dismiss, or "Create an automation" for signals like it.

## Orders and Catalogue
Orders page (Orders & products feature): orders from your Shopify store, so the AI can answer "where is my order". Connect Shopify with a webhook (Shopify Admin → Settings → Notifications → Webhooks, topics orders/create, orders/updated, orders/cancelled, plus checkouts/create and checkouts/update for abandoned carts) and paste its secret. Connect Shiprocket (email and password) to check delivery status and catch failed delivery attempts. COD orders can be confirmed on WhatsApp.
Catalogue page: what you sell - import from your WhatsApp catalogue, upload a CSV, or add by hand.

## Taking an order in a chat
Conversations → open a chat → "Take order" (also inside the customer panel, and in the phone app chat: "Take an order"). Needs two features on (Settings → Features): Orders & products, and Price list & payments - plus products with a price on the Catalogue page.
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
