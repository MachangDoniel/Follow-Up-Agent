"""Environment-driven settings, loaded once at startup and passed down explicitly."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_FALLBACK = "Sorry, I couldn't pick up just now — I'll get back to you shortly."

# Sent verbatim after several unanswered messages. Deliberately not generated:
# someone sending a burst may be upset or dealing with something urgent, and a
# fixed line cannot say the wrong thing.
DEFAULT_BURST_TEXT = (
    "Hi, this is an automated reply — I am busy at the moment and have not seen "
    "your messages yet. I will get back to you as soon as I can."
)


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true/false, got {raw!r}")


def _path(name: str, default: str) -> Path:
    path = Path(_str(name, default))
    return path if path.is_absolute() else PROJECT_ROOT / path


def _csv(name: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in _str(name).split(",") if p.strip())


def _windows(name: str) -> tuple[tuple[int, int], ...]:
    """Parse "23:00-07:00,13:00-14:00" into minute-of-day (start, end) pairs."""
    out: list[tuple[int, int]] = []
    for window in _csv(name):
        try:
            start_s, end_s = window.split("-")
            sh, sm = (int(x) for x in start_s.strip().split(":"))
            eh, em = (int(x) for x in end_s.strip().split(":"))
        except ValueError as exc:
            raise ConfigError(
                f"{name} entry {window!r} is not a HH:MM-HH:MM range"
            ) from exc
        if not (0 <= sh < 24 and 0 <= eh < 24 and 0 <= sm < 60 and 0 <= em < 60):
            raise ConfigError(f"{name} entry {window!r} has an out-of-range time")
        out.append((sh * 60 + sm, eh * 60 + em))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Settings:
    # LM Studio
    lmstudio_base_url: str
    lmstudio_model: str
    lmstudio_api_key: str
    lmstudio_timeout: float
    temperature: float
    max_tokens: int
    disable_thinking: bool

    # voice of the follow-up
    your_name: str
    persona: str
    fallback_text: str
    signoff: str
    max_chars: int
    history_messages: int

    # burst replies: "he's busy, this is his assistant" after N unanswered messages
    burst_enabled: bool
    burst_threshold: int
    burst_text: str
    burst_cooldown_hours: float

    # safety
    dry_run: bool
    cooldown_minutes: int
    reply_grace_seconds: float
    allow: tuple[str, ...]
    block: tuple[str, ...]
    quiet_hours: tuple[tuple[int, int], ...]

    # telegram
    telegram_enabled: bool
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session: Path

    # brain http (for the Node WhatsApp watcher)
    brain_host: str
    brain_port: int

    database_path: Path
    log_level: str
    log_file: Path

    @classmethod
    def load(cls, env_file: Path | None = None) -> Settings:
        """Read .env (if present) plus the real environment. Real env wins."""
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

        telegram_enabled = _bool("TELEGRAM_ENABLED", True)
        api_id_raw = _str("TELEGRAM_API_ID")
        if telegram_enabled and not api_id_raw:
            raise ConfigError(
                "TELEGRAM_API_ID is not set. Copy .env.example to .env and fill in the "
                "api_id/api_hash from https://my.telegram.org (or set "
                "TELEGRAM_ENABLED=false to run WhatsApp only)."
            )
        api_hash = _str("TELEGRAM_API_HASH")
        if telegram_enabled:
            if not api_hash:
                raise ConfigError("TELEGRAM_API_HASH is not set but TELEGRAM_ENABLED is true.")
            if not api_id_raw.isdigit():
                raise ConfigError(
                    f"TELEGRAM_API_ID must be the numeric api_id, got {api_id_raw!r}."
                )

        max_tokens = _int("MAX_TOKENS", 700)
        if max_tokens < 400:
            # Reasoning models spend hundreds of tokens thinking before writing a
            # word; a small budget silently yields an empty message.
            raise ConfigError(
                f"MAX_TOKENS={max_tokens} is too low. Local reasoning models such as "
                "gemma-4-e4b need ~700 to leave room for the actual reply."
            )

        burst_threshold = _int("BURST_THRESHOLD", 5)
        if burst_threshold < 2:
            raise ConfigError(
                f"BURST_THRESHOLD={burst_threshold} would auto-reply to almost every "
                "message. Use 3 or more."
            )

        max_chars = _int("MAX_CHARS", 300)
        signoff = _str("SIGNOFF")
        if signoff and len(signoff) + 40 > max_chars:
            raise ConfigError(
                f"SIGNOFF is {len(signoff)} chars but MAX_CHARS is only {max_chars}; "
                "there would be no room left for the message itself."
            )

        return cls(
            lmstudio_base_url=_str("LMSTUDIO_BASE_URL", "http://localhost:1234/v1").rstrip("/"),
            lmstudio_model=_str("LMSTUDIO_MODEL", "google/gemma-4-e4b"),
            lmstudio_api_key=_str("LMSTUDIO_API_KEY", "lm-studio"),
            lmstudio_timeout=_float("LMSTUDIO_TIMEOUT", 300.0),
            temperature=_float("TEMPERATURE", 0.7),
            max_tokens=max_tokens,
            disable_thinking=_bool("DISABLE_THINKING", True),
            your_name=_str("YOUR_NAME"),
            persona=_str("PERSONA"),
            fallback_text=_str("FALLBACK_TEXT", DEFAULT_FALLBACK),
            signoff=signoff,
            max_chars=max_chars,
            history_messages=_int("HISTORY_MESSAGES", 10),
            burst_enabled=_bool("BURST_REPLY_ENABLED", False),
            burst_threshold=burst_threshold,
            burst_text=_str("BURST_TEXT", DEFAULT_BURST_TEXT),
            burst_cooldown_hours=_float("BURST_COOLDOWN_HOURS", 6.0),
            dry_run=_bool("DRY_RUN", True),
            cooldown_minutes=_int("COOLDOWN_MINUTES", 30),
            reply_grace_seconds=_float("REPLY_GRACE_SECONDS", 30.0),
            allow=_csv("ALLOW"),
            block=_csv("BLOCK"),
            quiet_hours=_windows("QUIET_HOURS"),
            telegram_enabled=telegram_enabled,
            telegram_api_id=int(api_id_raw) if api_id_raw.isdigit() else 0,
            telegram_api_hash=api_hash,
            telegram_session=_path("TELEGRAM_SESSION", "data/telegram.session"),
            brain_host=_str("BRAIN_HOST", "127.0.0.1"),
            brain_port=_int("BRAIN_PORT", 8787),
            database_path=_path("DATABASE_PATH", "data/followups.sqlite3"),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
            log_file=_path("LOG_FILE", "logs/call-followup.log"),
        )
