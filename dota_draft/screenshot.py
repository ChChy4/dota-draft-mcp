"""Reads a Dota 2 draft from a Steam screenshot (Tool 4).

Measured on 1920x1080 screenshots (October 2026): the 10 top-bar slots sit at fixed positions;
a picked hero's portrait is the landscape portrait from Valve's CDN, scaled to about 0.42-0.50
and partly covered by rank banners and badges, so only a clear area in its upper middle is
compared. The user's own name is drawn brighter than the other nine, and the user's team is
green on the map.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from dota_draft.collect import DATA_DIR, PORTRAITS_DIR_NAME

log = logging.getLogger(__name__)

PORTRAITS_DIR = DATA_DIR / PORTRAITS_DIR_NAME
SCREENSHOT_DIR_ENV = "DOTA_SCREENSHOT_DIR"
DEFAULT_STEAM_DIR = Path(r"C:\Program Files (x86)\Steam")
MAX_AGE_S = 180
SCREEN_SIZE = (1920, 1080)

SIDES = ("Radiant", "Dire")  # Radiant is always on the left of the top bar, Dire on the right
SLOT_X = {"Radiant": (210, 335, 460, 585, 710), "Dire": (1095, 1220, 1345, 1470, 1595)}
SLOT_W = 115

# Compared area of a slot, relative to its left edge, sampled every STEP pixels.
MATCH_XS = np.arange(28, 28 + 60, 2)  # 30 columns
MATCH_YS = np.arange(6, 6 + 32, 2)  # 16 rows
# Where the portrait's top-left corner may sit relative to the slot, and its scale.
ORIGIN_DX = range(-12, 5, 2)
ORIGIN_DY = range(2, 9, 2)
SCALES = (0.42, 0.44, 0.46, 0.48, 0.50)
REF_SIZE = (256, 144)

MIN_MARGIN = 15  # picked: best hero beats the second best by this much (measured 22.7+ vs 4.5-)
UNKNOWN_SATURATION = 40  # unmatched but colourful: an unknown portrait (measured 45+ vs 33-)

NAME_Y = (82, 100)
NAME_TEXT_MIN = 120  # pixels brighter than this are name text
OWN_NAME_MIN = 200  # 90th-percentile brightness of my name (measured 225-243 vs 162-166)
OWN_NAME_GAP = 30

# Map regions: the minimap during picking, the strategy map after my pick.
MAP_REGIONS = ((105, 875, 435, 1055), (850, 207, 1125, 483))
# A real map has my green in one corner and enemy red in the opposite one (measured 275-352 green
# and 148-284 red); the hero grid under the strategy-map area gave red only.
MAP_MIN_PIXELS = 50


class ScreenshotError(Exception):
    """Raised when no usable screenshot is found."""


@dataclass(frozen=True)
class Slot:
    side: str
    number: int
    status: str  # "picked", "unknown" or "empty"
    hero_id: int | None
    diff: float
    margin: float


@dataclass(frozen=True)
class Reading:
    slots: list[Slot]
    own: tuple[str, int] | None  # (side, slot number) of my name
    side: str | None
    side_note: str
    name_side: str | None = None
    map_side: str | None = None
    timings: dict[str, float] = field(default_factory=dict)

    def picked(self, side: str) -> list[Slot]:
        return [s for s in self.slots if s.side == side and s.status == "picked"]


# --- finding the screenshot -------------------------------------------------------------------


def steam_dir() -> Path:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            return Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except (ImportError, OSError):
        return DEFAULT_STEAM_DIR


def screenshot_folders() -> list[Path]:
    """DOTA_SCREENSHOT_DIR if set, else every Steam account's Dota 2 screenshot folder."""
    configured = os.environ.get(SCREENSHOT_DIR_ENV)
    if configured:
        return [Path(configured)]
    return sorted(steam_dir().glob("userdata/*/760/remote/570/screenshots"))


def newest_screenshot(folders: list[Path]) -> Path:
    files = [f for folder in folders if folder.is_dir() for f in folder.glob("*.jpg")]
    if not files:
        where = ", ".join(str(f) for f in folders) or "no Steam screenshot folder found"
        raise ScreenshotError(f"No Dota 2 screenshots found ({where}). Press F12 in the draft, or set {SCREENSHOT_DIR_ENV}.")
    return max(files, key=lambda f: f.stat().st_mtime)


