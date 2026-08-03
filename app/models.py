"""Shared value objects passed between the watchers and the decision logic."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Platform = Literal["telegram", "whatsapp"]

# Why the call ended without being answered.
Reason = Literal["missed", "rejected", "busy", "cancelled", "disconnected"]

REASON_TEXT: dict[str, str] = {
    "missed": "the call rang out and was never answered",
    "rejected": "the call was declined",
    "busy": "the call was declined as busy",
    "cancelled": "the caller hung up before it was answered",
    "disconnected": "the call dropped before it connected",
}


@dataclass(frozen=True, slots=True)
class Message:
    sender: Literal["me", "them"]
    text: str


@dataclass(frozen=True, slots=True)
class MissedCall:
    platform: str
    contact_id: str
    contact_name: str
    reason: str
    video: bool = False
    history: tuple[Message, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        return f"{self.platform} {self.contact_name or self.contact_id}"

    @property
    def cooldown_key(self) -> str:
        return f"{self.platform}:{self.contact_id}"


@dataclass(frozen=True, slots=True)
class MessageBurst:
    """Someone has sent several messages in a row without a reply from you."""

    platform: str
    contact_id: str
    contact_name: str
    count: int
    history: tuple[Message, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        return f"{self.platform} {self.contact_name or self.contact_id}"


@dataclass(frozen=True, slots=True)
class SendEvent:
    """A reply that actually went out, on its way to your notification bot.

    Built at the send site rather than in the Decider, because the Decider
    commits to a send before the watcher performs it - and a send can still fail.
    """

    platform: str
    contact_id: str
    contact_name: str
    kind: str  # "call" | "burst"
    text: str
    occurred_at: float  # when the call rang / the burst hit the threshold
    # Telegram @username when they have one. Makes the notification's "open chat"
    # link a t.me address, which resolves anywhere, instead of a tg:// user id.
    contact_handle: str = ""
    reason: str = ""
    video: bool = False
    count: int = 0
    history: tuple[Message, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        return f"{self.platform} {self.contact_name or self.contact_id}"

    @property
    def headline(self) -> str:
        if self.kind == "burst":
            return f"{self.count} unanswered messages"
        kind = "video call" if self.video else "call"
        return {
            "rejected": f"Declined {kind}",
            "busy": f"Declined {kind} (busy)",
            "cancelled": f"Cancelled {kind}",
            "disconnected": f"Dropped {kind}",
        }.get(self.reason, f"Missed {kind}")


@dataclass(frozen=True, slots=True)
class Decision:
    send: bool
    text: str = ""
    skip_reason: str = ""
    used_fallback: bool = False

    def as_json(self) -> dict[str, object]:
        return {
            "send": self.send,
            "text": self.text,
            "skip_reason": self.skip_reason,
            "used_fallback": self.used_fallback,
        }
