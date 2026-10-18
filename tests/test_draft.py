import json
from datetime import datetime, timezone

import pytest

from dota_draft import draft, store
from dota_draft.store import MatchupData

# Hand-checkable data. Overall win rates: hero 1 = 50%, 2 = 60%, 3 = 40%, 4 = 50%.
AGGREGATE = {
    "built_at": "2026-10-18T12:00:00+00:00",
    "first_start": 1789000000,  # 2026-09-10
    "last_start": 1791500000,  # 2026-10-08
    "matches": 2000,
    "slices": 2,
    "filters": "test",
    "heroes": {
        "1": {"games": 1000, "wins": 500},
        "2": {"games": 1000, "wins": 600},
        "3": {"games": 1000, "wins": 400},
        "4": {"games": 1000, "wins": 500},
    },
    "pairs": [
        [1, 3, 50, 35],  # hero 1 won 35 of 50 vs hero 3
        [1, 4, 150, 60],  # hero 1 won 60 of 150 vs hero 4
        [2, 4, 50, 40],  # hero 2 won 40 of 50 vs hero 4; no games vs hero 3
    ],
}

NAMES = {1: "One", 2: "Two", 3: "Three", 4: "Four"}
name = NAMES.__getitem__
roles = lambda i: "Carry"


@pytest.fixture
def data() -> MatchupData:
    return MatchupData(AGGREGATE)


def test_versus_reads_both_directions(data):
    assert data.versus(1, 3) == store.Record(50, 35)
    assert data.versus(3, 1) == store.Record(50, 15)
    assert data.versus(2, 3) == store.Record(0, 0)


def test_edge_smoothing(data):
    # (35 + 50 * 0.5) / (50 + 50) = 0.60 -> advantage +0.10
    e = draft.edge(data, 1, 3)
    assert e.smoothed == pytest.approx(0.60)
    assert e.advantage == pytest.approx(0.10)
    # Reverse direction: hero 3 won 15 of 50; (15 + 50 * 0.4) / 100 = 0.35 -> -0.05
    assert draft.edge(data, 3, 1).advantage == pytest.approx(-0.05)
    # No games: the smoothed rate is the hero's own rate, so advantage 0
    assert draft.edge(data, 2, 3).advantage == pytest.approx(0.0)


def test_small_sample_counts_less(data):
    # Raw: hero 2 won 80% of 50 games vs hero 4 (+20 pts over 60%); smoothed only +10 pts.
    assert draft.edge(data, 2, 4).advantage == pytest.approx(0.10)


def test_suggest_averages_advantages_over_enemies(data):
    # Hero 1: (+0.10 + (85/200 - 0.5)) / 2 = (0.10 - 0.075) / 2 = 0.0125
    # Hero 2: (0 + 0.10) / 2 = 0.05
    result = draft.suggest(data, allies=[], enemies=[3, 4])
    assert [s.hero_id for s in result] == [2, 1]
    assert result[0].score == pytest.approx(0.05)
    assert result[1].score == pytest.approx(0.0125)
    assert [e.games for e in result[1].edges] == [50, 150]  # real games, not smoothed


def test_suggest_without_enemies_ranks_by_win_rate(data):
    result = draft.suggest(data, allies=[2], enemies=[])
    assert [s.hero_id for s in result] == [1, 4, 3]  # hero 2 is picked; 1 and 4 tie at 50%
    assert result[0].score == pytest.approx(0.5)


def test_suggest_respects_top_n(data):
    assert len(draft.suggest(data, [], [], top_n=2)) == 2


@pytest.mark.parametrize(
    ("groups", "message"),
    [
        ({"allies": [1, 2, 3, 4, 1], "enemies": []}, "allies must list 0 to 4"),
        ({"allies": [99], "enemies": []}, "Unknown hero ID 99"),
        ({"allies": [1], "enemies": [1]}, "appears more than once"),
    ],
)
def test_check_heroes_errors(data, groups, message):
    error = draft.check_heroes(data, groups, {"allies": (0, 4), "enemies": (0, 5)})
    assert message in error


def test_check_heroes_ok(data):
    assert draft.check_heroes(data, {"allies": [1], "enemies": [3, 4]}, {"allies": (0, 4), "enemies": (0, 5)}) is None


def test_format_suggestions(data):
    text = draft.format_suggestions(data, draft.suggest(data, [], [3, 4]), [], [3, 4], 3, name, roles)
    assert "Draft suggestions for position 3 (offlane)" in text
    assert "1. Two (roles: Carry) — score +5.00 pts | overall 60.0% in 1,000 games" in text
    assert "vs Three +0.00 pts (0 games); vs Four +10.00 pts (50 games)" in text
    assert "2,000 Divine/Immortal ranked All Pick matches from 2026-09-10 to 2026-10-08" in text


def test_format_suggestions_without_enemies(data):
    text = draft.format_suggestions(data, draft.suggest(data, [], []), [], [], 2, name, roles)
    assert "ranked by overall win rate" in text
    assert "1. Two (roles: Carry) — overall 60.0% in 1,000 games" in text


def test_format_items_uses_names_and_counts():
    popularity = {"start_game_items": {"16": 176, "44": 75, "999": 3}, "early_game_items": {}}
    lines = draft.format_items(popularity, {16: "Iron Branch", 44: "Tango"})
    assert lines[0] == "Starting items: Iron Branch (176), Tango (75), item 999 (3)"
    assert lines[1] == "Early items (first 10 min, cost at least 700): none listed"


def test_format_lane(data):
    text = draft.format_lane(data, 1, [3, 4], name, ["Starting items: Tango (1)"], "Items: fetched live.")
    assert "- vs Three: 50 games, 35 wins (70.0% raw), 60.0% smoothed, advantage +10.00 pts" in text
    assert "- vs Four: 150 games, 60 wins (40.0% raw), 42.5% smoothed, advantage -7.50 pts" in text
    assert "from PRO games" in text
    assert "Starting items: Tango (1)" in text


def test_format_lane_without_items(data):
    assert "Items unavailable." in draft.format_lane(data, 1, [3], name, None, "Items: unreachable.")


def test_age_text(data):
    text = data.age_text(now=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc))
    assert "collected 2026-10-18 12:00 UTC (2.0 days ago)" in text


def test_load_reports_missing_data(tmp_path):
    with pytest.raises(store.DataMissing, match="dota-draft-collect"):
        store.load(tmp_path / "aggregate.json")


def test_load_reads_file(tmp_path):
    path = tmp_path / "aggregate.json"
    path.write_text(json.dumps(AGGREGATE), encoding="utf-8")
    assert store.load(path).matches == 2000