def load_screen(path: Path) -> np.ndarray:
    with Image.open(path) as img:
        if img.size != SCREEN_SIZE:
            raise ScreenshotError(
                f"{path.name} is {img.size[0]}x{img.size[1]}; only {SCREEN_SIZE[0]}x{SCREEN_SIZE[1]} screenshots can be read."
            )
        return np.asarray(img.convert("RGB"), dtype=np.uint8)


# --- reference portraits and matching ---------------------------------------------------------


def load_portraits(folder: Path = PORTRAITS_DIR) -> tuple[list[int], np.ndarray]:
    """Hero IDs and portraits as one (heroes, 144, 256, 3) float32 array, transparency on black."""
    ids, images = [], []
    for path in sorted(folder.glob("*.png"), key=lambda p: p.stem):
        if not path.stem.isdigit():
            continue
        with Image.open(path) as img:
            rgba = img.convert("RGBA")
            flat = Image.alpha_composite(Image.new("RGBA", rgba.size, (0, 0, 0, 255)), rgba).convert("RGB")
            if flat.size != REF_SIZE:
                flat = flat.resize(REF_SIZE, Image.BICUBIC)
        ids.append(int(path.stem))
        images.append(np.asarray(flat, dtype=np.float32))
    if not ids:
        raise ScreenshotError(f"No reference portraits in {folder}. Run `uv run dota-draft-collect` first.")
    return ids, np.stack(images)


