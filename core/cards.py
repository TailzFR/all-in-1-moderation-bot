from __future__ import annotations

import io
import logging
import math
from datetime import datetime, timezone
from typing import Any

import config
from core import fonts

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = ImageDraw = None

log = logging.getLogger("manager.cards")

SCALE = 2
WIDTH = 960
HEIGHT = 376
PAD = 40

BACKGROUND = (30, 31, 34)
BORDER = (63, 65, 71)
TEXT = (242, 243, 245)
BODY = (219, 222, 225)
MUTED = (148, 155, 164)
FAINT = (128, 132, 142)
STAR_ON = (240, 178, 50)
STAR_OFF = (78, 80, 88)

def available() -> bool:
    return Image is not None and config.TICKET_CARDS_ENABLED


def filename(ticket_id: int | str) -> str:
    return f"ticket-{ticket_id}-summary.png"


def _font(size: int, bold: bool = False) -> Any:
    return fonts.load(size * SCALE, bold)


def _rgb(value: int) -> tuple[int, int, int]:
    return (value >> 16) & 255, (value >> 8) & 255, value & 255


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(round(x * amount + y * (1 - amount)) for x, y in zip(a, b))


def _compact(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "None"
    total = int(seconds)
    if total < 60:
        return "Under 1m"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = [(days, "d"), (hours, "h"), (minutes, "m")]
    shown = [f"{value}{unit}" for value, unit in parts if value][:2]
    return " ".join(shown)


def _stamp(value: float | None) -> str:
    if not value:
        return "Unknown"
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%d %b %Y, %H:%M UTC")


class _Canvas:
    def __init__(self) -> None:
        self.image = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), BACKGROUND)
        self.draw = ImageDraw.Draw(self.image)

    @staticmethod
    def _px(*values: float) -> list[float]:
        return [v * SCALE for v in values]

    def measure(self, value: str, size: int, bold: bool = False) -> float:
        return self.draw.textlength(value, font=_font(size, bold)) / SCALE

    def fit(self, value: str, size: int, bold: bool, width: float) -> str:
        font = _font(size, bold)
        limit = width * SCALE
        if self.draw.textlength(value, font=font) <= limit:
            return value
        while value and self.draw.textlength(value + "...", font=font) > limit:
            value = value[:-1]
        return value.rstrip() + "..."

    def text(
        self,
        x: float,
        y: float,
        value: str,
        *,
        size: int,
        bold: bool = False,
        fill: tuple[int, int, int] = TEXT,
        anchor: str = "la",
        width: float | None = None,
    ) -> float:
        if width is not None:
            value = self.fit(value, size, bold, width)
        font = _font(size, bold)
        self.draw.text(self._px(x, y), value, font=font, fill=fill, anchor=anchor)
        return self.draw.textlength(value, font=font) / SCALE

    def rect(self, box: tuple[float, float, float, float], fill: tuple[int, int, int], radius: float = 0) -> None:
        if radius:
            self.draw.rounded_rectangle(self._px(*box), radius=radius * SCALE, fill=fill)
        else:
            self.draw.rectangle(self._px(*box), fill=fill)

    def line(self, x1: float, y: float, x2: float, fill: tuple[int, int, int] = BORDER) -> None:
        self.draw.line(self._px(x1, y, x2, y), fill=fill, width=SCALE)

    def star(self, cx: float, cy: float, radius: float, fill: tuple[int, int, int]) -> None:
        points = []
        for index in range(10):
            angle = -math.pi / 2 + index * math.pi / 5
            r = radius if index % 2 == 0 else radius * 0.45
            points.append(((cx + r * math.cos(angle)) * SCALE, (cy + r * math.sin(angle)) * SCALE))
        self.draw.polygon(points, fill=fill)

    def avatar(self, x: float, y: float, size: float, data: bytes | None, initial: str, accent: tuple[int, int, int]) -> None:
        diameter = int(size * SCALE)
        mask = Image.new("L", (diameter * 4, diameter * 4), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, diameter * 4 - 1, diameter * 4 - 1), fill=255)
        mask = mask.resize((diameter, diameter), Image.LANCZOS)

        picture = None
        if data:
            try:
                picture = Image.open(io.BytesIO(data)).convert("RGB").resize((diameter, diameter), Image.LANCZOS)
            except Exception:
                picture = None

        if picture is None:
            picture = Image.new("RGB", (diameter, diameter), _mix(accent, BACKGROUND, 0.85))
            ImageDraw.Draw(picture).text(
                (diameter / 2, diameter / 2),
                (initial or "?")[:1].upper(),
                font=_font(int(size * 0.42), True),
                fill=TEXT,
                anchor="mm",
            )

        self.image.paste(picture, (int(x * SCALE), int(y * SCALE)), mask)

    def png(self) -> bytes:
        buffer = io.BytesIO()
        self.image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


