# Dota 2 Draft Helper MCP Server — Plan

Approved plan for a local MCP server that helps draft in Dota 2 at Divine/Immortal level,
using matchup data from OpenDota's `public_matches` table.

**Status (October 2026):** Phases 0–6 built, tested and pushed. This file describes the server
as built, including the changes made during the build (marked "changed during the build").

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

A separate command run by hand (before playing, ideally daily), not an MCP tool. It is the only
part that writes files, and only inside the project's `data/` folder.

1. Ask the explorer for the newest `match_id` with `ORDER BY match_id DESC LIMIT 1`.
   *Changed during the build:* every query uses literal ID bounds. A bound computed in SQL
   (a `max(match_id)` subquery) made the same query time out; with literal bounds it took 0.8 s.
2. Split match IDs into fixed ranges ("slices") of 1,000,000 IDs (~16 hours). Fixed ranges let a
   rerun see exactly which slices are missing.
3. Two explorer queries per slice: a summary (matches kept, earliest and latest `start_time`),
   then games and wins for every hero pair (each pair stored once, A vs B; B vs A is derived).
   Filters:
   - `avg_rank_tier >= 70` (Divine and Immortal)
   - `num_rank_tier >= 3`
   - full teams: 5 heroes per side, no zeros
   - ranked All Pick: `lobby_type = 7 AND game_mode = 22` (approved in Phase 2 after checking
     real data against `/constants`: every ranked match in a 16-hour sample was mode 22, 87% of
     the high-rank matches; Turbo is mode 23 in unranked lobbies)
4. Walk backward from the newest slice until a slice's newest match is more than 30 days old.
   Slices outside the 30-day window are deleted; slices saved with other filters are refetched.
5. Skip slices with matches from the last 6 hours, because the newest rows often have empty teams
   until they are filled in later.
6. If a query times out, split the slice in half and retry, up to 3 levels deep.
   *Changed during the build:* both queries split, not only the pairs query — the first full run
   stopped because a summary query timed out.
7. Pace requests at least 1.1 s apart and stop after 300 requests per run. The first full
   collection was ~140 requests in about 12 minutes (most older slices time out once before
   splitting); a daily run is 2–5 requests in a few seconds.
8. Write each file through a temporary file and rename, so an interrupted run leaves no
   half-written file. Finish by building `data/aggregate.json` from all slices, including the
   match ID range it covers (*added in Phase 4*, used by the skill query).

Result of the first full run: 46 slices, 1,693,237 matches, all 8,001 hero pairs (median 3,353
games per pair, 19 pairs under 100).

## MCP tools

The tools only read local data and the OpenDota API. Each tool has a description that tells
Claude when to use it, returns plain text, and says how old its data is. The server rereads
`aggregate.json` when the collector rewrites it, without a restart.

*Added during the build:* every tool's output ends with
`Server: started <time> from commit <hash>`, read from `.git` when the server starts. A running
server keeps the code it started with, so an old commit here means it needs a restart.

### Tool 1 — find_hero