class Matcher:
    """Every reference portrait sampled at every allowed placement, so a slot is one comparison."""

    def __init__(self, ids: list[int], portraits: np.ndarray) -> None:
        self.ids = ids
        height, width = portraits.shape[1:3]
        patches, valid = [], []
        for scale in SCALES:
            for dy in ORIGIN_DY:
                for dx in ORIGIN_DX:
                    u = (MATCH_XS - dx) / scale
                    v = (MATCH_YS - dy) / scale
                    ok = u.min() >= 0 and v.min() >= 0 and u.max() <= width - 1 and v.max() <= height - 1
                    valid.append(ok)
                    patches.append(self._sample(portraits, u, v) if ok else np.zeros((len(ids), len(MATCH_YS), len(MATCH_XS), 3), np.uint8))
        # (heroes, placements, values)
        self.patches = np.stack(patches, axis=1).reshape(len(ids), len(patches), -1)
        self.valid = np.array(valid)

    @staticmethod
    def _sample(portraits: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        """Bilinear samples of all portraits at columns u and rows v."""
        x0 = np.minimum(u.astype(int), portraits.shape[2] - 2)
        y0 = np.minimum(v.astype(int), portraits.shape[1] - 2)
        fx = (u - x0)[None, None, :, None]
        fy = (v - y0)[None, :, None, None]
        rows0, rows1 = portraits[:, y0], portraits[:, y0 + 1]
        top = rows0[:, :, x0] * (1 - fx) + rows0[:, :, x0 + 1] * fx
        bottom = rows1[:, :, x0] * (1 - fx) + rows1[:, :, x0 + 1] * fx
        return np.rint(top * (1 - fy) + bottom * fy).astype(np.uint8)

    def match(self, screen: np.ndarray, slot_x: int) -> tuple[int, float, float]:
        """(hero ID, difference, margin over the second best hero) for the slot at slot_x."""
        crop = screen[MATCH_YS][:, slot_x + MATCH_XS].reshape(1, 1, -1)
        diff = (np.maximum(self.patches, crop) - np.minimum(self.patches, crop)).sum(axis=2, dtype=np.uint32)
        diff = diff / crop.shape[2]
        diff[:, ~self.valid] = np.inf
        per_hero = diff.min(axis=1)
        first, second = np.argsort(per_hero)[:2]
        return self.ids[first], float(per_hero[first]), float(per_hero[second] - per_hero[first])


# --- own slot and side ------------------------------------------------------------------------


def saturation(screen: np.ndarray, slot_x: int) -> float:
    area = screen[6:38, slot_x + 28 : slot_x + 88].astype(np.int16)
    return float((area.max(axis=2) - area.min(axis=2)).mean())


def name_brightness(screen: np.ndarray, slot_x: int) -> float | None:
    """90th-percentile brightness of the name text under a slot, or None if there is no text."""
    gray = screen[NAME_Y[0] : NAME_Y[1], slot_x : slot_x + SLOT_W].astype(np.float32).mean(axis=2)
    text = np.sort(gray[gray > NAME_TEXT_MIN])
    return float(text[int(len(text) * 0.9)]) if len(text) else None


def own_slot(screen: np.ndarray) -> tuple[str, int] | None:
    values = [(name_brightness(screen, x) or 0.0, side, number)
              for side in SIDES for number, x in enumerate(SLOT_X[side], 1)]
    values.sort(reverse=True)
    (best, side, number), (second, _, _) = values[0], values[1]
    return (side, number) if best >= OWN_NAME_MIN and best - second >= OWN_NAME_GAP else None


def map_side(screen: np.ndarray) -> str | None:
    """My side from the map colours: my team is green. None if the maps disagree or show too little."""
    found = set()
    for x0, y0, x1, y1 in MAP_REGIONS:
        area = screen[y0:y1, x0:x1].astype(np.int16)
        r, g, b = area[..., 0], area[..., 1], area[..., 2]
        green = (g > 150) & (r < 110) & (b < 110)
        red = (r > 150) & (g < 90) & (b < 90)
        h, w = green.shape
        bottom_left = (slice(h // 2, h), slice(0, w // 2))
        top_right = (slice(0, h // 2), slice(w // 2, w))
        radiant = (int(green[bottom_left].sum()), int(red[top_right].sum()))
        dire = (int(green[top_right].sum()), int(red[bottom_left].sum()))
        if min(radiant) >= MAP_MIN_PIXELS and sum(dire) <= sum(radiant) // 10:
            found.add("Radiant")
        elif min(dire) >= MAP_MIN_PIXELS and sum(radiant) <= sum(dire) // 10:
            found.add("Dire")
    return found.pop() if len(found) == 1 else None


def decide_side(own: tuple[str, int] | None, by_map: str | None) -> tuple[str | None, str]:
    by_name = own[0] if own else None
    if by_name and by_map:
        if by_name == by_map:
            return by_name, "your name is drawn brightest, and the map agrees"
        return None, f"your name is in a {by_name} slot but the map shows you as {by_map}"
    if by_name:
        return by_name, "your name is drawn brightest (the map check found nothing)"
    if by_map:
        return by_map, "the map colours (your name could not be found)"
    return None, "neither your name nor the map colours could be found"


def read_draft(screen: np.ndarray, matcher: Matcher) -> Reading:
    start = time.perf_counter()
    slots = []
    for side in SIDES:
        for number, x in enumerate(SLOT_X[side], 1):
            hero_id, diff, margin = matcher.match(screen, x)
            if margin >= MIN_MARGIN:
                slots.append(Slot(side, number, "picked", hero_id, diff, margin))
            else:
                status = "unknown" if saturation(screen, x) >= UNKNOWN_SATURATION else "empty"
                slots.append(Slot(side, number, status, None, diff, margin))
    matched = time.perf_counter()
    own = own_slot(screen)
    by_map = map_side(screen)
    side, note = decide_side(own, by_map)
    timings = {"match_s": matched - start, "side_s": time.perf_counter() - matched}
    return Reading(slots, own, side, note, own[0] if own else None, by_map, timings)


# --- output -----------------------------------------------------------------------------------


def format_reading(reading: Reading, path: Path, age_s: float, name) -> list[str]:
    lines = [f"Screenshot: {path.name}, taken {age_s:.0f} s ago."]
    for side in SIDES:
        parts = []
        for s in (s for s in reading.slots if s.side == side):
            label = {"picked": name(s.hero_id) if s.hero_id else "", "unknown": "unknown hero", "empty": "empty or not locked in"}[s.status]
            if reading.own == (side, s.number):
                label += " (you)"
            parts.append(f"{s.number} {label}")
        lines.append(f"{side} (top bar {'left' if side == 'Radiant' else 'right'}): " + "; ".join(parts))
    if reading.side:
        slot = f", slot {reading.own[1]}" if reading.own and reading.own[0] == reading.side else ""
        lines.append(f"You are {reading.side}{slot} (found by: {reading.side_note}).")
    else:
        lines.append(f"Your side could not be determined: {reading.side_note}. Both readings follow.")
    if not any(s.status == "picked" for s in reading.slots):
        # Also true for in-game screenshots: on 77 of them no slot was read as picked, but 13 had
        # bright text where the names would be.
        lines.append("No picked heroes found. If this is not the draft screen, ask the user for a new F12 during the draft.")
    unknown = [f"{s.side} {s.number}" for s in reading.slots if s.status == "unknown"]
    if unknown:
        lines.append("Unknown portraits (not matched to any hero): " + ", ".join(unknown) + ". Ask the user if they matter.")
    return lines
