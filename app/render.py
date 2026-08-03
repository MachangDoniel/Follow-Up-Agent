"""Draws the conversation as a PNG, so the notification shows you what a
screenshot would have shown you.

Deliberately not a real screen capture. `screencapture` needs the Mac awake and
unlocked - which is exactly what it is not when you are away from it and missing
calls - needs Screen Recording permission, only covers WhatsApp (there is no
Telegram desktop app in this setup), and grabs whichever chat happens to be open
rather than the one that matters. The watchers already keep the last N messages
per chat, so we draw those instead: always the right conversation, always
available, no permissions.

Pillow is optional. If it is missing the notifier falls back to text-only.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from .models import Message

log = logging.getLogger(__name__)

try:
    from PIL import Image, ImageDraw, ImageFont

    AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    AVAILABLE = False

WIDTH = 760
PADDING = 28
BUBBLE_PADDING = 16
BUBBLE_GAP = 10
BUBBLE_MAX = int(WIDTH * 0.72)
RADIUS = 16

BG = (17, 20, 24)
HEADER = (233, 237, 239)
SUBTLE = (134, 150, 160)
THEM_BG = (32, 44, 51)
THEM_FG = (233, 237, 239)
ME_BG = (0, 92, 75)
ME_FG = (233, 237, 239)
REPLY_BG = (17, 74, 105)
ACCENT = (37, 211, 102)

# Helvetica ships with macOS. Emoji live in a separate colour font that Pillow
# can only use at one fixed size, so emoji in a message draw as blank boxes -
# an acceptable trade for keeping this dependency-light.
FONT_CANDIDATES = (
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
)


def _font(size: int, bold: bool = False):
    for path in FONT_CANDIDATES:
        try:
            # Helvetica.ttc index 1 is Bold.
            return ImageFont.truetype(path, size, index=1 if bold else 0)
        except OSError:
            continue
    return ImageFont.load_default()


@dataclass(slots=True)
class _Line:
    """One laid-out bubble, measured before anything is drawn."""

    lines: list[str]
    side: str  # "them" | "me" | "reply"
    width: int
    height: int


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    out: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            out.append("")
            continue
        line = words[0]
        for word in words[1:]:
            candidate = f"{line} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                line = candidate
            else:
                out.append(line)
                line = word
        out.append(line)
    return out


def chat_card(
    *,
    title: str,
    subtitle: str,
    history: tuple[Message, ...],
    reply: str,
    reply_label: str,
    max_messages: int = 5,
) -> bytes | None:
    """Render a chat-style card. Returns PNG bytes, or None if Pillow is absent."""
    if not AVAILABLE:
        return None

    try:
        return _render(title, subtitle, history, reply, reply_label, max_messages)
    except Exception:
        # A notification is a convenience; never let drawing it break a send.
        log.warning("could not render the chat card", exc_info=True)
        return None


def _render(
    title: str,
    subtitle: str,
    history: tuple[Message, ...],
    reply: str,
    reply_label: str,
    max_messages: int,
) -> bytes:
    title_font = _font(24, bold=True)
    sub_font = _font(16)
    body_font = _font(19)
    tag_font = _font(14, bold=True)

    # Measure on a throwaway image: heights are only known after wrapping.
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    line_height = body_font.getbbox("Ag")[3] + 7
    text_max = BUBBLE_MAX - 2 * BUBBLE_PADDING

    blocks: list[_Line] = []
    for message in history[-max_messages:]:
        lines = _wrap(scratch, message.text, body_font, text_max)
        width = max(scratch.textlength(line, font=body_font) for line in lines)
        blocks.append(
            _Line(
                lines=lines,
                side="me" if message.sender == "me" else "them",
                width=int(width) + 2 * BUBBLE_PADDING,
                height=len(lines) * line_height + 2 * BUBBLE_PADDING,
            )
        )

    reply_lines = _wrap(scratch, reply, body_font, text_max)
    reply_width = max(scratch.textlength(line, font=body_font) for line in reply_lines)
    tag_height = tag_font.getbbox("Ag")[3] + 10
    blocks.append(
        _Line(
            lines=reply_lines,
            side="reply",
            width=max(int(reply_width), int(scratch.textlength(reply_label, font=tag_font)))
            + 2 * BUBBLE_PADDING,
            height=len(reply_lines) * line_height + 2 * BUBBLE_PADDING + tag_height,
        )
    )

    header_height = PADDING + 34 + 24 + 18
    body_height = sum(b.height + BUBBLE_GAP for b in blocks)
    height = header_height + body_height + PADDING

    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)

    draw.rectangle((0, 0, 5, height), fill=ACCENT)
    draw.text((PADDING, PADDING), title, font=title_font, fill=HEADER)
    draw.text((PADDING, PADDING + 34), subtitle, font=sub_font, fill=SUBTLE)
    rule_y = header_height - 12
    draw.line((PADDING, rule_y, WIDTH - PADDING, rule_y), fill=(40, 48, 54), width=1)

    y = header_height
    for block in blocks:
        left = PADDING if block.side == "them" else WIDTH - PADDING - block.width
        fill = {"them": THEM_BG, "me": ME_BG, "reply": REPLY_BG}[block.side]
        draw.rounded_rectangle(
            (left, y, left + block.width, y + block.height), radius=RADIUS, fill=fill
        )

        text_y = y + BUBBLE_PADDING
        if block.side == "reply":
            draw.text((left + BUBBLE_PADDING, text_y), reply_label, font=tag_font, fill=ACCENT)
            text_y += tag_height

        colour = THEM_FG if block.side == "them" else ME_FG
        for line in block.lines:
            draw.text((left + BUBBLE_PADDING, text_y), line, font=body_font, fill=colour)
            text_y += line_height

        y += block.height + BUBBLE_GAP

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
