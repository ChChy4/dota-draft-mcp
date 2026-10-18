from datetime import datetime, timezone

import pytest

from dota_draft.heroes import HeroList
from dota_draft.server import format_find_hero

# A subset of OpenDota's /heroes response, enough to cover every matching rule.
RAW = [
    {"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage", "roles": ["Carry"]},
    {"id": 7, "name": "npc_dota_hero_earthshaker", "localized_name": "Earthshaker", "roles": []},
    {"id": 8, "name": "npc_dota_hero_juggernaut", "localized_name": "Juggernaut", "roles": []},
    {"id": 17, "name": "npc_dota_hero_storm_spirit", "localized_name": "Storm Spirit", "roles": []},
    {"id": 20, "name": "npc_dota_hero_vengefulspirit", "localized_name": "Vengeful Spirit", "roles": []},
    {"id": 27, "name": "npc_dota_hero_shadow_shaman", "localized_name": "Shadow Shaman", "roles": []},
    {"id": 39, "name": "npc_dota_hero_queenofpain", "localized_name": "Queen of Pain", "roles": []},
    {"id": 41, "name": "npc_dota_hero_faceless_void", "localized_name": "Faceless Void", "roles": []},
    {"id": 53, "name": "npc_dota_hero_furion", "localized_name": "Nature's Prophet", "roles": []},
    {"id": 74, "name": "npc_dota_hero_invoker", "localized_name": "Invoker", "roles": []},
    {"id": 91, "name": "npc_dota_hero_wisp", "localized_name": "Io", "roles": []},
    {"id": 106, "name": "npc_dota_hero_ember_spirit", "localized_name": "Ember Spirit", "roles": []},
    {"id": 107, "name": "npc_dota_hero_earth_spirit", "localized_name": "Earth Spirit", "roles": []},
    {"id": 126, "name": "npc_dota_hero_void_spirit", "localized_name": "Void Spirit", "roles": []},
]


@pytest.fixture
def heroes() -> HeroList:
    return HeroList(RAW, fetched_at=datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc))


@pytest.mark.parametrize(
    ("query", "hero_id", "method"),
    [
        ("Anti-Mage", 1, "name"),
        ("anti mage", 1, "name"),
        ("ANTIMAGE", 1, "name"),
        ("Io", 91, "name"),
        ("furion", 53, "internal name"),
        ("wisp", 91, "internal name"),
        ("es", 7, "short name"),
        ("vs", 20, "short name"),
        ("void", 41, "short name"),
        ("am", 1, "initials"),
        ("qop", 39, "initials"),
        ("np", 53, "initials"),
        ("jugg", 8, "start of name"),
        ("invo", 74, "start of name"),
        ("invokr", 74, "closest spelling"),
        ("jugernaut", 8, "closest spelling"),
    ],
)
def test_single_match(heroes, query, hero_id, method):
    matches = heroes.find(query)
    assert [(m.hero.id, m.method) for m in matches] == [(hero_id, method)]


def test_ambiguous_initials_return_all(heroes):
    assert sorted(m.hero.id for m in heroes.find("ss")) == [17, 27]


def test_ambiguous_prefix_returns_all(heroes):
    assert sorted(m.hero.id for m in heroes.find("earth")) == [7, 107]


@pytest.mark.parametrize("query", ["", "   ", "xyzzy"])
def test_no_match(heroes, query):
    assert heroes.find(query) == []


def test_output_single(heroes):
    text = format_find_hero("am", heroes)
    assert "Anti-Mage — hero ID 1 (matched by initials)." in text
    assert "fetched live from OpenDota /heroes at 2026-10-18 12:00 UTC" in text


def test_output_typo_asks_to_confirm(heroes):
    assert "confirm with the user" in format_find_hero("invokr", heroes)


def test_output_ambiguous(heroes):
    text = format_find_hero("ss", heroes)
    assert "matches 2 heroes" in text
    assert "- Storm Spirit — hero ID 17" in text
    assert "- Shadow Shaman — hero ID 27" in text


def test_output_no_match(heroes):
    assert 'No hero matches "xyzzy"' in format_find_hero("xyzzy", heroes)
