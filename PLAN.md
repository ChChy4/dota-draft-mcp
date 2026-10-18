# Dota 2 Draft Helper MCP Server — Plan

Approved plan for a local MCP server that helps draft in Dota 2 at Divine/Immortal level,
using matchup data from OpenDota's `public_matches` table.

## Goal

During a draft I tell Claude the allied heroes, the enemy heroes and my position. Claude uses
this server to suggest heroes for the draft and a laning plan, based on data from Divine and
Immortal public matches.

## Why this design

Findings from testing the OpenDota API (October 2026):

- `/heroes/{hero_id}/matchups` has samples too small to use (median 25–50 games per pair) and
  does not say which matches or ranks it is based on.
- The explorer's `public_matches` table has `avg_rank_tier`, `num_rank_tier` and both teams'
  hero IDs. In about 37 hours there were ~116,000 matches with `avg_rank_tier >= 70`, 99.8% of
  them with full teams.
- Explorer queries over 7 or 30 days time out (server limit about 16 seconds). A query over
  about 2.5 million match IDs (~37 hours) works: ~3 s for one hero, ~6 s for all hero pairs.
- Free API limits: 60 calls per minute, 3,000 per day, no key needed.

So: collect data in daily slices, store it locally, and answer draft questions from local data.

## Stack

- Python, FastMCP, uv. Dependencies: `fastmcp`, `httpx`; `pytest` for tests.
- Local only, stdio transport. Nothing is printed to stdout; all logging goes to stderr.
- No API keys.

## Data collection — `dota-draft-collect`

A separate command run by hand, not an MCP tool. It is the only part that writes files, and
only inside the project's `data/` folder.

1. Ask the explorer for the highest `match_id` (fast primary-key lookup).
2. Split match IDs into fixed ranges of 1,000,000 IDs (~15 hours). Fixed ranges let a rerun
   see exactly which ranges are missing.
3. One explorer query per range returns games and wins for every hero pair (each pair stored
   once, A vs B; B vs A is derived) plus the range's earliest and latest `start_time`.
   Filters:
   - `avg_rank_tier >= 70` (Divine and Immortal)
   - `num_rank_tier >= 3`
   - full teams: 5 heroes per side, no zeros
   - ranked games only: exact `game_mode` and `lobby_type` values are checked against real
     data and `/constants` at the start of Phase 2 before they are hard-coded (Turbo and
     unranked excluded)
4. Walk backward from the newest range until a range's newest match is more than 30 days old.
   Ranges entirely outside the 30-day window are deleted.
5. Skip the newest few hours of matches, because the newest rows often have empty teams until
   they are filled in later.
6. If a range times out, split it in half and retry.
7. Pace requests at least 1.1 s apart. A full 30-day collection is ~50 requests; a daily top-up
   is ~2.
8. Write each file through a temporary file and rename, so an interrupted run leaves no
   half-written file. Finish by building `data/aggregate.json` from all ranges.

## MCP tools

The tools only read local data and the OpenDota API. Each tool has a description that tells
Claude when to use it, returns plain text, and says how old its data is.

### Tool 1 — find a hero ID

- Fetches the hero list from `/heroes` once per server session and keeps it in memory.
- Matching order: exact name → the game's internal name (e.g. "furion" → Nature's Prophet) →
  initials (e.g. "qop") → a short alias list (wr, es, od, …) → closest spelling for typos.

### Tool 2 — suggest heroes

Input: allied heroes, enemy heroes (0 to 5), my position.

For each candidate hero **h**:

- **p_h** = h's overall win rate in the local data (after all filters).
- **Smoothed win rate vs enemy e** = (wins + 50 × p_h) ÷ (games + 50): 50 pseudo-games at the
  hero's own average, so tiny samples cannot top the list.
- **Advantage vs e** = smoothed win rate − p_h.
- **Score** = the average of the advantages against each listed enemy (every enemy counts
  equally).
- With no enemies: rank by p_h, and say so in the output.

Already-picked heroes are never suggested. Returns the top 20 with roles, score, advantage
against each enemy, and the **real** number of games. No minimum-sample cutoff (smoothing
handles small samples). Position is passed through for Claude, since the data has no positions.

### Tool 3 — lane info

For my hero against each enemy laner: real games and wins, smoothed win rate and advantage
(same formula as Tool 2), from local data. Plus starting items and early items (first 10
minutes) from `/heroes/{hero_id}/itemPopularity`, with item IDs converted to names using
`/constants`. The output notes that items come from pro games. If OpenDota is unreachable, the
matchup part is still returned and the output says items are unavailable.

## Files

```
PLAN.md                 this plan
CLAUDE.md               project rules for Claude sessions
pyproject.toml          uv project
README.md               setup, collecting data, connecting to Claude
.gitignore              ignores data/ and .venv/
dota_draft/
  __init__.py
  opendota.py           shared API client: request pacing, retries, stderr logging
  heroes.py             hero list and name matching (Tool 1 logic)
  store.py              reads aggregate.json and reports data age
  draft.py              scoring (Tool 2) and lane info (Tool 3) logic
  server.py             FastMCP server with the 3 tools
  collect.py            the data collection command
tests/
  test_heroes.py
  test_draft.py         small hand-checkable test data
data/                   created by the collector, not committed
```

## Build order

Each phase ends with a commit and push to GitHub once its tests pass.

### Phase 0 — setup

1. Write `PLAN.md`.
2. Write `CLAUDE.md`.
3. Create the scaffold: `pyproject.toml`, `.gitignore`, placeholder `README.md`, `dota_draft/`
   with empty modules, `tests/`.
4. `git init`, create the public GitHub repository `dota-draft-mcp`, commit and push with the
   message "scaffold from PLAN.md".

### Phase 1 — hero lookup (Tool 1)

API client, name matching, server with Tool 1.

- Test: `uv run pytest` with names such as "am", "anti mage", "invokr", "qop", "furion". Then
  connect the server to Claude Code and ask it to look up heroes.
- Commit: "Phase 1: hero lookup (Tool 1)".

### Phase 2 — data collector

First, read-only queries to check which `game_mode` and `lobby_type` values appear, compared
against `/constants`. Then the collector.

- Test: a full run; a second run should fetch only new ranges; compare Anti-Mage totals with
  earlier measurements.
- Commit: "Phase 2: data collector".

### Phase 3 — draft and lane tools (Tools 2 and 3)

- Test: unit tests on small data where the right answer can be worked out by hand; then real
  drafts in Claude.
- Commit: "Phase 3: draft and lane tools".

### Phase 4 — README and end-to-end checks

- Commit: "Phase 4: README and end-to-end checks".

## Risks

- **Unknown coverage.** `public_matches` is a sample of matches; its share of all games is not
  documented.
- **Rank labels.** `num_rank_tier` is undocumented; the data (values 1–10) fits it being the
  number of players with a known rank. Filtering to `>= 3` kept 89% of Divine+ matches in the
  37-hour sample.
- **No positions or lane outcomes.** Tool 3's numbers are whole-game win rates, not
  laning-phase results.
- **Patches get mixed.** A new patch within the 30 days blends old and new data.
- **The explorer can change.** If its timeout or behaviour changes, range splitting is the
  fallback.
- **Tool 3 needs the internet** for items.
- **Smoothing hides real outliers.** A true counter with few games ranks lower than its raw
  numbers suggest.
