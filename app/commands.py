"""Lets you drive the agent from the same Telegram bot that notifies you.

Long-polls getUpdates and handles a small set of commands. Runs as a task inside
`python -m app`, so it shares the live Store and Controls with the Decider - a
switch flipped here takes effect on the very next call, with no restart.

SECURITY: anyone who finds the bot can message it. Every update whose chat id is
not NOTIFY_CHAT_ID is dropped and logged. That check is the only thing standing
between a stranger and your kill switch, so it happens before anything is parsed.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time

import httpx

from .config import Settings
from .controls import PLATFORMS, Controls
from .notify import (
    PLATFORM_LABEL,
    ago,
    describe_contact,
    saved_names,
    set_name,
    to_jid,
)
from .store import Store
from .sweep import Sweeper

log = logging.getLogger(__name__)

POLL_TIMEOUT = 50  # seconds Telegram holds the connection open
BACKOFF_MAX = 60.0

HELP = """<b>Commands</b>

<b>Master switch</b>
/off — stop replying to anyone
/off 2h — stop for a while (30m, 2h, 8h)
/on — start again
/status — what it is doing right now

<b>Per platform</b>
/whatsapp off · /whatsapp on
/telegram off · /telegram on

<b>What it replies to</b>
/calls on · /calls off — missed-call follow-ups
/burst on · /burst off — the 5-unanswered-messages reply
/dryrun on — write replies to the log, send nothing
/dryrun off — send for real

<b>People</b>
/block Shujoy — never reply to them again
/block +8801787308210
/unblock Shujoy
/blocked — who is blocked
/allow Shujoy — reply to ONLY these people
/unallow Shujoy
/allowed — the allow list
/name +8801609982884 Dr. Dinesh — label a number
/names — names you have set

<b>Catch up</b>
/sweep — who is waiting on a reply (last 1h)
/sweep send — actually reply to them
/sweep 3h · /sweep 3h send — a different window

