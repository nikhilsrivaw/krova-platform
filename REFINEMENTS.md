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

**CONFIRMED WORKING LIVE - 2026-08-31.** Tested end to end on production
against "Krova Demo" (business_id 1863fab8-28ef-405e-88e8-e9f98180659f):
real inbound WhatsApp message -> real webhook delivery -> real Claude call
-> draft created -> auto-send guard correctly evaluated. First test message
was gibberish ("sfvxvxcvxcvxcvxcv"), agent correctly chose `no_action` -
no draft row at all, proving the agent doesn't force an answer onto
nonsense. Second test asked about business hours (no real hours configured
in this demo business's profile), agent correctly chose `escalate` rather
than invent an answer - draft created with status `pending`, NOT auto-sent,
exactly matching the `autonomy == "act" and proposal.action == "reply"`
guard in respond.py. This is the harder, more convincing proof than a
clean auto-send would have been: it shows act mode respects the agent's
own judgment rather than blindly firing everything.

Still not seen: an actual `reply`+auto-send happening (needs a question the
agent can confidently answer from real business profile data, which this
demo business doesn't have filled in). Next step to see the full happy
path: add real business details in Settings' Business Profile, then send
a message the agent can confidently answer.

Two unrelated, real bugs were found and fixed to get to this point, not
part of the original toggle work:
1. **No worker process ever ran on this deployment** -
   `services/workers/{respond,analyse,profile}.py` are designed as their
   own long-lived processes, but `docker-compose.prod.yml` only ran the
   API server. Every enqueued job (drafts, analysis, profile compression)
   had been silently piling up since this deployment went up - confirmed
   via a real backlog (5 analyse_business + 1 compress_customer, both
   pending). Fixed by adding three dedicated worker containers to
   `docker-compose.prod.yml`, each `python -m services.workers.X` (the
   plain script-path form fails with ModuleNotFoundError - confirmed
   directly, matching an error seen earlier the same night with a
   one-off diagnostic script).
2. **Settings could not save anything, ever, on production** -
   `POST /auth/me` (`update_me` in `services/api/routers/auth.py`, the
   real endpoint behind "Save Settings" including the autonomy picker)
   constructed its `MeResponse` without the required `capabilities` field
   that `GET /auth/me` includes, raising a Pydantic validation error
   internally on every call - surfaced to the browser as a bare
   "Failed to fetch" and a 500 in the network tab. Root-caused directly
   from the production request/response, not guessed. This means no
   profile edit, vertical change, or autonomy level had ever actually
   persisted on this deployment before this fix.
3. **WhatsApp's webhook Callback URL was pointed at a dead ngrok tunnel**
   (`https://flagstick-eligible-plank.ngrok-free.dev/webhooks/whatsapp`,
   left over from local dev) instead of
   `https://api.krova.space/webhooks/whatsapp` - Meta was successfully
   subscribed and sending every inbound WhatsApp event, just to a URL that
   no longer existed. No error surfaced anywhere because a webhook with
   nowhere to land doesn't look broken, it looks quiet - same shape as the
   Instagram Login mystery earlier this session. Fixed by updating the
   Callback URL in the WhatsApp use case's own webhook config page.

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

## Shared inbox polish (WhatChimp gap) - DONE this session

Confirmed by direct look, not memory: the Conversations page already had a
real per-thread assign-to-teammate dropdown and the backend already
returned `assigned_to_user_id` on every list item - the actual gap was
narrower than "no assignment feature," it was that the thread LIST never
surfaced it: no assignee shown at a glance, no All/Mine/Unassigned filter,
only visible once a thread was already open.

Added both in `web/app/conversations/page.tsx`: an All/Mine/Unassigned
filter row (gated behind `teamMembers.length > 1`, matching the existing
assign-dropdown's own gating) and a small initial-avatar badge per thread
row when assigned. No backend change needed - the data already existed.
Not yet visually verified in a live browser (no browser available in this
session) - worth a quick look once deployed.

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
