import json

import pytest

from dota_draft.collect import (
    MATCH_FILTER,
    build_aggregate,
    collect,
    fetch_split,
    merge_pairs,
    merge_summaries,
    write_json,
)
from dota_draft.opendota import OpenDotaError, QueryTimeout

DAY = 86400
NOW = 100 * DAY


def pairs_from_matches(matches):
    """Python version of the collector's pair SQL: (radiant, dire, radiant_win) -> pairs."""
    pairs = {}
    for radiant, dire, radiant_win in matches:
        for a in radiant:
            for b in dire:
                key = (min(a, b), max(a, b))
                entry = pairs.setdefault(key, [0, 0])
                entry[0] += 1
                entry[1] += int((a < b) == radiant_win)
    return [[a, b, g, w] for (a, b), (g, w) in sorted(pairs.items())]


def make_slice(matches, lo=0):
    return {"lo": lo, "hi": lo + 100, "matches": len(matches), "first_start": lo, "last_start": lo + 1,
            "filters": MATCH_FILTER, "pairs": pairs_from_matches(matches)}


def test_aggregate_hero_totals_and_pairs():
    m1 = ([1, 2, 3, 4, 5], [6, 7, 8, 9, 10], True)  # radiant wins
    m2 = ([1, 6, 11, 12, 13], [2, 7, 14, 15, 16], False)  # dire wins
    agg = build_aggregate([make_slice([m1], lo=0), make_slice([m2], lo=100)], built_at=NOW)

    assert agg["matches"] == 2
    assert agg["slices"] == 2
    assert agg["heroes"]["1"] == {"games": 2, "wins": 1}
    assert agg["heroes"]["2"] == {"games": 2, "wins": 2}
    assert agg["heroes"]["6"] == {"games": 2, "wins": 0}
    assert agg["heroes"]["7"] == {"games": 2, "wins": 1}
    pairs = {(a, b): (g, w) for a, b, g, w in agg["pairs"]}
    assert pairs[(1, 2)] == (1, 0)  # m2: hero 1 (radiant) lost to hero 2
    assert pairs[(1, 6)] == (1, 1)  # m1: hero 1 beat hero 6; in m2 they were teammates
    assert pairs[(6, 7)] == (1, 0)  # m2: hero 6 (radiant) lost to hero 7
    assert (1, 11) not in pairs  # teammates only


def test_pairs_split_on_timeout():
    calls = []

    def query(lo, hi):
        calls.append((lo, hi))
        if hi - lo > 250:
            raise QueryTimeout("too big")
        return {(1, 2): [hi - lo, 1]}

    assert fetch_split(query, merge_pairs, 0, 1000) == {(1, 2): [1000, 4]}
    assert (0, 250) in calls and (750, 1000) in calls


def test_summary_split_on_timeout():
    def query(lo, hi):
        if hi - lo > 500:
            raise QueryTimeout("too big")
        # The first half has no rows at all.
        if lo == 0:
            return {"matches": 0, "first_start": None, "last_start": None}
        return {"matches": 7, "first_start": lo, "last_start": hi}

    assert fetch_split(query, merge_summaries, 0, 1000) == {"matches": 7, "first_start": 500, "last_start": 1000}


def test_split_gives_up_after_max_depth():
    def query(lo, hi):
        raise QueryTimeout("always")

    with pytest.raises(QueryTimeout):
        fetch_split(query, merge_pairs, 0, 1000)


def test_write_json_refuses_outside_data_dir(tmp_path):
    with pytest.raises(ValueError):
        write_json(tmp_path / ".." / "outside.json", {}, tmp_path / "data")


class FakeSource:
    """Slices of 100 IDs; slice k's newest match started at LAST_START[k]."""

    LAST_START = {4: NOW - 3600, 3: NOW - 1 * DAY, 2: NOW - 10 * DAY, 1: NOW - 40 * DAY, 0: NOW - 50 * DAY}

    def __init__(self, fail_pairs=False):
        self.pair_calls = []
        self.fail_pairs = fail_pairs

    def newest_match_id(self):
        return 549

    def slice_summary(self, lo, hi):
        last = self.LAST_START[lo // 100]
        return {"matches": 1, "first_start": last - 100, "last_start": last}

    def slice_pairs(self, lo, hi):
        if self.fail_pairs:
            raise OpenDotaError("network down")
        self.pair_calls.append(lo)
        return {(1, 2): [5, 5]}


def read_aggregate(data_dir):
    return json.loads((data_dir / "aggregate.json").read_text(encoding="utf-8"))


def test_collect_fetches_window_skips_newest_and_rerun_fetches_nothing(tmp_path):
    data_dir = tmp_path / "data"
    source = FakeSource()

    assert collect(source, data_dir, now=NOW, slice_size=100)
    # Slice 4 is under 6 hours old; slice 1 is older than 30 days, which ends the walk.
    assert source.pair_calls == [300, 200]
    assert sorted(p.name for p in (data_dir / "slices").iterdir()) == ["200.json", "300.json"]
    agg = read_aggregate(data_dir)
    assert agg["slices"] == 2 and agg["matches"] == 2
    assert agg["pairs"] == [[1, 2, 10, 10]]
    assert agg["heroes"] == {"1": {"games": 2, "wins": 2}, "2": {"games": 2, "wins": 0}}

    assert collect(source, data_dir, now=NOW, slice_size=100)
    assert source.pair_calls == [300, 200]  # nothing fetched again


def test_collect_deletes_slices_that_leave_the_window(tmp_path):
    data_dir = tmp_path / "data"
    collect(FakeSource(), data_dir, now=NOW, slice_size=100)

    collect(FakeSource(), data_dir, now=NOW + 35 * DAY, slice_size=100)
    assert list((data_dir / "slices").iterdir()) == []
    assert read_aggregate(data_dir)["slices"] == 0


def test_collect_refetches_slices_saved_with_other_filters(tmp_path):
    data_dir = tmp_path / "data"
    old = {"lo": 300, "hi": 400, "filters": "old filter", "matches": 9, "first_start": 0,
           "last_start": NOW - DAY, "pairs": []}
    write_json(data_dir / "slices" / "300.json", old, data_dir)
    source = FakeSource()

    collect(source, data_dir, now=NOW, slice_size=100)
    assert 300 in source.pair_calls
    saved = json.loads((data_dir / "slices" / "300.json").read_text(encoding="utf-8"))
    assert saved["filters"] == MATCH_FILTER


def test_collect_reports_early_stop_and_still_writes_aggregate(tmp_path):
    data_dir = tmp_path / "data"
    assert not collect(FakeSource(fail_pairs=True), data_dir, now=NOW, slice_size=100)
    assert read_aggregate(data_dir)["slices"] == 0
