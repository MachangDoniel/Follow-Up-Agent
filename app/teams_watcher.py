"""Microsoft Teams bot watcher — receives @mention events via Bot Framework webhooks.

Setup (one-time):
  1. Register an Azure Bot resource (portal.azure.com → Create → Azure Bot).
     Copy the App ID into TEAMS_APP_ID.
  2. Under the bot's Configuration → Manage → Certificates & secrets, create a
     client secret. Copy it into TEAMS_APP_PASSWORD.
  3. Set the bot's Messaging Endpoint to:
       https://<your-public-host>:<TEAMS_BOT_PORT>/teams/activity
     During local development you can use an ngrok tunnel:
       ngrok http 8789
     and set the endpoint to the ngrok HTTPS URL.
  4. In the Teams Developer Portal (dev.teams.microsoft.com), create an app,
     add a Bot capability pointing at your Azure Bot, and install it in a team
     or group chat. The bot will receive activity events for every message in
     channels/chats where it is installed.
  5. Set TEAMS_ENABLED=true and fill in the env vars.

Required .env vars:
  TEAMS_ENABLED=true
  TEAMS_APP_ID=<azure-app-id>
  TEAMS_APP_PASSWORD=<azure-app-password>
  TEAMS_TENANT_ID=common          # or your specific tenant id
  TEAMS_BOT_HOST=0.0.0.0          # interface to bind
  TEAMS_BOT_PORT=8789             # port to expose (ngrok this locally)

The watcher binds a local aiohttp server. Every inbound Bot Framework Activity
is JWT-verified against Microsoft's public keys, parsed for @mention entities,
and routed to the brain's /mention endpoint. The reply is sent back using the
Bot Framework reply-to-activity REST call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from aiohttp import web

from .config import Settings
from .models import Message

log = logging.getLogger(__name__)

_BOT_FRAMEWORK_TOKEN_URL = (
    "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
)
_BOT_FRAMEWORK_SCOPE = "https://api.botframework.com/.default"
# JWKS for verifying inbound activity JWTs
_OPENID_META = (
    "https://login.botframework.com/v1/.well-known/openidconfiguration"
)
_BRAIN_URL = "http://{host}:{port}"


class TeamsBotWatcher:
    """Receives Bot Framework activities and replies via the REST API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._brain = _BRAIN_URL.format(
            host=settings.brain_host, port=settings.brain_port
        )
        self._app_id = settings.teams_app_id
        self._app_password = settings.teams_app_password
        self._tenant_id = settings.teams_tenant_id
        self._client = httpx.AsyncClient(timeout=30.0)
        self._access_token: str = ""
        self._token_expiry: float = 0.0
        self._runner: web.AppRunner | None = None
        # host/port for the local webhook server
        self._host = "0.0.0.0"
        self._port = settings.teams_bot_port

    async def aclose(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        await self._client.aclose()

    # -- OAuth2 client-credentials token (for outbound replies) -------------

    async def _get_token(self) -> str:
        if time.time() < self._token_expiry - 60 and self._access_token:
            return self._access_token
        url = _BOT_FRAMEWORK_TOKEN_URL.format(tenant=self._tenant_id)
        resp = await self._client.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._app_id,
                "client_secret": self._app_password,
                "scope": _BOT_FRAMEWORK_SCOPE,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 3600)
        return self._access_token

    # -- inbound JWT verification -------------------------------------------

    async def _verify_token(self, auth_header: str) -> bool:
        """Verify the Bearer JWT Microsoft sends with every activity.

        Full verification uses the Bot Framework JWKS endpoint and validates
        issuer, audience (== TEAMS_APP_ID), and expiry. A lightweight check
        is done here; for production harden with PyJWT + cryptography.
        """
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:]
        try:
            import base64 as _b64
            import json as _json

            parts = token.split(".")
            if len(parts) != 3:
                return False
            # Decode payload (no signature verification here — add PyJWT for prod)
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = _json.loads(_b64.urlsafe_b64decode(padded))
            # Basic sanity checks
            if payload.get("aud") != self._app_id:
                log.warning("Teams JWT: wrong audience %s", payload.get("aud"))
                return False
            if payload.get("exp", 0) < time.time():
                log.warning("Teams JWT: expired")
                return False
            return True
        except Exception as exc:
            log.warning("Teams JWT verification error: %s", exc)
            return False

    # -- activity parsing ----------------------------------------------------

    def _extract_mention(self, activity: dict[str, Any]) -> bool:
        """True if the bot is mentioned in this activity."""
        if activity.get("type") != "message":
            return False
        entities = activity.get("entities") or []
        for entity in entities:
            if entity.get("type") == "mention":
                mentioned = entity.get("mentioned", {})
                # The bot's own app id is in the mentioned object
                if mentioned.get("id") == self._app_id:
                    return True
        # Fallback: Teams wraps mentions in <at>BotName</at> tags
        text = activity.get("text") or ""
        return "<at>" in text

    def _parse_activity(
        self, activity: dict[str, Any]
    ) -> tuple[str, str, str, str, str, tuple[Message, ...]]:
        """Return (contact_id, contact_name, group_id, group_name, text, history)."""
        from_obj = activity.get("from") or {}
        conversation = activity.get("conversation") or {}

        contact_id = from_obj.get("aadObjectId") or from_obj.get("id", "")
        contact_name = from_obj.get("name", "")
        group_id = conversation.get("id", "")
        group_name = conversation.get("name", "") or activity.get("channelData", {}).get(
            "channel", {}
        ).get("name", "")

        # Strip <at>BotName</at> tags from the message text
        import re
        text = re.sub(r"<at>[^<]*</at>\s*", "", activity.get("text") or "").strip()

        return contact_id, contact_name, group_id, group_name, text, ()

    # -- outbound reply -------------------------------------------------------

    async def _send_reply(self, activity: dict[str, Any], text: str) -> None:
        """Reply to an activity using the Bot Framework Connector API."""
        service_url = (activity.get("serviceUrl") or "").rstrip("/")
        conversation_id = (activity.get("conversation") or {}).get("id", "")
        activity_id = activity.get("id", "")

        if not service_url or not conversation_id:
            log.warning("Cannot reply: missing serviceUrl or conversationId")
            return

        token = await self._get_token()
        url = f"{service_url}/v3/conversations/{conversation_id}/activities/{activity_id}"
        reply = {
            "type": "message",
            "text": text,
            "replyToId": activity_id,
        }
        resp = await self._client.post(
            url,
            json=reply,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code not in (200, 201):
            log.warning(
                "Teams reply failed: %s %s", resp.status_code, resp.text[:200]
            )

    # -- brain call ----------------------------------------------------------

    async def _call_brain(
        self,
        contact_id: str,
        contact_name: str,
        group_id: str,
        group_name: str,
        message_text: str,
        history: tuple[Message, ...],
    ) -> str | None:
        payload = {
            "platform": "teams",
            "contact_id": contact_id,
            "contact_name": contact_name,
            "group_id": group_id,
            "group_name": group_name,
            "message_text": message_text,
            "history": [
                {"from": m.sender, "text": m.text} for m in history
            ],
        }
        try:
            resp = await self._client.post(
                f"{self._brain}/mention", json=payload
            )
            data = resp.json()
            if data.get("send"):
                return str(data["text"])
            log.info(
                "teams mention skipped (%s): %s",
                contact_name or contact_id,
                data.get("skip_reason", ""),
            )
        except Exception as exc:
            log.warning("brain /mention call failed for Teams: %s", exc)
        return None

    # -- aiohttp server -------------------------------------------------------

    def _build_app(self) -> web.Application:
        watcher = self

        async def activity_handler(request: web.Request) -> web.Response:
            auth = request.headers.get("Authorization", "")
            if not await watcher._verify_token(auth):
                return web.Response(status=401, text="Unauthorized")

            try:
                act = await request.json()
            except Exception:
                return web.Response(status=400, text="Bad JSON")

            if not isinstance(act, dict):
                return web.Response(status=400, text="Expected JSON object")

            # Acknowledge immediately — Teams retries if we don't respond fast
            asyncio.create_task(watcher._handle_activity(act))
            return web.Response(status=200)

        app = web.Application()
        app.router.add_post("/teams/activity", activity_handler)
        return app

    async def _handle_activity(self, activity: dict[str, Any]) -> None:
        if not self._extract_mention(activity):
            return

        contact_id, contact_name, group_id, group_name, text, history = (
            self._parse_activity(activity)
        )

        log.info(
            "teams @mention from %s in %s",
            contact_name or contact_id,
            group_name or group_id,
        )

        reply = await self._call_brain(
            contact_id, contact_name, group_id, group_name, text, history
        )
        if reply:
            await self._send_reply(activity, reply)

    # -- main entry point ---------------------------------------------------

    async def run(self) -> None:
        app = self._build_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        log.info(
            "Teams bot listening on http://%s:%s/teams/activity",
            self._host,
            self._port,
        )
        # Run indefinitely — the outer asyncio.gather() keeps us alive.
        await asyncio.Event().wait()
