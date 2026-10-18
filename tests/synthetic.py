"""Synthetic draft screenshots for tests: generated patterns, no game art and no player names."""

from pathlib import Path

import numpy as np
from PIL import Image

from dota_draft import screenshot

BACKGROUND = (30, 35, 45)


def portrait(seed: int) -> Image.Image:
    """A smooth, colourful 256x144 'portrait' that differs for every seed."""
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 256, (9, 16, 3), dtype=np.uint8)
    return Image.fromarray(small).resize(screenshot.REF_SIZE, Image.BICUBIC)


def write_portraits(folder: Path, hero_ids: list[int]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for hero_id in hero_ids:
        portrait(hero_id).save(folder / f"{hero_id}.png")
    return folder


def blank() -> np.ndarray:
    return np.full((1080, 1920, 3), BACKGROUND, np.uint8)


def slot_x(side: str, number: int) -> int:
    return screenshot.SLOT_X[side][number - 1]


def place(screen: np.ndarray, image: Image.Image, side: str, number: int,
          scale: float = 0.46, dx: int = -6, dy: int = 4) -> None:
    """Draw a portrait into a top-bar slot the way the game scales it."""
    small = np.asarray(image.resize((round(256 * scale), round(144 * scale)), Image.BILINEAR))
    x, y = slot_x(side, number) + dx, dy
    screen[y : y + small.shape[0], x : x + small.shape[1]] = small


def greyed(image: Image.Image) -> Image.Image:
    """A not-locked-in hero: the portrait desaturated and darker."""
    return image.convert("L").point(lambda v: v * 0.6).convert("RGB")


def name(screen: np.ndarray, side: str, number: int, brightness: int) -> None:
    """Text-like stripes of the given brightness where a player's name is written."""
    x = slot_x(side, number)
    for row in range(85, 97, 2):
        screen[row, x + 20 : x + 90] = brightness


def map_colours(screen: np.ndarray, my_side: str) -> None:
    """My team green and the enemy red, in opposite corners of the picking-phase minimap."""
    x0, y0, x1, y1 = screenshot.MAP_REGIONS[0]
    bottom_left = (slice(y1 - 40, y1 - 20), slice(x0 + 20, x0 + 40))
    top_right = (slice(y0 + 20, y0 + 40), slice(x1 - 40, x1 - 20))
    mine, theirs = (bottom_left, top_right) if my_side == "Radiant" else (top_right, bottom_left)
    screen[mine] = (40, 200, 40)
    screen[theirs] = (200, 40, 40)


def save(screen: np.ndarray, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(screen).save(path, quality=95)
    return path
