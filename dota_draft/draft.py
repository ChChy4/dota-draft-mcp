"""Scoring (Tool 2) and lane info (Tool 3)."""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from dota_draft.store import MatchupData

PSEUDO_GAMES = 50  # smoothing: games added at the hero's own win rate
TOP_N = 20
ITEMS_SHOWN = 8
MIN_SKILL_GAMES = 30
SKILL_WINDOWS = (30, 90)  # days: try the first, fall back to the second if too few games
ORDERS_SHOWN = 3
MAXED_POINTS = 4  # a basic skill is maxed at 4 points
SKILL_RULE = "Base skill advice on this data. Do not suggest builds from memory that the data contradicts."
POSITIONS = {1: "safe lane carry", 2: "mid", 3: "offlane", 4: "soft support", 5: "hard support"}

SCORING_NOTE = (
    f"Advantage vs an enemy = smoothed win rate against it minus the hero's overall win rate. "
    f"Smoothing adds {PSEUDO_GAMES} games at the hero's own win rate, so small samples count less."
)


@dataclass(frozen=True)
class Edge:
    enemy_id: int
    games: int
    wins: int
    smoothed: float
    advantage: float


@dataclass(frozen=True)
class Suggestion:
    hero_id: int
    score: float
    win_rate: float
    games: int
    edges: list[Edge]


def edge(data: MatchupData, hero_id: int, enemy_id: int) -> Edge:
    base = data.hero(hero_id).win_rate
    record = data.versus(hero_id, enemy_id)
    smoothed = (record.wins + PSEUDO_GAMES * base) / (record.games + PSEUDO_GAMES)
    return Edge(enemy_id, record.games, record.wins, smoothed, smoothed - base)


def suggest(data: MatchupData, allies: Sequence[int], enemies: Sequence[int], top_n: int = TOP_N) -> list[Suggestion]:
    """Rank every unpicked hero by its average advantage against the enemies (or by win rate if none)."""
    picked = set(allies) | set(enemies)
    suggestions = []
    for hero_id, record in data.heroes.items():
        if hero_id in picked or not record.games:
            continue
        edges = [edge(data, hero_id, enemy_id) for enemy_id in enemies]
        score = sum(e.advantage for e in edges) / len(edges) if edges else record.win_rate
        suggestions.append(Suggestion(hero_id, score, record.win_rate, record.games, edges))
    suggestions.sort(key=lambda s: s.score, reverse=True)
    return suggestions[:top_n]


def check_heroes(data: MatchupData, groups: dict[str, Sequence[int]], limits: dict[str, tuple[int, int]]) -> str | None:
    """Return an error message if a group has the wrong size, repeats a hero, or uses an unknown ID."""
    seen: set[int] = set()
    for label, ids in groups.items():
        low, high = limits[label]
        if not low <= len(ids) <= high:
            return f"{label} must list {low} to {high} heroes; got {len(ids)}."
        for hero_id in ids:
            if hero_id not in data.heroes:
                return f"Unknown hero ID {hero_id} in {label}. Use find_hero to get valid IDs."
            if hero_id in seen:
                return f"Hero ID {hero_id} appears more than once in the draft."
            seen.add(hero_id)
    return None


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def pts(x: float) -> str:
    return f"{100 * x:+.2f} pts"


def format_suggestions(
    data: MatchupData,
    suggestions: list[Suggestion],
    allies: Sequence[int],
    enemies: Sequence[int],
    position: int,
    name: Callable[[int], str],
    roles: Callable[[int], str],
) -> str:
    listed = lambda ids: ", ".join(name(i) for i in ids) or "none"
    lines = [
        f"Draft suggestions for position {position} ({POSITIONS[position]})",
        f"Allies: {listed(allies)} | Enemies: {listed(enemies)}",
    ]
    if enemies:
        lines.append(
            f"Score = average advantage over the {len(enemies)} enemies. {SCORING_NOTE} "
            "Expected win rate in this draft = overall win rate + score. Ranked by score."
        )
    else:
        lines.append("No enemies picked yet, so heroes are ranked by overall win rate in the data.")
    lines.append("")
    for rank, s in enumerate(suggestions, 1):
        head = f"{rank}. {name(s.hero_id)} (roles: {roles(s.hero_id)}) —"
        score = f" score {pts(s.score)} | expected {pct(s.win_rate + s.score)} in this draft |" if enemies else ""
        lines.append(f"{head}{score} overall {pct(s.win_rate)} in {s.games:,} games")
        if s.edges:
            lines.append("   " + "; ".join(f"vs {name(e.enemy_id)} {pts(e.advantage)} ({e.games:,} games)" for e in s.edges))
    lines += [
        "",
        f"The data has no positions: use the roles to choose heroes that fit position {position}.",
        data.age_text(),
    ]
    return "\n".join(lines)


def format_items(popularity: dict[str, dict[str, int]], item_names: dict[int, str]) -> list[str]:
    def top(phase: str) -> str:
        counts = sorted(popularity.get(phase, {}).items(), key=lambda kv: -kv[1])[:ITEMS_SHOWN]
        return ", ".join(f"{item_names.get(int(item_id), f'item {item_id}')} ({count})" for item_id, count in counts) or "none listed"

    return [
        f"Starting items: {top('start_game_items')}",
        f"Early items (first 10 min, cost at least 700): {top('early_game_items')}",
    ]


