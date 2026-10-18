"""FastMCP server with the draft tools (stdio)."""

import logging
import threading

from fastmcp import FastMCP

from dota_draft.heroes import HeroList
from dota_draft.opendota import OpenDotaClient, OpenDotaError, setup_logging

log = logging.getLogger(__name__)

mcp = FastMCP("dota-draft")
client = OpenDotaClient()

_hero_list: HeroList | None = None
_hero_lock = threading.Lock()


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


def main() -> None:
    setup_logging()
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
