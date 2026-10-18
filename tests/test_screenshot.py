import os
import time

import pytest

from dota_draft import screenshot
from tests import synthetic

HEROES = [1, 2, 3, 4, 5, 6]
UNKNOWN_HERO = 999  # a portrait that is not among the references


@pytest.fixture(scope="module")
def matcher(tmp_path_factory):
    folder = synthetic.write_portraits(tmp_path_factory.mktemp("portraits"), HEROES)
    return screenshot.Matcher(*screenshot.load_portraits(folder))


def draft_screen(me=("Radiant", 5), map_side="Radiant"):
    """Radiant: 1, 2 picked, 3 empty, 4 not locked in, 5 picked. Dire: 1 picked, 2 unknown, 3-5 empty."""
    screen = synthetic.blank()
    synthetic.place(screen, synthetic.portrait(1), "Radiant", 1)
    synthetic.place(screen, synthetic.portrait(2), "Radiant", 2, scale=0.44, dx=-10)
    synthetic.place(screen, synthetic.greyed(synthetic.portrait(3)), "Radiant", 4)
    synthetic.place(screen, synthetic.portrait(4), "Radiant", 5, scale=0.48, dx=0, dy=2)
    synthetic.place(screen, synthetic.portrait(5), "Dire", 1)
    synthetic.place(screen, synthetic.portrait(UNKNOWN_HERO), "Dire", 2)
    for side in screenshot.SIDES:
        for number in range(1, 6):
            synthetic.name(screen, side, number, 240 if (side, number) == me else 150)
    if map_side:
        synthetic.map_colours(screen, map_side)
    return screen


def test_reads_picked_empty_unknown_and_not_locked_in(matcher):
    reading = screenshot.read_draft(draft_screen(), matcher)
    by_slot = {(s.side, s.number): s for s in reading.slots}
    assert [(n, by_slot[("Radiant", n)].status, by_slot[("Radiant", n)].hero_id) for n in range(1, 6)] == [
        (1, "picked", 1), (2, "picked", 2), (3, "empty", None), (4, "empty", None), (5, "picked", 4)]
    assert [(n, by_slot[("Dire", n)].status, by_slot[("Dire", n)].hero_id) for n in range(1, 6)] == [
        (1, "picked", 5), (2, "unknown", None), (3, "empty", None), (4, "empty", None), (5, "empty", None)]
    assert all(s.margin >= screenshot.MIN_MARGIN for s in reading.slots if s.status == "picked")


def test_own_slot_and_side_from_name_and_map(matcher):
    reading = screenshot.read_draft(draft_screen(), matcher)
    assert reading.own == ("Radiant", 5)
    assert reading.side == "Radiant"
    assert reading.side_note == "your name is drawn brightest, and the map agrees"


def test_dire_side(matcher):
    reading = screenshot.read_draft(draft_screen(me=("Dire", 3), map_side="Dire"), matcher)
    assert (reading.own, reading.side) == (("Dire", 3), "Dire")


def test_side_from_name_only_and_map_only(matcher):
    name_only = screenshot.read_draft(draft_screen(map_side=None), matcher)
    assert name_only.side == "Radiant" and "map check found nothing" in name_only.side_note
    map_only = screenshot.read_draft(draft_screen(me=None, map_side="Dire"), matcher)
    assert map_only.own is None and map_only.side == "Dire" and "map colours" in map_only.side_note


def test_side_unknown_when_cues_disagree_or_are_missing(matcher):
    conflict = screenshot.read_draft(draft_screen(me=("Radiant", 5), map_side="Dire"), matcher)
    assert conflict.side is None and "map shows you as Dire" in conflict.side_note
    nothing = screenshot.read_draft(draft_screen(me=None, map_side=None), matcher)
    assert nothing.side is None


def test_name_needs_a_clear_lead():
    screen = synthetic.blank()
    synthetic.name(screen, "Radiant", 1, 230)
    synthetic.name(screen, "Dire", 1, 215)  # within 30 of the brightest
    assert screenshot.own_slot(screen) is None


def test_format_reading(matcher, tmp_path):
    reading = screenshot.read_draft(draft_screen(), matcher)
    lines = screenshot.format_reading(reading, tmp_path / "shot.jpg", 25, lambda i: f"Hero{i}")
    text = "\n".join(lines)
    assert "Screenshot: shot.jpg, taken 25 s ago." in text
    assert "Radiant (top bar left): 1 Hero1; 2 Hero2; 3 empty or not locked in; 4 empty or not locked in; 5 Hero4 (you)" in text
    assert "Dire (top bar right): 1 Hero5; 2 unknown hero;" in text
    assert "You are Radiant, slot 5" in text
    assert "Unknown portraits (not matched to any hero): Dire 2." in text
    assert "No picked heroes found" not in text


def test_format_reading_without_picks(matcher, tmp_path):
    reading = screenshot.read_draft(synthetic.blank(), matcher)
    text = "\n".join(screenshot.format_reading(reading, tmp_path / "x.jpg", 5, str))
    assert "No picked heroes found" in text and "Both readings follow" in text


def test_newest_screenshot_and_folder_setting(tmp_path, monkeypatch):
    folder = tmp_path / "shots"
    old = synthetic.save(synthetic.blank(), folder / "old.jpg")
    new = synthetic.save(synthetic.blank(), folder / "new.jpg")
    os.utime(old, (time.time() - 100, time.time() - 100))
    monkeypatch.setenv(screenshot.SCREENSHOT_DIR_ENV, str(folder))
    assert screenshot.screenshot_folders() == [folder]
    assert screenshot.newest_screenshot(screenshot.screenshot_folders()) == new


def test_no_screenshots(tmp_path):
    with pytest.raises(screenshot.ScreenshotError, match="No Dota 2 screenshots found"):
        screenshot.newest_screenshot([tmp_path])


def test_other_screen_sizes_are_refused(tmp_path):
    path = tmp_path / "small.jpg"
    synthetic.Image.new("RGB", (1280, 720)).save(path)
    with pytest.raises(screenshot.ScreenshotError, match="only 1920x1080"):
        screenshot.load_screen(path)


def test_missing_portraits(tmp_path):
    with pytest.raises(screenshot.ScreenshotError, match="dota-draft-collect"):
        screenshot.load_portraits(tmp_path)