@dataclass(frozen=True)
class SkillSummary:
    days: int
    games: int
    games_in_first_window: int
    first_start: int | None
    last_start: int | None
    level1: list[tuple[int, int]]  # (ability ID, games), most common first
    maxed_first: list[tuple[int, int]]
    maxed_games: int  # games where some basic skill reached MAXED_POINTS
    orders: list[tuple[tuple[int, ...], int]]  # levels 1-6

    @property
    def enough(self) -> bool:
        return self.games >= MIN_SKILL_GAMES


def first_maxed(build: Sequence[int], is_talent: Callable[[int], bool]) -> int | None:
    """The first non-talent skill to reach MAXED_POINTS in a build, if any."""
    points: Counter[int] = Counter()
    for ability_id in build:
        if is_talent(ability_id):
            continue
        points[ability_id] += 1
        if points[ability_id] == MAXED_POINTS:
            return ability_id
    return None


def summarize_skills(
    rows: Sequence[tuple[int, Sequence[int]]], now: float, is_talent: Callable[[int], bool]
) -> SkillSummary:
    """rows: (start_time, skill order) for the longest window. Uses the shortest window with enough games."""
    windows = {days: [build for start, build in rows if start >= now - days * 86400] for days in SKILL_WINDOWS}
    days = next((d for d in SKILL_WINDOWS if len(windows[d]) >= MIN_SKILL_GAMES), SKILL_WINDOWS[-1])
    builds = windows[days]
    starts = [start for start, _ in rows if start >= now - days * 86400]
    maxed = Counter(m for m in (first_maxed(b, is_talent) for b in builds) if m is not None)
    return SkillSummary(
        days=days,
        games=len(builds),
        games_in_first_window=len(windows[SKILL_WINDOWS[0]]),
        first_start=min(starts, default=None),
        last_start=max(starts, default=None),
        level1=Counter(b[0] for b in builds if b).most_common(),
        maxed_first=maxed.most_common(),
        maxed_games=sum(maxed.values()),
        orders=Counter(tuple(b[:6]) for b in builds if len(b) >= 6).most_common(ORDERS_SHOWN),
    )


def format_skills(summary: SkillSummary, ability: Callable[[int], str]) -> list[str]:
    day = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    first = SKILL_WINDOWS[0]
    if not summary.enough:
        return [
            f"Skill builds: not enough data. Only {summary.games} pro games with this hero in the last "
            f"{summary.days} days (need {MIN_SKILL_GAMES}).",
            "Do not present a skill build from memory as data-backed; say there is not enough data.",
        ]
    lines = [
        f"Skill builds (from PRO/league matches, last {summary.days} days: {summary.games} games, "
        f"{day(summary.first_start)} to {day(summary.last_start)}):"
    ]
    if summary.days != first:
        lines.append(
            f"Only {summary.games_in_first_window} games in the last {first} days, so this uses the last {summary.days} days."
        )
    share = lambda n, total: f"{100 * n / total:.0f}% ({n})"
    lines.append(SKILL_RULE)
    lines.append("Level 1: " + ", ".join(f"{ability(a)} {share(n, summary.games)}" for a, n in summary.level1))
    if summary.maxed_first:
        lines.append(
            "Maxed first: "
            + ", ".join(f"{ability(a)} {share(n, summary.maxed_games)}" for a, n in summary.maxed_first)
            + f" (of {summary.maxed_games} games where a basic skill reached {MAXED_POINTS} points)"
        )
    lines.append("Most common orders, levels 1-6:")
    lines += [f"- {', '.join(ability(a) for a in order)} — {n} games" for order, n in summary.orders]
    return lines


def format_lane(
    data: MatchupData,
    hero_id: int,
    enemies: Sequence[int],
    name: Callable[[int], str],
    item_lines: list[str] | None,
    item_note: str,
    skill_lines: list[str] | None = None,
    skill_note: str = "",
) -> str:
    base = data.hero(hero_id)
    lines = [
        f"Lane info: {name(hero_id)} vs {', '.join(name(e) for e in enemies)}",
        f"{name(hero_id)} overall: {pct(base.win_rate)} in {base.games:,} games. {SCORING_NOTE}",
        "",
        "Matchups:",
    ]
    for e in (edge(data, hero_id, enemy_id) for enemy_id in enemies):
        raw = pct(e.wins / e.games) if e.games else "n/a"
        lines.append(
            f"- vs {name(e.enemy_id)}: {e.games:,} games, {e.wins:,} wins ({raw} raw), "
            f"{pct(e.smoothed)} smoothed, advantage {pts(e.advantage)}"
        )
    lines += [
        "These are whole-game win rates, not laning-phase results.",
        "",
        "Popular items for " + name(hero_id) + " (from PRO games, not Divine/Immortal pubs; OpenDota itemPopularity):",
        *(item_lines or ["Items unavailable."]),
        item_note,
        "",
        *(skill_lines or ["Skill builds unavailable."]),
        skill_note,
        data.age_text(),
    ]
    return "\n".join(lines)
