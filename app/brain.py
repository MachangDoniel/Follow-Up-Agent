"""HTTP front door for watchers that don't live in this process (the Node
WhatsApp watcher). The Telegram watcher calls the Decider directly.

  GET  /health
  GET  /recent
  POST /followup  {platform, contact_id, contact_name, reason, video, history[]}
               -> {send, text, skip_reason, used_fallback}
"""

from __future__ import annotations

import logging

from aiohttp import web

from .config import Settings
from .decide import Decider
from .models import Message, MessageBurst, MissedCall
from .store import Store

log = logging.getLogger(__name__)

VALID_REASONS = {"missed", "rejected", "busy", "cancelled", "disconnected"}


def parse_call(payload: dict) -> MissedCall:
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")

    platform = str(payload.get("platform") or "unknown")
    reason = str(payload.get("reason") or "missed")
    if reason not in VALID_REASONS:
        reason = "missed"

    return MissedCall(
        platform=platform,
        contact_id=str(payload.get("contact_id") or ""),
        contact_name=str(payload.get("contact_name") or ""),
        reason=reason,
        video=bool(payload.get("video")),
        history=parse_history(payload),
    )


def parse_history(payload: dict) -> tuple[Message, ...]:
    history = []
    for item in payload.get("history") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            sender = "me" if item.get("from") == "me" else "them"
            history.append(Message(sender=sender, text=text))
    return tuple(history)


def build_app(settings: Settings, decider: Decider, store: Store) -> web.Application:
    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "dry_run": settings.dry_run})

    async def recent(_request: web.Request) -> web.Response:
        return web.json_response(
            {"followups": [dict(row) for row in store.recent(limit=20)]}
        )

    async def followup(request: web.Request) -> web.Response:
        try:
            call = parse_call(await request.json())
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": f"bad request: {exc}"}, status=400)

        try:
            decision = await decider.decide(call)
        except Exception:
            # Never leave a watcher hanging on a bug in here.
            log.exception("decide() failed for %s", call.label)
            return web.json_response(
                {"send": False, "text": "", "skip_reason": "internal error"}
            )
        return web.json_response(decision.as_json())

    async def burst(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
            event = MessageBurst(
                platform=str(payload.get("platform") or "unknown"),
                contact_id=str(payload.get("contact_id") or ""),
                contact_name=str(payload.get("contact_name") or ""),
                count=int(payload.get("count") or 0),
                history=parse_history(payload),
            )
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": f"bad request: {exc}"}, status=400)

        try:
            decision = await decider.decide_burst(event)
        except Exception:
            log.exception("decide_burst() failed for %s", event.label)
            return web.json_response(
                {"send": False, "text": "", "skip_reason": "internal error"}
            )
        return web.json_response(decision.as_json())

    app = web.Application()
    app.add_routes(
        [
            web.get("/health", health),
            web.get("/recent", recent),
            web.post("/followup", followup),
            web.post("/burst", burst),
        ]
    )
    return app


async def serve(settings: Settings, decider: Decider, store: Store) -> web.AppRunner:
    runner = web.AppRunner(build_app(settings, decider, store), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.brain_host, settings.brain_port)
    await site.start()
    log.info("brain listening on http://%s:%s", settings.brain_host, settings.brain_port)
    return runner
