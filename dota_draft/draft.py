"""Scoring (Tool 2) and lane info (Tool 3)."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from dota_draft.store import MatchupData

PSEUDO_GAMES = 50  # smoothing: games added at the hero's own win rate
TOP_N = 20
ITEMS_SHOWN = 8
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
        lines.append(f"Score = average advantage over the {len(enemies)} enemies. {SCORING_NOTE}")
    else:
        lines.append("No enemies picked yet, so heroes are ranked by overall win rate in the data.")
    lines.append("")
    for rank, s in enumerate(suggestions, 1):
        head = f"{rank}. {name(s.hero_id)} (roles: {roles(s.hero_id)}) —"
        score = f" score {pts(s.score)} |" if enemies else ""
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


def format_lane(
    data: MatchupData,
    hero_id: int,
    enemies: Sequence[int],
    name: Callable[[int], str],
    item_lines: list[str] | None,
    item_note: str,
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
        data.age_text(),
    ]
    return "\n".join(lines)
