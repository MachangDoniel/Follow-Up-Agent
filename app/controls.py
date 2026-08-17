"""Runtime switches you can flip from the Telegram bot, without touching .env.

`Settings` is frozen on purpose: it is the configuration you decided on, and code
that can rewrite its own config is hard to reason about. But "stop replying to
people, right now, from my phone" is exactly the thing you need when you are
nowhere near the machine. So the mutable state lives here instead, in one small
object, persisted to sqlite so a pause survives a restart.

Every switch starts from the matching .env value and overrides it once you touch
it from the bot. `/reset` drops back to the file.
"""

from __future__ import annotations

import logging
import time

from .config import Settings
from .store import Store

log = logging.getLogger(__name__)

PLATFORMS = ("whatsapp", "telegram", "gchat", "teams")


class Controls:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store

    # -- persistence --------------------------------------------------------

    def _get(self, key: str, default: str) -> str:
        value = self._store.get_control(key)
        return default if value is None else value

    def _bool(self, key: str, default: bool) -> bool:
        return self._get(key, "1" if default else "0") == "1"

    def _set_bool(self, key: str, value: bool) -> None:
        self._store.set_control(key, "1" if value else "0")

    # -- master switch ------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._bool("enabled", True)

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._set_bool("enabled", value)

    @property
    def paused_until(self) -> float:
        try:
            return float(self._get("paused_until", "0"))
        except ValueError:
            return 0.0

    @paused_until.setter
    def paused_until(self, value: float) -> None:
        self._store.set_control("paused_until", str(value))

    # -- per-platform -------------------------------------------------------

    def platform_enabled(self, platform: str) -> bool:
        return self._bool(f"platform:{platform}", True)

    def set_platform(self, platform: str, value: bool) -> None:
        self._set_bool(f"platform:{platform}", value)

    # -- overrides of .env values ------------------------------------------

    @property
    def dry_run(self) -> bool:
        return self._bool("dry_run", self._settings.dry_run)

    @dry_run.setter
    def dry_run(self, value: bool) -> None:
        self._set_bool("dry_run", value)

    @property
    def calls_enabled(self) -> bool:
        """Missed-call follow-ups. The main feature, so it has no .env switch -
        turning it off is a runtime decision, not a configuration."""
        return self._bool("calls", True)

    @calls_enabled.setter
    def calls_enabled(self, value: bool) -> None:
        self._set_bool("calls", value)

    @property
    def burst_enabled(self) -> bool:
        return self._bool("burst", self._settings.burst_enabled)

    @burst_enabled.setter
    def burst_enabled(self, value: bool) -> None:
        self._set_bool("burst", value)

    @property
    def mentions_enabled(self) -> bool:
        """Group mention replies on Google Chat and Teams."""
        return self._bool("mentions", True)

    @mentions_enabled.setter
    def mentions_enabled(self, value: bool) -> None:
        self._set_bool("mentions", value)

    def _list(self, key: str) -> tuple[str, ...]:
        return tuple(p.strip() for p in self._get(key, "").split(",") if p.strip())

    def _add(self, key: str, who: str) -> bool:
        """Returns False if it was already there."""
        who = who.strip()
        current = self._list(key)
        if not who or any(who.lower() == entry.lower() for entry in current):
            return False
        self._store.set_control(key, ",".join((*current, who)))
        return True

    def _remove(self, key: str, who: str) -> bool:
        who = who.strip().lower()
        current = self._list(key)
        remaining = tuple(entry for entry in current if entry.lower() != who)
        if len(remaining) == len(current):
            return False
        self._store.set_control(key, ",".join(remaining))
        return True

    @property
    def blocked(self) -> tuple[str, ...]:
        """Runtime additions to BLOCK, on top of whatever .env already lists."""
        return self._list("blocked")

    def block(self, who: str) -> bool:
        return self._add("blocked", who)

    def unblock(self, who: str) -> bool:
        return self._remove("blocked", who)

    @property
    def allowed(self) -> tuple[str, ...]:
        """Runtime additions to ALLOW.

        Careful: an allow list is a whitelist. Empty means everyone is replied
        to; the moment it has one entry, every other contact is silently
        ignored. The bot says so out loud when the first entry goes in.
        """
        return self._list("allowed")

    def allow(self, who: str) -> bool:
        return self._add("allowed", who)

    def unallow(self, who: str) -> bool:
        return self._remove("allowed", who)

    def effective_allow(self) -> tuple[str, ...]:
        return (*self._settings.allow, *self.allowed)

    def reset(self) -> None:
        """Forget every override and go back to what .env says."""
        self.enabled = True
        self.paused_until = 0.0
        self.dry_run = self._settings.dry_run
        self.calls_enabled = True
        self.burst_enabled = self._settings.burst_enabled
        self.mentions_enabled = True
        self._store.set_control("blocked", "")
        self._store.set_control("allowed", "")
        for platform in PLATFORMS:
            self.set_platform(platform, True)

    # -- the question the Decider actually asks -----------------------------

    def blocked_reason(self, platform: str) -> str:
        """Why this platform is not replying right now, or "" if it is."""
        if not self.enabled:
            return "switched off from the bot (/on to resume)"

        remaining = self.paused_until - time.time()
        if remaining > 0:
            minutes = round(remaining / 60)
            when = time.strftime("%-I:%M %p", time.localtime(self.paused_until))
            return f"paused for another {minutes} min, until {when} (/on to resume)"

        if not self.platform_enabled(platform):
            return f"{platform} is switched off from the bot (/{platform} on to resume)"
        return ""

    def summary(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "paused_until": self.paused_until,
            "platforms": {p: self.platform_enabled(p) for p in PLATFORMS},
            "dry_run": self.dry_run,
            "calls": self.calls_enabled,
            "burst": self.burst_enabled,
            "mentions": self.mentions_enabled,
            "blocked": self.blocked,
            "allowed": self.allowed,
        }
