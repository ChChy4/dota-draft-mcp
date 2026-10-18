# CLAUDE.md

Local MCP server that helps draft in Dota 2 at Divine/Immortal level, using OpenDota data.
The approved design and build order are in [PLAN.md](PLAN.md). Follow it.

## Commands

- `uv run pytest` — run tests
- `uv run dota-draft-collect` — collect matchup data into `data/`
- `uv run dota-draft-server` — start the MCP server (stdio)

## Rules

- Python, FastMCP, uv. Local only, stdio transport.
- Never print to stdout: it carries the MCP protocol. Log to stderr with `logging`.
- MCP tools only read local data and the OpenDota API. They never write files.
- Only the collector writes files, and only inside the project's `data/` folder.
- Respect OpenDota's free limits: 60 requests per minute, 3,000 per day. No API keys.
- Every tool has a description that tells Claude when to use it, returns plain text, and says
  how old its data is.
- Build only what PLAN.md asks for. Nothing extra.
- Ask the user before restructuring anything.
- Commit and push at the end of every phase, once its tests pass.
