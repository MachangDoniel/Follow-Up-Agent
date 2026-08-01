"""Thin async client for the local LM Studio OpenAI-compatible endpoint."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """The model could not be reached or produced nothing usable."""


@dataclass(frozen=True, slots=True)
class Completion:
    content: str
    finish_reason: str


class LMStudio:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.lmstudio_base_url,
            timeout=settings.lmstudio_timeout,
            headers={"Authorization": f"Bearer {settings.lmstudio_api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def models(self) -> list[str]:
        response = await self._client.get("/models")
        response.raise_for_status()
        return [m["id"] for m in response.json().get("data", [])]

    async def _post(self, payload: dict) -> dict:
        try:
            response = await self._client.post("/chat/completions", json=payload)
            response.raise_for_status()
            return response.json()["choices"][0]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"LM Studio request failed: {exc}") from exc

    async def complete(self, system: str, user: str) -> Completion:
        payload = {
            "model": self._settings.lmstudio_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self._settings.temperature,
            "max_tokens": self._settings.max_tokens,
            "stream": False,
        }
        # Reasoning models spend most of their time thinking. Measured on
        # gemma-4-e4b: 422 reasoning tokens / 21.9s with thinking on, 131 / 7.6s
        # with it off, for the same quality of two-sentence reply.
        if self._settings.disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        try:
            choice = await self._post(payload)
        except LLMError:
            if "chat_template_kwargs" not in payload:
                raise
            # Not every chat template accepts the flag; fall back to a plain call
            # rather than degrading to the fixed text on every single call.
            log.warning("disabling thinking was rejected; retrying without it")
            payload.pop("chat_template_kwargs")
            choice = await self._post(payload)

        # Reasoning models put their scratchpad in reasoning_content and leave
        # content empty if the token budget ran out mid-thought.
        content = choice.get("message", {}).get("content") or ""
        finish = choice.get("finish_reason") or ""


        if not content.strip() and finish == "length":
            raise LLMError(
                f"model hit the {self._settings.max_tokens}-token limit while "
                "reasoning and never wrote a reply - raise MAX_TOKENS"
            )
        if not content.strip():
            raise LLMError(f"model returned empty content (finish_reason={finish!r})")

        return Completion(content=content, finish_reason=finish)
