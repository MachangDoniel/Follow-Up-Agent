"""Catch up on everyone left hanging in the last hour.

Two kinds of debt build up while the agent is not watching, or while it is
switched off:

  * people who sent several messages and got no reply
  * calls it saw, decided not to answer, and recorded as skipped

A sweep finds both, inside a time window, and puts them through the same
`Decider` a live event would go through. Nothing here decides anything on its
own: an allow list, a block, a cooldown and a dry run all still apply, because
the whole point is that a manual catch-up behaves like the automatic path.

The window matters. Replying to a message run from three days ago is worse than
not replying at all - the moment has passed and the reply reads as a bot. One
hour by default, and never the whole backlog.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

from .config import Settings
from .decide import Decider
from .models import Message, MessageBurst, MissedCall
from .store import Store

log = logging.getLogger(__name__)

# Space out sends. A burst of messages to many contacts in a few seconds is the
# exact shape WhatsApp's anti-spam looks for, and this is the one code path that
# can produce it.
SEND_GAP_SECONDS = 4.0


@dataclass(slots=True)
class Candidate:
    """One thing that happened in the window.

    Everything is reported; only some of it is `actionable`. A single unseen
    message is worth telling you about and is not worth an automatic reply -
    those are two different questions, and conflating them is what made an
    earlier version of this hide most of what happened.
    """

    platform: str
    contact_id: str
    contact_name: str
    kind: str  # "burst" | "call"
    count: int = 0
    reason: str = "missed"
    # None when WhatsApp gave us a count but no time to place it at - a chat
    # delivered by a history sync before this process ever saw it.
    age_seconds: float | None = 0.0
    status: str = ""  # what already happened to it, in your words
    actionable: bool = False
    history: tuple[Message, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        return self.contact_name or self.contact_id

    @property
    def what(self) -> str:
        if self.kind == "burst":
            return f"{self.count} message{'s' if self.count != 1 else ''}"
        return f"{self.reason} call"


@dataclass(slots=True)
class Outcome:
    candidate: Candidate
    sent: bool
    text: str = ""
    problem: str = ""


class Sweeper:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        decider: Decider,
        senders: dict[str, object],
    ) -> None:
        self._settings = settings
        self._store = store
        self._decider = decider
        # platform -> async (contact_id, text) -> None. The Telegram watcher
        # registers itself; WhatsApp goes out through the Node control server.
        self._senders = senders
        self._whatsapp = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{settings.whatsapp_control_port}", timeout=30.0
        )

    async def aclose(self) -> None:
        await self._whatsapp.aclose()

    # -- finding work -------------------------------------------------------

    async def activity(self, window_seconds: float) -> list[Candidate]:
        """Everything that happened in the window, actionable or not."""
        found = await self._message_runs(window_seconds)
        found += self._calls(window_seconds)
        found.sort(key=lambda c: (c.age_seconds is None, c.age_seconds or 0))
        return found

    async def candidates(self, window_seconds: float) -> list[Candidate]:
        """Only the things a sweep would actually reply to."""
        return [item for item in await self.activity(window_seconds) if item.actionable]

    async def _message_runs(self, window_seconds: float) -> list[Candidate]:
        """WhatsApp chats sitting on several messages with no reply from you."""
        try:
            response = await self._whatsapp.get("/chats")
            response.raise_for_status()
            chats = response.json().get("chats") or []
        except Exception as exc:
            log.warning("could not read the WhatsApp chat list: %s", exc)
            return []

        now = time.time()
        threshold = self._settings.burst_threshold
        out: list[Candidate] = []
        for chat in chats:
            # Our own timestamp, set when this process watched a message land.
            # Chats that arrived through a history sync have none, so fall back
            # to WhatsApp's own last-activity time - otherwise the very chats a
            # sync exists to reveal are the ones the window filter drops.
            # `unanswered` is what this agent counted; `unread` is what your chat
            # list shows. They disagree after a restart, and the larger one is
            # the honest answer to "how many are they waiting on".
            count = max(int(chat.get("unanswered") or 0), int(chat.get("unread") or 0))

            last_incoming = float(chat.get("last_incoming_at") or 0) / 1000
            last_activity = float(chat.get("last_activity_at") or 0) / 1000
            when = last_incoming or last_activity

            age: float | None
            if when:
                age = now - when
                if age > window_seconds:
                    continue
            elif count:
                # No timestamp, but people are demonstrably waiting. Reporting
                # "nothing happened" while your chat list shows 11 unread is the
                # worse error by far, so it is shown with its age marked unknown
                # and left for you to judge before sending.
                age = None
            else:
                continue

            # Only meaningful when we know when their message landed.
            replied = bool(last_incoming) and (
                float(chat.get("last_outgoing_at") or 0) / 1000 > last_incoming
            )

            if replied or count == 0:
                status, actionable = "you replied", False
            elif count >= threshold:
                status, actionable = "waiting", True
            else:
                status, actionable = f"waiting (under the {threshold} threshold)", False

            out.append(
                Candidate(
                    platform="whatsapp",
                    contact_id=str(chat.get("contact_id") or ""),
                    contact_name=str(chat.get("contact_name") or ""),
                    kind="burst",
                    count=max(count, 1),
                    age_seconds=age,
                    status=status,
                    actionable=actionable,
                    history=tuple(
                        Message(
                            sender="me" if item.get("from") == "me" else "them",
                            text=str(item.get("text") or ""),
                        )
                        for item in chat.get("history") or []
                        if str(item.get("text") or "").strip()
                    ),
                )
            )
        return out

    def _calls(self, window_seconds: float) -> list[Candidate]:
        """Every call the agent recorded in the window, answered or not.

        Only ones it recorded. A call that arrived while the watcher was down
        left no trace anywhere, and neither WhatsApp nor Telegram will hand over
        a call log after the fact - so those are simply not recoverable.
        """
        now = time.time()
        rows = self._store.recent(limit=200)
        seen: set[tuple[str, str]] = set()
        out: list[Candidate] = []
        for row in rows:
            if row["kind"] != "call":
                continue
            age = now - row["created_at"]
            if age > window_seconds:
                continue
            key = (row["platform"], row["contact_id"])
            if key in seen:
                continue  # one entry per contact, the most recent
            seen.add(key)

            if row["sent"]:
                status, actionable = "replied by the bot", False
            else:
                status, actionable = f"no reply — {row['skip_reason'] or 'skipped'}", True

            out.append(
                Candidate(
                    platform=row["platform"],
                    contact_id=row["contact_id"],
                    contact_name=row["contact_name"],
                    kind="call",
                    reason=row["reason"] or "missed",
                    age_seconds=age,
                    status=status,
                    actionable=actionable,
                )
            )
        return out

    # -- acting on it -------------------------------------------------------

    async def run(self, window_seconds: float) -> list[Outcome]:
        outcomes: list[Outcome] = []
        for candidate in await self.candidates(window_seconds):
            outcome = await self._handle(candidate)
            outcomes.append(outcome)
            if outcome.sent:
                await asyncio.sleep(SEND_GAP_SECONDS)
        return outcomes

    async def _handle(self, candidate: Candidate) -> Outcome:
        if candidate.kind == "burst":
            decision = await self._decider.decide_burst(
                MessageBurst(
                    platform=candidate.platform,
                    contact_id=candidate.contact_id,
                    contact_name=candidate.contact_name,
                    count=candidate.count,
                    history=candidate.history,
                )
            )
        else:
            decision = await self._decider.decide(
                MissedCall(
                    platform=candidate.platform,
                    contact_id=candidate.contact_id,
                    contact_name=candidate.contact_name,
                    reason=candidate.reason,
                    history=candidate.history,
                )
            )

        if not decision.send:
            return Outcome(candidate, sent=False, problem=decision.skip_reason)

        try:
            await self._send(candidate.platform, candidate.contact_id, decision.text)
        except Exception as exc:
            log.warning("sweep: sending to %s failed: %s", candidate.label, exc)
            return Outcome(candidate, sent=False, text=decision.text, problem=str(exc))
        return Outcome(candidate, sent=True, text=decision.text)

    async def _send(self, platform: str, contact_id: str, text: str) -> None:
        if platform == "whatsapp":
            response = await self._whatsapp.post(
                "/send", json={"jid": contact_id, "text": text}
            )
            if response.status_code != 200:
                raise RuntimeError(f"watcher returned {response.status_code}: {response.text[:120]}")
            return

        sender = self._senders.get(platform)
        if sender is None:
            raise RuntimeError(f"no sender is wired up for {platform}")
        await sender(contact_id, text)  # type: ignore[operator]
