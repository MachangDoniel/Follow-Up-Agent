"""Turning a missed call into the text of a follow-up message."""

from __future__ import annotations

import re
import time

from .config import Settings
from .models import REASON_TEXT, MessageBurst, MissedCall

SYSTEM_PROMPT = """You write short follow-up text messages on behalf of {name}, \
who has just failed to take an incoming call.

Rules:
- Output ONLY the message text. No quotation marks, no preamble, no explanation.
- One or two short sentences. Under {max_chars} characters.
- Sound like a real person typing on their phone, not a receptionist or an AI.
- Write in the same language AND the same script they use. If they write \
Bengali in Latin letters, reply in Latin letters too - do not switch to \
Bengali script. If there is no conversation history, use English.
- Acknowledge missing the call and offer a light next step.
- NEVER state a reason for missing the call. You do not know the reason. Do not \
say {name} was busy, asleep, driving, in a meeting, or dealing with an emergency \
- writing any of those would be telling the caller something untrue.
- Never invent facts: no appointments, no times, no promises about when {name} \
will call back.
- Apologise at most once, briefly.{signoff_rule}{persona}"""

SIGNOFF_RULE = (
    "\n- Do not sign the message or put a name at the end. A signature is "
    "appended automatically."
)

BURST_SYSTEM_PROMPT = """You are the personal assistant of {name}, writing on \
their behalf. Someone has sent {name} several messages in a row and {name} has \
not seen them yet.

Rules:
- Output ONLY the message text. No quotation marks, no preamble, no explanation.
- One or two short sentences. Under {max_chars} characters.
- Make clear you are replying on {name}'s behalf, not as {name}.
- Say {name} is busy right now and will get back to them.
- You may acknowledge the general topic, but do NOT answer their questions, \
agree to anything, or make any commitment for {name}. You do not know the answers.
- Write in the same language AND the same script they use. If they write \
Bengali in Latin letters, reply in Latin letters too - do not switch to \
Bengali script. If unclear, use English.
- Never invent facts: not where {name} is, not what they are doing, not when \
they will reply.{formal}"""

# Names carrying a respectful honorific get a formal register instead of the
# casual default. Matched as whole words, never substrings: "di" and "da" are
# two letters and would otherwise fire on Nadia, Sandip, Adam, Dawood...
HONORIFICS = frozenset(
    {
        "sir", "mam", "ma'am", "madam",
        "bhai", "vai", "bhaiya", "bhaia",
        "dada", "da", "didi", "di",
        "apu", "apa", "khala", "chacha", "mama", "kaku",
    }
)

NAME_TOKENS = re.compile(r"[^a-z']+")

FORMAL_RULE = (
    "\n- This contact is addressed with a respectful honorific, so write in a "
    "polite, formal register: complete sentences, proper capitalisation, no "
    "slang, no abbreviations. Courteous but still brief."
)


def is_formal(contact_name: str) -> bool:
    """True if the saved name carries an honorific like Bhai, Dada, Sir, Didi."""
    tokens = {t for t in NAME_TOKENS.split((contact_name or "").lower()) if t}
    return bool(tokens & HONORIFICS)


# Some reasoning models inline their scratchpad instead of splitting it out.
THINK_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)
CODE_FENCE = re.compile(r"^```[a-zA-Z]*\n?|```$")

# AI tells, plus unfilled template placeholders. The placeholder half must stay
# outside the \b group: "[" after a space is not a word boundary.
BAD_OUTPUT = re.compile(
    r"\b(?:as an ai|as an assistant|language model|i cannot|i can't help|"
    r"here is the message|here's the message)\b"
    r"|\[(?:your[ _]?name|name|contact|caller|insert[^\]]*)\]",
    re.I,
)


