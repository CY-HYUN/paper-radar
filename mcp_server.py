# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.2,<3"]
# ///
"""A thin MCP layer over the radar's files, so a teammate can ask Claude instead of opening them.

    uv run mcp_server.py <workspace>            serve over stdio (what `claude mcp add` runs)
    uv run mcp_server.py --selftest             the six tools against a planted workspace, no MCP transport
    uv run mcp_server.py --smoke <workspace>    copy the workspace to a temp folder, start this server on the copy as
                                                a subprocess, and call every tool over MCP

Read-mostly by design. The weekly collection stays radar.py's scheduled job: an MCP server runs only while an AI
host is open, so a week nobody asks would collect nothing. The two tools that write keep the ledger's own gates:
update_problem rewrites one cell of one row only if ledger.load() still passes on the result (row cap, baseline_ref
shape, forbidden terms in public_query), and start_card is card.py's new_card, which refuses a paper the radar never
surfaced. Nothing here sends a request outside the machine. The MCP SDK is the only dependency and only this file
needs it; the radar itself stays standard library.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import card  # noqa: E402
import ledger  # noqa: E402

EDITABLE = [c for c in ledger.COLUMNS if c != "id"]
TOOLS = ["list_problems", "show_problem", "week_candidates", "pending_updates", "update_problem", "start_card"]


def resolve_ws(arg: str) -> Path:
    """A workspace given relative to this tool folder (`workspace`), or absolute; PAPER_RADAR_WORKSPACE overrides."""
    p = Path(os.environ.get("PAPER_RADAR_WORKSPACE") or arg)
    return p if p.is_absolute() else (HERE / p).resolve()


def list_problems(ws: Path) -> str:
    rows = ledger.load(ws)
    out = [f"{len(rows)} of {ledger.MAX_ROWS} rows · {sum(r.active for r in rows)} active · "
           f"{sum(r.stale for r in rows)} stale (last verified more than {ledger.STALE_DAYS} days ago)"]
    for r in rows:
        state = "stale" if r.stale else r.status
        out.append(f"[{r.id}] {state} · {r.problem} · verified {r.last_verified} · baseline {r.baseline_ref} · "
                   f"seeds {len(r.seed_papers)} · owner {r.owner}")
    return "\n".join(out)


def _row(ws: Path, row_id: str) -> ledger.Row:
    rows = {r.id: r for r in ledger.load(ws)}
    if row_id not in rows:
        raise ledger.LedgerError(f"row {row_id} is not in the ledger (rows: {', '.join(rows)})")
    return rows[row_id]


def show_problem(ws: Path, row_id: str) -> str:
    r = _row(ws, row_id)
    state = "stale" if r.stale else r.status
    return "\n".join([f"row {r.id} ({state})", f"problem: {r.problem}",
                      f"metric, baseline and the command that produced it: {r.metric_baseline_command}",
                      f"baseline_ref (the commit the number was measured on): {r.baseline_ref}",
                      f"failing examples: {r.failing_examples}", f"tried and rejected: {r.tried_rejected}",
                      f"seed papers: {', '.join(r.seed_papers) or '-'}", f"public query: {r.public_query}",
                      f"owner: {r.owner}", f"last verified: {r.last_verified}"])


def _latest(folder: Path, name: str | None) -> Path:
    files = sorted(folder.glob("*.md")) if folder.is_dir() else []
    if not files:
        raise ledger.LedgerError(f"no files in {folder.name}/ yet: the weekly job has not written one")
    if name is None:
        return files[-1]
    hit = [f for f in files if f.stem == name]
    if not hit:
        raise ledger.LedgerError(f"{folder.name}/{name}.md does not exist (have: {', '.join(f.stem for f in files[-5:])})")
    return hit[0]


def week_candidates(ws: Path, week: str | None = None, row_id: str | None = None) -> str:
    path = _latest(ws / "candidates", week)
    text = path.read_text(encoding="utf-8")
    if row_id is None:
        return f"{path.name}\n\n{text}"
    sections = re.split(r"(?m)^(?=## row )", text)
    hit = [s for s in sections if re.match(rf"## row {re.escape(row_id)}\b", s)]
    if not hit:
        return f"{path.name}: no candidates for row {row_id} this week"
    return f"{path.name}\n\n{hit[0].strip()}"


def pending_updates(ws: Path) -> str:
    path = _latest(ws / "proposals", None)
    lines = path.read_text(encoding="utf-8").splitlines()
    open_lines = [ln for ln in lines if ln.startswith("[ ]")]
    decided = sum(1 for ln in lines if re.match(r"^\[[yn]\]", ln))
    head = (f"{path.name}: {len(open_lines)} undecided, {decided} decided. To accept one, edit the ledger "
            f"(update_problem or by hand) and flip its [ ] to [y] in that file; [n] if it is wrong.")
    return "\n".join([head, *open_lines])


def update_problem(ws: Path, row_id: str, field: str, value: str, today: date | None = None) -> str:
    today = today or date.today()
    if field not in EDITABLE:
        raise ledger.LedgerError(f"field must be one of {', '.join(EDITABLE)}; got {field!r}")
    if "|" in value or "\n" in value or not value.strip():
        raise ledger.LedgerError("value must be one non-empty line with no '|' (it is a table cell)")
    path = ws / "ledger.md"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    idx = next((i for i, ln in enumerate(lines)
                if ln.lstrip().startswith("|") and [c.strip() for c in ln.strip().strip("|").split("|")][:1] == [row_id]),
               None)
    if idx is None:
        raise ledger.LedgerError(f"row {row_id} is not in the ledger")
    cells = [c.strip() for c in lines[idx].strip().strip("|").split("|")]
    old = cells[ledger.COLUMNS.index(field)]
    cells[ledger.COLUMNS.index(field)] = value.strip()
    if field != "last_verified":            # a changed row was looked at today; the stale clock restarts
        cells[ledger.COLUMNS.index("last_verified")] = today.isoformat()
    lines[idx] = "| " + " | ".join(cells) + " |" + ("\n" if lines[idx].endswith("\n") else "")
    new_text = "".join(lines)
    with tempfile.TemporaryDirectory() as d:  # validate the result with the ledger's own gates before writing
        shutil.copy(ws / "forbidden_terms.txt", Path(d) / "forbidden_terms.txt")
        (Path(d) / "ledger.md").write_text(new_text, encoding="utf-8")
        ledger.load(Path(d), today)
    path.write_text(new_text, encoding="utf-8")
    return f"row {row_id}: {field} {old!r} -> {value.strip()!r}; last_verified {today}"


def start_card(ws: Path, row_id: str, paper_id: str) -> str:
    out = card.new_card(ws, row_id, paper_id)
    return f"card started: {out.relative_to(ws).as_posix()} (fill the five sections; a card needs its smallest test)"


def build_server(ws: Path):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name="paper-radar", instructions=(
        "The team's open problems (at most ten), each with its metric, the command that measured it and what was "
        "already tried, plus the papers the weekly job matched to them. Read tools first; update_problem changes one "
        "cell and keeps the ledger's gates."))

    @server.tool()
    def list_problems_tool() -> str:
        """List the open problems with their state (open, stale, closed), last verification date and baseline commit."""
        return list_problems(ws)

    @server.tool()
    def show_problem_tool(row_id: str) -> str:
        """Show one problem in full: metric and command, baseline commit, failing examples, what was tried, seeds."""
        return show_problem(ws, row_id)

    @server.tool()
    def week_candidates_tool(week: str | None = None, row_id: str | None = None) -> str:
        """Papers the weekly job matched. week like '2026-W39' (default: latest); row_id limits to one problem."""
        return week_candidates(ws, week, row_id)

    @server.tool()
    def pending_updates_tool() -> str:
        """Ledger updates the weekly job proposed from the team's git history that nobody has decided yet."""
        return pending_updates(ws)

    @server.tool()
    def update_problem_tool(row_id: str, field: str, value: str) -> str:
        """Change one cell of one problem (e.g. a new number after re-running its command). Refused if the ledger's
        gates fail. Resets last_verified to today."""
        return update_problem(ws, row_id, field, value)

    @server.tool()
    def start_card_tool(row_id: str, paper_id: str) -> str:
        """Start a card (precedent, our version, difference, smallest one-day test) for a paper the radar surfaced."""
        return start_card(ws, row_id, paper_id)

    return server


