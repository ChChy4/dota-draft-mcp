"""FastMCP server with the draft tools (stdio)."""

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

from fastmcp import FastMCP

from dota_draft import draft, store
from dota_draft.heroes import HeroList
from dota_draft.opendota import OpenDotaClient, OpenDotaError, setup_logging

log = logging.getLogger(__name__)

mcp = FastMCP("dota-draft")
client = OpenDotaClient()

_hero_list: HeroList | None = None
_hero_lock = threading.Lock()
_item_names: dict[int, str] | None = None
_item_lock = threading.Lock()
_abilities: tuple[dict[int, str], set[int]] | None = None
_ability_lock = threading.Lock()


def get_hero_list() -> HeroList:
    """Fetch the hero list once per server session and keep it in memory."""
    global _hero_list
    with _hero_lock:
        if _hero_list is None:
            log.info("Fetching hero list from OpenDota")
            _hero_list = HeroList(client.get("/heroes"))
        return _hero_list


def format_find_hero(query: str, heroes: HeroList) -> str:
    matches = heroes.find(query)
    if not matches:
        lines = [f'No hero matches "{query}". Ask the user to check the spelling.']
    elif len(matches) == 1:
        m = matches[0]
        lines = [f"{m.hero.name} — hero ID {m.hero.id} (matched by {m.method})."]
        if m.method == "closest spelling":
            lines.append("This is a guess from a misspelling; confirm with the user if unsure.")
    else:
        lines = [f'"{query}" matches {len(matches)} heroes (by {matches[0].method}). Ask the user which one:']
        lines += [f"- {m.hero.name} — hero ID {m.hero.id}" for m in matches]
    stamp = heroes.fetched_at.strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"Hero list: fetched live from OpenDota /heroes at {stamp} ({len(heroes.heroes)} heroes).")
    return "\n".join(lines)


@mcp.tool
def find_hero(name: str) -> str:
    """Find a Dota 2 hero's ID from its name.

    Use this whenever the user names a hero, to get the hero ID the other draft tools need.
    Accepts full names ("Anti-Mage"), short names ("am", "qop", "jugg"), the game's internal
    names ("furion") and misspellings ("invokr"). If several heroes match, it lists them so
    you can ask the user which one they meant.
    """
    try:
        heroes = get_hero_list()
    except OpenDotaError as exc:
        log.error("Hero list unavailable: %s", exc)
        return f"Could not fetch the hero list from OpenDota: {exc}"
    return format_find_hero(name, heroes)


def get_item_names() -> dict[int, str]:
    """Item ID -> display name from /constants/items, fetched once per server session."""
    global _item_names
    with _item_lock:
        if _item_names is None:
            log.info("Fetching item constants from OpenDota")
            items = client.get("/constants/items")
            _item_names = {int(v["id"]): v.get("dname") or key for key, v in items.items() if "id" in v}
        return _item_names


def get_abilities() -> tuple[dict[int, str], set[int]]:
    """Ability ID -> display name, and the IDs that are talents. Fetched once per server session."""
    global _abilities
    with _ability_lock:
        if _abilities is None:
            log.info("Fetching ability constants from OpenDota")
            ids = client.get("/constants/ability_ids")
            details = client.get("/constants/abilities")
            names: dict[int, str] = {}
            talents: set[int] = set()
            # Some keys hold several IDs for one ability, e.g. "3060,1617".
            for key, internal in ids.items():
                for part in key.split(","):
                    if part.strip().isdigit():
                        names[int(part)] = (details.get(internal) or {}).get("dname") or internal
                        if internal.startswith("special_bonus_"):
                            talents.add(int(part))
            _abilities = (names, talents)
        return _abilities


def fetch_skill_rows(data: store.MatchupData, hero_id: int, days: int, now: float) -> list[tuple[int, list[int]]]:
    """(start_time, skill order) for the hero in league matches of the last `days` days."""
    since_id = data.match_id_since(days, now)
    if since_id is None:
        raise OpenDotaError("the matchup data has no match ID range; rerun `uv run dota-draft-collect`")
    rows = client.explorer(
        "SELECT m.start_time, pm.ability_upgrades_arr AS skills"
        " FROM player_matches pm JOIN matches m ON m.match_id = pm.match_id"
        f" WHERE pm.hero_id = {int(hero_id)} AND pm.match_id >= {since_id}"
        f" AND m.start_time >= {int(now - days * 86400)} AND m.leagueid > 0"
        " AND pm.ability_upgrades_arr IS NOT NULL AND cardinality(pm.ability_upgrades_arr) >= 6"
    )
    return [(int(r["start_time"]), [int(a) for a in r["skills"]]) for r in rows]


