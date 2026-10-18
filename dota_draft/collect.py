"""Data collection command: fetches matchup slices into data/.

Match IDs are split into fixed ranges ("slices"). Each slice is fetched once from the OpenDota
explorer and saved as data/slices/<first match id>.json. A run walks backward from the newest
match until it leaves the 30-day window, fetching only the slices that are missing, then
deletes slices outside the window and rebuilds data/aggregate.json from the rest. It also
downloads any missing reference hero portraits into data/portraits/ for reading screenshots.
"""

import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from dota_draft.opendota import OpenDotaClient, OpenDotaError, QueryTimeout, setup_logging

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PORTRAITS_DIR_NAME = "portraits"  # reference hero portraits for reading screenshots
PORTRAIT_CDN = "https://cdn.cloudflare.steamstatic.com"

SLICE_SIZE = 1_000_000  # match IDs per slice, about 16 hours of matches
WINDOW_DAYS = 30
SETTLE_HOURS = 6  # the newest rows often have empty teams until they are filled in
MAX_SPLIT_DEPTH = 3
MAX_REQUESTS_PER_RUN = 300
MAX_SLICES_BACK = 200

# Divine/Immortal, at least 3 players with a known rank, full teams, ranked All Pick.
MATCH_FILTER = (
    "avg_rank_tier >= 70 AND num_rank_tier >= 3"
    " AND cardinality(radiant_team) = 5 AND cardinality(dire_team) = 5"
    " AND 0 <> ALL(radiant_team) AND 0 <> ALL(dire_team)"
    " AND lobby_type = 7 AND game_mode = 22"
)

# A pair is stored once as (hero_a, hero_b) with hero_a < hero_b; wins are hero_a's wins.
Pairs = dict[tuple[int, int], list[int]]


class RequestBudgetExceeded(OpenDotaError):
    """Raised when a run would make more requests than MAX_REQUESTS_PER_RUN."""


class ExplorerSource:
    """Runs the collector's SQL on the OpenDota explorer. Bounds are always literal numbers:
    a computed bound (e.g. a max() subquery) makes the same query time out."""

    def __init__(self, client: OpenDotaClient, max_requests: int = MAX_REQUESTS_PER_RUN) -> None:
        self.client = client
        self.max_requests = max_requests
        self.requests = 0

    def _query(self, sql: str) -> list[dict[str, Any]]:
        if self.requests >= self.max_requests:
            raise RequestBudgetExceeded(f"stopped after {self.requests} requests this run")
        self.requests += 1
        return self.client.explorer(sql)

    def newest_match_id(self) -> int:
        rows = self._query("SELECT match_id FROM public_matches ORDER BY match_id DESC LIMIT 1")
        return int(rows[0]["match_id"])

    def slice_summary(self, lo: int, hi: int) -> dict[str, int | None]:
        """Matches kept by the filter, and the time span of all rows in [lo, hi)."""
        rows = self._query(
            f"SELECT count(*) FILTER (WHERE {MATCH_FILTER}) AS matches,"
            " min(start_time) AS first_start, max(start_time) AS last_start"
            f" FROM public_matches WHERE match_id >= {int(lo)} AND match_id < {int(hi)}"
        )
        row = rows[0]
        return {key: None if row[key] is None else int(row[key]) for key in ("matches", "first_start", "last_start")}

    def slice_pairs(self, lo: int, hi: int) -> Pairs:
        rows = self._query(
            "WITH m AS (SELECT radiant_team, dire_team, radiant_win FROM public_matches"
            f" WHERE match_id >= {int(lo)} AND match_id < {int(hi)} AND {MATCH_FILTER}),"
            " p AS (SELECT r AS a, d AS b, radiant_win AS a_win"
            " FROM m, unnest(radiant_team) AS r, unnest(dire_team) AS d)"
            " SELECT least(a, b) AS hero_a, greatest(a, b) AS hero_b, count(*) AS games,"
            " sum(((a < b) = a_win)::int) AS wins_a FROM p GROUP BY 1, 2"
        )
        return {(int(r["hero_a"]), int(r["hero_b"])): [int(r["games"]), int(r["wins_a"])] for r in rows}


