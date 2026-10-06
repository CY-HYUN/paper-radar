"""Current problems: every weekly run asks Claude what the team is working on or stuck on right now, from the last
`days` of this repository's history on every branch, so the papers follow the pipeline as it changes instead of a list
someone has to keep current. A problem nobody has touched for `days` simply stops coming back.

Each problem must cite at least two commits that are really in the history it was given (an invented hash drops the
problem), and its public_query passes the same forbidden-terms gate as a ledger row. The problems become rows A1..An
next to the ledger's rows for that run only, and are written to auto/<date>.md with their evidence.

What leaves the repository: the commit messages, to the Anthropic API. Turn it on only for history you may send there.
Nothing else changes: the arXiv window is still fetched by category and date only and matched here.

    python current.py <workspace>     # print this week's problems (needs ANTHROPIC_API_KEY; writes nothing)
    python current.py --selftest      # a fake model: kept, invented-evidence and forbidden-term cases
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from datetime import date
from pathlib import Path

import ledger

SCHEMA = {
    "type": "object",
    "properties": {"problems": {"type": "array", "items": {
        "type": "object",
        "properties": {"problem": {"type": "string"},
                       "evidence": {"type": "array", "items": {"type": "string"}},
                       "public_query": {"type": "string"},
                       "must_have": {"type": "array", "items": {"type": "string"}}},
        "required": ["problem", "evidence", "public_query", "must_have"],
        "additionalProperties": False}}},
    "required": ["problems"],
    "additionalProperties": False,
}

PROMPT = """Below is the recent git history of {about}: every branch, newest first, each commit as \
'<hash> <date> <subject>' followed by its body.

List at most {n} problems the team is working on or stuck on NOW: something that fails, is measured as weak, or is \
being changed to fix a shortfall. Skip routine plumbing (renames, dependency bumps, formatting) unless it is the \
problem itself. Prefer problems with recent commits; a problem nobody touched in the last weeks is over.

