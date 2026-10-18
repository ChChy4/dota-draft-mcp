"""Reads data/aggregate.json and reports data age."""

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dota_draft.collect import DATA_DIR

AGGREGATE_PATH = DATA_DIR / "aggregate.json"


class DataMissing(Exception):
    """Raised when the collector has not produced data yet."""


@dataclass(frozen=True)
class Record:
    games: int
    wins: int

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.games if self.games else None


class MatchupData:
    """Hero totals and hero-vs-hero games/wins from the collector's aggregate."""

    def __init__(self, aggregate: dict) -> None:
        self.matches: int = aggregate["matches"]
        self.first_start: int | None = aggregate["first_start"]
        self.last_start: int | None = aggregate["last_start"]
        self.first_match_id: int | None = aggregate.get("first_match_id")
        self.last_match_id: int | None = aggregate.get("last_match_id")
        self.built_at = datetime.fromisoformat(aggregate["built_at"])
        self.heroes = {int(h): Record(v["games"], v["wins"]) for h, v in aggregate["heroes"].items()}
        self._pairs = {(a, b): (games, wins) for a, b, games, wins in aggregate["pairs"]}

    def hero(self, hero_id: int) -> Record:
        return self.heroes.get(hero_id, Record(0, 0))

    def versus(self, hero_id: int, enemy_id: int) -> Record:
        """Games and wins of hero_id against enemy_id."""
        if hero_id < enemy_id:
            games, wins = self._pairs.get((hero_id, enemy_id), (0, 0))
            return Record(games, wins)
        games, wins = self._pairs.get((enemy_id, hero_id), (0, 0))
        return Record(games, games - wins)

    def match_id_since(self, days: int, now: float) -> int | None:
        """A match ID at or before the first match of the last `days` days, estimated from the
        collected range (IDs grow roughly evenly with time), with a 10% margin."""
        if None in (self.first_match_id, self.last_match_id, self.first_start, self.last_start):
            return None
        ids_per_second = (self.last_match_id - self.first_match_id) / max(self.last_start - self.first_start, 1)
        seconds_back = days * 86400 + max(now - self.last_start, 0)
        return max(int(self.last_match_id - ids_per_second * seconds_back * 1.1), 0)

    def age_text(self, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        day = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        age_days = (now - self.built_at).total_seconds() / 86400
        span = f"{day(self.first_start)} to {day(self.last_start)}" if self.first_start else "no matches"
        return (
            f"Matchup data: {self.matches:,} Divine/Immortal ranked All Pick matches from {span}; "
            f"collected {self.built_at.strftime('%Y-%m-%d %H:%M UTC')} ({age_days:.1f} days ago)."
        )


_cache: tuple[Path, float, MatchupData] | None = None
_lock = threading.Lock()


def load(path: Path = AGGREGATE_PATH) -> MatchupData:
    """Load the aggregate, reloading only when the collector has rewritten it."""
    global _cache
    with _lock:
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            raise DataMissing(
                f"No matchup data at {path}. Run `uv run dota-draft-collect` in the project folder first."
            ) from None
        if _cache is None or _cache[:2] != (path, mtime):
            _cache = (path, mtime, MatchupData(json.loads(path.read_text(encoding="utf-8"))))
        return _cache[2]
