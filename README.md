# paper-radar

Every week, find new papers that match the problems a team is actually stuck on.

A team writes its open problems in one markdown table (`demo/ledger.md`): at most ten rows, each with its metric,
the command that measured it, the commit it was measured on, what was already tried and rejected, and seed papers.
A scheduled GitHub Actions job collects papers per row (citations of the seed papers from Semantic Scholar, then the
best keyword matches in the week's arXiv categories) and proposes ledger updates from the repository's own history.
No model runs in the weekly job. The only text that leaves the repository is each row's `public_query`, checked
against a forbidden-terms list on every run.

`mcp_server.py` adds a thin MCP layer so the ledger and the week's candidates can be read, and one cell updated
under the same gates, from Claude or any MCP client:

```bash
claude mcp add paper-radar -- uv run mcp_server.py demo
```

## Run

```bash
uv run --no-project python radar.py check demo     # selftests, dry fetch, proposals without fetching
uv run --no-project python radar.py weekly demo    # what the workflow runs
uv run mcp_server.py --selftest                    # the six MCP tools against a planted workspace
uv run mcp_server.py --smoke demo                  # every tool called over MCP on a copy of the workspace
```

Standard library only; `mcp_server.py` declares its one dependency (the MCP SDK) inline, so `uv run` fetches it.
Python 3.11 from uv is deliberate: arXiv answered HTTP 406 to Python 3.12.6 with OpenSSL 3.0.15 on 2026-09-21.

Work in progress (October 2026): a demo ledger on public research questions.
