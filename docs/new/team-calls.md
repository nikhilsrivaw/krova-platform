# Team calls: the right person, with context, and what was said is kept

Built 2026-10-12. Four pieces, each small, all reusing what KROVA already holds.

## 1. Each member has a phone, and calls ring the right one

`business_members.phone` (digits, no plus). Set by the member (`PUT /team/me/phone`, "My phone" on the phone
app's Today screen) or by owner/admin (`PATCH /team/members/{id}/phone`, Team page).

A transferred call (caller asks for a person, presses 0, or the AI escalates) used to ring the business's single
`staff_phone_number`. Now `shared/team/call_routing.py` orders the list:

1. the customer's own team member, if on the team, not Away, with a number;
2. then the other available members, longest since a chat first (the same fairness as chat round-robin);
3. then the old business staff number, if set.

Capped at 3 members + the fallback. One number at a time, 18 s each. `/voice/transfer-xml` walks the list: Plivo
`<Dial action=...>` posts back the result (`DialStatus`), and the next index is dialled. Everyone tried -> the caller
is told honestly and the call ends. A business that never set a member phone behaves as before (rings its one number).
Menu keys that ring a specific number use the old single-number form.

Why one at a time rather than ringing everyone: we know exactly who answered (needed for the write-up), and a
customer is not put on hold for a dozen phones.

## 2. The pick-up briefing

`shared/team/briefing.py`, `GET /team/briefing/{customer_id}`. No AI call - read from what exists: lead enquiry,
AI summary, last messages, open promises and what is owed, open escalation, next booking, last call, team notes.
A private customer is only a name to an agent. Agents under "assigned only" visibility cannot read a teammate's.

Shown as the default **Briefing** tab in the desktop customer drawer, and as a collapsible card at the top of the
phone app's thread. The member being put through to a transferred call is pushed a one-line version
(`Asha · asked: 2BHK in Baner · owes ₹5,000 · 1 overdue promise`).

## 3. Call a customer from the app

`POST /calls/start {customer_id}`. Checks: the member has a phone; the customer has one; a voice number is
connected; plan active; wallet not empty (`require_wallet`); the customer is not a teammate's (the same
`guard_reply` as a message - refused with their name, or claimed if unowned); private chats closed to agents; no
call already being connected.

Plivo rings the **member** first (`make_call`, answer_url = `/voice/bridge-answer`). When they pick up they hear
`Calling <headline>. Connecting you now.` and the customer is dialled from the business number (`callerId`) - the
customer sees a number they know, the member never gives out theirs. `/voice/bridge-dialed` records the customer
leg's outcome; `/voice/bridge-hangup` closes the Call row.

The three webhooks live in `shared/channels/voice/human_calls.py` (voice service) and are proxied by
`services/api/voice_proxy.py`. The app polls `GET /calls/{id}` every 2 s: `ringing_you -> talking -> ended`.

Money: a click-to-call is two Plivo legs. `record_leg_costs` fetches both CDRs (retrying, then estimating from the
number's rate) and writes a `voice_call_minutes` usage event; the wallet then charges cost + 25% like any call.
The leg of a *transferred* call to the member was billed by Plivo and recorded nowhere before; it is now.

## 4. Writing the call up, and speed to lead

`POST /calls/{id}/wrapup {note, promises[]}`. The line becomes a team note and a timeline entry (an outbound voice
message with `sent_by_user_id`, no analysis). Each promise becomes a `we_owe` commitment, confirmed by the author and
pointing at that timeline entry, due 6 pm IST on the chosen day. So "I'll send the quote tomorrow" said on a phone is
chased like one the AI heard. Idempotent; at most 5 promises.

Calls still to write up (placed, or transferred and answered) show on the phone Today screen and the desktop
Command Center, and count in `GET /team/my-work` (`calls_to_write_up`).

Leads (`GET /leads`) now carry `first_touch_at` (first message or call by a person after the lead arrived) and the
owner's name. The Leads page shows "Answered after 3 min" or "Nobody has answered yet - waiting 12 min".

## Not verified against live Plivo

- `DialStatus` values and that `<Dial action>` is called for a transferred call's A-leg (docs say yes; unconfirmed live).
- `callerId` on the bridged `<Dial>` being accepted for the business number.
- That the CDR of the member leg/customer leg is queryable by the UUIDs we store (`CallUUID` on answer,
  `DialBLegUUID` on dial end). If not, cost falls back to the estimate and the usage event says so.
- Wrap-up for a *transferred* call depends on `DialStatus=completed` identifying who answered.

## Known gaps, by design

- The conversation on a human call is not transcribed, so promises are typed by the member, not extracted.
  Recording + transcription is a separate, costlier build.
- Cancelling or moving a booking does not message the customer (unchanged).
- No ring-everyone mode and no working-hours routing for calls; "Away" is the only switch.