def merge_pairs(target: Pairs, source: Pairs) -> Pairs:
    for key, (games, wins) in source.items():
        entry = target.setdefault(key, [0, 0])
        entry[0] += games
        entry[1] += wins
    return target


def merge_summaries(first: dict[str, int | None], second: dict[str, int | None]) -> dict[str, int | None]:
    starts = [s for s in (first["first_start"], second["first_start"]) if s is not None]
    lasts = [s for s in (first["last_start"], second["last_start"]) if s is not None]
    return {
        "matches": (first["matches"] or 0) + (second["matches"] or 0),
        "first_start": min(starts, default=None),
        "last_start": max(lasts, default=None),
    }


def fetch_split(query: Callable[[int, int], Any], merge: Callable[[Any, Any], Any], lo: int, hi: int, depth: int = 0) -> Any:
    """Run query on [lo, hi); on a server timeout, split the range in half and merge the results."""
    try:
        return query(lo, hi)
    except QueryTimeout:
        if depth >= MAX_SPLIT_DEPTH:
            raise
        mid = (lo + hi) // 2
        log.warning("Timeout on %d-%d, splitting in half", lo, hi)
        return merge(fetch_split(query, merge, lo, mid, depth + 1), fetch_split(query, merge, mid, hi, depth + 1))