def skill_section(data: store.MatchupData, hero_id: int) -> tuple[list[str] | None, str]:
    now = time.time()
    try:
        names, talents = get_abilities()
        first, last = draft.SKILL_WINDOWS
        rows = fetch_skill_rows(data, hero_id, first, now)
        if len(rows) < draft.MIN_SKILL_GAMES:
            rows = fetch_skill_rows(data, hero_id, last, now)
    except (OpenDotaError, ValueError, KeyError, TypeError) as exc:
        log.error("Skill data unavailable: %s", exc)
        return None, f"Skill data: could not be fetched from OpenDota ({exc})."
    summary = draft.summarize_skills(rows, now, talents.__contains__)
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ability = lambda a: names.get(a, f"ability {a}")
    return draft.format_skills(summary, ability), f"Skill data: fetched live from the OpenDota explorer at {stamp}."


def hero_labels() -> tuple[Callable[[int], str], Callable[[int], str], str]:
    """Name and roles lookups from the hero list, plus a note on where they came from."""
    try:
        heroes = get_hero_list()
    except OpenDotaError as exc:
        log.error("Hero list unavailable: %s", exc)
        return (lambda i: f"hero {i}"), (lambda i: "unknown"), "Hero names and roles unavailable (OpenDota unreachable)."
    by_id = {h.id: h for h in heroes.heroes}
    name = lambda i: by_id[i].name if i in by_id else f"hero {i}"
    roles = lambda i: ", ".join(by_id[i].roles) if i in by_id and by_id[i].roles else "unknown"
    stamp = heroes.fetched_at.strftime("%Y-%m-%d %H:%M UTC")
    return name, roles, f"Hero names and roles: fetched live from OpenDota /heroes at {stamp}."


@mcp.tool
def suggest_heroes(allies: list[int], enemies: list[int], position: int) -> str:
    """Suggest heroes for the user's pick in a Dota 2 draft, from Divine/Immortal ranked matchup data.

    Use this during a draft once you know the hero IDs (get them with find_hero). Pass the
    allied heroes already picked (0-4, not counting the user), the enemy heroes picked so far
    (0-5) and the user's position (1 = safe lane carry, 2 = mid, 3 = offlane, 4 = soft support,
    5 = hard support). Returns the top 20 unpicked heroes by average advantage against the
    enemies, with roles, per-enemy advantages and sample sizes. The data has no positions, so
    choose the best 5 for the user's position from the roles and your Dota knowledge.
    """
    if position not in draft.POSITIONS:
        return "position must be 1 (safe lane carry), 2 (mid), 3 (offlane), 4 (soft support) or 5 (hard support)."
    try:
        data = store.load()
    except store.DataMissing as exc:
        return str(exc)
    error = draft.check_heroes(data, {"allies": allies, "enemies": enemies}, {"allies": (0, 4), "enemies": (0, 5)})
    if error:
        return error
    name, roles, hero_note = hero_labels()
    text = draft.format_suggestions(data, draft.suggest(data, allies, enemies), allies, enemies, position, name, roles)
    return f"{text}\n{hero_note}"


@mcp.tool
def lane_info(my_hero: int, enemy_laners: list[int]) -> str:
    """Lane matchup info for the user's hero against the enemy heroes in their lane.

    Use this after the draft (or once the lanes are clear) to plan the laning phase. Pass the
    user's hero ID and the IDs of the 1-3 enemy heroes they will lane against (get IDs with
    find_hero). Returns games, wins, smoothed win rate and advantage against each enemy from
    Divine/Immortal ranked data, popular starting and early items for the user's hero, and its
    skill builds: level 1 skill, skill maxed first and the most common orders for levels 1-6
    (items and skills come from pro matches).

    Base all skill advice on the skill data this tool returns. Do not suggest skill builds from
    memory that the data contradicts; if it says there is not enough data, say so.
    """
    try:
        data = store.load()
    except store.DataMissing as exc:
        return str(exc)
    error = draft.check_heroes(data, {"my_hero": [my_hero], "enemy_laners": enemy_laners}, {"my_hero": (1, 1), "enemy_laners": (1, 3)})
    if error:
        return error
    name, _, hero_note = hero_labels()
    try:
        popularity = client.get(f"/heroes/{my_hero}/itemPopularity")
        item_lines = draft.format_items(popularity, get_item_names())
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        item_note = f"Items: fetched live from OpenDota at {stamp}."
    except OpenDotaError as exc:
        log.error("Items unavailable: %s", exc)
        item_lines, item_note = None, "Items: OpenDota could not be reached, so items are missing."
    skill_lines, skill_note = skill_section(data, my_hero)
    text = draft.format_lane(data, my_hero, enemy_laners, name, item_lines, item_note, skill_lines, skill_note)
    return f"{text}\n{hero_note}"


def main() -> None:
    setup_logging()
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
