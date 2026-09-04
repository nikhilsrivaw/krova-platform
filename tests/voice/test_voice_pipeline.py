"""
The voice pipeline against a real database.

Two things this proves that the framing tests cannot:

  1. A call and a WhatsApp message from the same phone number resolve to the
     SAME customer record - the entire cross-channel thesis, tested on the
     one channel that was built last.

  2. Barge-in actually cancels an in-flight reply and clears what was queued,
     using a fake TTS that is deliberately slow so there is something to
     interrupt.

What this cannot prove: that Plivo's real audio frames, Sarvam's real STT
output, and this code's assumptions about both actually agree. That needs a
live call.
"""

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone


from sqlalchemy import select  # noqa: E402

from shared.ai import agent as agent_module  # noqa: E402
from shared.channels import ingest  # noqa: E402
from shared.channels.voice.pipeline import CallPipeline  # noqa: E402
from shared.channels.voice.tenant import VoiceRoute  # noqa: E402
from shared.db.models import (  # noqa: E402
    Business,
    BusinessDNA,
    Channel,
    ChannelConnection,
    ConnectionStatus,
    Direction,
    IdentityKind,
    Message,
)
from shared.db.session import AsyncSessionLocal  # noqa: E402
from shared.verticals import seed_dna  # noqa: E402

ok = True


def check(label, cond, extra=None):
    global ok
    if not cond:
        ok = False
    suffix = f"  -> {extra!r}" if (not cond and extra is not None) else ""
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{suffix}")


class FakeCaller:
    """Records what the pipeline sent, in order."""

    def __init__(self):
        self.audio_sent: list[bytes] = []
        self.clears = 0

    async def send_audio(self, chunk: bytes) -> None:
        self.audio_sent.append(chunk)

    async def send_clear(self) -> None:
        self.clears += 1


def slow_speak_factory(chunk_delay: float, chunks_per_char: int = 1):
    """
    A fake TTS that yields slowly, so a test can interrupt it mid-stream.

    Real TTS arrives in small chunks over tens to hundreds of milliseconds;
    this exaggerates the gap so a test task has time to call on_transcript
    again before speak() finishes on its own. Consumes a STREAM of text
    pieces, matching the real speak() signature used since replies are
    spoken sentence by sentence as Claude generates them, not as one
    complete string.
    """

    async def speak(text_chunks):
        async for text in text_chunks:
            for word in text.split():
                await asyncio.sleep(chunk_delay)
                yield word.encode()

    return speak


