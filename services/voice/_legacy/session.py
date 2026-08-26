"""
One live phone call.

Holds conversation history, the in-flight generation task, and the
transcript/usage writes.

Barge-in is the subtle part. When a caller talks over the agent we must:
  1. cancel the Claude request immediately (stop paying for tokens
     nobody will hear),
  2. record only the part the caller actually heard, so Claude's idea of
     what it said matches reality.
"""

import asyncio
import logging
import time

from app import metrics
from app.agent import stream_reply
from app.services import call_service
from app.tenants import AgentConfig

logger = logging.getLogger(__name__)


class CallSession:
    def __init__(
        self,
        websocket,
        config: AgentConfig,
        provider_call_id: str,
        call_id: str | None = None,
    ) -> None:
        self.ws = websocket
        self.config = config
        self.provider_call_id = provider_call_id
        self.call_id = call_id  # our DB id; None if logging is unavailable
        self.history: list[dict] = []
        self._task: asyncio.Task | None = None
        self._seq = 0
        self._pending_usage: list[tuple[str, int, int]] = []
        self._partial: list[str] = []
        self._recorded_reply = False

    # --- transcript ------------------------------------------------------

    async def _record(self, role: str, text: str) -> None:
        if not self.call_id or not text.strip():
            return
        self._seq += 1
        try:
            await call_service.log_turn(
                call_id=self.call_id,
                tenant_id=self.config.tenant_id,
                seq=self._seq,
                role=role,
                text=text,
            )
        except Exception:
            # Never let a logging failure drop a live call.
            logger.warning("failed to log turn", exc_info=True)

    # --- history ---------------------------------------------------------

    def _add_user(self, text: str) -> None:
        """
        Append caller speech.

        If the last entry is already a user turn (which happens when the
        caller interrupts before the agent said anything), merge rather
        than append - the API expects alternating roles.
        """
        if self.history and self.history[-1]["role"] == "user":
            self.history[-1]["content"] += " " + text
        else:
            self.history.append({"role": "user", "content": text})

    def _add_assistant(self, text: str) -> None:
        if not text.strip():
            return
        if self.history and self.history[-1]["role"] == "assistant":
            self.history[-1]["content"] += " " + text
        else:
            self.history.append({"role": "assistant", "content": text})

    # --- speaking --------------------------------------------------------

    async def _send(self, token: str, last: bool = False) -> None:
        await self.ws.send_json({"type": "text", "token": token, "last": last})

    async def say(self, text: str) -> None:
        """Speak a fixed line, e.g. the greeting. Not model-generated."""
        await self._send(text, last=True)
        self._add_assistant(text)
        metrics.turns_total.labels(self.config.tenant_id, "agent").inc()
        await self._record("agent", text)

    def _capture_usage(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self._pending_usage.append((model, input_tokens, output_tokens))
        tenant = self.config.tenant_id
        metrics.llm_tokens_total.labels(tenant, model, "input").inc(input_tokens)
        metrics.llm_tokens_total.labels(tenant, model, "output").inc(output_tokens)
        metrics.llm_cost_usd_total.labels(tenant, model).inc(
            call_service.llm_cost_usd(model, input_tokens, output_tokens)
        )

    async def _flush_usage(self) -> None:
        if not self.call_id:
            self._pending_usage.clear()
            return
        for model, tin, tout in self._pending_usage:
            try:
                await call_service.add_usage(
                    call_id=self.call_id,
                    model=model,
                    input_tokens=tin,
                    output_tokens=tout,
                )
            except Exception:
                logger.warning("failed to record usage", exc_info=True)
        self._pending_usage.clear()

    async def _persist(self, spoken: list[str]) -> None:
        """
        Write what was said and what it cost.

        Called before the end-of-turn frame on the happy path, and again
        from close() if the turn was interrupted - because a cancelled
        task can be stopped part-way through its own finally block, and
        losing billing data to a race is not acceptable.
        """
        text = " ".join(spoken).strip()
        if text and not self._recorded_reply:
            self._recorded_reply = True
            self._add_assistant(text)
            await self._record("agent", text)
        await self._flush_usage()

    async def _generate(self) -> None:
        spoken: list[str] = []
        self._recorded_reply = False
        tenant = self.config.tenant_id
        model = self.config.model
        started = time.perf_counter()
        first_audio_at: float | None = None
        try:
            async for chunk in stream_reply(
                self.config.system_prompt,
                self.history,
                model=model,
                on_usage=self._capture_usage,
            ):
                spoken.append(chunk)
                await self._send(chunk, last=False)

                if first_audio_at is None:
                    # The silence the caller actually heard. This is the
                    # number that decides whether the agent feels human.
                    first_audio_at = time.perf_counter() - started
                    metrics.time_to_first_audio.labels(tenant, model).observe(
                        first_audio_at
                    )

            # Persist BEFORE the terminal frame. Once the client sees
            # last=true it may close the socket, which cancels this task.
            metrics.reply_duration.labels(tenant, model).observe(
                time.perf_counter() - started
            )
            await self._persist(spoken)
            await self._send("", last=True)
            logger.info(
                "reply complete call=%s chunks=%d", self.provider_call_id, len(spoken)
            )
        except asyncio.CancelledError:
            logger.info(
                "reply interrupted call=%s spoken=%d",
                self.provider_call_id,
                len(spoken),
            )
            # Keep the partial reply for close() to persist.
            self._partial = spoken
            raise
        except Exception:
            logger.exception("generation failed call=%s", self.provider_call_id)
            metrics.errors_total.labels("generation").inc()
            self._partial = spoken
            try:
                await self._send(
                    "Sorry, I ran into a problem. Could you say that again?",
                    last=True,
                )
            except Exception:
                pass

    # --- events from Telnyx ----------------------------------------------

    async def on_prompt(self, text: str) -> None:
        """Caller finished a sentence. Cancel anything in flight, then reply."""
        await self.cancel()
        self._add_user(text)
        metrics.turns_total.labels(self.config.tenant_id, "caller").inc()
        await self._record("caller", text)
        logger.info("caller call=%s said=%r", self.provider_call_id, text)
        self._task = asyncio.create_task(self._generate())

    async def cancel(self) -> None:
        """Stop the current reply, if any, and wait for it to unwind."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def on_interrupt(self) -> None:
        logger.info("barge-in call=%s", self.provider_call_id)
        metrics.barge_ins_total.labels(self.config.tenant_id).inc()
        await self.cancel()

    async def close(self) -> None:
        await self.cancel()
        # Safety net: an interrupted turn may not have persisted itself.
        try:
            await self._persist(self._partial)
        except Exception:
            logger.warning("failed to persist final turn", exc_info=True)
        if self.call_id:
            try:
                await call_service.finish_call(call_id=self.call_id)
            except Exception:
                logger.warning("failed to close call record", exc_info=True)
