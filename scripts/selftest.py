#!/usr/bin/env python3
"""Check the setup before trusting it with real calls.

    uv run python scripts/selftest.py

Verifies config, LM Studio, the database, the WhatsApp watcher's dependencies,
and end-to-end message generation on a fake missed call. Sends nothing.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import compose  # noqa: E402
from app.config import ConfigError, Settings  # noqa: E402
from app.llm import LLMError, LMStudio  # noqa: E402
from app.models import Message, MissedCall  # noqa: E402
from app.store import Store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

OK, WARN, BAD = "  ok  ", " warn ", " FAIL "
failures = 0


def report(status: str, message: str) -> None:
    global failures
    if status is BAD:
        failures += 1
    print(f"[{status}] {message}")


async def main() -> int:
    print("call-followup selftest\n")

    if not (ROOT / ".env").exists():
        report(WARN, ".env not found - using defaults. Copy .env.example to .env.")

    try:
        settings = Settings.load()
    except ConfigError as exc:
        report(BAD, f"config: {exc}")
        return 1
    report(OK, f"config loaded (dry_run={settings.dry_run})")

    if not settings.your_name:
        report(WARN, "YOUR_NAME is empty - generated messages will be generic")
    if settings.dry_run:
        report(WARN, "DRY_RUN is on - nothing will actually be sent")
    else:
        report(WARN, "DRY_RUN is OFF - this will message real people")
    if settings.telegram_enabled:
        report(OK, "Telegram enabled")
        if not settings.telegram_session.exists():
            report(WARN, "no Telegram session yet - first run will ask for a login code")
    else:
        report(WARN, "Telegram disabled (TELEGRAM_ENABLED=false)")

    # storage
    try:
        store = Store(settings.database_path)
        rows = store.recent(limit=1)
        store.close()
        report(OK, f"database writable at {settings.database_path.name} ({len(rows)} rows seen)")
    except Exception as exc:  # noqa: BLE001
        report(BAD, f"database: {exc}")

    # whatsapp watcher deps
    if (ROOT / "whatsapp" / "node_modules" / "@whiskeysockets" / "baileys").exists():
        report(OK, "WhatsApp watcher dependencies installed")
    else:
        report(WARN, "WhatsApp deps missing - run `npm install` in whatsapp/")

    # whatsapp pairing. creds.json is written the moment Baileys starts, well
    # before the QR is scanned, so its existence proves nothing - only a
    # populated "me" means the device is actually linked.
    creds_path = ROOT / "data" / "whatsapp-auth" / "creds.json"
    if not creds_path.exists():
        report(WARN, "WhatsApp not linked yet - run `npm start` in whatsapp/ and scan the QR")
    else:
        try:
            creds = json.loads(creds_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            report(WARN, f"WhatsApp auth unreadable ({exc}); delete data/whatsapp-auth to reset")
        else:
            me = creds.get("me") or {}
            if me.get("id"):
                report(OK, f"WhatsApp linked as {me.get('name') or me['id'].split(':')[0]}")
            else:
                report(WARN, "WhatsApp auth exists but is NOT linked - the QR was never scanned")

    # LM Studio
    llm = LMStudio(settings)
    try:
        models = await llm.models()
    except Exception as exc:  # noqa: BLE001
        report(BAD, f"LM Studio unreachable at {settings.lmstudio_base_url}: {exc}")
        await llm.aclose()
        return 1
    report(OK, f"LM Studio reachable, {len(models)} models loaded")
    if settings.lmstudio_model not in models:
        report(BAD, f"model {settings.lmstudio_model!r} not loaded. Available: {', '.join(models)}")
        await llm.aclose()
        return 1
    report(OK, f"model {settings.lmstudio_model} is loaded")

    # end-to-end generation on a fake call
    call = MissedCall(
        platform="telegram",
        contact_id="selftest",
        contact_name="Alice",
        reason="rejected",
        history=(
            Message(sender="them", text="hey are you free to talk about the invoice?"),
            Message(sender="me", text="yeah give me an hour"),
        ),
    )
    system, user = compose.build_prompt(settings, call)
    try:
        completion = await llm.complete(system, user)
    except LLMError as exc:
        report(BAD, f"generation failed: {exc}")
        await llm.aclose()
        return 1
    finally:
        await llm.aclose()

    text = compose.clean(completion.content, compose.body_budget(settings))
    if compose.is_unusable(text):
        report(BAD, f"generated text rejected by guardrail: {text!r}")
    else:
        report(OK, "generation works")
        final = compose.with_signoff(text, settings)
        if len(final) > settings.max_chars:
            report(BAD, f"final message is {len(final)} chars, over MAX_CHARS")
        print("\n  sample follow-up for a rejected call from Alice:")
        for line in final.split("\n"):
            print(f"    {line}")
        print()

    print("selftest failed\n" if failures else "all good\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
