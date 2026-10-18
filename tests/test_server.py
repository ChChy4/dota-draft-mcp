import asyncio
import re

from fastmcp import Client

from dota_draft import server, store
from dota_draft.heroes import HeroList

SHA = "315f48c0123456789abcdef0123456789abcdef0"
SERVER_LINE = re.compile(r"\nServer: started \d{4}-\d\d-\d\d \d\d:\d\d UTC from commit (\w+)\.$")


def make_git(tmp_path, head, files=None):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text(head + "\n", encoding="utf-8")
    for name, content in (files or {}).items():
        (git / name).parent.mkdir(parents=True, exist_ok=True)
        (git / name).write_text(content, encoding="utf-8")
    return tmp_path


def test_commit_from_branch_ref(tmp_path):
    root = make_git(tmp_path, "ref: refs/heads/main", {"refs/heads/main": SHA + "\n"})
    assert server.read_git_commit(root) == "315f48c"


def test_commit_from_packed_refs(tmp_path):
    root = make_git(tmp_path, "ref: refs/heads/main", {"packed-refs": f"# pack-refs\n{SHA} refs/heads/main\n"})
    assert server.read_git_commit(root) == "315f48c"


def test_commit_from_detached_head(tmp_path):
    assert server.read_git_commit(make_git(tmp_path, SHA)) == "315f48c"


def test_commit_unknown_without_git(tmp_path):
    assert server.read_git_commit(tmp_path) == "unknown"


def test_server_line_uses_start_values():
    text = server.with_server_line("hello")
    match = SERVER_LINE.search(text)
    assert text.startswith("hello\n") and match
    assert match.group(1) == server.STARTED_COMMIT


def test_every_tool_ends_with_server_line(monkeypatch):
    raw = [{"id": 1, "name": "npc_dota_hero_antimage", "localized_name": "Anti-Mage", "roles": []}]
    monkeypatch.setattr(server, "get_hero_list", lambda: HeroList(raw))

    def no_data():
        raise store.DataMissing("No matchup data.")

    monkeypatch.setattr(store, "load", no_data)
    calls = {
        "find_hero": {"name": "am"},
        "suggest_heroes": {"allies": [], "enemies": [], "position": 9},  # error path
        "lane_info": {"my_hero": 1, "enemy_laners": [2]},  # no data path
    }

    async def run():
        async with Client(server.mcp) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert names == set(calls)  # a new tool must be added here too
            return {name: (await client.call_tool(name, args)).content[0].text for name, args in calls.items()}

    for name, text in asyncio.run(run()).items():
        assert SERVER_LINE.search(text), f"{name} output has no server line:\n{text}"