For each problem:
- problem: one plain sentence a new teammate understands.
- evidence: the hashes (as written) of at least two commits that show it.
- public_query: 2 or 3 clauses separated by ';', each a few technical words a research paper on this problem would use \
in its title or abstract. Public vocabulary only: no file, function, class, branch, product, company or person names.
- must_have: 2 to 5 short phrases (one to three words, e.g. "CAD", "B-rep", "tool use") such that a paper relevant \
to this problem contains at least one of them word for word, and most unrelated papers in {categories} contain none. \
Same public-vocabulary rule.
"""


def history(repo: Path, days: int, max_chars: int, refs: str = "--remotes") -> tuple[str, dict[str, str], int]:
    """Newest-first commits of the last `days` on every remote branch; whole commits up to max_chars.
    Returns (text, {hash: subject}, commits dropped by the cap)."""
    out = subprocess.run(["git", "-C", str(repo), "log", refs, f"--since={days} days ago", "--date=short",
                          "--format=%x1e%h %ad %s%n%b"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode:
        raise ledger.LedgerError(f"git log failed in {repo}: {out.stderr.strip()[:200]}")
    commits = [c.strip() for c in out.stdout.split("\x1e") if c.strip()]
    kept, subjects, size = [], {}, 0
    for c in commits:
        if size + len(c) > max_chars:
            break
        kept.append(c)
        subjects[c.split(" ", 1)[0]] = c.splitlines()[0]
        size += len(c)
    return "\n\n".join(kept), subjects, len(commits) - len(kept)


def ask(text: str, cfg: dict, client=None) -> list[dict]:
    if client is None:
        import anthropic                            # only the weekly run needs the SDK; selftests pass a fake
        client = anthropic.Anthropic()
    msg = client.messages.create(
        model=cfg["model"], max_tokens=cfg.get("max_tokens", 8000), thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": PROMPT.format(n=cfg["max_problems"], about=cfg["about"], categories=cfg["categories"])
                   + "\n<history>\n" + text + "\n</history>"}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
    return json.loads(next(b for b in msg.content if b.type == "text").text)["problems"]


def to_rows(problems: list[dict], subjects: dict[str, str], forbidden: list[str], cap: int, today: date):
    """Keep problems with two real commits and a clean query; every drop is returned with its reason, never silent."""
    rows, dropped = [], []
    for p in problems[:cap]:
        real = [h for h in p["evidence"] if any(k.startswith(h[:7]) or h.startswith(k) for k in subjects) and len(h) >= 7]
        anchors = [a.strip() for a in p["must_have"] if a.strip()]
        hits = ledger.leak_hits(p["public_query"] + " " + " ".join(anchors), forbidden)
        if len(real) < 2:
            dropped.append((p["problem"], f"{len(real)} of {len(p['evidence'])} cited commits are in the history"))
        elif hits:
            dropped.append((p["problem"], f"public_query or must_have carries forbidden terms {hits}"))
        elif not p["public_query"].strip(" ;") or not anchors:
            dropped.append((p["problem"], "empty public_query or must_have"))
        else:
            rows.append((ledger.Row(f"A{len(rows) + 1}", p["problem"], "(auto, no metric)", "-", "", "", [],
                                    p["public_query"], "auto", today, "open", anchors=anchors), real))
    return rows, dropped


def run(ws: Path, today: date | None = None, client=None, write: bool = True) -> tuple[list[ledger.Row], str]:
    """Rows for this run and a one-word state for runs.txt: ok:<n>, off (no key or disabled) or FAILED:<reason>."""
    today = today or date.today()
    full = tomllib.loads((ws / "radar.toml").read_text(encoding="utf-8"))
    cfg = {**full.get("auto", {}), "categories": ", ".join(full.get("arxiv", {}).get("categories", [])) or "the watched categories"}
    if not cfg.get("enabled", False):
        return [], "off"
    if client is None and not os.environ.get("ANTHROPIC_API_KEY"):
        return [], "off(no ANTHROPIC_API_KEY)"
    text, subjects, cut = history((ws / cfg["repo"]).resolve(), cfg["days"], cfg["max_history_chars"], cfg.get("refs", "--remotes"))
    if not subjects:
        return [], f"FAILED:no commits in the last {cfg['days']} days"
    try:
        problems = ask(text, cfg, client)
    except Exception as e:                          # an API failure turns the run red; the ledger rows still run
        print(f"[fail] current problems: {type(e).__name__}: {e}", file=sys.stderr)
        return [], f"FAILED:{type(e).__name__}"
    kept, dropped = to_rows(problems, subjects, ledger.load_forbidden(ws), cfg["max_problems"], today)
    lines = [f"# Current problems — {today}", "",
             f"{cfg['model']} read {len(subjects)} commits from the last {cfg['days']} days"
             f"{f' ({cut} older ones over the size cap left out)' if cut else ''}; kept {len(kept)}, dropped {len(dropped)}.", ""]
    for row, real in kept:
        lines += [f"## {row.id} — {row.problem}", f"query: {row.public_query}", f"must have one of: {', '.join(row.anchors)}",
                  "evidence:"]
        lines += [f"- {subjects[next(k for k in subjects if k.startswith(h[:7]) or h.startswith(k))]}" for h in real]
        lines.append("")
    lines += [f"- dropped: {prob} ({why})" for prob, why in dropped]
    if write:
        (ws / "auto").mkdir(exist_ok=True)
        (ws / "auto" / f"{today}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return [row for row, _ in kept], f"ok:{len(kept)}"


class _Fake:
    """Stands in for anthropic.Anthropic(): returns a fixed answer and records what it was sent."""

    def __init__(self, answer: dict):
        self.answer, self.sent = answer, []
        self.messages = self

    def create(self, **kw):
        self.sent.append(kw)
        block = type("B", (), {"type": "text", "text": json.dumps(self.answer)})()
        return type("M", (), {"content": [block]})()


def selftest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, ws = Path(tmp) / "repo", Path(tmp) / "repo" / "ws"
        ws.mkdir(parents=True)

        def git(*a):
            return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout

        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        for msg in ("scoring: rotated parts score like the original", "scoring: test 9 poses", "readme typo"):
            git("commit", "-q", "--allow-empty", "-m", msg)
        hashes = git("log", "--format=%h").split()
        (ws / "forbidden_terms.txt").write_text("secretco\n", encoding="utf-8")
        (ws / "radar.toml").write_text('[arxiv]\ncategories = ["cs.CV"]\n[auto]\nenabled = true\nmodel = "fake"\nrepo = ".."\n'
                                       'refs = "--all"\ndays = 28\nmax_problems = 5\nmax_history_chars = 50000\n'
                                       'about = "a test team"\n', encoding="utf-8")
        fake = _Fake({"problems": [
            {"problem": "score blind to rotation", "evidence": hashes[1:3], "public_query": "pose invariant shape metric",
             "must_have": ["CAD", "shape metric"]},
            {"problem": "invented", "evidence": [hashes[0], "deadbee"], "public_query": "anything", "must_have": ["CAD"]},
            {"problem": "leaky", "evidence": hashes[:2], "public_query": "pipeline", "must_have": ["SecretCo"]}]})
        rows, state = run(ws, today=date(2026, 10, 6), client=fake, write=False)
        assert state == "ok:1" and [r.id for r in rows] == ["A1"] and rows[0].active, (state, rows)
        assert rows[0].anchors == ["CAD", "shape metric"], rows[0].anchors
        assert rows[0].problem == "score blind to rotation" and rows[0].seed_papers == []
        sent = fake.sent[0]["messages"][0]["content"]
        assert all(h in sent for h in hashes) and "readme typo" in sent, "the history did not reach the model"
        assert "a test team" in sent and "cs.CV" in sent, "the workspace's own description did not reach the prompt"
        assert fake.sent[0]["output_config"]["format"]["schema"] is SCHEMA
        (ws / "radar.toml").write_text('[auto]\nenabled = false\n', encoding="utf-8")
        assert run(ws, client=fake) == ([], "off")
    print("current selftest ok: 1 kept, invented evidence and forbidden term dropped, disabled is off")


if __name__ == "__main__":
    ledger.utf8_stdio()
    if sys.argv[1:] == ["--selftest"]:
        selftest()
    elif len(sys.argv) == 2:
        w = Path(sys.argv[1])
        rows, state = run(w if w.is_absolute() else (Path(__file__).resolve().parent / w).resolve(), write=False)
        print(state)
    else:
        raise SystemExit(__doc__)
