"""Tells you, over a Telegram bot, every time the agent replied on your behalf.

This is a BotFather bot, deliberately separate from the Telethon user session in
telegram_watcher.py. That session is you: anything it sends lands in someone
else's chat. A bot can only message people who started a chat with it, which is
exactly the property you want for a notification channel pointed at yourself.

Nothing in here is allowed to break a send. Every failure is logged and
swallowed - the reply has already gone out by the time we are called.
"""

from __future__ import annotations

import html
import json
import logging
import time
from pathlib import Path

import httpx

from . import render
from .config import PROJECT_ROOT, Settings
from .models import SendEvent

log = logging.getLogger(__name__)

PLATFORM_LABEL = {
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "gchat": "Google Chat",
    "teams": "Microsoft Teams",
}


def _load_json(path: Path) -> dict[str, str]:
    try:
        with path.open() as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _whatsapp_lookups() -> tuple[dict[str, str], dict[str, str]]:
    """(lid -> phone jid, phone jid -> address-book name), reread when they change.

    The Node watcher owns both files; this side only reads them, so that a row
    stored months ago as a bare LID still resolves to a name and number today.
    The alternative - rewriting old rows once a mapping is learnt - would edit
    an audit trail after the fact, which is exactly what an audit trail is not.
    """
    global _LOOKUP_CACHE, _LOOKUP_STAMPS

    paths = (
        PROJECT_ROOT / "data" / "whatsapp-lid-map.json",
        PROJECT_ROOT / "data" / "whatsapp-contacts.json",
        NAMES_PATH,
    )
    stamps = tuple(path.stat().st_mtime if path.exists() else 0.0 for path in paths)
    if stamps != _LOOKUP_STAMPS:
        lid_map, synced, yours = (_load_json(path) for path in paths)
        # Names you set by hand win: WhatsApp will not hand this linked device
        # your address book unless it is re-paired with a full history sync, so
        # `/name` is the only way most contacts ever get a name at all.
        _LOOKUP_CACHE = (lid_map, {**synced, **yours})
        _LOOKUP_STAMPS = stamps
    return _LOOKUP_CACHE


NAMES_PATH = PROJECT_ROOT / "data" / "names.json"


def set_name(contact_id: str, name: str) -> None:
    """Save a name you chose for a contact. Empty name removes it."""
    names = _load_json(NAMES_PATH)
    if name:
        names[contact_id] = name
    else:
        names.pop(contact_id, None)
    NAMES_PATH.parent.mkdir(parents=True, exist_ok=True)
    NAMES_PATH.write_text(json.dumps(names, indent=2))


def saved_names() -> dict[str, str]:
    return _load_json(NAMES_PATH)


def to_jid(who: str) -> str:
    """Turn "+880 1609-982884" into the jid the store and the map both use."""
    digits = "".join(c for c in who if c.isdigit())
    return f"{digits}@s.whatsapp.net" if digits else ""


_LOOKUP_CACHE: tuple[dict[str, str], dict[str, str]] = ({}, {})
_LOOKUP_STAMPS: tuple[float, ...] = ()


def describe_contact(platform: str, contact_name: str, contact_id: str) -> tuple[str, str]:
    """Split a stored contact into (name, identifier) for display.

    Either alone is regularly useless: a bare id tells you nothing you would
    recognise, and a name may be a push name the sender chose for themselves.
    A LID is neither - it is an opaque WhatsApp handle, so it gets resolved to a
    real number where we know one and labelled honestly where we do not.
    """
    lid_map, contacts = _whatsapp_lookups() if platform == "whatsapp" else ({}, {})

    # Rows written before a LID was resolved stored the LID digits as the name.
    # Those are not names, and must not survive the lookup as one.
    placeholders = {contact_id.split("@")[0]}

    if contact_id.endswith("@lid"):
        resolved = lid_map.get(contact_id, "")
        if not resolved:
            local = contact_id.split("@")[0]
            name = "" if contact_name.lstrip("+") in placeholders else contact_name
            return name, f"unknown caller · lid {local}"
        contact_id = resolved

    local = contact_id.split("@")[0]
    placeholders.add(local)
    identifier = f"+{local}" if platform == "whatsapp" and local.isdigit() else local

    name = contacts.get(contact_id, "")
    if not name and contact_name.lstrip("+") not in placeholders:
        name = contact_name
    return name, identifier


# Telegram rejects captions over 1024 characters outright.
CAPTION_LIMIT = 1000


def ago(seconds: float | None) -> str:
    """Compact relative time: 5m, 3h, 2d, 4mo, 1y.

    Months are "mo", not "M". A set where minute and month differ only by the
    case of one letter is misread at a glance, which is the only way anyone
    reads a timestamp in a notification.

    Each unit floors rather than rounds, so nothing ever reads "24h ago" or
    "60m ago" - it rolls into the next unit instead.
    """
    if seconds is None:
        return "time unknown"
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"

    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"

    days = hours // 24
    if days < 30:
        return f"{days}d ago"

    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    return f"{max(1, days // 365)}y ago"