def write_bytes(path: Path, content: bytes, data_dir: Path) -> None:
    """Write through a temporary file and rename, only inside data_dir."""
    path = path.resolve()
    if not path.is_relative_to(data_dir.resolve()):
        raise ValueError(f"refusing to write outside {data_dir}: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)


def write_json(path: Path, data: Any, data_dir: Path) -> None:
    write_bytes(path, json.dumps(data, separators=(",", ":")).encode("utf-8"), data_dir)


def download(url: str) -> bytes:
    response = httpx.get(url, timeout=30, follow_redirects=True)
    response.raise_for_status()
    return response.content


def collect_portraits(client: OpenDotaClient, data_dir: Path, fetch: Callable[[str], bytes] = download,
                      now: float | None = None) -> int:
    """Download missing reference hero portraits into data_dir/portraits; return how many were fetched."""
    now = time.time() if now is None else now
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    folder = data_dir / PORTRAITS_DIR_NAME
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {"heroes": {}}
    fetched = 0
    for hero in client.get("/constants/heroes").values():
        hero_id = int(hero["id"])
        path = folder / f"{hero_id}.png"
        if path.is_file():
            continue
        url = PORTRAIT_CDN + hero["img"].split("?")[0]
        write_bytes(path, fetch(url), data_dir)
        manifest["heroes"][str(hero_id)] = {"name": hero["localized_name"], "url": url, "downloaded_at": stamp}
        fetched += 1
    manifest["updated_at"] = stamp
    write_json(manifest_path, manifest, data_dir)
    log.info("Portraits: fetched %d new, %d in %s", fetched, len(list(folder.glob("*.png"))), folder)
    return fetched


def load_slices(slices_dir: Path) -> dict[int, dict[str, Any]]:
    slices = {}
    for path in slices_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            slices[int(data["lo"])] = data
        except (ValueError, KeyError) as exc:
            log.warning("Ignoring unreadable slice %s: %s", path.name, exc)
    return slices


def slice_is_valid(data: dict[str, Any], slice_size: int) -> bool:
    return data.get("filters") == MATCH_FILTER and data.get("hi", 0) - data.get("lo", 0) == slice_size


def build_aggregate(slices: list[dict[str, Any]], built_at: float) -> dict[str, Any]:
    pairs: Pairs = {}
    for data in slices:
        merge_pairs(pairs, {(a, b): [g, w] for a, b, g, w in data["pairs"]})
    # Each match puts a hero in 5 pairs (one per enemy), so pair totals / 5 = the hero's matches.
    totals: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for (a, b), (games, wins) in pairs.items():
        totals[a][0] += games
        totals[a][1] += wins
        totals[b][0] += games
        totals[b][1] += games - wins
    return {
        "built_at": datetime.fromtimestamp(built_at, timezone.utc).isoformat(timespec="seconds"),
        "first_start": min((s["first_start"] for s in slices), default=None),
        "last_start": max((s["last_start"] for s in slices), default=None),
        "first_match_id": min((s["lo"] for s in slices), default=None),
        "last_match_id": max((s["hi"] for s in slices), default=None),
        "matches": sum(s["matches"] for s in slices),
        "slices": len(slices),
        "filters": MATCH_FILTER,
        "heroes": {str(h): {"games": g // 5, "wins": w // 5} for h, (g, w) in sorted(totals.items())},
        "pairs": [[a, b, g, w] for (a, b), (g, w) in sorted(pairs.items())],
    }


def collect(source: Any, data_dir: Path, now: float | None = None, slice_size: int = SLICE_SIZE) -> bool:
    """Fetch missing slices, drop old ones, rebuild the aggregate. Returns False if the walk stopped early."""
    now = time.time() if now is None else now
    cutoff = now - WINDOW_DAYS * 86400
    settled = now - SETTLE_HOURS * 3600
    slices_dir = data_dir / "slices"
    stored = {lo: s for lo, s in load_slices(slices_dir).items() if slice_is_valid(s, slice_size)}

    complete = True
    fetched = 0
    try:
        newest = source.newest_match_id()
        top = (newest + 1) // slice_size - 1  # newest slice whose IDs are all <= newest
        for k in range(top, top - MAX_SLICES_BACK, -1):
            lo, hi = k * slice_size, (k + 1) * slice_size
            if lo in stored:
                if stored[lo]["last_start"] < cutoff:
                    break
                continue
            summary = fetch_split(source.slice_summary, merge_summaries, lo, hi)
            if summary["last_start"] is None:
                log.info("Slice %d has no matches at all, skipping", lo)
                continue
            if summary["last_start"] < cutoff:
                break
            if summary["last_start"] > settled:
                log.info("Slice %d is newer than %d hours, leaving it for a later run", lo, SETTLE_HOURS)
                continue
            pairs = fetch_split(source.slice_pairs, merge_pairs, lo, hi)
            data = {
                "lo": lo,
                "hi": hi,
                "filters": MATCH_FILTER,
                "fetched_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                **summary,
                "pairs": [[a, b, g, w] for (a, b), (g, w) in sorted(pairs.items())],
            }
            write_json(slices_dir / f"{lo}.json", data, data_dir)
            stored[lo] = data
            fetched += 1
            log.info("Saved slice %d: %d matches, %d pairs", lo, summary["matches"], len(pairs))
        else:
            log.warning("Stopped after %d slices without leaving the %d-day window", MAX_SLICES_BACK, WINDOW_DAYS)
    except OpenDotaError as exc:
        complete = False
        log.error("Collection stopped early: %s", exc)

    kept = {lo: s for lo, s in stored.items() if s["last_start"] >= cutoff}
    for path in slices_dir.glob("*.json"):
        lo = int(path.stem) if path.stem.isdigit() else None
        if lo not in kept:
            log.info("Deleting %s (outside the window or out of date)", path.name)
            path.unlink()

    aggregate = build_aggregate([kept[lo] for lo in sorted(kept)], now)
    write_json(data_dir / "aggregate.json", aggregate, data_dir)
    log.info(
        "Fetched %d new slices; aggregate has %d slices, %d matches, %d hero pairs",
        fetched, aggregate["slices"], aggregate["matches"], len(aggregate["pairs"]),
    )
    return complete


def main() -> None:
    setup_logging()
    client = OpenDotaClient(timeout_s=60)
    source = ExplorerSource(client)
    complete = collect(source, DATA_DIR)
    log.info("Requests this run: %d", source.requests)
    try:
        collect_portraits(client, DATA_DIR)
    except (OpenDotaError, httpx.HTTPError, KeyError, ValueError) as exc:
        complete = False
        log.error("Portrait download stopped early: %s", exc)
    sys.exit(0 if complete else 1)


if __name__ == "__main__":
    main()
