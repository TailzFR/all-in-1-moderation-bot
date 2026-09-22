from __future__ import annotations

import io
import logging
import math
import random
from typing import Any

import config
from core import fonts

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    Image = ImageDraw = ImageFilter = None

log = logging.getLogger("manager.captcha")

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

WIDTH = 540
HEIGHT = 150
BACKGROUND = (245, 246, 247)
INK = (28, 30, 34)
FILENAME = "captcha.png"


def available() -> bool:
    return Image is not None and fonts.available()


def new_code(length: int | None = None) -> str:
    size = length or config.VERIFY_CODE_LENGTH
    return "".join(random.choice(ALPHABET) for _ in range(size))


def decoys(code: str, count: int) -> list[str]:
    produced: set[str] = set()
    positions = range(len(code))
    guard = 0
    while len(produced) < count and guard < count * 60:
        guard += 1
        candidate = list(code)
        for index in random.sample(positions, min(len(code), random.choice((2, 2, 3)))):
            choices = [c for c in ALPHABET if c != candidate[index]]
            candidate[index] = random.choice(choices)
        option = "".join(candidate)
        if option != code:
            produced.add(option)
    return list(produced)


def options_for(code: str, count: int | None = None) -> list[str]:
    total = max(2, count or config.VERIFY_OPTION_COUNT)
    choices = decoys(code, total - 1)[: total - 1]
    choices.append(code)
    random.shuffle(choices)
    return choices


def render(code: str) -> bytes | None:
    if not available():
        return None
    try:
        return _render(code)
    except Exception:
        log.exception("Could not render a captcha image.")
        return None


def _ink(shade: int = 0) -> tuple[int, int, int]:
    jitter = random.randint(-shade, shade) if shade else 0
    return tuple(max(0, min(255, value + jitter)) for value in INK)


def _render(code: str) -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)

    for _ in range(random.randint(700, 1100)):
        x = random.randint(0, WIDTH - 1)
        y = random.randint(0, HEIGHT - 1)
        grey = random.randint(180, 225)
        draw.point((x, y), fill=(grey, grey, grey))

    for _ in range(random.randint(2, 3)):
        points = []
        amplitude = random.uniform(6, 15)
        period = random.uniform(70, 130)
        offset = random.uniform(0, math.tau)
        base = random.randint(35, HEIGHT - 35)
        for x in range(-10, WIDTH + 10, 4):
            points.append((x, base + amplitude * math.sin(offset + x / period)))
        grey = random.randint(120, 165)
        draw.line(points, fill=(grey, grey, grey), width=random.randint(1, 2), joint="curve")

    slots = len(code)
    margin = 26
    step = (WIDTH - margin * 2) / slots

    for index, character in enumerate(code):
        size = random.randint(46, 60)
        font = fonts.load(size, bold=random.random() < 0.5)
        glyph = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
        ImageDraw.Draw(glyph).text((size, size), character, font=font, fill=(*_ink(15), 255), anchor="mm")
        glyph = glyph.rotate(random.uniform(-26, 26), resample=Image.BICUBIC, expand=True)

        centre = margin + step * (index + 0.5) + random.randint(-3, 3)
        x = int(centre - glyph.width / 2)
        y = int(HEIGHT / 2 - glyph.height / 2 + random.randint(-11, 11))
        image.paste(glyph, (x, y), glyph)

    x1 = random.randint(0, WIDTH // 3)
    y1 = random.randint(10, HEIGHT - 10)
    x2 = random.randint(WIDTH * 2 // 3, WIDTH)
    y2 = random.randint(10, HEIGHT - 10)
    grey = random.randint(70, 120)
    draw.line((x1, y1, x2, y2), fill=(grey, grey, grey), width=random.randint(1, 2))

    image = image.filter(ImageFilter.SMOOTH)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def challenge() -> tuple[str, list[str], bytes | None]:
    code = new_code()
    return code, options_for(code), render(code)