def render_ticket_card(
    ticket: dict[str, Any],
    *,
    community: str,
    opener_name: str,
    handler_name: str,
    closer_name: str,
    avatar: bytes | None = None,
    status: str = "Closed",
    accent: int = config.COLOR_PRIMARY,
) -> bytes | None:
    if not available():
        return None
    try:
        return _render(ticket, community, opener_name, handler_name, closer_name, avatar, status, accent)
    except Exception:
        log.exception("Could not render the summary card for ticket #%s.", ticket.get("id"))
        return None


def _render(
    ticket: dict[str, Any],
    community: str,
    opener_name: str,
    handler_name: str,
    closer_name: str,
    avatar: bytes | None,
    status: str,
    accent_value: int,
) -> bytes:
    accent = _rgb(accent_value)
    canvas = _Canvas()
    right = WIDTH - PAD

    canvas.rect((0, 0, 6, HEIGHT), accent)

    canvas.avatar(PAD, 36, 64, avatar, opener_name, accent)

    type_name = config.TICKET_TYPES.get(ticket.get("type", ""), {}).get("name", "Ticket")
    eyebrow = f"{community}  /  {type_name} Ticket".upper()

    pill_text = status.upper()
    pill_width = canvas.measure(pill_text, 12, True) + 28
    canvas.rect((right - pill_width, 38, right, 66), _mix(accent, BACKGROUND, 0.28), radius=14)
    canvas.text(right - pill_width / 2, 52, pill_text, size=12, bold=True, fill=TEXT, anchor="mm")

    text_left = PAD + 64 + 20
    text_width = right - pill_width - 24 - text_left
    canvas.text(text_left, 40, eyebrow, size=13, bold=True, fill=MUTED, width=text_width)
    canvas.text(text_left, 60, f"Ticket #{ticket.get('id')}", size=30, bold=True, fill=TEXT, width=text_width)

    subject = str((ticket.get("answers") or {}).get("subject") or "No subject provided")
    canvas.text(PAD, 120, subject, size=18, fill=BODY, width=right - PAD)

    canvas.line(PAD, 158, right)

    created = ticket.get("created_at")
    closed = ticket.get("closed_at")
    first = ticket.get("first_response_at")
    conversation = [
        m for m in ticket.get("messages", [])
        if m.get("role") in ("user", "staff") and not m.get("internal")
    ]

    cells = [
        ("Opened by", opener_name),
        ("Handled by", handler_name),
        ("Closed by", closer_name),
        ("Scope", config.SCOPE_LABELS.get(ticket.get("scope") or "", "Not set")),
        ("Time open", _compact(closed - created) if created and closed else "Unknown"),
        ("First response", _compact(first - created) if created and first else "No reply"),
        ("Messages", str(len(conversation))),
        ("Rating", None),
    ]

    column = (right - PAD) / 4
    for index, (label, value) in enumerate(cells):
        x = PAD + (index % 4) * column
        y = 180 + (index // 4) * 70
        canvas.text(x, y, label.upper(), size=12, bold=True, fill=MUTED, width=column - 16)
        if value is not None:
            canvas.text(x, y + 20, value, size=20, bold=True, fill=TEXT, width=column - 16)
            continue

        rating = ticket.get("rating")
        if rating:
            for star in range(5):
                canvas.star(x + 11 + star * 26, y + 34, 11, STAR_ON if star < int(rating) else STAR_OFF)
        else:
            canvas.text(x, y + 20, "Awaiting", size=20, bold=True, fill=FAINT)

    canvas.line(PAD, 318, right)
    canvas.text(
        PAD,
        334,
        f"Opened {_stamp(created)}   /   Closed {_stamp(closed)}",
        size=13,
        fill=FAINT,
        width=right - PAD - 220,
    )
    canvas.text(right, 334, config.BOT_NAME, size=13, bold=True, fill=FAINT, anchor="ra", width=200)

    return canvas.png()
