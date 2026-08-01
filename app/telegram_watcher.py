"""Telegram side: watches your own account for calls that were never answered.

Logs in as YOU over MTProto. Bots cannot do this - the Bot API never sees calls.
"""

from __future__ import annotations

import asyncio
import logging
import time

from telethon import TelegramClient, events, types

from .config import Settings
from .decide import Decider
from .models import Message, MessageBurst, MissedCall

log = logging.getLogger(__name__)

DISCARD_REASONS: dict[type, str] = {
    types.PhoneCallDiscardReasonMissed: "missed",
    types.PhoneCallDiscardReasonBusy: "busy",
    # Telegram reports Hangup both when you reject and when the caller gives up.
    types.PhoneCallDiscardReasonHangup: "rejected",
    types.PhoneCallDiscardReasonDisconnect: "disconnected",
}

RING_RECORD_TTL = 3600.0
PRUNE_INTERVAL = 300.0


def display_name(entity: object) -> str:
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    name = " ".join(part for part in parts if part).strip()
    return name or getattr(entity, "username", None) or str(getattr(entity, "id", "?"))


class TelegramWatcher:
    def __init__(self, settings: Settings, decider: Decider) -> None:
        self._settings = settings
        self._decider = decider
        settings.telegram_session.parent.mkdir(parents=True, exist_ok=True)
        self._client = TelegramClient(
            str(settings.telegram_session),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        # call id -> {caller, accepted, started, video}
        self._ringing: dict[int, dict] = {}
        # user id -> messages they have sent since your last reply
        self._unanswered: dict[int, int] = {}

    async def run(self) -> None:
        self._client.add_event_handler(self._on_raw, events.Raw)
        if self._settings.burst_enabled:
            self._client.add_event_handler(self._on_message, events.NewMessage())
        await self._client.start()
        me = await self._client.get_me()
        log.info("watching Telegram calls for %s", display_name(me))
        pruner = asyncio.create_task(self._prune_forever())
        try:
            await self._client.run_until_disconnected()
        finally:
            pruner.cancel()
            await self._client.disconnect()

    async def _on_raw(self, update: object) -> None:
        if not isinstance(update, types.UpdatePhoneCall):
            return
        call = update.phone_call

        # Incoming call started ringing. Calls we place produce PhoneCallWaiting
        # instead, so outgoing calls are filtered out for free.
        if isinstance(call, types.PhoneCallRequested):
            self._ringing[call.id] = {
                "caller": call.admin_id,
                "accepted": False,
                "started": time.monotonic(),
                "started_at": time.time(),
                "video": bool(getattr(call, "video", False)),
            }
            return

        # Picked up somewhere - stop tracking it as unanswered.
        if isinstance(call, types.PhoneCallAccepted | types.PhoneCall):
            record = self._ringing.get(call.id)
            if record is not None:
                record["accepted"] = True
            return

        if isinstance(call, types.PhoneCallDiscarded):
            record = self._ringing.pop(call.id, None)
            if record is None or record["accepted"]:
                return
            if (call.duration or 0) > 0:
                return  # it connected after all
            reason = DISCARD_REASONS.get(type(call.reason), "missed")
            await self._follow_up(
                record["caller"], reason, record["video"], record["started_at"]
            )

    async def _follow_up(
        self, caller_id: int, reason: str, video: bool, started_at: float
    ) -> None:
        try:
            entity = await self._client.get_entity(caller_id)
        except (ValueError, TypeError) as exc:
            log.warning("could not resolve caller %s: %s", caller_id, exc)
            return
        if getattr(entity, "bot", False):
            return

        name = display_name(entity)

        if await self._replied_since(caller_id, started_at):
            log.info("you already replied to %s yourself, standing down", name)
            return

        log.info("unanswered %scall from %s (%s)", "video " if video else "", name, reason)

        call = MissedCall(
            platform="telegram",
            contact_id=str(caller_id),
            contact_name=name,
            reason=reason,
            video=video,
            history=await self._history(caller_id),
        )
        decision = await self._decider.decide(call)
        if not decision.send:
            return

        # Give yourself a window to answer by hand. Generation already ate part
        # of it, so wait out the remainder rather than adding on top.
        remaining = self._settings.reply_grace_seconds - (time.time() - started_at)
        if remaining > 0:
            await asyncio.sleep(remaining)

        if await self._replied_since(caller_id, started_at):
            log.info("you replied to %s while it was thinking, not sending", name)
            return

        try:
            await self._client.send_message(caller_id, decision.text)
        except Exception:
            log.exception("sending follow-up to %s failed", name)

    async def _on_message(self, event) -> None:
        """Count consecutive incoming messages, and reply once at the threshold."""
        if not event.is_private:
            return
        chat_id = event.chat_id

        if event.out:
            self._unanswered.pop(chat_id, None)
            return

        count = self._unanswered.get(chat_id, 0) + 1
        self._unanswered[chat_id] = count
        # Fire exactly at the threshold so a long conversation doesn't re-trigger.
        if count != self._settings.burst_threshold:
            return

        try:
            entity = await event.get_sender()
        except (ValueError, TypeError):
            return
        if entity is None or getattr(entity, "bot", False):
            return

        name = display_name(entity)
        log.info("%d unanswered messages from %s", count, name)

        decision = await self._decider.decide_burst(
            MessageBurst(
                platform="telegram",
                contact_id=str(getattr(entity, "id", chat_id)),
                contact_name=name,
                count=count,
                history=await self._history(chat_id),
            )
        )
        if not decision.send:
            return
        try:
            await self._client.send_message(chat_id, decision.text)
        except Exception:
            log.exception("burst reply to %s failed", name)

    async def _replied_since(self, caller_id: int, started_at: float) -> bool:
        """True if you have sent this contact anything since the call began."""
        try:
            messages = await self._client.get_messages(caller_id, limit=5)
        except Exception:
            return False  # can't tell; don't block the follow-up on it
        for message in messages:
            if not message.out or message.date is None:
                continue
            if message.date.timestamp() > started_at:
                return True
        return False

    async def _history(self, caller_id: int) -> tuple[Message, ...]:
        try:
            messages = await self._client.get_messages(
                caller_id, limit=self._settings.history_messages
            )
        except Exception:
            log.warning("could not read history with %s", caller_id, exc_info=True)
            return ()
        history = [
            Message(sender="me" if message.out else "them", text=text)
            for message in reversed(messages)
            if (text := (getattr(message, "message", "") or "").strip())
        ]
        return tuple(history)

    async def _prune_forever(self) -> None:
        """Drop ring records we never saw an ending for."""
        while True:
            await asyncio.sleep(PRUNE_INTERVAL)
            cutoff = time.monotonic() - RING_RECORD_TTL
            stale = [k for k, v in self._ringing.items() if v["started"] < cutoff]
            for call_id in stale:
                self._ringing.pop(call_id, None)
