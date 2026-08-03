"""Should we reply to this caller at all? Pure functions, no I/O."""

from __future__ import annotations

import time

from .config import Settings
from .models import MessageBurst, MissedCall

Contact = MissedCall | MessageBurst


def matches(patterns: tuple[str, ...], call: Contact) -> bool:
    """True if any pattern appears in the contact's id or name, case-insensitive."""
    haystack = (call.contact_id.lower(), call.contact_name.lower())
    return any(
        pattern.lower() in field
        for pattern in patterns
        if pattern.strip()
        for field in haystack
    )


def in_quiet_hours(settings: Settings, now: float | None = None) -> bool:
    if not settings.quiet_hours:
        return False
    local = time.localtime(now if now is not None else time.time())
    minutes = local.tm_hour * 60 + local.tm_min
    for start, end in settings.quiet_hours:
        if start <= end:
            if start <= minutes < end:
                return True
        elif minutes >= start or minutes < end:  # window wraps past midnight
            return True
    return False


def blocked_reason(
    settings: Settings,
    call: Contact,
    seconds_since_last: float | None,
    cooldown_seconds: float | None = None,
    allow: tuple[str, ...] | None = None,
) -> str:
    """Empty string means go ahead; otherwise a human-readable skip reason.

    `allow` overrides ALLOW from .env, so entries added from the Telegram bot
    take part in the same whitelist rather than being a second, separate test -
    a whitelist only means anything when there is exactly one of it.
    """
    allow = settings.allow if allow is None else allow

    if not call.contact_id:
        return "no contact id"
    if settings.block and matches(settings.block, call):
        return "contact is on the block list"
    if allow and not matches(allow, call):
        return "contact is not on the allow list"
    if in_quiet_hours(settings):
        return "inside quiet hours"

    cooldown = (
        settings.cooldown_minutes * 60 if cooldown_seconds is None else cooldown_seconds
    )
    if cooldown > 0 and seconds_since_last is not None and seconds_since_last < cooldown:
        minutes = round(cooldown / 60)
        return f"already messaged within {minutes} min"
    return ""
