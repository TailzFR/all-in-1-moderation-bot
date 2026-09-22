from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import config

try:
    from PIL import ImageFont
except ImportError:
    ImageFont = None

WINDOWS_FONTS = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"

REGULAR = (
    WINDOWS_FONTS / "segoeui.ttf",
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    Path("/Library/Fonts/Arial.ttf"),
    WINDOWS_FONTS / "arial.ttf",
)

BOLD = (
    WINDOWS_FONTS / "segoeuib.ttf",
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    Path("/Library/Fonts/Arial Bold.ttf"),
    WINDOWS_FONTS / "arialbd.ttf",
)

_cache: dict[tuple[bool, int], Any] = {}


def available() -> bool:
    return ImageFont is not None


def load(size: int, bold: bool = False) -> Any:
    key = (bold, size)
    if key in _cache:
        return _cache[key]

    candidates = [config.FONT_DIR / ("Bold.ttf" if bold else "Regular.ttf"), *(BOLD if bold else REGULAR)]
    font = None
    for path in candidates:
        if not path.is_file():
            continue
        try:
            font = ImageFont.truetype(str(path), size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default(size=size)

    _cache[key] = font
    return font