def selftest() -> None:
    header = "| " + " | ".join(ledger.COLUMNS) + " |\n|" + "---|" * len(ledger.COLUMNS) + "\n"
    row1 = "| 1 | score blind to mirror | metric 0.90 · cmd.py | date:2026-09-01 | part A | none | 2510.21862 | shape metric blind to reflection | me | 2026-09-20 | open |\n"
    row2 = "| 2 | fold imitation | metric 0.71 · cmd2.py | - | part B | none | - | detecting sheet-metal bend features | me | 2026-08-01 | open |\n"
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "forbidden_terms.txt").write_text("ACME\n", encoding="utf-8")
        (ws / "ledger.md").write_text("# ledger\n\n" + header + row1 + row2, encoding="utf-8")
        (ws / "candidates").mkdir()
        (ws / "candidates" / "2026-W39.md").write_text(
            "# Candidates\n\n## row 1 — blind (1 new)\n- ARXIV:2609.00001 · paper one\n\n## row 2 — fold (1 new)\n- ARXIV:2609.00002 · paper two\n",
            encoding="utf-8")
        (ws / "proposals").mkdir()
        (ws / "proposals" / "2026-09-27.md").write_text(
            "# proposals\n\n[ ] row 1 | baseline_ref | x -> STALE | source: git\n[y] row 2 | tried_rejected | - -> y | source: a\n",
            encoding="utf-8")
        (ws / "seen.json").write_text('{"papers": {"ARXIV:2609.00001": {"title": "paper one", "first_seen": "2026-09-21", "via": "matched"}}}', encoding="utf-8")
        (ws / "cards").mkdir()
        (ws / "cards" / "CARD_TEMPLATE.md").write_text("# template\n\n## Precedent\n", encoding="utf-8")
        today = date(2026, 10, 1)

        listing = list_problems(ws)
        assert "2 of 10 rows" in listing and "[2] stale" in listing, listing
        assert "cmd.py" in show_problem(ws, "1")
        wc = week_candidates(ws, row_id="2")
        assert "paper two" in wc and "paper one" not in wc, wc
        assert "1 undecided, 1 decided" in pending_updates(ws)

        before = (ws / "ledger.md").read_text(encoding="utf-8")
        for bad, why in ((("1", "public_query", "drawings from ACME"), "ACME"), (("1", "problem", "a | b"), "'|'"),
                         (("1", "id", "9"), "field must be"), (("7", "problem", "x"), "row 7")):
            try:
                update_problem(ws, *bad, today=today)
            except ledger.LedgerError as e:
                assert why in str(e), (bad, e)
            else:
                raise AssertionError(f"update_problem accepted {bad}")
            assert (ws / "ledger.md").read_text(encoding="utf-8") == before, "a refused update changed the file"
        msg = update_problem(ws, "2", "metric_baseline_command", "metric 0.69 · cmd2.py", today=today)
        assert "last_verified 2026-10-01" in msg, msg
        rows = {r.id: r for r in ledger.load(ws, today)}
        assert rows["2"].metric_baseline_command == "metric 0.69 · cmd2.py" and not rows["2"].stale

        assert "card started: cards/" in start_card(ws, "1", "ARXIV:2609.00001")
        for bad in (("1", "ARXIV:2609.00001"), ("1", "ARXIV:2609.99999")):   # second card for the same pair, unseen paper
            try:
                start_card(ws, *bad)
            except ledger.LedgerError:
                pass
            else:
                raise AssertionError(f"start_card accepted {bad}")
    print("selftest ok: list, show, week filter, pending count; update refused on a planted term, a pipe, the id "
          "column and a missing row with the file unchanged, accepted otherwise with last_verified reset; card "
          "started once, refused for a repeat and for an unseen paper")


