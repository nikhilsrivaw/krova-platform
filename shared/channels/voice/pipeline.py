"""
One live call, orchestrated.

Three concurrent things happen for the length of a call: caller audio flows
in from Plivo, transcripts flow out of Sarvam's STT, and replies flow out
through Sarvam's TTS back to Plivo. This module is the state machine that
keeps them coherent.

Barge-in is the part worth reading carefully. When a caller talks over the
agent, three things must happen together:

  1. tell Plivo to stop playing whatever is queued (clearAudio) - otherwise
     the agent keeps talking into a call the caller has already redirected
  2. cancel the in-flight Claude generation - tokens generated for a reply
     nobody will hear are pure cost
  3. record only the words the caller actually heard before interrupting, so
     Claude's own memory of what it said matches reality - otherwise the next
     turn reasons from a sentence that was never fully spoken

Every turn - caller and agent - is written through the same ingest() every
other channel uses, with channel=voice. That single line is what makes a
phone call join the same customer record as their WhatsApp messages, with no
special-casing anywhere else in the platform.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from shared.ai import agent as agent_module
from shared.ai import context as agent_context
from shared.auth.encryption import decrypt
from shared.billing import usage
from shared.channels import ingest
from shared.channels.voice import plivo_client
from shared.channels.voice.tenant import VoiceRoute
from shared.config.settings import settings
from shared.db.models import (
    Business,
    Call,
    Channel,
    ChannelConnection,
    Customer,
    Direction,
    IdentityKind,
    IntakeChannel,
    UsageEventType,
)
from shared.scheduling import booking as scheduling_booking
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# How long to wait after the caller stops speaking before treating the
# utterance as something the agent should answer. Sarvam's own VAD produces
# transcript.final, so this is a light debounce on top rather than the
# primary signal.
FINAL_DEBOUNCE_S = 0.3

SendAudio = Callable[[bytes], Awaitable[None]]
SendClear = Callable[[], Awaitable[None]]


async def _single_chunk(text: str):
    """Wrap one fixed string (the greeting) as the one-item stream speak() expects."""
    yield text


@dataclass(slots=True)
class Turn:
    role: str  # "caller" | "agent"
    text: str
    complete: bool = True  # False when cut off by barge-in


@dataclass(slots=True)
class CallPipeline:
    """
    Holds one call's state. Fed transcripts and audio; drives the reply.

    Every I/O boundary is a plain async callable, injected rather than opened
    here - that is what makes the barge-in logic testable without a live
    Plivo or Sarvam connection.
    """

    route: VoiceRoute
    caller_phone: str
    provider_call_id: str
    send_audio: SendAudio
    send_clear: SendClear
    # Takes a STREAM of text pieces, not one string - a reply is spoken
    # sentence by sentence as Claude generates it, so TTS can start on the
    # first sentence while later ones are still being decided. The greeting
    # (a fixed string known instantly) uses the same path via
    # _single_chunk(), rather than keeping a second, one-shot code path.
    speak: Callable[["asyncio.AsyncIterator[str]"], "asyncio.AsyncIterator[bytes]"]
    db: AsyncSession
    call_row_id: uuid.UUID | None = None
    # Set only for an outbound campaign call (see outbound.py) - the
    # AI-drafted line explaining why KROVA is calling, spoken instead of
    # route.greeting. None (the default) preserves inbound behaviour
    # exactly: every existing call still opens with route.greeting.
    opening_line: str | None = None

    history: list[dict] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    customer_id: uuid.UUID | None = None
    # What Sarvam's STT actually heard the caller speaking, not what the
    # business's connection was configured for - a caller code-mixing
    # Hindi and English should get a reply spoken back the same way, not
    # forced into whatever language_code the connection happened to default
    # to. Read by relay.py's speak() closure; falls back to route.language
    # until the first final transcript reports one.
    detected_language: str | None = None
    _reply_task: asyncio.Task | None = field(default=None, repr=False)
    _spoken_chars: int = 0
    # The caller's own turn, for source_message_ids on a booking made from
    # it - an AI-mediated booking must cite the conversation that
    # authorised it, the same rule book.py enforces for every channel.
    _last_inbound_message_id: uuid.UUID | None = None

    async def start(self) -> None:
        """Greet the caller. The first thing anyone hears on the call."""
        await self._say_stream(_single_chunk(self.opening_line or self.route.greeting), record=True)

    async def on_transcript(self, text: str, *, is_final: bool, language: str | None = None) -> None:
        """
        A piece of caller speech arrived.

        Partial transcripts are used only to detect that the caller has
        started talking again - which is what triggers barge-in - never to
        drive a reply. Only a final transcript is answered.
        """
        if not text.strip():
            return

        # Only a final transcript's language is trusted - a partial can
        # report a different, unsettled guess before more audio arrives
        # (e.g. flickering en-IN then correcting to hi-IN as a Hindi
        # sentence continues), and unlike partial text (explicitly never
        # used to drive a reply, per this docstring), detected_language
        # DOES drive which language the reply is spoken in - so trusting a
        # partial guess here was the one place that rule wasn't actually
        # being followed, and a plausible source of a reply coming back in
        # the wrong language mid-call.
        if language and is_final:
            self.detected_language = language

        if self._reply_task is not None and not self._reply_task.done():
            await self._barge_in()

        if is_final:
            await self._handle_utterance(text.strip())

    async def _barge_in(self) -> None:
        """
        The caller started talking while the agent was still speaking.

        Order matters: silence Plivo first, so nothing more plays while the
        rest of this unwinds, then stop paying for a generation nobody will
        hear.
        """
        await self.send_clear()

        if self._reply_task is not None:
            self._reply_task.cancel()
            try:
                await self._reply_task
            except asyncio.CancelledError:
                pass
            self._reply_task = None
            # Cancelling mid-flush/commit leaves the shared session in a
            # rolled-back-but-not-cleared state - confirmed on a real call,
            # where the NEXT _store_turn on this same session raised
            # PendingRollbackError and silently dropped a turn. One session
            # lives for the whole call, so a cancellation anywhere in it has
            # to leave the session clean for whatever runs next.
            try:
                await self.db.rollback()
            except Exception:
                logger.exception("failed to clear session state after barge-in cancel")

        # The agent's turn is on record as whatever fraction was actually
        # spoken before it was cut off, not the sentence it never finished -
        # so the next turn reasons from what the caller heard.
        if self.turns and self.turns[-1].role == "agent" and not self.turns[-1].complete:
            spoken = self.turns[-1].text[: self._spoken_chars].strip()
            if spoken and self.history and self.history[-1]["role"] == "assistant":
                self.history[-1]["content"] = spoken
                self.turns[-1].text = spoken

    async def _handle_utterance(self, text: str) -> None:
        """Store what the caller said, then generate and speak a reply."""
        self.turns.append(Turn(role="caller", text=text))
        self.history.append({"role": "user", "content": text})

        await self._store_turn(direction=Direction.inbound, text=text)

        self._reply_task = asyncio.create_task(self._reply(started_at=time.monotonic()))
        try:
            await self._reply_task
        except asyncio.CancelledError:
            pass

    async def _reply(self, *, started_at: float | None = None) -> None:
        """
        Ask the agent what to say, speaking each sentence as it is decided.

        Uses agent_module.stream_reply, not draft_reply: a live call cannot
        wait for a whole JSON tool-call to finish (action, message,
        reasoning, gap, confidence, in that order) before it can even start
        speaking - the reasoning/gap/confidence generated AFTER the message
        was pure added latency nobody heard the benefit of. Streaming plain
        text instead means the first sentence can reach Sarvam the instant
        Claude finishes writing it.

        escalate is the case worth reading carefully. On text channels it
        means "queue this for a person, the customer hears nothing yet" -
        correct there, since someone can pick it up in an hour. A live call
        has nobody to hand off to mid-conversation: silence is not neutral,
        it sounds like the line dropped. So escalate here still speaks -
        honestly, naming what it does not know rather than guessing - and
        records the gap the same way a text escalation does, so the
        business's knowledge still compounds even though nothing was queued.
        """
        if self.customer_id is None:
            logger.warning("reply requested with no resolved customer, skipping")
            return

        t_context = time.monotonic()
        context = await agent_context.build(self.route.business_id, self.customer_id, self.db)
        t_stream_start = time.monotonic()
        if started_at is not None:
            logger.info(
                "voice latency call=%s context=%.2fs",
                self.provider_call_id,
                t_stream_start - t_context,
            )

        events = agent_module.stream_reply(context)
        try:
            first = await events.__anext__()
        except StopAsyncIteration:
            logger.warning("stream_reply produced no events at all")
            return

        action = first.action if isinstance(first, agent_module.ReplyStart) else "escalate"

        if action == "no_action":
            async for _ in events:
                pass
            return

        if action == "escalate":
            gap: str | None = None
            cost_paise = 0
            async for ev in events:
                if isinstance(ev, agent_module.ReplyDone):
                    gap = ev.gap
                    cost_paise = ev.cost_paise
            usage.record(
                business_id=self.route.business_id,
                event_type=UsageEventType.ai_reply_generated,
                channel="voice",
                quantity=1,
                unit="call",
                krova_cost_paise=cost_paise,
                source_type="call",
                source_id=self.call_row_id,
                db=self.db,
            )
            if gap:
                await agent_module.record_gap(self.route.business_id, gap, self.db)
            if self.call_row_id is not None:
                call_row = await self.db.get(Call, self.call_row_id)
                if call_row is not None:
                    call_row.escalated = True
                    call_row.escalation_reason = gap

            # A business that has opted into a warm transfer gets a real
            # handoff instead of an apology - staff_phone_number is unset
            # for every business by default, so this is additive: nothing
            # about today's apologize-and-hangup behaviour changes unless a
            # business has explicitly configured a number.
            if self.route.staff_phone_number and await self._try_transfer():
                await self._say_stream(
                    _single_chunk(
                        "Let me connect you to someone who can help with that right now."
                    ),
                    record=True,
                )
                return

            spoken = (
                f"I don't have {gap} on hand right now, but I'll make sure "
                "someone follows up with you on that."
                if gap
                else "I don't have enough to answer that properly, but I'll make "
                "sure someone follows up with you."
            )
            await self._say_stream(_single_chunk(spoken), record=True)
            return

        if first.book_slot:
            business = await self.db.get(Business, self.route.business_id)
            customer = await self.db.get(Customer, self.customer_id)
            appointment = None
            if business is not None and customer is not None and self._last_inbound_message_id is not None:
                appointment = await scheduling_booking.try_book_from_agent(
                    self.db,
                    book_slot=first.book_slot,
                    book_doctor=first.book_doctor,
                    book_property=first.book_property,
                    business=business,
                    customer=customer,
                    intake_channel=IntakeChannel.voice,
                    source_message_ids=[self._last_inbound_message_id],
                )
            if appointment is None:
                # The reply already generated for this turn most likely
                # assumes success ("you're all set for 3pm!") - speaking it
                # would be exactly the invented-fact problem the whole agent
                # exists to avoid, same reasoning respond.py's own booking
                # path follows for text. Drain the rest of the stream
                # unspoken and say something honest instead.
                async for _ in events:
                    pass
                logger.info(
                    "voice booking failed call=%s book_slot=%s, speaking fallback instead",
                    self.provider_call_id, first.book_slot,
                )
                if self.call_row_id is not None:
                    call_row = await self.db.get(Call, self.call_row_id)
                    if call_row is not None:
                        call_row.escalated = True
                        call_row.escalation_reason = f"Could not book {first.book_slot}"
                await self._say_stream(
                    _single_chunk(
                        "I wasn't able to lock in that exact time - let me "
                        "have someone confirm the details with you."
                    ),
                    record=True,
                )
                return
            logger.info(
                "voice booking succeeded call=%s appointment=%s",
                self.provider_call_id, appointment.id,
            )

        reply_cost = {"paise": 0}

        async def reply_text_chunks():
            async for ev in events:
                if isinstance(ev, agent_module.ReplyChunk):
                    yield ev.text
                elif isinstance(ev, agent_module.ReplyDone):
                    reply_cost["paise"] = ev.cost_paise

        if started_at is not None:
            t_first_event = time.monotonic()
            logger.info(
                "voice latency call=%s time-to-action=%.2fs (utterance-to-action=%.2fs)",
                self.provider_call_id,
                t_first_event - t_stream_start,
                t_first_event - started_at,
            )

        await self._say_stream(reply_text_chunks(), record=True)
        usage.record(
            business_id=self.route.business_id,
            event_type=UsageEventType.ai_reply_generated,
            channel="voice",
            quantity=1,
            unit="call",
            krova_cost_paise=reply_cost["paise"],
            source_type="call",
            source_id=self.call_row_id,
            db=self.db,
        )

    async def request_transfer(self) -> None:
        """
        The caller pressed the keypad's escape-hatch digit (0) asking for a
        person, independent of anything the AI itself decided - Plivo
        delivers DTMF over this same relay socket (RFC-2833, out of band
        from the audio codec), confirmed against Plivo's own streaming
        docs. Every enterprise IVR keeps this safety net; until now a
        caller had no way out except talking to the agent.

        Reuses _barge_in() rather than duplicating its silence-then-cancel-
        then-patch-transcript ordering - a caller pressing 0 mid-reply
        should stop hearing the AI exactly the same way a caller talking
        over it does. If nothing was playing this is close to a no-op.

        Reuses _try_transfer() (the same Live Call Modification call the
        AI's own escalate path already makes) rather than a second transfer
        mechanism - one way to bridge a call, two ways to trigger it.
        """
        await self._barge_in()

        if self.call_row_id is not None:
            call_row = await self.db.get(Call, self.call_row_id)
            if call_row is not None:
                call_row.escalated = True
                call_row.escalation_reason = "Caller pressed 0 to reach a person"

        if self.route.staff_phone_number and await self._try_transfer():
            await self._say_stream(
                _single_chunk(
                    "Let me connect you to someone who can help with that right now."
                ),
                record=True,
            )
            return

        await self._say_stream(
            _single_chunk(
                "There's no one else available to transfer you to right now, "
                "but I'll make sure someone follows up with you."
            ),
            record=True,
        )

    async def _try_transfer(self) -> bool:
        """
        Ask Plivo to bridge this live call to the business's configured
        staff number, via its Live Call Modification API.

        Returns False for anything short of Plivo actually accepting the
        request - the caller's job either way is the same: fall back to
        the honest "someone will follow up" message rather than claim a
        transfer that never happened. The request shape here is correct
        per Plivo's documented Transfer API; the exact timing of when
        Plivo actually cuts the Stream mid-transfer is not yet confirmed
        against a real call, since no number is connected in production
        yet - called before speaking the "connecting you" line specifically
        so a caller is only ever told a transfer is happening once Plivo
        has actually accepted the request.
        """
        connection = await self.db.get(ChannelConnection, self.route.connection_id)
        if connection is None or not connection.access_token:
            logger.warning(
                "transfer requested call=%s but no voice connection credentials found",
                self.provider_call_id,
            )
            return False

        auth_id = (connection.extra or {}).get("subaccount_auth_id")
        if not auth_id:
            logger.warning(
                "transfer requested call=%s but connection has no subaccount_auth_id",
                self.provider_call_id,
            )
            return False

        aleg_url = (
            f"{settings.public_base_url.rstrip('/')}/voice/transfer-xml?"
            f"{urlencode({'number': f'+{self.route.staff_phone_number}'})}"
        )
        try:
            await plivo_client.transfer_call(
                auth_id=auth_id,
                auth_token=decrypt(connection.access_token),
                call_uuid=self.provider_call_id,
                aleg_url=aleg_url,
            )
        except plivo_client.PlivoError:
            logger.exception("call transfer failed call=%s", self.provider_call_id)
            return False

        logger.info(
            "call transfer triggered call=%s to=+%s",
            self.provider_call_id, self.route.staff_phone_number,
        )
        return True

    async def _say_stream(self, text_chunks, *, record: bool) -> None:
        """
        Speak a reply as its text arrives, tracking what actually reached
        the caller and what Claude has said so far as one and the same
        growing string.

        `_spoken_chars` is read by `_barge_in` if this gets cut off
        mid-flow, which is how a partial sentence becomes the honest record
        instead of the whole thing - unchanged from before streaming, but
        now measured against `turn.text` as it grows rather than a string
        that was already complete when speaking began. `self.history[-1]`
        is kept live-updated for the same reason: `_barge_in` patches
        `self.history[-1]["content"]` on interruption, and that code was
        never rewritten - it still finds an assistant entry there because
        one is appended immediately, not after the reply is fully known.
        """
        turn = Turn(role="agent", text="", complete=False)
        self.turns.append(turn)
        if record:
            self.history.append({"role": "assistant", "content": ""})

        async def tracked_chunks():
            async for chunk in text_chunks:
                turn.text += (" " if turn.text and not turn.text.endswith(" ") else "") + chunk
                if record:
                    self.history[-1]["content"] = turn.text
                yield chunk

        self._spoken_chars = 0
        t_start = time.monotonic()
        t_first_chunk: float | None = None
        try:
            async for chunk in self.speak(tracked_chunks()):
                if t_first_chunk is None:
                    t_first_chunk = time.monotonic()
                await self.send_audio(chunk)
                # Rough proportional tracking - good enough for barge-in
                # recovery, not claimed as exact.
                self._spoken_chars = min(
                    len(turn.text), self._spoken_chars + max(1, len(chunk) // 40)
                )
            turn.complete = True
            self._spoken_chars = len(turn.text)
        except asyncio.CancelledError:
            raise
        finally:
            if t_first_chunk is not None:
                logger.info(
                    "voice latency call=%s tts_connect_and_first_chunk=%.2fs",
                    self.provider_call_id,
                    t_first_chunk - t_start,
                )
            if record and turn.text.strip():
                await self._store_turn(
                    direction=Direction.outbound,
                    text=turn.text[: self._spoken_chars] if not turn.complete else turn.text,
                )

    async def _store_turn(self, *, direction: Direction, text: str) -> None:
        """
        Write this turn through the same path every channel uses.

        This one line is what makes a phone call join the same customer
        record as a WhatsApp conversation - identity resolves on phone
        number, and voice needs no special case anywhere downstream.
        """
        if not text.strip():
            return
        try:
            result = await ingest.ingest(
                business_id=self.route.business_id,
                channel=Channel.voice,
                direction=direction,
                identity_kind=IdentityKind.phone,
                identity_value=self.caller_phone,
                external_id=f"{self.provider_call_id}:{int(time.time() * 1000)}",
                text=text,
                occurred_at=datetime.now(timezone.utc),
                # A call transcript is drafted-and-approved territory later,
                # not something to draft a reply to - the reply already
                # happened, out loud.
                enqueue_analysis=True,
                db=self.db,
            )
            if result.customer is not None:
                self.customer_id = result.customer.id
            if direction == Direction.inbound and result.message is not None:
                self._last_inbound_message_id = result.message.id
            await self.db.commit()
        except Exception:
            logger.exception("failed to store call turn - continuing the call")
            await self.db.rollback()