def build_prompt(settings: Settings, call: MissedCall) -> tuple[str, str]:
    name = settings.your_name or "the user"
    contact = call.contact_name or "an unknown contact"

    # A casual persona ("lowercase is fine") would fight the formal register,
    # so for honorific contacts the formal rule replaces it rather than joining it.
    if is_formal(contact):
        tone = FORMAL_RULE
    else:
        persona = settings.persona.strip()
        tone = f"\n- {persona}" if persona else ""

    system = SYSTEM_PROMPT.format(
        name=name,
        max_chars=body_budget(settings),
        signoff_rule=SIGNOFF_RULE if settings.signoff else "",
        persona=tone,
    )

    what_happened = REASON_TEXT.get(call.reason, "the call was not answered")
    kind = "video call" if call.video else "call"

    lines = [
        f"Incoming {call.platform.capitalize()} {kind} from: {contact}",
        f"What happened: {what_happened}.",
        f"Local time now: {time.strftime('%A %H:%M')}.",
        "",
    ]

    history = call.history[-settings.history_messages :]
    if history:
        lines.append("Most recent messages in this chat (oldest first):")
        for message in history:
            who = name if message.sender == "me" else contact
            text = message.text.replace("\n", " ").strip()
            if text:
                lines.append(f"{who}: {text[:400]}")
    else:
        lines.append("There is no previous conversation with this contact.")

    lines += ["", f"Write the follow-up message {name} should send now."]
    return system, "\n".join(lines)


def build_burst_prompt(settings: Settings, burst: MessageBurst) -> tuple[str, str]:
    name = settings.your_name or "the user"
    contact = burst.contact_name or "an unknown contact"
    system = BURST_SYSTEM_PROMPT.format(
        name=name,
        max_chars=settings.max_chars,
        formal=FORMAL_RULE if is_formal(contact) else "",
    )

    lines = [
        f"{contact} has sent {burst.count} messages on "
        f"{burst.platform.capitalize()} with no reply from {name}.",
        f"Local time now: {time.strftime('%A %H:%M')}.",
        "",
    ]

    history = burst.history[-settings.history_messages :]
    if history:
        lines.append("Their recent messages (oldest first):")
        for message in history:
            who = name if message.sender == "me" else contact
            text = message.text.replace("\n", " ").strip()
            if text:
                lines.append(f"{who}: {text[:400]}")

    lines += ["", f"Write the holding reply to send {contact} on {name}'s behalf."]
    return system, "\n".join(lines)


def clean(raw: str, max_chars: int) -> str:
    """LLMs love wrapping the answer in quotes or a 'Here you go:' line."""
    text = THINK_BLOCK.sub("", raw or "").strip()
    text = CODE_FENCE.sub("", text).strip()

    lines = [line for line in text.split("\n") if line.strip()]
    # drop a leading "Here's a message:" style line when real content follows
    if len(lines) > 1 and lines[0].rstrip().endswith(":") and len(lines[0]) < 80:
        lines = lines[1:]
    text = " ".join(line.strip() for line in lines).strip()

    if len(text) >= 2 and text[0] in "\"'“‘" and text[-1] in "\"'”’":
        text = text[1:-1].strip()

    if len(text) > max_chars:
        cut = text[:max_chars]
        stop = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        text = (cut[: stop + 1] if stop > max_chars * 0.5 else cut.rstrip()).strip()
    return text


def is_unusable(text: str) -> bool:
    return len(text) < 4 or bool(BAD_OUTPUT.search(text))


# --------------------------------------------------------------------------
# sign-off
# --------------------------------------------------------------------------

NON_ALNUM = re.compile(r"[^a-z0-9]+")


def body_budget(settings: Settings) -> int:
    """Characters left for the message once the signature is accounted for."""
    if not settings.signoff:
        return settings.max_chars
    return max(40, settings.max_chars - len(settings.signoff) - 1)


def _already_signed(text: str, signoff: str) -> bool:
    """True if the model signed it anyway, so we don't stamp the name twice."""
    name = NON_ALNUM.sub("", signoff.lower())
    if not name:
        return False
    tail = NON_ALNUM.sub("", text.lower())[-len(name) :]
    return tail == name


def with_signoff(text: str, settings: Settings) -> str:
    """Append the signature on its own line, unless it is already there."""
    signoff = settings.signoff
    if not signoff or not text or _already_signed(text, signoff):
        return text
    return f"{text}\n{signoff}"