async def main():
    async with AsyncSessionLocal() as db:
        biz = Business(name="Sharma Dental", vertical="clinic", autonomy="draft")
        db.add(biz)
        await db.flush()
        dna = seed_dna("clinic")
        dna["pricing_notes"] = "Consultation Rs 500. Cleaning Rs 1,200."
        db.add(BusinessDNA(business_id=biz.id, **dna))
        await db.flush()

        connection = ChannelConnection(
            business_id=biz.id,
            channel=Channel.voice,
            external_account_id="+91 88888 77777",
            status=ConnectionStatus.active,
            extra={"greeting": "Hello, thanks for calling Sharma Dental."},
        )
        db.add(connection)
        await db.flush()
        await db.commit()

        # ── cross-channel identity: prove voice and WhatsApp are one person ──
        tag = uuid.uuid4().hex[:6]
        caller_phone = "+91 97000 88001"

        wa = await ingest.ingest(
            business_id=biz.id, channel=Channel.whatsapp, direction=Direction.inbound,
            identity_kind=IdentityKind.phone, identity_value=caller_phone,
            external_id=f"{tag}-wa", text="Hi, I messaged about a cleaning",
            occurred_at=datetime.now(timezone.utc) - timedelta(days=2),
            display_name="Priya", enqueue_analysis=False, db=db,
        )
        await db.commit()
        whatsapp_customer_id = wa.customer.id

        route = VoiceRoute(
            business_id=biz.id,
            business_name=biz.name,
            connection_id=connection.id,
            greeting="Hello, thanks for calling Sharma Dental.",
            language="en-IN",
            language_mode="adaptive",
            speaker="shubh",
            staff_phone_number=None,
            copilot_mode=False,
        )

        caller = FakeCaller()
        pipeline = CallPipeline(
            route=route,
            caller_phone=caller_phone,
            provider_call_id=f"call-{tag}",
            send_audio=caller.send_audio,
            send_clear=caller.send_clear,
            speak=slow_speak_factory(0.01),
            db=db,
        )

        await pipeline._handle_utterance("Hi, following up about the cleaning I asked about")
        await db.commit()

        check(
            "VOICE RESOLVES TO THE SAME CUSTOMER AS WHATSAPP",
            pipeline.customer_id == whatsapp_customer_id,
            (pipeline.customer_id, whatsapp_customer_id),
        )

        stored = (
            await db.execute(
                select(Message).where(
                    Message.customer_id == whatsapp_customer_id,
                    Message.channel == Channel.voice,
                )
            )
        ).scalars().all()
        check("call turn stored on the shared timeline", len(stored) >= 1, len(stored))
        check("stored as voice channel", stored and stored[0].channel == Channel.voice)

        thread = (
            await db.execute(
                select(Message)
                .where(Message.customer_id == whatsapp_customer_id)
                .order_by(Message.occurred_at)
            )
        ).scalars().all()
        channels_seen = {str(m.channel) for m in thread}
        check(
            "ONE TIMELINE, TWO CHANNELS",
            len(channels_seen) >= 2,
            channels_seen,
        )
        print(f"\n  {biz.name} customer timeline:")
        for m in thread:
            who = "caller" if m.direction == Direction.inbound else "business"
            print(f"    [{m.channel}] {who}: {(m.content or '')[:60]}")

        # ── barge-in: interrupt an in-flight reply ──────────────────────────
        caller2 = FakeCaller()
        biz2 = Business(name="Second Clinic", vertical="clinic", autonomy="draft")
        db.add(biz2)
        await db.flush()
        db.add(BusinessDNA(business_id=biz2.id, **seed_dna("clinic")))
        await db.flush()

        conn2 = ChannelConnection(
            business_id=biz2.id, channel=Channel.voice,
            external_account_id="+91 88888 77778", status=ConnectionStatus.active,
        )
        db.add(conn2)
        await db.flush()
        await db.commit()

        route2 = VoiceRoute(
            business_id=biz2.id, business_name=biz2.name, connection_id=conn2.id,
            greeting="Hello.", language="en-IN", language_mode="adaptive", speaker="shubh",
            staff_phone_number=None, copilot_mode=False,
        )

        # A speak() slow enough that the test can barge in before it finishes,
        # and long enough that _spoken_chars will be a real fraction, not
        # everything or nothing.
        pipeline2 = CallPipeline(
            route=route2,
            caller_phone="+91 97000 88002",
            provider_call_id=f"call2-{tag}",
            send_audio=caller2.send_audio,
            send_clear=caller2.send_clear,
            speak=slow_speak_factory(0.05),
            db=db,
        )

        async def long_reply_chunks():
            yield "This is a long reply with many words that will take a while to finish speaking"

        say_task = asyncio.create_task(
            pipeline2._say_stream(long_reply_chunks(), record=True)
        )
        await asyncio.sleep(0.12)  # let a few words go out
        check("some audio sent before interrupt", len(caller2.audio_sent) > 0,
              len(caller2.audio_sent))
        sent_before_barge_in = len(caller2.audio_sent)

        pipeline2._reply_task = say_task
        await pipeline2._barge_in()

        check("clearAudio sent to caller", caller2.clears == 1, caller2.clears)
        check("in-flight speech task actually stopped", say_task.done())
        await asyncio.sleep(0.1)
        check(
            "no more audio sent after clear",
            len(caller2.audio_sent) == sent_before_barge_in,
            (sent_before_barge_in, len(caller2.audio_sent)),
        )

        # the interrupted turn should be recorded as incomplete, not the
        # full sentence it never finished
        if pipeline2.turns and pipeline2.turns[-1].role == "agent":
            interrupted = pipeline2.turns[-1]
            check("interrupted turn marked incomplete", not interrupted.complete)

        await db.rollback()


asyncio.run(main())
print("\nall passed" if ok else "\nFAILURES PRESENT")
