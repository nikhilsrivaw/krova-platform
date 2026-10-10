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
import re
import time
import uuid
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from shared.ai import agent as agent_module
from shared.ai import voice_guard
from shared.ai import context as agent_context
from shared.auth.encryption import decrypt
from shared.billing import usage
from shared.channels import ingest
from shared.channels.voice import keypad_menu, plivo_client
from shared.care import post_call_actions
from shared.identity import resolver
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
from shared.scheduling import queue_booking
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# How long to wait after the caller stops speaking before treating the
# utterance as something the agent should answer. Sarvam's own VAD produces
# transcript.final, so this is a light debounce on top rather than the
# primary signal.
FINAL_DEBOUNCE_S = 0.3

SendAudio = Callable[[bytes], Awaitable[None]]
SendClear = Callable[[], Awaitable[None]]


# How long the partials must stop growing before a reply is speculated
# on. Short enough to land well inside Sarvam's own 500ms silence window,
# long enough that a caller drawing breath mid-sentence rearms it rather
# than burning a generation per word.
_SPECULATION_PAUSE_SECONDS = 0.3
_SPECULATION_ENABLED = True

# How alike a final transcript and the partial speculated on have to be
# for the generated reply to still answer the right question. Measured
# against real calls: Sarvam rewrites its own text when finalising a turn
# - dropping fillers ("Uh, can you..." -> "Can you..."), correcting proper
# nouns ("what crow" -> "what CROA"), and appending the last word or two
# ("am I talking to" -> "am I talking to Krova?"). Every one of those is
# the same question and a reply written against either is right, but exact
# comparison threw all of them away. A genuine change of subject scores
# far below this, and is still discarded.
_SPECULATION_MATCH_RATIO = 0.82

_PUNCTUATION = str.maketrans("", "", ".,!?;:'\"-–—")
# Sounds a speaker makes while thinking, which an STT may or may not keep.
_FILLERS = {"uh", "um", "er", "ah", "hmm", "mm", "haan", "acha"}


def _normalise_for_match(text: str) -> str:
    """Compare transcripts the way a listener would - words, not punctuation or fillers."""
    words = text.lower().translate(_PUNCTUATION).split()
    kept = [w for w in words if w not in _FILLERS]
    return " ".join(kept or words)


def _same_utterance(speculated: str, final: str) -> bool:
    a, b = _normalise_for_match(speculated), _normalise_for_match(final)
    if not a or not b:
        return False
    if a == b or b.startswith(a) and len(b.split()) - len(a.split()) <= 3:
        return True
    return SequenceMatcher(None, a, b).ratio() >= _SPECULATION_MATCH_RATIO


async def _events_from_queue(queue: "asyncio.Queue"):
    """
    Replay a speculative generation as the same event stream stream_reply
    produces, so _reply cannot tell the difference.

    Pieces already generated come straight out; the rest arrive as they
    are written. None is the producer's end marker.
    """
    while True:
        event = await queue.get()
        if event is None:
            return
        yield event


@dataclass(slots=True)
class _Speculation:
    """A reply generated against a partial transcript, waiting to be confirmed."""

    text: str
    queue: "asyncio.Queue"
    task: asyncio.Task


# Plivo Stream audio is 8 kHz mu-law: one byte per sample.
_MULAW_BYTES_PER_SECOND = 8000
_PLAYBACK_MARGIN_SECONDS = 0.5

# A caller asking for a person: a person word AND a request in the same
# utterance. A person word alone ("team size", "manager ka naam") or a bank
# "transfer" never counts, so an ordinary question does not hand the call over.
_PERSON_NOUN = (
    r"(?:insaan\w*|human|real person|person|manager|owner|staff|team|agent|"
    r"executive|representative|operator|sales|someone|somebody|koi|kisi)"
)
_PERSON_WANT = (
    r"(?:baat (?:karn|karwa|krn|krwa|kar|karo|karao)\w*|talk to|speak to|speak with|"
    r"connect (?:me|kar|karo|karwa|kardo|kar do|krdo|krwa|to)|bulao|bulwa\w*|"
    r"chahiye|karni hai|karna hai|want to)"
)
_PERSON_REQUEST = re.compile(
    rf"\b(?=.*\b{_PERSON_NOUN}\b)(?=.*\b{_PERSON_WANT}\b)"
    r"|\btransfer (?:kar|karo|kardo|kar do|karwa)\w*|\btransfer me\b",
    re.IGNORECASE,
)


