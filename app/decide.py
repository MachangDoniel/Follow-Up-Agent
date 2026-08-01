"""The one place that decides whether a follow-up goes out, and what it says.

Both watchers route through here so Telegram and WhatsApp behave identically.
"""

from __future__ import annotations

import logging

from . import compose, gating
from .config import Settings
from .llm import LLMError, LMStudio
from .models import Decision, MessageBurst, MissedCall
from .store import Store

log = logging.getLogger(__name__)


class Decider:
    def __init__(self, settings: Settings, store: Store, llm: LMStudio) -> None:
        self._settings = settings
        self._store = store
        self._llm = llm

    async def decide(self, call: MissedCall) -> Decision:
        settings = self._settings

        since = self._store.seconds_since_last_send(call.platform, call.contact_id)
        skip = gating.blocked_reason(settings, call, since)
        if skip:
            log.info("skipping %s: %s", call.label, skip)
            self._record(call, sent=False, skip_reason=skip, text="")
            return Decision(send=False, skip_reason=skip)

        text, used_fallback = await self._write(call)

        if settings.dry_run:
            log.info("DRY RUN %s would send: %s", call.label, text)
            self._record(call, sent=False, skip_reason="dry_run", text=text)
            return Decision(
                send=False, text=text, skip_reason="dry_run", used_fallback=used_fallback
            )

        # Recorded as sent before the watcher actually sends. A send that fails
        # still burns the cooldown, which is the safe direction: better one
        # missing follow-up than a duplicate storm at someone's phone.
        self._record(call, sent=True, skip_reason="", text=text)
        log.info("sending to %s: %s", call.label, text)
        return Decision(send=True, text=text, used_fallback=used_fallback)

    async def decide_burst(self, burst: MessageBurst) -> Decision:
        """Someone sent several messages with no reply from you.

        Written by the model in an assistant voice, since their own messages
        give it real context. Falls back to BURST_TEXT if the model fails, so
        this path always produces something.
        """
        settings = self._settings

        if not settings.burst_enabled:
            return Decision(send=False, skip_reason="burst replies are disabled")
        if burst.count < settings.burst_threshold:
            return Decision(send=False, skip_reason="below the burst threshold")

        since = self._store.seconds_since_last_send(
            burst.platform, burst.contact_id, kind="burst"
        )
        skip = gating.blocked_reason(
            settings, burst, since, cooldown_seconds=settings.burst_cooldown_hours * 3600
        )
        if skip:
            log.info("burst: skipping %s: %s", burst.label, skip)
            self._record_burst(burst, sent=False, skip_reason=skip, text="")
            return Decision(send=False, skip_reason=skip)

        # A burst always carries context - their own messages - so the model has
        # something real to work with. No sign-off: the reply says it is not you.
        text, _ = await self._generate(
            *compose.build_burst_prompt(settings, burst),
            settings.burst_text,
            burst.label,
            sign=False,
        )

        if settings.dry_run:
            log.info("burst DRY RUN %s would send: %s", burst.label, text)
            self._record_burst(burst, sent=False, skip_reason="dry_run", text=text)
            return Decision(send=False, text=text, skip_reason="dry_run")

        self._record_burst(burst, sent=True, skip_reason="", text=text)
        log.info("burst: sending to %s after %d unanswered", burst.label, burst.count)
        return Decision(send=True, text=text)

    def _record_burst(
        self, burst: MessageBurst, *, sent: bool, skip_reason: str, text: str
    ) -> None:
        self._store.record(
            platform=burst.platform,
            contact_id=burst.contact_id,
            contact_name=burst.contact_name,
            reason=f"{burst.count} unanswered messages",
            kind="burst",
            sent=sent,
            skip_reason=skip_reason,
            text=text,
        )

    async def _write(self, call: MissedCall) -> tuple[str, bool]:
        """Returns (text, used_fallback)."""
        settings = self._settings
        fallback = compose.with_signoff(settings.fallback_text, settings)

        # A bare missed call with no conversation gives the model nothing to
        # personalise from, so it would spend 15-30s producing something no
        # better than the fixed line. Only involve it when there is context.
        if not call.history:
            log.info("%s: no chat history, using the fixed text", call.label)
            return fallback, True

        system, user = compose.build_prompt(settings, call)
        return await self._generate(system, user, fallback, call.label, sign=True)

    async def _generate(
        self, system: str, user: str, fallback: str, label: str, *, sign: bool
    ) -> tuple[str, bool]:
        """Run the model, falling back to fixed text if it fails or misbehaves."""
        try:
            completion = await self._llm.complete(system, user)
        except LLMError as exc:
            log.warning("%s: falling back to the fixed text (%s)", label, exc)
            return fallback, True

        settings = self._settings
        budget = compose.body_budget(settings) if sign else settings.max_chars
        text = compose.clean(completion.content, budget)
        if compose.is_unusable(text):
            log.warning(
                "%s: model output rejected by guardrail (%r), using fallback",
                label,
                text[:120],
            )
            return fallback, True
        return (compose.with_signoff(text, settings) if sign else text), False

    def _record(self, call: MissedCall, *, sent: bool, skip_reason: str, text: str) -> None:
        self._store.record(
            platform=call.platform,
            contact_id=call.contact_id,
            contact_name=call.contact_name,
            reason=call.reason,
            sent=sent,
            skip_reason=skip_reason,
            text=text,
        )
