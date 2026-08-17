"""The fixed replies, and picking one that is not the last one you sent.

A missed call with no chat history gives the model nothing to work from, so
`decide.py` skips it and sends a fixed line. That was one hard-coded sentence,
which meant anyone who called twice got the identical message twice - the single
clearest way to look like a bot.

The pool lives in a text file rather than in code so it can be edited without a
deploy, and split by register so honorific contacts get the same formality the
model is instructed to use for them.
"""

from __future__ import annotations

import logging
import random
import re
from pathlib import Path

log = logging.getLogger(__name__)

CASUAL = "casual"
FORMAL = "formal"
BURST = "burst"
MENTION = "mention"

# Used when YOUR_NAME is empty, so a burst line stays grammatical.
ANONYMOUS = "the person you're trying to reach"


def load(path: Path) -> dict[str, tuple[str, ...]]:
    """Parse the pool file into {register: lines}. Missing file yields {}."""
    pools: dict[str, list[str]] = {CASUAL: [], FORMAL: [], BURST: []}
    current = CASUAL
    try:
        text = path.read_text()
    except OSError:
        return {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().lower()
            pools.setdefault(current, [])
            continue
        pools[current].append(line)

    return {name: tuple(lines) for name, lines in pools.items() if lines}


class Fallbacks:
    """Picks a line, avoiding the one used last so a repeat call reads human."""

    def __init__(self, pools: dict[str, tuple[str, ...]], default: str) -> None:
        self._pools = pools
        self._default = default
        self._last: dict[str, str] = {}

    @property
    def count(self) -> int:
        return sum(len(lines) for lines in self._pools.values())

    def pick(self, register: str = CASUAL, *, your_name: str = "") -> str:
        options = self._pools.get(register) or self._pools.get(CASUAL) or ()
        if not options:
            return self._default
        if len(options) == 1:
            return self._personalise(options[0], your_name)

        # Avoid an immediate repeat. Sampling until it differs would be fine
        # with 20 options and unbounded with 2, so exclude it outright.
        last = self._last.get(register)
        choices = [line for line in options if line != last] or list(options)
        chosen = random.choice(choices)
        self._last[register] = chosen
        return self._personalise(chosen, your_name)

    @staticmethod
    def _personalise(text: str, your_name: str) -> str:
        """Substitute {name}, capitalising the stand-in when it opens a sentence.

        A real name is already capitalised; ANONYMOUS is a common noun, so
        "This is an automatic reply. the person you're trying to reach..."
        needs fixing and "— the person you're trying to reach" does not.
        """
        if your_name:
            return text.replace("{name}", your_name)

        def substitute(match: re.Match[str]) -> str:
            before = text[: match.start()].rstrip()
            opens = not before or before[-1] in ".!?"
            return ANONYMOUS[0].upper() + ANONYMOUS[1:] if opens else ANONYMOUS

        return re.sub(r"\{name\}", substitute, text)