# "Contact the team" asks for a person, but "the sales team's contact number" asks
# for a detail - the second must not hand the call over.
_CONTACT_REQUEST = re.compile(
    rf"\b(?=.*\b{_PERSON_NOUN}\b)(?=.*\b(?:contact|reach|sampark)\b)"
    r"(?!.*\b(?:numbers?|nambar|e-?mail|mail|address|details?|info|hours|timings?)\b)",
    re.IGNORECASE,
)

# A listener's "hmm / haan / ok" while the agent talks is not an interruption.
_BACKCHANNEL = frozenset({
    "haan", "han", "ha", "haa", "ji", "haanji", "hmm", "hm", "mm", "ok", "okay", "achha",
    "accha", "theek", "thik", "yes", "yeah", "yep", "right", "sure", "oh",
})

# Quiet line: ask once, then end the call rather than keep a dead line (and its cost) open.
_IDLE_PROMPT_AFTER = 20.0
_IDLE_HANGUP_AFTER = 15.0
_MAX_CALL_SECONDS = 20 * 60
_WATCH_TICK = 2.0


def _is_backchannel(text: str) -> bool:
    words = re.findall(r"[\w']+", text.lower())
    return 0 < len(words) <= 2 and all(w in _BACKCHANNEL for w in words)


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
    # "customer" (default) is every existing call, unchanged. "owner" is
    # set only when the caller's number matched Business.owner_phone (see
    # relay.py) - _reply() branches to _owner_reply() instead, which never
    # touches booking/escalation-transfer/customer identity at all.
    # "scripted" is set when the outbound call came from a CallCampaign
    # with a CallScript attached - _reply() branches to _scripted_reply()
    # instead, working through scripted_context.questions. Unlike "owner",
    # a scripted call has a real Customer and IS persisted through
    # ingest() normally - only the reply loop differs.
    # An outbound call's opening line as a stream rather than a finished
    # string. Set instead of opening_line wherever the line can be
    # generated live (relay.py's adhoc branch today): the caller hears the
    # first sentence while Claude is still writing the rest, instead of
    # waiting out the whole generation in silence before synthesis even
    # starts. opening_line stays for the paths that already know their
    # line up front (a CallScript's fixed intro).
    opening_stream: "Callable[[], asyncio.AsyncIterator[str]] | None" = None
    mode: str = "customer"
    # Set only alongside mode="scripted" - see shared/ai/context.py's
    # ScriptedContext.
    scripted_context: "agent_context.ScriptedContext | None" = None
    # The CallScript row this call is running, if any - carried here so
    # relay.py can pass it through to _analyze_call once the call ends,
    # for the post-call structured extraction
    # (shared/ai/call_script_extract.py).
    call_script_id: uuid.UUID | None = None

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
    # True only while an OUTBOUND call's opening line is still being
    # spoken. On an outbound call the person who picks up says "hello?"
    # before anything else - that is simply how answering a phone works -
    # and without this that first word barge-in-cancelled the opening line
    # every single time, so the caller heard silence and hung up.
    # Confirmed on a real call: ClearedAudio fired the same second the
    # call started and no TTS chunk was ever produced. Inbound greetings
    # are deliberately NOT protected: there the caller dialled in and is
    # listening, and talking over the greeting really does mean "skip it".
    _opening_protected: bool = False
    _speculation: "_Speculation | None" = field(default=None, repr=False)
    _speculation_timer: asyncio.Task | None = field(default=None, repr=False)
    # Built once per call, then reused with only its conversation half
    # refreshed - see _call_context.
    _context_cache: "agent_context.AgentContext | None" = field(default=None, repr=False)
    # The caller's own turn, for source_message_ids on a booking made from
    # it - an AI-mediated booking must cite the conversation that
    # authorised it, the same rule book.py enforces for every channel.
    _last_inbound_message_id: uuid.UUID | None = None
    # Closes the Stream websocket. Set by the relay. A live transfer only runs
    # once the stream has ended (keepCallAlive waits for it), so the handover
    # needs this, not just the Plivo request.
    end_stream: "Callable[[], object] | None" = None
    # Audio handed to Plivo for the line being spoken, so the wait before a
    # transfer matches how long the line actually plays.
    _audio_bytes_sent: int = 0
    _audio_started_at: float | None = field(default=None, repr=False)
    # Serialises the "store the caller's turn, start the reply" step. Transcripts are
    # now handled concurrently so a caller can interrupt, and two of them must never
    # use the one database session at the same moment.
    _turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # Said once per call: the same promise twice in a row is what made the agent sound broken.
    _followup_told: bool = False
    _person_ask_told: bool = False
    _no_customer_count: int = 0
    _last_activity: float = field(default_factory=time.monotonic, repr=False)
    _idle_prompted: bool = False
    _started_at: float = field(default_factory=time.monotonic, repr=False)

    async def start(self) -> None:
        """Greet the caller. The first thing anyone hears on the call."""
        if self.mode == "owner":
            # route.greeting is the customer-facing "thank you for calling"
            # line - wrong register for the business's own owner.
            await self._say_stream(
                _single_chunk("Hi, this is KROVA. What would you like to know?"),
                record=True,
            )
            return
        if self.opening_stream is not None or self.opening_line:
            self._opening_protected = True
            try:
                chunks = (
                    self.opening_stream()
                    if self.opening_stream is not None
                    else _single_chunk(self.opening_line or "")
                )
                await self._say_stream(chunks, record=True)
            finally:
                self._opening_protected = False
            return
        greeting = self.route.greeting
        if self.mode == "customer":
            prompt = keypad_menu.spoken_prompt(self.route.keypad_menu, self.route.language)
            if prompt:
                greeting = f"{greeting} {prompt}"
        await self._say_stream(_single_chunk(greeting), record=True)

    async def on_transcript(self, text: str, *, is_final: bool, language: str | None = None) -> None:
        """
        A piece of caller speech arrived.

        Partial transcripts are used only to detect that the caller has
        started talking again - which is what triggers barge-in - never to
        drive a reply. Only a final transcript is answered.
        """
        if not text.strip():
            return
        self._last_activity = time.monotonic()
        self._idle_prompted = False

        # An outbound call's opening line is why the call was placed at
        # all - it has to be heard. Ignoring the caller's speech outright
        # (rather than only skipping the barge-in) matters: letting
        # _handle_utterance run here would start a second reply task over
        # the top of the opening line, which is exactly the interleaved-
        # audio bug relay.py's own comment says tracking the greeting as
        # _reply_task was added to fix. A "hello" carries nothing worth
        # answering anyway; the caller's next turn is handled normally.
        if self._opening_protected:
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
            if _is_backchannel(text):
                return
            await self._barge_in()

        if is_final:
            # Ground truth for diagnosing a "repeats itself / doesn't
            # listen" report: this is every final transcript the pipeline
            # ever acted on, call-tagged. If a future occurrence shows the
            # same text logged here turn after turn, the fault is upstream
            # (Sarvam/Plivo audio never actually changing); if this text
            # visibly changes but _say_stream's own log line keeps
            # producing the same reply, the fault is in agent_module/
            # context instead - this one line is what turns "it happened
            # again" into a provable root cause instead of another guess.
            logger.info(
                "voice turn call=%s turn=%d heard: %r",
                self.provider_call_id, len(self.turns) + 1, text.strip(),
            )
            await self._handle_utterance(text.strip())
        else:
            self._arm_speculation(text.strip())

    def _arm_speculation(self, partial: str) -> None:
        """
        Start writing a reply to a partial transcript that has stopped
        growing, before the STT declares the turn over.

        Sarvam holds a turn open for silence_duration_ms (500ms) after the
        caller actually stops talking, and only then emits speech_end and
        a final transcript - measured 74ms apart, so essentially all of
        that half second is dead time we cannot see from here. Meanwhile
        Claude's own time to first token is around 700ms. Run end to end
        that is roughly 1.2s of silence before a single word is spoken
        back; started against the last partial instead, most of the
        generation happens inside the window the caller is already
        waiting through.

        The safety property that makes this sound rather than reckless:
        a speculative reply is never spoken. It is generated into a queue
        and only played once the final transcript arrives and matches what
        was speculated on - see _handle_utterance. A mismatch (the caller
        carried on talking) throws the work away and answers the real
        thing, costing a wasted Haiku call and nothing else.
        """
        if not _SPECULATION_ENABLED or self.mode != "customer":
            return
        if self.customer_id is None or not partial:
            return
        if self._speculation_timer is not None:
            self._speculation_timer.cancel()
        self._speculation_timer = asyncio.create_task(self._speculate_after_pause(partial))

    async def _speculate_after_pause(self, partial: str) -> None:
        """
        Wait out a short pause in the partials, then speculate on the last
        one. Rearmed by every new partial, so a caller still mid-sentence
        keeps pushing this back and only the text they actually finished
        on is ever generated against - one wasted call per turn at worst,
        not one per word.
        """
        try:
            await asyncio.sleep(_SPECULATION_PAUSE_SECONDS)
        except asyncio.CancelledError:
            return

        self._discard_speculation()
        queue: asyncio.Queue = asyncio.Queue()
        self._speculation = _Speculation(
            text=partial,
            queue=queue,
            task=asyncio.create_task(self._run_speculation(partial, queue)),
        )

    async def _run_speculation(self, partial: str, queue: asyncio.Queue) -> None:
        """
        Generate against a partial into a queue, speaking nothing.

        Pushes the same pieces _reply's own speaking path consumes, then a
        None sentinel so a reader knows the reply ended rather than
        stalled. Any failure is swallowed into the sentinel too: a
        speculative miss must never surface as an error on a live call,
        it just means the real path does the work itself.
        """
        try:
            # Not appended to self.history: a customer reply is built from self.turns,
            # and popping a shared list from a background task removed the real
            # turn that had been appended in the meantime.
            context = await self._call_context()
            context.recent = context.recent + [
                {"direction": "inbound", "channel": "voice", "text": partial}
            ]
            events = agent_module.stream_reply(context)
            async for ev in events:
                await queue.put(ev)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("speculative reply failed - the real turn will generate its own")
        finally:
            await queue.put(None)

    def _discard_speculation(self) -> None:
        if self._speculation is not None:
            self._speculation.task.cancel()
            self._speculation = None

    def _take_speculation(self, final_text: str) -> "_Speculation | None":
        """
        The in-flight speculation, if it was written against what the
        caller actually ended up saying.

        Compared on normalised text rather than exactly: an STT's final
        transcript routinely differs from its last partial only by
        punctuation or capitalisation, and throwing away a good generation
        over a comma would defeat the point. Anything more different than
        that is a real mismatch and is discarded.
        """
        spec, self._speculation = self._speculation, None
        if spec is None:
            return None
        if not _same_utterance(spec.text, final_text):
            logger.info(
                "speculative reply discarded - caller said something else (%r vs %r)",
                spec.text, final_text,
            )
            spec.task.cancel()
            return None
        return spec

    async def _call_context(self) -> "agent_context.AgentContext":
        """
        The agent's context for this turn, built once per call rather than
        once per turn.

        agent_context.build() is a real pile of queries - the business, its
        DNA, its whole knowledge base, this customer, their commitments,
        plus a per-vertical query and, for a scheduling business, a
        per-doctor availability lookup. None of that can change while one
        phone call is in progress, but it was being re-run before every
        single reply, on the one path where the caller is listening to
        silence while it happens.

        What does change each turn is the conversation, and the live call
        already has that in memory (self.history) rather than needing to
        read it back out of the database - the turns are appended there as
        they happen, by _handle_utterance and _say_stream.

        Deliberately not refreshed mid-call: a stale slot list is already
        harmless, because booking.try_book_from_agent re-checks real
        availability at the moment of booking rather than trusting whatever
        the context said (see its own note on that), so the worst case is
        the agent offering a time that booking then declines - exactly what
        already happens when a slot is taken during a call.
        """
        if self._context_cache is None:
            self._context_cache = await agent_context.build(
                self.route.business_id, self.customer_id, self.db
            )
            if self._context_cache.customer_summary and voice_guard.has_record_claim(
                self._context_cache.customer_summary
            ):
                self._context_cache.customer_summary = None
        # The turns as this call has actually heard them, in the shape
        # AgentContext.recent already uses.
        self._context_cache.recent = [
            {
                "direction": "inbound" if t.role == "caller" else "outbound",
                "channel": "voice",
                "text": t.text,
            }
            for t in self.turns
            if t.text.strip()
        ]
        return self._context_cache

    async def _barge_in(self) -> None:
        """
        The caller started talking while the agent was still speaking.

        Order matters: silence Plivo first, so nothing more plays while the
        rest of this unwinds, then stop paying for a generation nobody will
        hear - including any speculative one, which is answering a question
        the caller has now talked over.
        """
        await self.send_clear()

        if self._speculation_timer is not None:
            self._speculation_timer.cancel()
            self._speculation_timer = None
        self._discard_speculation()

        if self._reply_task is not None and self._reply_task is not asyncio.current_task():
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
        if self._speculation_timer is not None:
            self._speculation_timer.cancel()
            self._speculation_timer = None
        # Claimed before anything below can await: whatever was already
        # being written against this caller's last partial is either the
        # answer to what they actually said, or wasted work to discard.
        speculation = self._take_speculation(text)

        async with self._turn_lock:
            # A second utterance can arrive while the first is still being answered:
            # stop that reply before starting this one, never run two at once.
            if self._reply_task is not None and not self._reply_task.done():
                await self._barge_in()

            self.turns.append(Turn(role="caller", text=text))
            self.history.append({"role": "user", "content": text})

            if self.mode != "owner":
                # An owner call is not persisted through ingest() - see
                # context.py's OwnerContext docstring on why: ingest() resolves
                # a Customer from caller_phone, and the owner's own number must
                # never become a Customer row. self.history above already
                # carries this call's conversation in memory, which is what
                # _owner_reply()/stream_owner_reply() actually read from.
                await self._store_turn(direction=Direction.inbound, text=text)

            task = asyncio.create_task(
                self._reply(started_at=time.monotonic(), speculation=speculation)
            )
            self._reply_task = task
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _reply(
        self, *, started_at: float | None = None, speculation: "_Speculation | None" = None,
    ) -> None:
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
        if self.mode == "owner":
            # An owner call has no Customer row at all (see relay.py) -
            # self.customer_id is never set for it, so this has to branch
            # before the check below, not after.
            await self._owner_reply()
            return

        if self.mode == "scripted" and self.scripted_context is not None:
            await self._scripted_reply()
            return

        if self.customer_id is None:
            # Saving the caller's turn failed. Silence sounds like a dead line, so say so.
            logger.warning("reply requested with no resolved customer")
            self._no_customer_count += 1
            if self._no_customer_count >= 3:
                await self._say_goodbye(
                    "I'm sorry, I'm having trouble on my side. Please call back in a few minutes."
                )
                return
            await self._say_stream(
                _single_chunk("Sorry, I missed that. Could you say it once more?"), record=False,
            )
            return

        # A caller who asks for a person gets the transfer straight away, not
        # whenever the model happens to choose escalate. Before this, "team se
        # baat karni hai" was answered by the AI as text and nothing transferred.
        if self.route.staff_phone_number and self._caller_asked_for_person():
            await self.request_transfer()
            return
        if self._caller_asked_for_person():
            await self._person_asked_without_transfer()
            return

        t_context = time.monotonic()
        if speculation is not None:
            # Already generating - possibly already finished - against
            # exactly what the caller said, started while the STT was
            # still holding the turn open. Read it instead of asking the
            # same question again.
            events = _events_from_queue(speculation.queue)
            logger.info(
                "voice latency call=%s using speculative reply", self.provider_call_id,
            )
        else:
            context = await self._call_context()
            events = agent_module.stream_reply(context)
        t_stream_start = time.monotonic()
        if started_at is not None:
            logger.info(
                "voice latency call=%s context=%.2fs",
                self.provider_call_id,
                t_stream_start - t_context,
            )

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
            await agent_module.notify_escalation(
                self.route.business_id, reason=gap or "needs review",
                customer_id=self.customer_id, channel="voice", db=self.db,
                request_summary=self._caller_summary(),
            )
            if gap:
                await agent_module.record_gap(self.route.business_id, gap, self.db)
            if self.call_row_id is not None:
                call_row = await self.db.get(Call, self.call_row_id)
                if call_row is not None:
                    call_row.escalated = True
                    call_row.escalation_reason = gap

            await self._say_stream(_single_chunk(self._followup_line(gap)), record=True)
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
                await agent_module.notify_escalation(
                    self.route.business_id, reason=f"Could not book {first.book_slot}",
                    customer_id=self.customer_id, channel="voice", db=self.db,
                )
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

        if first.book_token:
            business = await self.db.get(Business, self.route.business_id)
            customer = await self.db.get(Customer, self.customer_id)
            queue_entry = None
            if business is not None and customer is not None and self._last_inbound_message_id is not None:
                queue_entry = await queue_booking.try_book_token_from_agent(
                    self.db,
                    book_token=first.book_token,
                    business=business,
                    customer=customer,
                    intake_channel=IntakeChannel.voice,
                    source_message_ids=[self._last_inbound_message_id],
                )
            if queue_entry is None:
                # Same reasoning as the book_slot branch above - the reply
                # already generated for this turn most likely assumes
                # success ("you're #12 in line!"). Drain the rest of the
                # stream unspoken and say something honest instead.
                async for _ in events:
                    pass
                logger.info(
                    "voice token booking failed call=%s book_token=%s, speaking fallback instead",
                    self.provider_call_id, first.book_token,
                )
                if self.call_row_id is not None:
                    call_row = await self.db.get(Call, self.call_row_id)
                    if call_row is not None:
                        call_row.escalated = True
                        call_row.escalation_reason = f"Could not add to {first.book_token} queue"
                await agent_module.notify_escalation(
                    self.route.business_id, reason=f"Could not add to {first.book_token} queue",
                    customer_id=self.customer_id, channel="voice", db=self.db,
                )
                await self._say_stream(
                    _single_chunk(
                        "I wasn't able to add you to that queue right now - let "
                        "me have someone confirm the details with you."
                    ),
                    record=True,
                )
                return
            logger.info(
                "voice token booking succeeded call=%s entry=%s",
                self.provider_call_id, queue_entry.id,
            )

        if first.requested_service and self.call_row_id is not None:
            # Not a booking attempt - just a record of what the caller
            # asked for, so a business's own automation rule can react to
            # it once the call ends (call.completed's own context dict -
            # see relay.py's _analyze_call). Overwritten by a later turn
            # if the caller mentions something else; the reply for this
            # turn still gets spoken normally below, same as any other
            # reply.
            call_row = await self.db.get(Call, self.call_row_id)
            if call_row is not None:
                call_row.requested_service = first.requested_service

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

    async def _owner_reply(self) -> None:
        """
        The owner voice interface's reply loop - agent_module.
        stream_owner_reply's counterpart to _reply() above, much simpler:
        no booking, no customer-escalation transfer, no gap-recording,
        since none of that applies to an owner asking about their own
        ledger. self.history (not a DB-refreshed AgentContext.recent, the
        customer path's source) is what carries the conversation so far -
        see stream_owner_reply's own docstring for why.
        """
        context = await agent_context.build_owner(self.route.business_id, self.db)
        events = agent_module.stream_owner_reply(context, self.history)
        try:
            first = await events.__anext__()
        except StopAsyncIteration:
            logger.warning("stream_owner_reply produced no events at all")
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
            spoken = (
                f"I don't have {gap} on hand right now."
                if gap
                else "I don't have enough to answer that from the ledger right now."
            )
            await self._say_stream(_single_chunk(spoken), record=True)
            return

        reply_cost = {"paise": 0}

        async def reply_text_chunks():
            async for ev in events:
                if isinstance(ev, agent_module.ReplyChunk):
                    yield ev.text
                elif isinstance(ev, agent_module.ReplyDone):
                    reply_cost["paise"] = ev.cost_paise

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

    async def _scripted_reply(self) -> None:
        """
        A CallScript's reply loop - agent_module.stream_scripted_reply's
        counterpart to _reply() above. Same shape as _owner_reply (no
        booking, no transfer), but unlike an owner call this one has a
        real customer_id and IS persisted through ingest() normally -
        _handle_utterance/_say_stream's own mode check already only
        special-cases "owner", so nothing extra is needed here for that.
        """
        assert self.scripted_context is not None
        events = agent_module.stream_scripted_reply(self.scripted_context, self.history)
        try:
            first = await events.__anext__()
        except StopAsyncIteration:
            logger.warning("stream_scripted_reply produced no events at all")
            return

        action = first.action if isinstance(first, agent_module.ReplyStart) else "escalate"

        if action == "no_action":
            async for _ in events:
                pass
            return

        if action == "escalate":
            cost_paise = 0
            async for ev in events:
                if isinstance(ev, agent_module.ReplyDone):
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
            await self._say_stream(
                _single_chunk("Sorry, I think there's been some confusion - thank you for your time."),
                record=True,
            )
            return

        reply_cost = {"paise": 0}

        async def reply_text_chunks():
            async for ev in events:
                if isinstance(ev, agent_module.ReplyChunk):
                    yield ev.text
                elif isinstance(ev, agent_module.ReplyDone):
                    reply_cost["paise"] = ev.cost_paise

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

    def _followup_line(self, gap: str | None) -> str:
        """
        What to say when the agent cannot answer. The "someone will follow up" promise
        is made in full once; later gaps get a short acknowledgement, because the same
        sentence turn after turn is what made callers think the agent was stuck.
        """
        # A floor reason such as "needs a person: refund" is for staff, not for speech.
        topic = gap.strip() if gap else None
        if topic and (topic.lower().startswith("needs a person") or len(topic.split()) > 6):
            topic = None
        if self._followup_told:
            if topic:
                return f"I don't have {topic} either - I've noted it for the team. Is there anything else I can help with?"
            return "I've noted that for the team. Is there anything else I can help with?"
        self._followup_told = True
        if topic:
            return f"I don't have {topic} on hand right now, but I'll make sure someone follows up with you on that."
        return "I don't have enough to answer that properly, but I'll make sure someone follows up with you."

    async def _person_asked_without_transfer(self) -> None:
        """The caller wants a person and this business has no number to hand the call to."""
        reason = "Caller asked to reach a person"
        if self.call_row_id is not None:
            call_row = await self.db.get(Call, self.call_row_id)
            if call_row is not None:
                call_row.escalated = True
                call_row.escalation_reason = reason
        if not self._person_ask_told:
            await agent_module.notify_escalation(
                self.route.business_id, reason=reason, customer_id=self.customer_id,
                channel="voice", db=self.db, request_summary=self._caller_summary(),
            )
        line = (
            "That's noted - the team will call you back. Is there anything else I can help with?"
            if self._person_ask_told
            else "I can't transfer you right now, but I've noted that you want to speak to the team, "
            "and they'll call you back soon."
        )
        self._person_ask_told = True
        await self._say_stream(_single_chunk(line), record=True)

    async def on_dtmf(self, digit: str) -> None:
        """Keypad 0 means "a person". Stop whatever is being said first, then transfer."""
        menu = self.route.keypad_menu if self.mode == "customer" else None
        option = keypad_menu.option_for(menu, digit)
        if option is None and digit != "0" and not keypad_menu.is_live(menu):
            return
        self._last_activity = time.monotonic()
        await self._barge_in()
        if option is None and digit != "0":
            # A key the menu does not use: say so rather than leave the caller in silence.
            self._reply_task = asyncio.create_task(
                self._say_stream(_single_chunk("Sorry, that option isn't available."), record=False)
            )
            return
        if option is None:
            # 0 with no key of its own keeps its original meaning: reach a person.
            self._reply_task = asyncio.create_task(self.request_transfer())
            return
        logger.info("voice menu key call=%s digit=%s action=%s", self.provider_call_id, digit, option["action"])
        await self._fire_keypad_rules(digit, option)
        if option["action"] == "transfer":
            self._reply_task = asyncio.create_task(self.request_transfer(
                number=option["number"],
                reason=f"Caller pressed {digit}: {option['label']}",
                line=f"Connecting you to {option['label']}. One moment.",
            ))
        elif option["action"] == "say":
            self._reply_task = asyncio.create_task(
                self._say_stream(_single_chunk(option["message"]), record=True)
            )
        else:
            self._reply_task = asyncio.create_task(
                self._say_stream(_single_chunk("Sure, go ahead. I'm listening."), record=False)
            )

    async def _fire_keypad_rules(self, digit: str, option: dict) -> None:
        """Tell the business's automations which key was pressed. Never allowed to disturb the call."""
        try:
            if self.customer_id is None and self.caller_phone not in ("", "unknown"):
                # Pressing a key before saying anything: the caller is still a person
                # to the business, so find or create them from their number.
                resolution = await resolver.resolve(
                    self.route.business_id, IdentityKind.phone, self.caller_phone, self.db,
                )
                self.customer_id = resolution.customer.id
                await self.db.commit()
            if self.customer_id is None:
                return
            await post_call_actions.apply_rules(
                self.db, business_id=self.route.business_id, trigger_type="keypad.pressed",
                customer_id=self.customer_id, call_id=self.call_row_id, channel="voice",
                context={"digit": digit, "key_name": option["label"], "action": option["action"]},
            )
            await self.db.commit()
        except Exception:
            logger.exception("keypad automations failed call=%s", self.provider_call_id)
            try:
                await self.db.rollback()
            except Exception:
                pass

    async def _say_goodbye(self, line: str) -> bool:
        """Say a closing line, let it play out, then end the call. False if the caller spoke over it."""
        self._audio_bytes_sent = 0
        self._audio_started_at = None
        task = asyncio.create_task(self._say_stream(_single_chunk(line), record=True))
        self._reply_task = task
        await asyncio.wait({task})
        if task.cancelled():
            return False
        await self._wait_for_line_playback()
        if self.end_stream is not None:
            await self.end_stream()
        return True

    async def watch_idle(self) -> None:
        """
        End calls that have gone quiet. Without this a line left open (a phone put down,
        a voicemail, an IVR) stays connected for up to the stream timeout while the
        business pays for every minute.
        """
        while True:
            await asyncio.sleep(_WATCH_TICK)
            if self._opening_protected:
                continue
            if self._reply_task is not None and not self._reply_task.done():
                continue
            now = time.monotonic()
            if now - self._started_at > _MAX_CALL_SECONDS:
                logger.info("voice call hit the time limit call=%s", self.provider_call_id)
                await self._say_goodbye(
                    "We have reached the time limit for this call. Please call again if you need anything else. Goodbye."
                )
                return
            quiet = now - self._last_activity
            if not self._idle_prompted and quiet > _IDLE_PROMPT_AFTER:
                self._idle_prompted = True
                self._reply_task = asyncio.create_task(
                    self._say_stream(_single_chunk("Are you still there?"), record=True)
                )
            elif self._idle_prompted and quiet > _IDLE_HANGUP_AFTER:
                logger.info("voice call silent, ending call=%s", self.provider_call_id)
                if await self._say_goodbye("I can't hear you, so I'll end the call now. Goodbye."):
                    return
                self._idle_prompted = False

    async def request_transfer(
        self, *, number: str | None = None, reason: str | None = None, line: str | None = None,
    ) -> None:
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
                call_row.escalation_reason = reason or "Caller asked to reach a person"

        await agent_module.notify_escalation(
            self.route.business_id, reason=reason or "Caller asked to reach a person",
            customer_id=self.customer_id, channel="voice", db=self.db,
        )

        target = number or self.route.staff_phone_number
        if target:
            # Speak first, then wait for the line to finish playing: the
            # transfer cuts the audio stream, so anything not yet heard is lost.
            self._audio_bytes_sent = 0
            self._audio_started_at = None
            await self._say_stream(
                _single_chunk(
                    line or "Let me connect you to someone who can help with that right now."
                ),
                record=True,
            )
            await self._wait_for_line_playback()
            if await self._try_transfer(target):
                return

        await self._say_stream(
            _single_chunk(
                "There's no one else available to transfer you to right now, "
                "but I'll make sure someone follows up with you."
            ),
            record=True,
        )

    async def _wait_for_line_playback(self) -> None:
        """
        Hold the call open until the audio already sent has finished playing.

        _say_stream returns once the audio is handed over, which can be faster
        than real time. The duration comes from the bytes actually sent, not a
        guessed constant.
        """
        if self._audio_started_at is None:
            return
        duration = self._audio_bytes_sent / _MULAW_BYTES_PER_SECOND
        remaining = duration - (time.monotonic() - self._audio_started_at)
        await asyncio.sleep(max(0.0, remaining) + _PLAYBACK_MARGIN_SECONDS)

    def _caller_summary(self) -> str | None:
        """What the caller said most recently, in their own words, for the staff member who follows up."""
        said = [t.text for t in self.turns if t.role == "caller" and t.text][-3:]
        return " | ".join(said)[:500] or None

    def _caller_asked_for_person(self) -> bool:
        """Transfer only when the caller asks for a person - not for every question the agent cannot answer."""
        last = next((t.text for t in reversed(self.turns) if t.role == "caller"), "")
        return bool(_PERSON_REQUEST.search(last or "") or _CONTACT_REQUEST.search(last or ""))

    async def _try_transfer(self, target: str | None = None) -> bool:
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
            f"{urlencode({'number': f'+{target or self.route.staff_phone_number}'})}"
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
            self.provider_call_id, target or self.route.staff_phone_number,
        )
        if self.end_stream is not None:
            await self.end_stream()
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
                if self._audio_started_at is None:
                    self._audio_started_at = time.monotonic()
                self._audio_bytes_sent += len(chunk)
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
            if turn.text.strip():
                # Paired with on_transcript's "heard" log - the other half
                # of the ground truth needed to prove whether a "repeats
                # itself" report is the agent regenerating the same reply
                # for different input, or audio/relay replaying old output.
                logger.info(
                    "voice turn call=%s said (complete=%s): %r",
                    self.provider_call_id, turn.complete, turn.text.strip(),
                )
            self._last_activity = time.monotonic()
            if record and turn.text.strip() and self.mode != "owner":
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
