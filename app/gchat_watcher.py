"""Google Chat bot watcher — receives @mention events via Google Cloud Pub/Sub.

Setup (one-time):
  1. Create a GCP project and enable the Chat API.
  2. Create a service account; give it the `chat.bot` OAuth scope.
     Download the JSON key to data/gchat-service-account.json.
  3. Configure the bot in Google Chat API console:
       - Connection: Cloud Pub/Sub
       - Pub/Sub topic: projects/<GCHAT_PROJECT_ID>/topics/<your-topic>
  4. Create a Pub/Sub subscription (push or pull) on that topic.
     This watcher uses *pull*, so no public HTTPS endpoint is needed.
  5. Add the bot to the Google Chat spaces where you want it to respond.
  6. Set GCHAT_ENABLED=true and fill in the env vars below.

Required .env vars:
  GCHAT_ENABLED=true
  GCHAT_PROJECT_ID=my-gcp-project
  GCHAT_PUBSUB_SUBSCRIPTION=projects/my-gcp-project/subscriptions/my-sub
  GCHAT_SERVICE_ACCOUNT_FILE=data/gchat-service-account.json
  GCHAT_BOT_NAME=mybot         # the @-name, lowercase, no @

The watcher continuously pulls messages from Pub/Sub, filters for MESSAGE events
that contain a @mention of the bot (the Chat API sets `annotations` with
USER_MENTION type and the bot's membership name), and POSTs to /mention on the
brain.  After the brain decides to reply, it sends the text back via the Chat
REST API (spaces.messages.create with thread key so it posts in-thread).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

import httpx

from .config import Settings
from .models import Message

log = logging.getLogger(__name__)

_BRAIN_URL = "http://{host}:{port}"
_CHAT_API = "https://chat.googleapis.com/v1"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPES_PUBSUB = "https://www.googleapis.com/auth/pubsub"
_SCOPES_CHAT = "https://www.googleapis.com/auth/chat.bot"


class GoogleChatWatcher:
    """Pulls @mention events from Pub/Sub and replies via the Chat REST API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._brain = _BRAIN_URL.format(
            host=settings.brain_host, port=settings.brain_port
        )
        self._sa_file = settings.gchat_service_account_file
        self._subscription = settings.gchat_pubsub_subscription
        self._bot_name = settings.gchat_bot_name.lower().lstrip("@")
        self._access_token_pubsub: str = ""
        self._token_expiry_pubsub: float = 0.0
        self._access_token_chat: str = ""
        self._token_expiry_chat: float = 0.0
        self._client = httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- OAuth2 service-account token ---------------------------------------

    async def _get_token(self, scope: str, cache_attr: str, expiry_attr: str) -> str:
        """Return a valid Bearer token for the given scope, refreshing when near expiry."""
        cached = getattr(self, cache_attr)
        expiry = getattr(self, expiry_attr)
        if time.time() < expiry - 60 and cached:
            return cached

        try:
            import json as _json

            sa = _json.loads(self._sa_file.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Cannot read service account file: {exc}") from exc

        # Build a signed JWT for the service account.
        import time as _time

        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:
            raise RuntimeError(
                "pip install cryptography  (needed for Google Chat JWT signing)"
            ) from exc

        now = int(_time.time())
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
        ).rstrip(b"=")
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "iss": sa["client_email"],
                    "scope": scope,
                    "aud": _TOKEN_URL,
                    "iat": now,
                    "exp": now + 3600,
                }
            ).encode()
        ).rstrip(b"=")
        msg = header + b"." + payload
        private_key = serialization.load_pem_private_key(
            sa["private_key"].encode(), password=None
        )
        sig = base64.urlsafe_b64encode(
            private_key.sign(msg, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[arg-type]
        ).rstrip(b"=")
        assertion = (msg + b"." + sig).decode()

        resp = await self._client.post(
            _TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        token = data["access_token"]
        setattr(self, cache_attr, token)
        setattr(self, expiry_attr, time.time() + data.get("expires_in", 3600))
        return token

    async def _get_pubsub_token(self) -> str:
        return await self._get_token(
            _SCOPES_PUBSUB,
            cache_attr="_access_token_pubsub",
            expiry_attr="_token_expiry_pubsub",
        )

    async def _get_chat_token(self) -> str:
        return await self._get_token(
            _SCOPES_CHAT,
            cache_attr="_access_token_chat",
            expiry_attr="_token_expiry_chat",
        )

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    # -- Pub/Sub pull -------------------------------------------------------

    async def _pull(self) -> list[dict[str, Any]]:
        """Pull up to 10 messages from the Pub/Sub subscription."""
        token = await self._get_pubsub_token()
        url = f"https://pubsub.googleapis.com/v1/{self._subscription}:pull"
        resp = await self._client.post(
            url,
            json={"maxMessages": 10},
            headers=self._auth_headers(token),
        )
        if resp.status_code == 200:
            return resp.json().get("receivedMessages", [])
        log.warning("Pub/Sub pull returned %s: %s", resp.status_code, resp.text[:200])
        return []

    async def _ack(self, ack_ids: list[str]) -> None:
        if not ack_ids:
            return
        token = await self._get_pubsub_token()
        url = f"https://pubsub.googleapis.com/v1/{self._subscription}:acknowledge"
        await self._client.post(
            url,
            json={"ackIds": ack_ids},
            headers=self._auth_headers(token),
        )

    # -- event parsing -------------------------------------------------------

    def _is_mention(self, event: dict) -> bool:
        """True if the event is a MESSAGE type directed at the bot (DM or @mention)."""
        if event.get("type") != "MESSAGE":
            return False

        space = event.get("space", {})
        # 1:1 Direct Messages in Google Chat are always directed at the bot
        space_type = space.get("type", "")
        if space_type in ("DIRECT_MESSAGE", "DM") or space.get("singleUserBotMention"):
            return True

        message = event.get("message", {})
        for annotation in message.get("annotations", []):
            if annotation.get("type") == "USER_MENTION":
                user = annotation.get("userMention", {}).get("user", {})
                # The bot's display name in the mention annotation
                display = user.get("displayName", "").lower()
                if self._bot_name in display or user.get("type") == "BOT":
                    return True
        # Also check plain text @mention or bot name if annotations are absent
        text = message.get("text", "").lower()
        return f"@{self._bot_name}" in text or self._bot_name in text

    def _parse_event(
        self, event: dict
    ) -> tuple[str, str, str, str, str, str, tuple[Message, ...]]:
        """Return (platform, contact_id, contact_name, group_id, group_name, text, history)."""
        message = event.get("message", {})
        sender = message.get("sender", {})
        space = event.get("space", {})

        contact_id = sender.get("name", "")  # e.g. users/12345
        contact_name = sender.get("displayName", "")
        group_id = space.get("name", "")  # e.g. spaces/AAAA
        group_name = space.get("displayName", "")
        text = message.get("text", "").strip()

        return "gchat", contact_id, contact_name, group_id, group_name, text, ()

    # -- send reply ---------------------------------------------------------

    async def _send_reply(self, space_name: str, thread_name: str, text: str) -> None:
        """Post a message in the same thread the mention came from."""
        token = await self._get_chat_token()
        url = f"{_CHAT_API}/{space_name}/messages"
        body: dict[str, Any] = {"text": text}
        if thread_name:
            body["thread"] = {"name": thread_name}
        resp = await self._client.post(
            url,
            json=body,
            headers=self._auth_headers(token),
        )
        if resp.status_code not in (200, 201):
            log.warning(
                "Chat API reply failed: %s %s", resp.status_code, resp.text[:200]
            )

    # -- brain call ---------------------------------------------------------

    async def _call_brain(
        self,
        contact_id: str,
        contact_name: str,
        group_id: str,
        group_name: str,
        message_text: str,
        history: tuple[Message, ...],
    ) -> str | None:
        """POST to /mention. Returns the text to send, or None if we should skip."""
        payload = {
            "platform": "gchat",
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
                "gchat mention skipped (%s): %s",
                contact_name or contact_id,
                data.get("skip_reason", ""),
            )
        except Exception as exc:
            log.warning("brain /mention call failed: %s", exc)
        return None

    # -- main loop ----------------------------------------------------------

    async def run(self) -> None:
        log.info(
            "Google Chat watcher started — subscription: %s", self._subscription
        )
        while True:
            try:
                messages = await self._pull()
            except Exception as exc:
                log.warning("Pub/Sub pull error: %s — retrying in 10 s", exc)
                await asyncio.sleep(10)
                continue

            ack_ids: list[str] = []
            for msg in messages:
                ack_ids.append(msg["ackId"])
                try:
                    raw_data = base64.b64decode(msg["message"]["data"]).decode()
                    event = json.loads(raw_data)
                except (KeyError, ValueError) as exc:
                    log.debug("Could not decode Pub/Sub message: %s", exc)
                    continue

                if not self._is_mention(event):
                    continue

                (
                    platform,
                    contact_id,
                    contact_name,
                    group_id,
                    group_name,
                    text,
                    history,
                ) = self._parse_event(event)

                log.info(
                    "gchat @mention from %s in %s",
                    contact_name or contact_id,
                    group_name or group_id,
                )

                reply = await self._call_brain(
                    contact_id, contact_name, group_id, group_name, text, history
                )
                if reply:
                    space_name = event.get("space", {}).get("name", "")
                    thread_name = (
                        event.get("message", {})
                        .get("thread", {})
                        .get("name", "")
                    )
                    await self._send_reply(space_name, thread_name, reply)

            await self._ack(ack_ids)
            # Poll every 2 s — fast enough to feel responsive, slow enough
            # not to burn Pub/Sub quota.
            await asyncio.sleep(2)