<b>History</b>
/recent — the last 10 decisions, sent or skipped
/reset — drop every override, go back to .env"""


def parse_duration(text: str) -> float | None:
    """"30m" / "2h" / "45" -> seconds. None if it is not a duration."""
    text = text.strip().lower()
    if not text:
        return None
    unit, number = text[-1], text[:-1]
    if unit.isdigit():
        unit, number = "m", text
    if not number.isdigit():
        return None
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
    return None if factor is None else int(number) * factor


def on_off(argument: str) -> bool | None:
    value = argument.strip().lower()
    if value in {"on", "yes", "true", "1", "start", "enable"}:
        return True
    if value in {"off", "no", "false", "0", "stop", "disable"}:
        return False
    return None


class CommandServer:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        controls: Controls,
        sweeper: Sweeper | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._controls = controls
        self._sweeper = sweeper
        self._sweeping = False
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{settings.notify_bot_token}",
            timeout=POLL_TIMEOUT + 15,
        )
        self._offset: int | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def run(self) -> None:
        await self._register_commands()
        await self._skip_backlog()
        log.info("bot commands active; send /help to the bot")
        backoff = 1.0
        while True:
            try:
                updates = await self._poll()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Network blips and Telegram 5xx are routine over days of uptime.
                log.warning("command poll failed (%s); retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue

            for update in updates:
                try:
                    await self._handle(update)
                except Exception:
                    log.exception("command handler failed")

    async def _skip_backlog(self) -> None:
        """Ignore anything sent while we were down.

        Telegram queues updates for 24h. Without this, a restart would replay
        every message you sent the bot meanwhile - so an `/off` you sent and then
        undid by hand could silently re-apply itself on the next restart.
        """
        try:
            response = await self._client.get("/getUpdates", params={"offset": -1})
            response.raise_for_status()
            updates = response.json().get("result") or []
            if updates:
                self._offset = updates[-1]["update_id"] + 1
                log.info("skipped %d message(s) queued while the bot was down", len(updates))
        except Exception:
            log.warning("could not skip the command backlog", exc_info=True)

    async def _poll(self) -> list[dict]:
        params: dict[str, object] = {"timeout": POLL_TIMEOUT}
        if self._offset is not None:
            params["offset"] = self._offset
        response = await self._client.get("/getUpdates", params=params)
        response.raise_for_status()
        updates = response.json().get("result") or []
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    async def _register_commands(self) -> None:
        """Populate the bot's command menu, so you get autocomplete in the app."""
        menu = [
            ("status", "what the agent is doing right now"),
            ("on", "resume replying"),
            ("off", "stop replying (optionally /off 2h)"),
            ("whatsapp", "on / off for WhatsApp only"),
            ("telegram", "on / off for Telegram only"),
            ("calls", "on / off - missed-call follow-ups"),
            ("dryrun", "on / off - generate without sending"),
            ("burst", "on / off - the 5-message reply"),
            ("block", "never reply to a contact"),
            ("unblock", "undo a block"),
            ("blocked", "list blocked contacts"),
            ("allow", "reply to ONLY these people"),
            ("unallow", "undo an allow"),
            ("allowed", "show the allow list"),
            ("name", "label a number, e.g. /name +880… Dinesh"),
            ("names", "names you have set"),
            ("sweep", "who is waiting; /sweep send to reply"),
            ("recent", "the last decisions, sent or skipped"),
            ("reset", "drop overrides, go back to .env"),
            ("help", "the full list"),
        ]
        try:
            await self._client.post(
                "/setMyCommands",
                json={"commands": [{"command": c, "description": d} for c, d in menu]},
            )
        except Exception:
            log.warning("could not register the bot command menu", exc_info=True)

    # -- dispatch -----------------------------------------------------------

    async def _handle(self, update: dict) -> None:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = str(message.get("text") or "").strip()
        if not text:
            return

        if chat_id != str(self._settings.notify_chat_id):
            # Not you. Say nothing back - an unauthorised sender should not
            # learn that the bot does anything at all.
            log.warning("ignoring a command from chat %s: %r", chat_id, text[:80])
            return

        command, _, argument = text.partition(" ")
        command = command.lstrip("/").split("@")[0].lower()
        argument = argument.strip()

        if command == "sweep":
            await self._sweep(argument)
            return
        await self._reply(self._run_command(command, argument))

    def _run_command(self, command: str, argument: str) -> str:
        match command:
            case "start" | "help":
                return HELP
            case "status":
                return self._status()
            case "on":
                self._controls.enabled = True
                self._controls.paused_until = 0.0
                return "▶️ On. Replying again."
            case "off":
                return self._off(argument)
            case "whatsapp" | "telegram":
                return self._platform(command, argument)
            case "dryrun":
                return self._toggle_dry_run(argument)
            case "calls":
                return self._toggle_calls(argument)
            case "burst":
                return self._toggle_burst(argument)
            case "block":
                if not argument:
                    return "Who? Try <code>/block Shujoy</code> or <code>/block +8801787308210</code>"
                ok = self._controls.block(argument)
                return (
                    f"🚫 Blocked <b>{html.escape(argument)}</b>. They will never get a reply."
                    if ok
                    else f"<b>{html.escape(argument)}</b> was already blocked."
                )
            case "unblock":
                if not argument:
                    return "Who? Try <code>/unblock Shujoy</code>"
                ok = self._controls.unblock(argument)
                return (
                    f"✅ Unblocked <b>{html.escape(argument)}</b>."
                    if ok
                    else f"<b>{html.escape(argument)}</b> was not in the runtime block list."
                )
            case "blocked":
                return self._blocked()
            case "allow":
                return self._allow(argument)
            case "unallow":
                return self._unallow(argument)
            case "allowed":
                return self._allowed()
            case "name":
                return self._name(argument)
            case "names":
                return self._names()
            case "recent":
                return self._recent(argument)
            case "reset":
                self._controls.reset()
                return "♻️ Every override dropped. Back to whatever .env says.\n\n" + self._status()
            case _:
                return f"Don't know <code>/{html.escape(command)}</code>. Try /help"

    @staticmethod
    def _who(row) -> str:
        """Name and number together - a name alone is not identifying."""
        name, identifier = describe_contact(
            row["platform"], row["contact_name"], row["contact_id"]
        )
        name, identifier = html.escape(name), html.escape(identifier)
        return f"<b>{name}</b> <code>{identifier}</code>" if name else f"<code>{identifier}</code>"

    async def _sweep(self, argument: str) -> None:
        """`/sweep` previews, `/sweep send` acts. Never both in one command.

        A sweep is the only path that can message several people in quick
        succession, so it does not happen on a single word. You see the list
        first, then decide.
        """
        if self._sweeper is None:
            await self._reply("Sweeps are not available (the brain started without one).")
            return
        if self._sweeping:
            await self._reply("A sweep is already running. Give it a minute.")
            return

        words = argument.lower().split()
        send = "send" in words
        window = self._settings.sweep_window_minutes * 60
        for word in words:
            if word == "send":
                continue
            parsed = parse_duration(word)
            if parsed is None:
                await self._reply(
                    f"Didn't understand <code>{html.escape(word)}</code>. "
                    "Try <code>/sweep</code>, <code>/sweep send</code>, or <code>/sweep 3h send</code>."
                )
                return
            window = parsed

        pretty_window = f"{round(window / 3600, 1):g}h" if window >= 3600 else f"{round(window / 60)}m"

        if not send:
            activity = await self._sweeper.activity(window)
            await self._reply(self._preview(activity, pretty_window))
            return

        self._sweeping = True
        await self._reply(f"🧹 Sweeping the last {pretty_window}. This can take a few minutes…")
        try:
            outcomes = await self._sweeper.run(window)
            await self._reply(self._sweep_report(outcomes, pretty_window))
        except Exception as exc:
            log.exception("sweep failed")
            await self._reply(f"Sweep failed: <code>{html.escape(str(exc))}</code>")
        finally:
            self._sweeping = False

    def _preview(self, activity: list, window: str) -> str:
        """Everything that happened, with the repliable part marked.

        A single unseen message is worth knowing about and is not worth an
        automatic reply. Showing only what the agent would act on hid most of
        what actually happened, which is the opposite of what you want from a
        catch-up report.
        """
        if not activity:
            return (
                f"Nothing happened in the last {window} — no messages, no calls.\n\n"
                "<i>Only what the watchers saw themselves. Anything that arrived "
                "while they were down leaves no trace.</i>"
            )

        icons = {"waiting": "🔴", "you replied": "✅", "replied by the bot": "🤖"}
        lines = [f"<b>Last {window}</b>", ""]
        for c in activity:
            name, identifier = describe_contact(c.platform, c.contact_name, c.contact_id)
            who = f"<b>{html.escape(name)}</b>" if name else f"<code>{html.escape(identifier)}</code>"
            icon = icons.get(c.status, "🟡" if c.actionable else "⚪️")
            kind = "📞" if c.kind == "call" else "💬"
            lines.append(
                f"{icon} {who}\n"
                f"   {kind} {c.what} · {ago(c.age_seconds)} · <i>{html.escape(c.status)}</i>"
            )

        actionable = [c for c in activity if c.actionable]
        lines.append("")
        if actionable:
            lines.append(
                f"<b>{len(actionable)} of {len(activity)}</b> would get a reply "
                f"({self._settings.burst_threshold}+ message runs and unanswered calls)."
            )
            lines.append("<b>/sweep send</b> to send them.")
        else:
            lines.append(f"<i>{len(activity)} item(s), none needing a reply.</i>")
        return "\n".join(lines)

    def _sweep_report(self, outcomes: list, window: str) -> str:
        if not outcomes:
            return f"Nothing to do in the last {window}."

        sent = [o for o in outcomes if o.sent]
        skipped = [o for o in outcomes if not o.sent]
        lines = [f"<b>Sweep done</b> · last {window}", ""]

        for outcome in sent:
            c = outcome.candidate
            name, identifier = describe_contact(c.platform, c.contact_name, c.contact_id)
            who = f"<b>{html.escape(name)}</b>" if name else f"<code>{html.escape(identifier)}</code>"
            lines.append(f"✅ {who}\n   <i>{html.escape(outcome.text[:110])}</i>")

        for outcome in skipped:
            c = outcome.candidate
            name, identifier = describe_contact(c.platform, c.contact_name, c.contact_id)
            who = html.escape(name or identifier)
            lines.append(f"⏭ {who} — {html.escape(outcome.problem or 'skipped')}")

        lines.append("")
        lines.append(f"{len(sent)} sent, {len(skipped)} skipped.")
        return "\n".join(lines)

    # -- individual commands ------------------------------------------------

    def _off(self, argument: str) -> str:
        if argument:
            seconds = parse_duration(argument)
            if seconds is None:
                return "Didn't understand that. Try <code>/off 30m</code>, <code>/off 2h</code>, or plain <code>/off</code>."
            self._controls.paused_until = time.time() + seconds
            self._controls.enabled = True  # timed pause, not the master switch
            until = time.strftime("%-I:%M %p", time.localtime(self._controls.paused_until))
            return f"⏸ Paused until <b>{until}</b>. Nothing goes out before then. /on to end it early."
        self._controls.enabled = False
        return "⏹ Off. No replies on either platform until you send /on."

    def _platform(self, platform: str, argument: str) -> str:
        value = on_off(argument)
        if value is None:
            state = "on" if self._controls.platform_enabled(platform) else "off"
            return f"{PLATFORM_LABEL.get(platform, platform)} is <b>{state}</b>. Send <code>/{platform} on</code> or <code>/{platform} off</code>."
        self._controls.set_platform(platform, value)
        name = PLATFORM_LABEL.get(platform, platform)
        return f"{'✅' if value else '🚫'} {name} is now <b>{'on' if value else 'off'}</b>."

    def _toggle_dry_run(self, argument: str) -> str:
        value = on_off(argument)
        if value is None:
            state = "on" if self._controls.dry_run else "off"
            return f"Dry run is <b>{state}</b>. Send <code>/dryrun on</code> or <code>/dryrun off</code>."
        self._controls.dry_run = value
        return (
            "🧪 Dry run <b>on</b>. Replies are written and logged, never sent."
            if value
            else "📤 Dry run <b>off</b>. Replies go to real people now."
        )

    def _toggle_calls(self, argument: str) -> str:
        value = on_off(argument)
        if value is None:
            state = "on" if self._controls.calls_enabled else "off"
            return (
                f"Missed-call follow-ups are <b>{state}</b>. "
                "Send <code>/calls on</code> or <code>/calls off</code>."
            )
        self._controls.calls_enabled = value
        return (
            "✅ Missed-call follow-ups <b>on</b>."
            if value
            else "🚫 Missed-call follow-ups <b>off</b>. Missed calls get nothing."
        )

    def _allow(self, argument: str) -> str:
        if not argument:
            return "Who? Try <code>/allow Shujoy</code> or <code>/allow +8801787308210</code>"
        was_empty = not self._controls.effective_allow()
        if not self._controls.allow(argument):
            return f"<b>{html.escape(argument)}</b> is already on the allow list."

        who = html.escape(argument)
        if was_empty:
            # Going from empty to one entry silences everybody else. That is the
            # whole point of a whitelist and also the easiest way to mute the
            # agent by accident, so it never happens quietly.
            return (
                f"⚠️ <b>Allow list is now on</b>, with only <b>{who}</b> on it.\n\n"
                "<b>Everyone else is now ignored</b> — no follow-ups, no burst replies, "
                "for anyone not on this list.\n\n"
                f"/unallow {who} turns it back off, or /allowed to see the list."
            )
        return f"✅ <b>{who}</b> added. /allowed to see the list."

    def _unallow(self, argument: str) -> str:
        if not argument:
            return "Who? Try <code>/unallow Shujoy</code>"
        if not self._controls.unallow(argument):
            return (
                f"<b>{html.escape(argument)}</b> is not on the runtime allow list. "
                "/allowed to see it."
            )
        if not self._controls.effective_allow():
            return (
                f"✅ Removed <b>{html.escape(argument)}</b>. The allow list is empty "
                "again, so <b>everyone</b> gets replies."
            )
        return f"✅ Removed <b>{html.escape(argument)}</b>. /allowed to see the rest."

    def _name(self, argument: str) -> str:
        """`/name +8801609982884 Dr. Dinesh` — WhatsApp will not give this linked
        device your address book, so names have to come from you."""
        # "+880 1609-982884 Dr. Dinesh" - the number itself contains spaces and
        # dashes, so split on the first token that has a letter in it, not on
        # the first space.
        match = re.match(r"^\s*([+\d][\d\s()\-.]*?)(?:\s+([^\d].*))?$", argument)
        if not match:
            return (
                "Give me a number first: <code>/name +8801609982884 Dr. Dinesh</code>\n"
                "Leave the name off to clear it."
            )
        number, label = match.group(1), match.group(2) or ""
        jid = to_jid(number)
        if not jid:
            return (
                "Give me a number first: <code>/name +8801609982884 Dr. Dinesh</code>\n"
                "Leave the name off to clear it."
            )
        label = label.strip()
        set_name(jid, label)
        pretty = html.escape("+" + jid.split("@")[0])
        if not label:
            return f"Cleared the name for <code>{pretty}</code>."
        return f"✅ <code>{pretty}</code> is now <b>{html.escape(label)}</b>."

    def _names(self) -> str:
        names = saved_names()
        if not names:
            return (
                "No names set yet.\n\n"
                "WhatsApp does not share your address book with this linked device, "
                "so callers show as numbers until you label them:\n"
                "<code>/name +8801609982884 Dr. Dinesh</code>"
            )
        lines = ["<b>Names you have set</b>"]
        for jid, label in names.items():
            lines.append(f"• <b>{html.escape(label)}</b> <code>+{html.escape(jid.split('@')[0])}</code>")
        return "\n".join(lines)

    def _allowed(self) -> str:
        runtime = self._controls.allowed
        from_env = self._settings.allow
        if not runtime and not from_env:
            return "Allow list is empty, so <b>everyone</b> gets replies."
        lines = ["<b>Only these people get replies</b>"]
        for who in from_env:
            lines.append(f"• {html.escape(who)} <i>(from .env)</i>")
        for who in runtime:
            lines.append(f"• {html.escape(who)} — /unallow {html.escape(who)}")
        lines.append("")
        lines.append("<i>Everyone else is ignored.</i>")
        return "\n".join(lines)

    def _toggle_burst(self, argument: str) -> str:
        value = on_off(argument)
        if value is None:
            state = "on" if self._controls.burst_enabled else "off"
            return f"Burst replies are <b>{state}</b>. Send <code>/burst on</code> or <code>/burst off</code>."
        self._controls.burst_enabled = value
        return (
            f"✅ Burst replies <b>on</b> — fires at {self._settings.burst_threshold} unanswered messages."
            if value
            else "🚫 Burst replies <b>off</b>. Missed calls still get a follow-up."
        )

    def _status(self) -> str:
        c = self._controls
        lines = ["<b>Status</b>"]

        if not c.enabled:
            lines.append("⏹ <b>Off</b> — nothing will be sent (/on to resume)")
        elif c.paused_until > time.time():
            until = time.strftime("%-I:%M %p", time.localtime(c.paused_until))
            minutes = round((c.paused_until - time.time()) / 60)
            lines.append(f"⏸ <b>Paused</b> for {minutes} min, until {until}")
        elif c.dry_run:
            lines.append("🧪 <b>Dry run</b> — generating, not sending")
        else:
            lines.append("▶️ <b>Live</b> — replying for real")

        lines.append("")
        for platform in PLATFORMS:
            lines.append(
                f"{'✅' if c.platform_enabled(platform) else '🚫'} "
                f"{PLATFORM_LABEL.get(platform, platform)}"
            )

        lines.append("")
        lines.append(f"{'✅' if c.calls_enabled else '🚫'} missed-call follow-ups: "
                     f"<b>{'on' if c.calls_enabled else 'off'}</b>")
        lines.append(f"{'✅' if c.burst_enabled else '🚫'} burst replies: "
                     f"<b>{'on' if c.burst_enabled else 'off'}</b> "
                     f"(at {self._settings.burst_threshold} unanswered)")
        lines.append(f"cooldown: {self._settings.cooldown_minutes} min per contact")
        if self._settings.quiet_hours:
            windows = ", ".join(
                f"{h // 60:02d}:{h % 60:02d}-{e // 60:02d}:{e % 60:02d}"
                for h, e in self._settings.quiet_hours
            )
            lines.append(f"quiet hours: {windows}")
        allowed = c.effective_allow()
        if allowed:
            lines.append(
                f"⚠️ allow list active — <b>only</b> {html.escape(', '.join(allowed))} "
                "get replies"
            )

        blocked = c.blocked
        if blocked:
            lines.append(f"blocked from the bot: {html.escape(', '.join(blocked))}")

        rows = self._store.recent(limit=1)
        if rows:
            row = rows[0]
            what = "sent to" if row["sent"] else "skipped"
            lines.append("")
            lines.append(
                f"last: {what} {self._who(row)} "
                f"· {ago(time.time() - row['created_at'])}"
            )
        return "\n".join(lines)

    def _blocked(self) -> str:
        runtime = self._controls.blocked
        from_env = self._settings.block
        if not runtime and not from_env:
            return "Nobody is blocked."
        lines = ["<b>Blocked</b>"]
        for who in from_env:
            lines.append(f"• {html.escape(who)} <i>(from .env)</i>")
        for who in runtime:
            lines.append(f"• {html.escape(who)} — /unblock {html.escape(who)}")
        return "\n".join(lines)

    def _recent(self, argument: str) -> str:
        try:
            limit = max(1, min(int(argument), 25)) if argument.strip().isdigit() else 10
        except ValueError:
            limit = 10

        rows = self._store.recent(limit=limit)
        if not rows:
            return "Nothing yet — no calls or bursts have been decided on."

        lines = [f"<b>Last {len(rows)}</b>"]
        now = time.time()
        today = time.localtime(now).tm_yday
        for row in rows:
            stamp = time.localtime(row["created_at"])
            # "how long ago" is the question being asked; the clock time is
            # context, and only useful beyond today.
            clock = time.strftime("%-I:%M %p", stamp)
            when = clock if stamp.tm_yday == today else time.strftime("%-d %b, %-I:%M %p", stamp)
            when = f"{ago(now - row['created_at'])} · {when}"
            who = self._who(row)
            if row["sent"]:
                head = f"✅ {who} · {row['platform']} · {when}"
                body = f"\n   <i>{html.escape(row['text'][:120])}</i>"
            else:
                head = f"⏭ {who} · {row['platform']} · {when}"
                body = f"\n   skipped: {html.escape(row['skip_reason'] or 'unknown')}"
            lines.append(head + body)
        return "\n".join(lines)

    async def _reply(self, text: str) -> None:
        try:
            response = await self._client.post(
                "/sendMessage",
                json={
                    "chat_id": self._settings.notify_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            if response.status_code != 200:
                log.warning("command reply rejected: %s", response.text[:300])
        except Exception:
            log.warning("could not reply to a command", exc_info=True)
