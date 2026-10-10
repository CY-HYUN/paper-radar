# paper-radar

Every week, find new papers that match the problems a team is actually stuck on.

Python 3.11 standard library · GitHub Actions weekly cron · arXiv API and Semantic Scholar API · MCP server (official Python SDK, 6 tools)

A team writes its open problems in one markdown table (`demo/ledger.md`): at most ten rows, each with its metric,
the command that measured it, the commit it was measured on, what was already tried and rejected, and seed papers.
A scheduled GitHub Actions job collects papers per row (citations of the seed papers from Semantic Scholar, then the
best keyword matches in the week's arXiv categories) and proposes ledger updates from the repository's own history.
A new seed brings every paper that ever cited it, so per seed and run only the newest `citers_per_seed` (default 10)
unseen citers are listed and the rest are recorded as seen with one count line. Before this cap, the first run on
2026-10-01 listed 1,002 papers for one demo row (`demo/candidates/2026-W40.md`).
By default no model runs in the weekly job, and no free text leaves the machine: each row's `public_query` is matched locally.
Outgoing requests carry only arXiv categories, a date window and paper ids, enforced by a URL allow-list and a
forbidden-terms check on every request (`fetch.py`); each one is logged in `demo/outbound_requests.txt`.

`mcp_server.py` adds a thin MCP layer so the ledger and the week's candidates can be read, and one cell updated
under the same gates, from Claude or any MCP client:

```bash
claude mcp add -s user paper-radar -- uv run /path/to/paper-radar/mcp_server.py demo
```

Give the script's full path so the server starts wherever Claude is opened; a relative workspace name (`demo`) is
read from the script's folder. `claude mcp list` should then show it as connected.

## Current problems from the repository's history (optional)

A hand-kept ledger goes stale as the pipeline changes: a row not verified for 14 days stops being searched, which keeps
old problems out but also leaves the radar quiet once nobody keeps it. With `[auto] enabled = true` in `radar.toml`,
each weekly run gives Claude the last `days` (default 28) of a repository's commits on every branch and asks for at
most five problems being worked on now (`current.py`). Each problem must cite at least two commits that are really in
that history, or it is dropped; its search words and its own domain gate (`must_have`) pass the same forbidden-terms
check as a ledger row. The problems are searched as rows `A1`..`A5` for that run and written with their evidence to
`<workspace>/auto/<date>.md`; a problem with no commit in the window simply stops coming back.

This sends the commit messages to the Anthropic API, so turn it on only for history you may send there. It needs the
`ANTHROPIC_API_KEY` secret; without it the run records `current=off` and uses the ledger alone. The demo keeps it off,
because this repository's own history is about the radar.

## Run

```bash
uv run --no-project --python 3.11 python radar.py check demo     # selftests, dry fetch, proposals without fetching
uv run --no-project --python 3.11 python radar.py weekly demo    # what the workflow runs
uv run mcp_server.py --selftest                                  # the six MCP tools against a planted workspace
uv run mcp_server.py --smoke demo                                # every tool called over MCP on a copy of the workspace
```

Standard library only; `mcp_server.py` declares its one dependency (the MCP SDK) inline, so `uv run` fetches it.
Python 3.11 from uv is deliberate: arXiv answered HTTP 406 to Python 3.12.6 with OpenSSL 3.0.15 on 2026-09-21.

Maintained as my own weekly paper radar (GitHub Actions); the MCP server reads the demo workspace.
