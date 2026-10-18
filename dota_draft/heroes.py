"""Hero list and name matching (Tool 1)."""

import difflib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Community short names that the other rules do not find, or find ambiguously
# (e.g. "es" is also the initials of Ember Spirit and Earth Spirit).
ALIASES = {
    "wr": "Windranger",
    "es": "Earthshaker",
    "vs": "Vengeful Spirit",
    "ns": "Night Stalker",
    "ls": "Lifestealer",
    "naix": "Lifestealer",
    "bs": "Bloodseeker",
    "bb": "Bristleback",
    "bm": "Beastmaster",
    "tb": "Terrorblade",
    "ul": "Underlord",
    "abba": "Abaddon",
    "void": "Faceless Void",
    "potm": "Mirana",
    "rhasta": "Shadow Shaman",
}

MIN_PREFIX = 3
TYPO_CUTOFF = 0.75


@dataclass(frozen=True)
class Hero:
    id: int
    name: str
    internal_name: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class Match:
    hero: Hero
    method: str


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def initials(name: str) -> str:
    return "".join(word[0] for word in re.split(r"[\s\-]+", name) if word).lower()


class HeroList:
    """Heroes from OpenDota's /heroes, with name matching."""

    def __init__(self, raw: list[dict[str, Any]], fetched_at: datetime | None = None) -> None:
        self.fetched_at = fetched_at or datetime.now(timezone.utc)
        self.heroes = sorted(
            (
                Hero(
                    id=item["id"],
                    name=item["localized_name"],
                    internal_name=item["name"].removeprefix("npc_dota_hero_"),
                    roles=tuple(item.get("roles") or ()),
                )
                for item in raw
            ),
            key=lambda hero: hero.id,
        )
        self.by_name = {normalize(hero.name): hero for hero in self.heroes}
        self.by_internal = {normalize(hero.internal_name): hero for hero in self.heroes}

    def find(self, query: str) -> list[Match]:
        """Return the best matches for a hero name: one hero, several if ambiguous, or none."""
        q = normalize(query)
        if not q:
            return []
        if q in self.by_name:
            return [Match(self.by_name[q], "name")]
        if q in self.by_internal:
            return [Match(self.by_internal[q], "internal name")]
        if q in ALIASES:
            return [Match(self.by_name[normalize(ALIASES[q])], "short name")]
        found = [Match(h, "initials") for h in self.heroes if len(q) >= 2 and initials(h.name) == q]
        if found:
            return found
        if len(q) >= MIN_PREFIX:
            found = [
                Match(h, "start of name")
                for h in self.heroes
                if normalize(h.name).startswith(q) or normalize(h.internal_name).startswith(q)
            ]
            if found:
                return found
        keys = {**self.by_internal, **self.by_name}
        close = difflib.get_close_matches(q, keys, n=3, cutoff=TYPO_CUTOFF)
        heroes = list(dict.fromkeys(keys[key] for key in close))
        return [Match(hero, "closest spelling") for hero in heroes]
