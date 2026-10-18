import asyncio
import os
import re
import time

import pytest
from fastmcp import Client

from dota_draft import screenshot, server, store
from dota_draft.heroes import HeroList
from dota_draft.store import MatchupData
from tests import synthetic
from tests.test_screenshot import draft_screen

SHA = "315f48c0123456789abcdef0123456789abcdef0"
SERVER_LINE = re.compile(r"\nServer: started \d{4}-\d\d-\d\d \d\d:\d\d UTC from commit (\w+)\.$")


def make_git(tmp_path, head, files=None):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text(head + "\n", encoding="utf-8")
    for name, content in (files or {}).items():
        (git / name).parent.mkdir(parents=True, exist_ok=True)
        (git / name).write_text(content, encoding="utf-8")
    return tmp_path


def test_commit_from_branch_ref(tmp_path):
    root = make_git(tmp_path, "ref: refs/heads/main", {"refs/heads/main": SHA + "\n"})
    assert server.read_git_commit(root) == "315f48c"


def test_commit_from_packed_refs(tmp_path):
    root = make_git(tmp_path, "ref: refs/heads/main", {"packed-refs": f"# pack-refs\n{SHA} refs/heads/main\n"})
    assert server.read_git_commit(root) == "315f48c"


def test_commit_from_detached_head(tmp_path):
    assert server.read_git_commit(make_git(tmp_path, SHA)) == "315f48c"


def test_commit_unknown_without_git(tmp_path):
    assert server.read_git_commit(tmp_path) == "unknown"


def test_server_line_uses_start_values():
    text = server.with_server_line("hello")
    match = SERVER_LINE.search(text)
    assert text.startswith("hello\n") and match
    assert match.group(1) == server.STARTED_COMMIT


def test_every_tool_ends_with_server_line(monkeypatch):
    raw = [{"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage", "roles": []}]
    monkeypatch.setattr(server, "get_hero_list", lambda: HeroList(raw))

    def no_data():
        raise store.DataMissing("No matchup data.")

    monkeypatch.setattr(store, "load", no_data)
    calls = {
        "find_hero": {"name": "am"},
        "suggest_heroes": {"allies": [], "enemies": [], "position": 9},  # error path
        "lane_info": {"my_hero": 1, "enemy_laners": [2]},  # no data path
        "draft_from_screenshot": {"position": 9},  # error path
    }

    async def run():
        async with Client(server.mcp) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert names == set(calls)  # a new tool must be added here too
            return {name: (await client.call_tool(name, args)).content[0].text for name, args in calls.items()}

    for name, text in asyncio.run(run()).items():
        assert SERVER_LINE.search(text), f"{name} output has no server line:\n{text}"


def test_parse_abilities_splits_comma_separated_ids():
    # Shapes taken from the real /constants responses, including a key with two IDs.
    ids = {
        "5007": "axe_berserkers_call",
        "3060,1617": "some_shared_ability",
        "6145": "special_bonus_strength_15",
        "bad,": "ignored_not_a_number",
    }
    details = {
        "axe_berserkers_call": {"dname": "Berserker's Call"},
        "special_bonus_strength_15": {"dname": "+15 Strength"},
    }
    names, talents = server.parse_abilities(ids, details)
    assert names == {
        5007: "Berserker's Call",
        3060: "some_shared_ability",  # no display name: falls back to the internal name
        1617: "some_shared_ability",
        6145: "+15 Strength",
    }
    assert talents == {6145}


# --- draft_from_screenshot, on synthetic screenshots -------------------------------------------

SYNTHETIC_HEROES = [1, 2, 3, 4, 5, 6]


@pytest.fixture
def screenshot_env(tmp_path, monkeypatch):
    """A screenshot folder and portraits on disk, matchup data for heroes 1-6, a fresh matcher."""
    portraits = synthetic.write_portraits(tmp_path / "portraits", SYNTHETIC_HEROES)
    monkeypatch.setattr(screenshot, "PORTRAITS_DIR", portraits)
    monkeypatch.setattr(server, "_matcher", None)
    shots = tmp_path / "shots"
    monkeypatch.setenv(screenshot.SCREENSHOT_DIR_ENV, str(shots))
    aggregate = {
        "built_at": "2026-10-18T12:00:00+00:00", "first_start": 1789000000, "last_start": 1791500000,
        "matches": 600, "slices": 1, "filters": "test",
        "heroes": {str(h): {"games": 100, "wins": 40 + h} for h in SYNTHETIC_HEROES},
        "pairs": [[1, 6, 60, 20], [4, 6, 60, 50]],
    }
    monkeypatch.setattr(store, "load", lambda: MatchupData(aggregate))
    raw = [{"id": h, "name": f"npc_dota_hero_h{h}", "localized_name": f"Hero{h}", "roles": ["Carry"]} for h in SYNTHETIC_HEROES]
    monkeypatch.setattr(server, "get_hero_list", lambda: HeroList(raw))
    return shots


def call_screenshot_tool(position=3):
    async def run():
        async with Client(server.mcp) as client:
            return (await client.call_tool("draft_from_screenshot", {"position": position})).content[0].text

    return asyncio.run(run())


def test_screenshot_tool_reads_draft_and_suggests(screenshot_env):
    synthetic.save(draft_screen(), screenshot_env / "draft.jpg")
    text = call_screenshot_tool()
    assert text.startswith("During a draft, answer briefly: the top 3 heroes for position 3, one line each.")
    assert "Radiant (top bar left): 1 Hero1; 2 Hero2; 3 empty or not locked in; 4 empty or not locked in; 5 Hero4 (you)" in text
    assert "You are Radiant, slot 5 (found by: your name is drawn brightest, and the map agrees)." in text
    # My own pick (Hero4) is not an ally; the picked enemy is Hero5.
    assert "Allies: Hero1, Hero2 | Enemies: Hero5" in text
    assert "Draft suggestions for position 3 (offlane)" in text
    assert "Unknown portraits (not matched to any hero): Dire 2." in text
    assert SERVER_LINE.search(text)


def test_screenshot_tool_gives_both_readings_when_side_is_unclear(screenshot_env):
    synthetic.save(draft_screen(me=("Radiant", 5), map_side="Dire"), screenshot_env / "draft.jpg")
    text = call_screenshot_tool()
    assert "Your side could not be determined" in text
    assert "--- If you are Radiant:" in text and "--- If you are Dire:" in text
    # As Dire, Hero5 is an ally and Radiant's picks are enemies.
    assert "Allies: Hero5 | Enemies: Hero1, Hero2, Hero4" in text


def test_screenshot_tool_refuses_old_screenshots(screenshot_env):
    path = synthetic.save(draft_screen(), screenshot_env / "draft.jpg")
    os.utime(path, (time.time() - 200, time.time() - 200))
    text = call_screenshot_tool()
    assert "draft.jpg) is 3 minutes old, so it was not used" in text
    assert "Draft suggestions" not in text


def test_screenshot_tool_reports_missing_screenshots_and_wrong_size(screenshot_env):
    assert "No Dota 2 screenshots found" in call_screenshot_tool()
    screenshot_env.mkdir(parents=True, exist_ok=True)
    synthetic.Image.new("RGB", (1280, 720)).save(screenshot_env / "small.jpg")
    assert "only 1920x1080 screenshots can be read" in call_screenshot_tool()


def test_screenshot_tool_position_check(screenshot_env):
    assert call_screenshot_tool(position=7).startswith("position must be 1")
