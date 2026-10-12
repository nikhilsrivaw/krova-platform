# Several voice numbers per business

Built 2026-10-12. Before this a second number could be bought and would answer calls, but everything else
assumed one: the settings and keypad-menu endpoints crashed (`scalar_one_or_none` over two rows), and every
outgoing call took "the first active number" with no ordering.

## What a number is

Each number is a `ChannelConnection`. The owner's choices live in its `extra` (no migration):

| key | meaning |
|---|---|
| `label` | what the owner calls it ("Sales", "Delhi branch"); the digits if blank |
| `use` | `both` (default), `inbound`, `outbound` |
| `default_outbound` | at most one per business - where outgoing calls come from unless something more specific applies |

Already per number (unchanged): greeting, language, language mode, voice, staff phone, copilot mode,
keypad menu. `Call.connection_id` records which number a call used.

## Choosing the number for an outgoing call

`shared/channels/voice/numbers.py::pick_outbound`, in this order:

1. the number the caller named (a campaign's "Call from") - used if it may call out, otherwise refused, never swapped;
2. for a campaign with a purpose, a number whose series is allowed for it (promotional = 140, service = 080/022);
3. the number marked default for outgoing calls;
4. the oldest number that may call out (a stable answer, not whichever row came back).

Used by: voice campaigns, ad-hoc/deadline calls, the COD call failsafe, escalation SMS, the automation SMS
action, click-to-call. The staff phone for alerts is the default number's, else the first number that has one.

An outgoing-only number declines incoming calls with a short spoken message. An incoming-only number is never
used for outgoing calls and cannot be the default.

## Campaigns and the Indian series rule

`CallCampaign.connection_id` (migration a3c9e5f7b1d2) is the chosen "Call from" (null = automatic). Before a
campaign is sent, `_check_number_series` judges **that number's own series** against the campaign's purpose. A
business with a 140 line and an 080 line can therefore send both kinds. The old business-wide typed
`outbound_number_series` setting is no longer required (the series is read from the number's digits; the typed
value could only disagree with a number it was not about). The setting still exists and is ignored here.

## The screens

- Voice -> Phone Numbers: a card per number - name, series badge, what it is for, "outgoing calls go from here",
  rent per month, last-30-day calls and cost, Release.
- Voice -> Settings, the keypad menu card, and Call Logs: a number picker when there is more than one number.
- Call Logs show which number each call used and can be limited to one.
- Campaign form: a "Call from" choice when more than one number can call out.

API: `GET /voice-onboarding/numbers/mine`; `connection_id` query parameter on `agent-settings` (GET/PATCH),
`keypad-menu` (GET/PUT) and `logs`; `label`, `use`, `default_outbound` in the agent-settings PATCH.

## Not built

- Number rotation to protect caller reputation (needs real call volume first).
- Per-number business hours or per-number team routing (team phones are per person, not per number).
- Reporting by number beyond calls and cost in the last 30 days.
- Anything checked against two real Plivo numbers - this was verified with scripted stand-ins only.
