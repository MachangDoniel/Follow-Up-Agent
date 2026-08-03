#!/usr/bin/env python3
"""Send yourself one fake notification, so you can check the setup without
waiting to miss a real call.

    uv run python scripts/test_notify.py

Writes the rendered card to data/notify-preview.png either way, so you can look
at it even before the bot token is filled in.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import render  # noqa: E402
from app.config import ConfigError, Settings  # noqa: E402
from app.models import Message, SendEvent  # noqa: E402
from app.notify import Notifier  # noqa: E402

# Your own number, so the "Open the chat" link lands in a chat that certainly
# exists. An invented number makes WhatsApp answer "isn't on WhatsApp", which
# looks like a broken link when the link is fine.
SAMPLE = SendEvent(
    platform="whatsapp",
    contact_id="8801893097217@s.whatsapp.net",
    contact_name="Shujoy",
    kind="call",
    text="hey sorry, couldn't pick up just now — in the middle of something. "
    "i'll call you back in a bit.",
    occurred_at=time.time() - 240,
    reason="missed",
    history=(
        Message(sender="them", text="bhai are you free tonight?"),
        Message(sender="me", text="probably, why?"),
        Message(sender="them", text="thinking of going out, let me call you"),
        Message(sender="them", text="calling"),
    ),
)


async def main() -> int:
    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    preview = Path(__file__).resolve().parent.parent / "data" / "notify-preview.png"
    card = render.chat_card(
        title=f"{SAMPLE.headline} — {SAMPLE.contact_name}",
        subtitle="WhatsApp · just now",
        history=SAMPLE.history,
        reply=SAMPLE.text,
        reply_label="AUTO-REPLY SENT",
        max_messages=settings.notify_history,
    )
    if card:
        preview.write_bytes(card)
        print(f"card written to {preview}")
    else:
        print("Pillow is not installed - notifications will be text only (run: uv sync)")

    notifier = Notifier(settings)
    if not notifier.enabled:
        print("NOTIFY_ENABLED is false, so nothing was sent. Fill in the bot token,")
        print("set NOTIFY_ENABLED=true in .env, and run this again.")
        return 0

    try:
        await notifier.notify(SAMPLE)
        print("sent - check the chat with your bot.")
        print("Nothing there? The log line above says why (bad token, wrong chat id,")
        print("or you never sent the bot a first message).")
    finally:
        await notifier.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
