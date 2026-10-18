# Automations

## What an automation is
Automations page. A rule has one trigger (something that happened - a new lead, a complaint, a payment overdue, a booking) and steps that run in order. Each step can have conditions (all must hold, else that step is skipped) and a wait before it. Owner and admins only.

## Ways to make a rule
- Ready-made tab: common rules already set up (welcome a new lead, chase an overdue payment, birthday wish, no-show rebook...). Press Add.
- Describe it, KROVA builds it: write what you want in English or Hinglish ("payment 3 din overdue ho jaye to payment link bhejo"). KROVA asks what is missing, then opens the rule on the canvas unsaved. Check it and press Save. Each business gets a fixed number of these AI replies a month.
- New rule: build it yourself on the canvas - pick a trigger, then press + to add steps.

## Steps you can use
Send a WhatsApp follow-up (or one of your own approved templates), send an Instagram reply, reply privately to a comment, call the customer (AI call), alert the team, create a task, assign the chat, tag the customer, move them to a pipeline stage, send a WhatsApp Flow, send a carousel, send a payment link (on bookings, tokens, and payments due/overdue).

## Conditions, waits and stopping
- Conditions use the trigger's own fields (for example days_overdue, source of a lead, text of a message). Every trigger also has hour of the day and day of the week, in your business's time.
- Daily triggers ("Every day while a payment is overdue", "...customer has gone quiet", "...date approaching") fire every day for every match. Always add a condition that picks the day (for example days overdue equals 3), or the customer gets the message daily.
- A wait before a step delays it (up to 30 days). A step after a wait can stop if, since the rule started, the customer replied, booked or paid.
- How often for one customer (trigger panel): every time, only once per customer, or at most once every N days.

## Sending limits
Rules or Scheduling tab → "Sending limits" line. Quiet hours (default 9 pm to 9 am): steps to customers wait until they end. Daily cap (default 2 messages a customer a day). Neither applies while the customer is chatting with you right now, or to team alerts.

## Why didn't my automation run?
Open the Activity tab. Every step is listed with what happened: ran, skipped (with the reason - condition not met, quiet hours, daily cap, already ran for this customer), queued (waiting), or failed. Common causes:
- The WhatsApp template behind "Send a WhatsApp follow-up" (post_call_followup) isn't approved yet - the yellow notice on the page has a button to create it. Outside the 24-hour window a follow-up needs it.
- The rule is paused, or set to one channel only.
- A condition never matches (check the field and value).
Use "Try it first" on the canvas to see what a rule would do with sample data, without sending anything.