async def _smoke(ws: Path) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    with tempfile.TemporaryDirectory() as d:
        copy = Path(d) / "ws"
        shutil.copytree(ws, copy, ignore=shutil.ignore_patterns("state"))
        params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).resolve()), str(copy)])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = sorted(t.name for t in (await session.list_tools()).tools)
                assert names == sorted(f"{t}_tool" for t in TOOLS), names
                rows = ledger.load(copy)
                first = rows[0].id
                calls = [("list_problems_tool", {}), ("show_problem_tool", {"row_id": first}),
                         ("week_candidates_tool", {"row_id": first}), ("pending_updates_tool", {}),
                         ("update_problem_tool", {"row_id": first, "field": "owner", "value": rows[0].owner})]
                seen = card.json.loads((copy / "seen.json").read_text(encoding="utf-8"))["papers"] if (copy / "seen.json").exists() else {}
                if seen:
                    calls.append(("start_card_tool", {"row_id": first, "paper_id": next(iter(seen))}))
                for name, args in calls:
                    res = await session.call_tool(name, args)
                    text = " ".join(getattr(c, "text", "") for c in res.content)
                    status = "ERROR" if res.is_error else "ok"
                    print(f"{status:5} {name}({', '.join(f'{k}={v}' for k, v in args.items())}) -> {text[:110]!r}")
    print(f"smoke done on a copy of {ws.name}: {len(calls)} of {len(TOOLS)} tools called over MCP; the original is untouched")


if __name__ == "__main__":
    ledger.utf8_stdio()
    if sys.argv[1:] == ["--selftest"]:
        selftest()
    elif len(sys.argv) == 3 and sys.argv[1] == "--smoke":
        import anyio
        anyio.run(_smoke, resolve_ws(sys.argv[2]))
    elif len(sys.argv) == 2:
        build_server(resolve_ws(sys.argv[1])).run("stdio")
    else:
        raise SystemExit(__doc__)