class Notifier:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: httpx.AsyncClient | None = None
        if settings.notify_enabled:
            self._client = httpx.AsyncClient(
                base_url=f"https://api.telegram.org/bot{settings.notify_bot_token}",
                timeout=20.0,
            )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def notify(self, event: SendEvent) -> None:
        if self._client is None:
            return
        if event.kind == "burst" and not self._settings.notify_bursts:
            return

        try:
            caption = self._caption(event)
            image = self._card(event) if self._settings.notify_image else None
            if image:
                await self._send_photo(caption, image)
            else:
                await self._send_text(caption)
            log.info("notified you about %s", event.label)
        except Exception:
            log.warning("could not send the notification for %s", event.label, exc_info=True)

    # -- message building ---------------------------------------------------

    def _caption(self, event: SendEvent) -> str:
        platform = PLATFORM_LABEL.get(event.platform, event.platform.title())
        when = time.localtime(event.occurred_at)
        elapsed = ago(time.time() - event.occurred_at)
        if event.kind == "burst":
            icon = "💬"
        elif event.kind == "mention":
            icon = "🔔"
        else:
            icon = "📞"

        # Same resolution the bot's /status uses, so a caller is described
        # identically whether you read about them in a push or ask after it.
        name, identifier = describe_contact(
            event.platform, event.contact_name, event.contact_id
        )
        name, identifier = html.escape(name), html.escape(identifier)
        who = f"{name} <code>{identifier}</code>" if name else f"<code>{identifier}</code>"

        lines = [
            f"{icon} <b>{html.escape(event.headline)}</b> · {platform}",
            f"👤 {who}",
        ]
        if event.kind == "mention" and (event.group_name or event.group_id):
            group = html.escape(event.group_name or event.group_id)
            lines.append(f"👥 {group}")
        lines += [
            f"🕐 {time.strftime('%-I:%M %p, %-d %b', when)} · {elapsed}",
        ]
        link = self._chat_link(event)
        if link:
            lines.append(f'💬 <a href="{html.escape(link, quote=True)}">Open the chat</a>')
        lines += [
            "",
            "↩️ <b>Replied:</b>",
            f"<blockquote>{html.escape(self._trim(event.text))}</blockquote>",
        ]
        return "\n".join(lines)

    def _readable_id(self, event: SendEvent) -> str:
        """The contact id in a form worth showing a human.

        A raw WhatsApp jid (+8801787308210@s.whatsapp.net) looks enough like an
        email address that phones try to open it, which is confusing when it is
        just an identifier. Strip it back to the phone number.
        """
        if event.platform == "whatsapp":
            local = event.contact_id.split("@")[0]
            if event.contact_id.endswith("@lid"):
                return ""  # an opaque WhatsApp id; means nothing to you
            digits = "".join(c for c in local if c.isdigit())
            return f"+{digits}" if digits else ""
        if event.contact_handle:
            return f"@{event.contact_handle}"
        return event.contact_id

    def _chat_link(self, event: SendEvent) -> str | None:
        """A link that opens the conversation in the app it happened in.

        https only, and not by preference. Telegram drops link entities whose
        scheme it does not recognise - `whatsapp://send?phone=...` comes back
        from the API with ok:true and is then rendered as dead plain text, which
        looks exactly like a bug. wa.me and t.me survive, and on a phone the OS
        hands them straight to the installed app anyway; the trip through
        web.whatsapp.com only happens on a desktop with no app registered.

        tg://user?id= is the one exception: Telegram keeps it as a tappable
        mention, and it is all we have for a contact with no username.
        """
        if event.platform == "whatsapp":
            # describe_contact resolves a LID to a real number where the map
            # knows one, and labels it "unknown caller" where it does not - so
            # a digits-only identifier is exactly the linkable case.
            _, identifier = describe_contact(
                event.platform, event.contact_name, event.contact_id
            )
            digits = "".join(c for c in identifier if c.isdigit())
            if not identifier.startswith("+") or not digits:
                return None
            return f"https://wa.me/{digits}"

        if event.platform == "telegram":
            if event.contact_handle:
                return f"https://t.me/{event.contact_handle.lstrip('@')}"
            if event.contact_id.isdigit():
                return f"tg://user?id={event.contact_id}"
        return None

    def _trim(self, text: str) -> str:
        # Leave room for the markup around it.
        budget = CAPTION_LIMIT - 220
        return text if len(text) <= budget else text[: budget - 1].rstrip() + "…"

    def _card(self, event: SendEvent) -> bytes | None:
        platform = PLATFORM_LABEL.get(event.platform, event.platform.title())
        when = time.strftime("%-I:%M %p, %-d %b", time.localtime(event.occurred_at))
        return render.chat_card(
            title=f"{event.headline} — {event.contact_name or event.contact_id}",
            subtitle=f"{platform} · {when}",
            history=event.history,
            reply=event.text,
            reply_label="AUTO-REPLY SENT",
            max_messages=self._settings.notify_history,
        )

    # -- transport ----------------------------------------------------------

    async def _send_text(self, caption: str) -> None:
        assert self._client is not None
        response = await self._client.post(
            "/sendMessage",
            json={
                "chat_id": self._settings.notify_chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        self._check(response)

    async def _send_photo(self, caption: str, image: bytes) -> None:
        assert self._client is not None
        response = await self._client.post(
            "/sendPhoto",
            data={
                "chat_id": self._settings.notify_chat_id,
                "caption": caption,
                "parse_mode": "HTML",
            },
            files={"photo": ("conversation.png", image, "image/png")},
        )
        self._check(response)

    def _check(self, response: httpx.Response) -> None:
        if response.status_code == 200 and response.json().get("ok"):
            return
        # Telegram's error body says exactly what is wrong (bad token, wrong
        # chat id, bot never started); pass it through instead of a bare code.
        raise RuntimeError(f"Telegram API returned {response.status_code}: {response.text[:300]}")