- Fetches the hero list from `/heroes` once per server session and keeps it in memory.
- Matching order (*changed during the build*): exact name → the game's internal name
  ("furion" → Nature's Prophet) → short-name aliases (wr, es, vs, ns, …) → initials ("qop") →
  start of a name, 3+ letters ("jugg") → closest spelling for typos. Aliases come before
  initials because some initials are shared ("es" is also Ember Spirit and Earth Spirit); the
  "start of name" rule was added so short names like "jugg" match.
- If several heroes match ("ss": Storm Spirit and Shadow Shaman), all are listed so Claude asks.

### Tool 2 — suggest_heroes

Input: allied heroes (0 to 4), enemy heroes (0 to 5), my position (1–5).

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
With enemies picked, each hero also shows its **expected win rate in this draft** = p_h + score
(*added in Phase 4*; ranking stays by score). Invalid input (wrong group sizes, repeated heroes,
unknown IDs, position outside 1–5) returns an error message.

### Tool 3 — lane_info

For my hero against each enemy laner (1 to 3): real games and wins, raw and smoothed win rate
and advantage (same formula as Tool 2), from local data. Plus the top 8 starting items and top 8
early items (first 10 minutes) from `/heroes/{hero_id}/itemPopularity`, with item IDs converted
to names using `/constants/items`. The output notes that items come from pro games.

Skill builds (*added in Phase 4*), from `player_matches.ability_upgrades_arr` in league (pro)
matches via the explorer — the only matches that have skill orders:

- How often each skill is taken at level 1.
- Which basic skill is maxed first (the first non-talent skill to reach 4 points), with
  percentages.
- The 3 most common orders for levels 1–6, each with its number of games.
- 30-day window; if the hero has fewer than 30 games, a 90-day window, said in the output; if
  still under 30, "not enough data".
- Marked as coming from pro matches. The description and output tell Claude to base skill advice
  on this data and not to suggest builds from memory that the data contradicts.
- Ability names come from `/constants/ability_ids` and `/constants/abilities`; talents
  (`special_bonus_*`) are excluded. *Changed during the build:* some `ability_ids` keys hold
  several IDs ("3060,1617") and are split.

If OpenDota is unreachable or returns unexpected data, the matchup part is still returned and
the output says items or skills are unavailable. Speed: ~6 s for the first call of a session
(hero, item and ability lists are fetched once), ~3 s after that.

## Connecting to Claude

*Added during the build.*

- **Claude Code:** the project `.mcp.json` runs `uv run dota-draft-server` from the project
  folder (project scope, nothing changed outside the project).
- **Claude Desktop:** an `mcpServers.dota-draft` entry in `claude_desktop_config.json` with full
  paths to `uv.exe` and the project (`uv run --directory <project> dota-draft-server`), because
  Desktop does not start servers in the project folder and its PATH did not include uv. On this
  machine the app is a Microsoft Store install, so the file is
  `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\` (the same file appears at
  `%APPDATA%\Claude\` from inside the app).

## Files

```
PLAN.md                 this plan
CLAUDE.md               project rules for Claude sessions
README.md               setup, collecting data, connecting to Claude, tools, limitations
pyproject.toml          uv project (scripts: dota-draft-server, dota-draft-collect)
uv.lock
.mcp.json               Claude Code project server config
.gitignore              ignores data/, .venv/, caches, .claude/settings.local.json, tools log, scratch/
tools/
  install-desktop-config.ps1  adds the server to Claude Desktop's config after the app quits
dota_draft/
  __init__.py
  opendota.py           shared API client: request pacing, retries, explorer queries, stderr logging
  heroes.py             hero list and name matching (Tool 1 logic)
  store.py              reads aggregate.json, data age, match ID estimates
  draft.py              scoring (Tool 2), lane info and skill builds (Tool 3)
  screenshot.py         reading a draft from a Steam screenshot (Tool 4 logic)
  server.py             FastMCP server with the 4 tools and the Server line
  collect.py            the data collection command (matchups and reference portraits)
tests/
  test_heroes.py        name matching
  test_collect.py       pairs, splitting, window, deletion, refetching, portrait download
  test_draft.py         scoring, lane info, skill builds (hand-checkable data)
  test_screenshot.py    slot reading, own slot and side, screenshot checks (synthetic images)
  synthetic.py          builds synthetic screenshots and portraits for tests
  test_server.py        Server line, commit reading, ability ID parsing, every tool via MCP
data/                   created by the collector, not committed (includes data/portraits/)
scratch/                local experiments, not committed
```

## Build order

Each phase ends with a commit and push to GitHub once its tests pass.

### Phase 0 — setup ✔ `c48b835`

1. Write `PLAN.md`.
2. Write `CLAUDE.md`.
3. Create the scaffold: `pyproject.toml`, `.gitignore`, placeholder `README.md`, `dota_draft/`
   with empty modules, `tests/`.
4. `git init`, create the public GitHub repository `dota-draft-mcp`, commit and push with the
   message "scaffold from PLAN.md".

### Phase 1 — hero lookup (Tool 1) ✔ `92302f2`

API client, name matching, server with Tool 1.

- Test: `uv run pytest` with names such as "am", "anti mage", "invokr", "qop", "furion"; then
  the server over stdio against the live hero list.
- Commit: "Phase 1: hero lookup (Tool 1)".

### Phase 2 — data collector ✔ `e681811`

First, read-only queries to check which `game_mode` and `lobby_type` values appear, compared
against `/constants`. Then the collector.

- Test: a full run; a second run should fetch only new slices; compare Anti-Mage totals with
  earlier measurements (51.1% win rate in both).
- Commit: "Phase 2: data collector".

*Between phases:* "Connect server to Claude Code" (`56baf35`) added `.mcp.json` and the
`.gitignore` line for `.claude/settings.local.json`.

### Phase 3 — draft and lane tools (Tools 2 and 3) ✔ `8a613c5`

- Test: unit tests on small data where the right answer can be worked out by hand; then real
  drafts over stdio.
- Commit: "Phase 3: draft and lane tools".

### Phase 4 — skill builds and expected win rate ✔ `315f48c`

Skill builds in Tool 3 and expected win rate in Tool 2, as described above.

- Test: unit tests on hand-made skill orders (level 1 counts, maxed first, top orders, 30/90-day
  fallback, "not enough data"); then real calls over stdio.
- Commit: "Phase 4: skill builds and expected win rate".

*Between phases:* "Show server commit and start time in every tool output" (`e824613`), after an
old server (started between the Phase 3 and Phase 4 commits) showed no expected win rate.

### Phase 5 — README and end-to-end checks

- Unit test for comma-separated keys in `/constants/ability_ids`.
- README: what the server does, setup, collector, connecting to Claude Code and Claude Desktop,
  the tools, data sources, limitations, troubleshooting.
- Connect the server to Claude Desktop (approved config change).
- End-to-end checks: all unit tests; a daily collector run; all three tools called from a real
  Claude Code session (Server line on the latest commit); `lane_info` over stdio on the working
  tree; the Desktop config validated and its command started from outside the project.
- Update this plan with the changes made during the build.
- Commit: "Phase 5: README and end-to-end checks".

### Phase 6 — draft from a Steam screenshot ✔ (approved and built; see "Built" at the end)

**Why.** Typing heroes is too slow in a ranked pick. The prototype in `scratch/` (October 2026)
read 51 of 55 picked portraits on 18 screenshots at 1920×1080, with no wrong heroes, in about
2 s per screenshot (C#/PowerShell, unoptimised); the user's own slot was found by name
brightness on one screenshot per side.

**New tool `draft_from_screenshot(position)`.** In one call:

1. **Find the newest screenshot.** Default: the newest `.jpg` in any
   `<Steam>\userdata\*\760\remote\570\screenshots` folder (Steam path from the registry key
   `HKCU\Software\Valve\Steam\SteamPath`). Override with the environment variable
   `DOTA_SCREENSHOT_DIR` (set in the MCP config `env`, not committed, because the path is
   personal). Only `.jpg` files Steam writes; nothing else on disk is read.
2. **Refuse old screenshots.** If its file time is more than 3 minutes old, return its age and
   ask for a fresh F12, so an old draft is never used.
3. **Refuse other screen sizes.** Only 1920×1080 was measured; anything else returns a clear
   message instead of guessing slot positions.
4. **Read the 10 top-bar slots** (left x = 210, 335, 460, 585, 710; right x = 1095 … 1595;
   match area slot x+28…+88, y 6…38), as in the prototype:
   - for each slot, the best reference portrait over a small search (portrait origin slot x−12…+4,
     y 2…8, scale 0.42…0.50);
   - **picked** if the best hero beats the second best by ≥ 15 (measured: picks 22.7–39.5,
     everything else ≤ 4.5);
   - otherwise **unknown portrait** if the slot is colourful (saturation ≥ 40: the 4 unmatched
     portraits measured 45–99, empty and greyed-out slots ≤ 33), else **empty / not locked in**.
5. **Find my slot and side.** My name is drawn brighter: the 90th-percentile brightness of the
   name text (y 82…100 under each slot) was 225 and 243 for my name and 162–166 for the other
   nine. Rule: the brightest slot if it is ≥ 200 and at least 30 above the next. Second check:
   my team is green on the map (hero-pick minimap x 105…435, y 875…1055; strategy-phase map
   x 850…1125, y 207…483): green bottom-left = Radiant, green top-right = Dire.
   - Both cues agree, or only one gives an answer → use it, and say which cue decided.
   - They disagree, or neither gives an answer → **return both readings** (as Radiant and as Dire,
     each with its suggestions) and say the side could not be determined.
6. **Return**
   - the detected draft: my slot (side and number, and my hero if already picked), allies,
     enemies, unknown slots, empty slots, and the screenshot's file name and age;
   - then exactly what `suggest_heroes(allies, enemies, position)` returns for that draft
     (allies = picked heroes on my side except my own slot);
   - a line telling Claude: during a draft, answer briefly — the top 3 heroes for my position,
     one line each;
   - the Server line, like every tool.
   - Other players' names are never returned or logged; only slot numbers.

**Reference portraits.** The collector downloads `/constants/heroes` (one OpenDota request) and the
127 portrait images from Valve's CDN into `data/portraits/<hero id>.png` with a small
`data/portraits/manifest.json` (source URL, download time). Only missing portraits are fetched;
a new hero is picked up on the next run. The server only reads them and builds its matching
tables in memory on the first call (target: under 1 s, measured in 6c).

**Python port.** The matcher moves to Python with **Pillow** (JPEG/PNG decoding) and **numpy**
(vectorised comparison). These are **two new dependencies** and need approval. Plan for speed:
all candidate portrait patches (127 heroes × 5 scales × 9 × 4 positions, sampled every 2nd
pixel ≈ 33 MB) are prepared once; each slot is then one vectorised comparison. Target: reading a
screenshot (find + load + 10 slots + side) **under 3 s**, measured, not assumed.

**Files**

| File | Change |
|---|---|
| `pyproject.toml`, `uv.lock` | add `pillow`, `numpy` |
| `dota_draft/collect.py` | download reference portraits into `data/portraits/` |
| `dota_draft/screenshot.py` | **new**: find newest screenshot, age and size checks, slot matching, own slot and side, the detected-draft text |
| `dota_draft/server.py` | new tool `draft_from_screenshot(position)` reusing the `suggest_heroes` code path |
| `tests/test_screenshot.py` | **new**: synthetic images only (see below) |
| `tests/test_collect.py` | portrait download: only missing files fetched, written inside `data/` |
| `tests/test_server.py` | the new tool is included in the "every tool has the Server line" test |
| `README.md`, `PLAN.md` | the tool, the screenshot folder setting, limitations |

**Build order** (each step ends with its tests passing; one commit and push at the end of the
phase, "Phase 6: draft from a Steam screenshot")

- **6a.** Add Pillow and numpy (after approval).
- **6b.** Collector downloads portraits. Test: unit tests with a fake downloader; a real run
  fetches 127 files, a second run fetches 0.
- **6c.** `screenshot.py`: port and vectorise the matcher, own-slot and side detection. Tests on
  synthetic images. Then a **local accuracy and speed check** against the 18 draft screenshots in
  the Steam folder (not committed): expect 51 of 55 picks, 0 wrong heroes, my slot found on the two
  screenshots where it is known, and the measured time per screenshot.
- **6d.** The tool in `server.py`. Tests through the in-memory MCP client with a temporary
  screenshot folder holding synthetic images: fresh vs. too old, wrong size, picks, unknown slot,
  side found, side not found (both readings), the brief-answer line, the Server line.
- **6e.** README and PLAN; end to end: restart Claude, take an F12 in a practice game with bots,
  call the tool from Claude Code and Claude Desktop, time it.

**Testing without screenshots in the repo.** Full screenshots show other players' names and are
never committed. Tests build a synthetic 1920×1080 image: generated "portraits" (patterned noise,
not game art) placed into slots at the measured scale and offsets, grey boxes for empty slots,
desaturated copies for not-locked-in heroes, a bright and dim text-like bar for names, and green
and red blobs for the map. The real-screenshot check in 6c runs locally only.

**Risks**

- **One resolution, one language.** Only 1920×1080 and Russian-language screens were measured.
  Other sizes are refused; another language or UI scale could move the name text.
- **Own-slot detection rests on two screenshots** (one per side). Very short names or other
  scripts may measure differently; the map check and "both readings" are the fallback.
- **Portrait art changes** (Valve updates, personas, cosmetics) give "unknown", as for 4 old
  portraits. New heroes appear only after the collector has downloaded their portrait.
- **Not-locked-in heroes are ignored** — an F12 taken while someone is still hovering misses them.
- **Speed is unknown in Python** until 6c measures it; the 3 s target may need a coarser search.
- **Wrong screenshot.** With several Steam accounts, the newest file across all accounts is used;
  an in-game F12 taken after the draft would be read as a draft (expected result: no picks and no
  own slot, reported as "not a draft screenshot?" — to be checked in 6c on in-game screenshots).
- **Privacy.** The tool reads screenshots that contain other players' names; it never returns,
  logs or stores them.

**Built (changed during the build in italics where it differs from the proposal)**

- **6a.** Pillow 13.0.0 and numpy 2.5.4. *Running servers keep `dota-draft-server.exe` open, so
  `uv add` could not reinstall the project; the dependencies were recorded with
  `uv add --no-sync` and installed with `uv sync --inexact --no-install-project`. The failed first
  attempt had already removed `dota-draft-collect.exe`; it comes back with the next `uv sync` run
  while no server is running.*
- **6b.** First real run fetched 127 portraits (8.3 MB); the second fetched 0.
- **6c.** `screenshot.py` reproduces the prototype on all 180 slots of the 18 draft screenshots
  (57 picked, 6 unknown, 117 empty; 0 differences) in 0.54–0.63 s per screenshot (load + read).
  Building the matching tables takes about 1.5 s and loading the portraits about 0.9 s, once.
  *Map rule tightened:* during picking the strategy-map area covers the hero grid and showed red
  only (up to 374 pixels), so a side now needs at least 50 green pixels in one corner **and** 50
  red in the opposite one. *Not-a-draft warning:* on 77 in-game screenshots no slot was read as
  picked, but 13 had bright text where the names would be, so the warning ("no picked heroes
  found") now depends only on there being no picks. On 3 Captains Mode screenshots the name and
  map cues disagree; the tool gives both readings there.
- **6d.** `draft_from_screenshot` in `server.py`. *Added a background warm-up when the server
  starts* (matcher, matchup data, hero list), because the first call right after start took
  3.01 s without it. Measured over stdio on a fresh copy of the newest real screenshot: first
  call 2.70 s right after start, 1.10 s 10 s after start, then about 0.6 s.
- **Tests.** 83 unit tests, the new ones on synthetic images only (`tests/synthetic.py`,
  `tests/test_screenshot.py`, `tests/test_server.py`). The real-screenshot checks are scripts in
  `scratch/` and are not committed.

## Risks

- **Unknown coverage.** `public_matches` is a sample of matches; its share of all games is not
  documented.
- **Rank labels.** `num_rank_tier` is undocumented; the data (values 1–10) fits it being the
  number of players with a known rank. Filtering to `>= 3` kept 89% of Divine+ matches in the
  37-hour sample.
- **No positions, no ally synergy, no lane outcomes.** Tool 2 measures heroes against enemies
  only, and Tool 3's numbers are whole-game win rates, not laning-phase results.
- **Patches get mixed.** A new patch within the 30 days blends old and new data.
- **The explorer can change.** If its timeout or behaviour changes, slice splitting is the
  fallback.
- **Tool 3 needs the internet** for items and skills.
- **Smoothing hides real outliers.** A true counter with few games ranks lower than its raw
  numbers suggest.
- **Skill data is thin and pro-only.** In a 30-day test, all 7,431 skill orders came from league
  matches; the median hero had 38 games and 56 of 125 heroes had under 30.
- **Running servers keep old code** until they restart. The Server line shows which commit a
  server started from.
