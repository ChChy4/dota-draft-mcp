# dota-draft-mcp

A local MCP server that helps you draft in Dota 2 at Divine/Immortal level. During a draft you
tell Claude the allied heroes, the enemy heroes and your position; Claude uses this server's
data to suggest heroes and plan your lane.

- **Matchups** come from about 1.7 million Divine/Immortal ranked All Pick matches over the last
  30 days, collected from OpenDota and stored on your computer, so draft answers are instant.
- **Items and skill builds** come live from OpenDota's pro (league) matches.

Built with Python, [FastMCP](https://gofastmcp.com) and [uv](https://docs.astral.sh/uv/). Runs
locally over stdio. No API key needed.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/getting-started/installation/) (it installs the
right Python for you) and Git.

```bash
git clone https://github.com/ChChy4/dota-draft-mcp.git
cd dota-draft-mcp
uv sync
uv run pytest
```

## Collect the data (before playing, ideally daily)

```bash
uv run dota-draft-collect
```

This fetches hero-vs-hero games and wins from OpenDota's `public_matches` table and saves them
in `data/`. It keeps only matches with:

- average rank Divine or higher (`avg_rank_tier >= 70`) and at least 3 players with a known rank
- full teams (no missing heroes)
- ranked All Pick (`lobby_type = 7`, `game_mode = 22`), so Turbo and unranked are excluded

Matches are fetched in slices of 1,000,000 match IDs (about 16 hours each). A run fetches only
the slices that are missing, skips the newest 6 hours (their rows are often incomplete), and
deletes slices older than 30 days.

- **First run:** about 46 slices, roughly 10–15 minutes and ~140 requests.
- **Daily run:** 1–2 new slices, a few seconds.

Run it before you play, ideally once a day, so the data stays within the last 30 days. Every
tool says how old the data is. The collector stays within OpenDota's free limits (60 requests
per minute, 3,000 per day) and stops after 300 requests in one run.

## Connect to Claude

### Claude Code

The repository includes a project [`.mcp.json`](.mcp.json), so Claude Code picks the server up
when you start it in this folder. Approve `dota-draft` when asked, then check it:

```bash
claude mcp list
```

### Claude Desktop

Add this to `claude_desktop_config.json` (on Windows `%APPDATA%\Claude\`; for the Microsoft
Store app `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\`), keeping everything
already in the file, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "dota-draft": {
      "command": "C:\\full\\path\\to\\uv.exe",
      "args": ["run", "--directory", "C:\\full\\path\\to\\dota-draft-mcp", "dota-draft-server"]
    }
  }
}
```

Use full paths: Claude Desktop does not start the server in the project folder, and its PATH may
not include uv. Find uv's path with `where uv` (Windows) or `which uv` (macOS/Linux).

## The tools

| Tool | Use it for | Data |
|---|---|---|
| `find_hero(name)` | Turn a hero name into its ID. Accepts full names, short names (`am`, `qop`, `jugg`, `es`), internal names (`furion`) and typos (`invokr`); lists all matches when a name is ambiguous (`ss`). | Live hero list from OpenDota |
| `suggest_heroes(allies, enemies, position)` | The top 20 heroes for your pick: roles, score, expected win rate in this draft, the advantage against each enemy, and sample sizes. | Local Divine/Immortal data |
| `lane_info(my_hero, enemy_laners)` | Your hero against each enemy laner, popular starting and early items, and skill builds (level 1 skill, skill maxed first, most common orders for levels 1–6). | Local data + live pro items and skills |

**How the score works.** For each enemy: the hero's win rate against that enemy, smoothed by
adding 50 games at the hero's own win rate, minus the hero's overall win rate. The score is the
average over the enemies, so every enemy counts equally. Expected win rate in this draft =
overall win rate + score. With no enemies picked yet, heroes are ranked by overall win rate.

**Skill builds** use the last 30 days of pro matches. If a hero has fewer than 30 games, the
last 90 days are used instead (the output says so); if still fewer than 30, the tool says there
is not enough data. The tool tells Claude to base skill advice on this data and not on memory.

Every tool's output ends with lines saying how old its data is and which commit the server
started from.

## Data sources

- [OpenDota API](https://docs.opendota.com): the explorer (`public_matches`, `player_matches`,
  `matches`), `/heroes`, `/heroes/{id}/itemPopularity` and `/constants` (items, abilities).

## Limitations

- **No positions.** The match data does not record positions. `suggest_heroes` returns roles and
  Claude chooses heroes that fit your position.
- **No ally synergy.** Only heroes on opposite teams are measured; nothing says how well two
  allies work together.
- **Whole-game win rates, not laning results.** A good matchup can still be a hard lane.
- **Items and skills come from pro games only**, not Divine/Immortal pubs; OpenDota has skill
  orders only for league matches. Many heroes have few pro games, so their skill builds may fall
  back to 90 days or show "not enough data".
- **Unknown coverage.** `public_matches` is a sample of all matches; OpenDota does not document
  its share.
- **Patches mix.** A new patch within the 30 days blends old and new data.

## Troubleshooting

- **The `Server:` line shows an old commit.** The server only loads code when it starts. In
  Claude Code use `/mcp` → `dota-draft` → **Reconnect**; in Claude Desktop restart the app.
- **"No matchup data".** Run `uv run dota-draft-collect` in the project folder.
- **The collector logs timeouts.** That is expected for older slices: it splits the slice in half
  and retries. If it stops early, run it again; it continues where it stopped.
