# Refinements to discuss and build

Running list from the 2026-08-29/30 session. Each item is a real gap or
follow-up identified while wiring up Instagram + WhatsApp on production -
not yet scoped in detail, just captured so nothing gets lost.

## Auto-reply toggle (WhatChimp gap closer) - DONE this session

Turned out to already be 90% built and not require new design: the schema
field, the three-way setting (`observe`/`draft`/`act`), the `POST /autonomy`
endpoint, and the Settings UI picker ("Act Mode: Autonomous sending") all
already existed. The only missing piece was that `draft_for_message()`
(`services/workers/respond.py`) never actually checked for `act` - every
non-`observe` value produced the identical pending draft a person had to
approve, so the setting silently did nothing when set to `act`.

Fixed by extracting the real send logic (phone lookup, WhatsApp connection,
send, ingest as outbound, mark sent) out of `approvals.py`'s `approve()`
into a shared `shared/channels/send_draft.py`, used by both the human
approval endpoint and the worker's new auto-send branch. Auto-send only
fires for a genuine `reply` action (never `escalate` - that's the agent
saying it doesn't know what to say, which `act` mode doesn't override) and
only for WhatsApp (an Instagram-channel draft is left pending either way,
since send_draft has no Instagram path yet).

Brand-safe as designed: this is the business explicitly opting into
autonomy for their own account, not KROVA deciding for them - the default
stays `draft` (human review), and `act` requires the owner to deliberately
choose it in Settings.

**Not yet tested live** - built and pushed, but never exercised against a
real inbound message + `act` mode on the production deployment. Next
session should flip a test business to Act Mode, send a real WhatsApp
message in, and confirm it actually auto-sends and appears correctly in
Conversations (not just that the code path runs without erroring).

## Bulk-send safety (WhatsApp campaigns)

- DONE this session: added `SEND_PACE_SECONDS = 0.25` pacing between
  individual sends in `services/api/routers/campaigns.py`'s
  `send_campaign()` - was previously bursting sends back-to-back with no
  delay, which is a quality-rating risk even under Meta's daily tier cap
  (the existing `TIER_250`/`TIER_1K`/`TIER_10K` limit logic was already
  solid, this was the one missing piece).
- STILL OPEN: that pacing makes an existing architectural risk worse - the
  whole send loop runs synchronously inside one HTTP request. A
  250-recipient campaign now takes ~60+ seconds of pure pacing time on top
  of actual API latency, risking timeouts for anything but small
  campaigns. Real fix: move `send_campaign` onto the existing Postgres job
  queue (`shared/db/queue.py`, real infra already running via
  `services/workers/` + `services/api/scheduler.py`) - endpoint enqueues a
  job and returns immediately, a worker processes it paced in the
  background, and the queue's existing retry/reclaim logic handles a crash
  mid-run for free. Scoped work: new job type + handler, endpoint change
  to enqueue instead of run inline, frontend polling for status instead of
  waiting on the response.

## Shared inbox polish (WhatChimp gap)

KROVA has the underlying pieces (customer assignment, team performance
analytics, Approvals queue) but it was never confirmed whether the
Conversations page has an explicit "claim/assign this conversation to me"
UI the way a dedicated shared-inbox product does. Needs a direct look at
the current Conversations page UI (not assumed from memory) before deciding
whether this is a real gap or already good enough.

## Instagram - see project_instagram_fb_login_parked.md (Claude's memory)

App Review submitted 2026-08-30 for `instagram_business_basic` +
`instagram_business_manage_messages`. Not a code task right now - waiting
on Meta's decision. Full history, root cause, and everything tried is in
that memory file, not repeated here.

## Smaller loose ends mentioned in passing

- Remove the temporary "Connect Instagram (legacy)" button and its handler
  in `web/app/settings/page.tsx` once the Instagram Login vs Facebook Login
  path question is fully settled (both are currently live side by side for
  testing purposes).
- Remove the temporary Instagram test-send box in Settings
  (`web/app/settings/page.tsx`) once a real Instagram inbox/composer UI
  exists - it was only ever meant for the App Review screencast.
- No-code chatbot builder (the more ambitious version of the auto-reply
  idea above) - explicitly parked as a bigger product decision, not
  started.
