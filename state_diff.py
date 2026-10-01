"""Keep the ledger current without anyone re-typing it: read the sources that change on their own and write
PROPOSED edits for a human to accept. This script never edits ledger.md and never touches public_query.

Portable core (any company, day one):
  git      for each repo in radar.toml: commits on the watched paths after a row's baseline_ref propose
           `baseline_ref -> STALE`; a repo silent for `dead_after_weeks` proposes `command target dead`;
           a merge commit naming a PR number that a row mentions proposes `status -> landed`
Adapters (this workspace only, deleted or rewritten elsewhere):
  dated_markdown_logs   pasted chat logs with `## YYYY-MM-DD` sections: arXiv ids become seed candidates for the
                        row whose problem words appear in the section (else `row ?`); verdict sentences become
                        tried_rejected candidates
  attempts_table        a `| question | what came out | where |` table under a heading: tried_rejected candidates

Output: proposals/<date>.md, one line per proposal:
    [ ] row 1 | baseline_ref | date:2026-08-21 -> STALE (1a2b3c4 2026-09-17 scoring: ...) | source: team-repo origin/main
The human flips `[ ]` to `[y]` (then edits ledger.md by hand) or `[n]`; a proposal already marked either way in any
earlier proposals file is not made again. The header prints the denominators (commits read, sections parsed) so
"zero proposals" can be told apart from "parser read nothing".

    python state_diff.py <workspace> [--since YYYY-MM-DD] [--no-fetch]
    python state_diff.py --selftest
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import fetch
import ledger

FIELDS_ALLOWED = {"baseline_ref", "status", "seed_papers", "failing_examples", "tried_rejected", "last_verified"}
ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})")
SECTION = re.compile(r"^## (\d{4}-\d{2}-\d{2})", re.M)


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {r.stderr.strip()[:200]}")
    return r.stdout


def decided(ws: Path) -> set[str]:
    """Proposal bodies already answered [y] or [n] in any proposals file; they are never proposed again."""
    out = set()
    for f in (ws / "proposals").glob("*.md") if (ws / "proposals").exists() else []:
        for ln in f.read_text(encoding="utf-8").splitlines():
            if ln.startswith(("[y]", "[n]")):
                out.add(ln[3:].strip())
    return out


class Proposals:
    def __init__(self, already: set[str]):
        self.lines, self.skipped, self.already = [], 0, already

    def add(self, row: str, field: str, old: str, new: str, source: str) -> None:
        assert field in FIELDS_ALLOWED, field                 # public_query is never proposed by a machine
        body = f"row {row} | {field} | {old} -> {new} | source: {source}"
        if body in self.already or body in {l[4:] for l in self.lines}:
            self.skipped += 1
            return
        self.lines.append(f"[ ] {body}")


def rows_matching(rows, pattern: str):
    rx = re.compile(pattern, re.I)
    return [r for r in rows if rx.search(r.problem + " " + r.metric_baseline_command)]


def git_layer(ws: Path, cfg: dict, rows, since: date, today: date, props: Proposals, counts: Counter, do_fetch: bool) -> None:
    for repo in cfg.get("repos", []):
        path = (ws / repo["path"]).resolve()
        if not (path / ".git").exists() and not (path / "HEAD").exists():
            counts["repos_missing"] += 1
            props.lines.append(f"    (repo {repo['name']} not found at {path}; its rows were not checked)")
            continue
        if do_fetch:
            try:
                git(path, "fetch", "--quiet")
            except RuntimeError as e:
                counts["fetch_failed"] += 1
                print(f"[warn] {e}", file=sys.stderr)
        affected = rows_matching(rows, repo["affects_rows"])
        branch = repo["branch"]
        try:
            last = git(path, "log", "-1", "--format=%ad", "--date=short", branch).strip()
        except RuntimeError as e:                   # the branch was deleted or renamed: report, do not crash the week
            counts["repos_unreadable"] += 1
            props.lines.append(f"    (repo {repo['name']}: {str(e)[:120]}; its rows were not checked)")
            continue
        counts["repos_read"] += 1
        if last and (today - date.fromisoformat(last)).days > 7 * cfg["dead_after_weeks"]:
            for r in affected:
                props.add(r.id, "status", r.status, f"command target dead? ({repo['name']} last commit {last})", f"{repo['name']} {branch}")
        for r in affected:
            if r.baseline_ref == "-":
                continue
            rng = [f"--since={r.baseline_ref[5:]}", branch] if r.baseline_ref.startswith("date:") else [f"{r.baseline_ref}..{branch}"]
            try:
                log = git(path, "log", "--format=%h %ad %s", "--date=short", *rng, "--", *repo["watch_paths"]).strip()
            except RuntimeError:
                counts["sha_not_in_repo"] += 1
                continue
            commits = [ln for ln in log.splitlines() if ln]
            counts["commits_read"] += len(commits)
            if commits:
                newest = commits[0][:90]
                props.add(r.id, "baseline_ref", r.baseline_ref, f"STALE ({len(commits)} commits on watched paths, newest {newest})", f"{repo['name']} {branch}")
        for r in rows:
            for pr in set(re.findall(r"(?:PR|#)\s?#?(\d{1,4})\b", r.metric_baseline_command + " " + r.tried_rejected)):
                merged = git(path, "log", f"--since={since}", "--format=%h %ad %s", "--date=short", "--grep", f"Merge pull request #{pr} ", branch).strip()
                if merged:
                    props.add(r.id, "status", r.status, f"landed (PR #{pr}: {merged.splitlines()[0][:80]})", f"{repo['name']} {branch}")


def carry_decisions(out: Path) -> list[str]:
    """The [y]/[n] lines already in today's file. A second run on the same day (the missed-start task firing after a
    hand-run, or `radar.py check`) must not erase what the human decided (review 2026-09-22)."""
    if not out.exists():
        return []
    return [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln.startswith(("[y]", "[n]"))]


def logs_layer(ws: Path, cfg: dict, rows, since: date, props: Proposals, counts: Counter) -> None:
    verdict = re.compile(cfg["verdict_pattern"], re.I)
    row_words = {r.id: fetch.words(r.problem) for r in rows}
    for rel in cfg["paths"]:
        path = (ws / rel).resolve()
        if not path.exists():
            counts["logs_missing"] += 1
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        marks = list(SECTION.finditer(text))
        for i, m in enumerate(marks):
            day = date.fromisoformat(m.group(1))
            if day < since:
                continue
            counts["sections_parsed"] += 1
            body = text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)]
            bw = fetch.words(body)
            home = [rid for rid, w in row_words.items() if len(w & bw) >= max(2, len(w) // 2)]
            target = home[0] if len(home) == 1 else "?"
            src = f"{path.name} §{day}"
            for aid in dict.fromkeys(ARXIV_URL.findall(body)):
                line = next((ln.strip() for ln in body.splitlines() if aid in ln), "")
                props.add(target, "seed_papers", "-", f"{aid} ({' '.join(line.split()[:20])})", src)
            for ln in body.splitlines():
                if verdict.search(ln):
                    props.add(target, "tried_rejected", "-", f"paper verdict: {' '.join(ln.split()[:25])}", src)
                    break


def attempts_layer(ws: Path, cfg: dict, rows, props: Proposals, counts: Counter) -> None:
    path = (ws / cfg["path"]).resolve()
    if not path.exists():
        counts["attempts_missing"] += 1
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    start = text.find(cfg["heading"])
    if start < 0:
        counts["attempts_heading_missing"] += 1
        return
    end = text.find("\n### ", start + len(cfg["heading"]))
    section = text[start: end if end > 0 else len(text)]
    row_words = {r.id: fetch.words(r.problem + " " + r.metric_baseline_command) for r in rows}
    for ln in section.splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")] if ln.strip().startswith("|") else []
        if len(cells) != 3 or cells[0] in ("question", "") or set(cells[0]) <= set("-: "):
            continue
        counts["attempts_rows"] += 1
        qw = fetch.words(cells[0] + " " + cells[1])
        # four shared words, not three: the first live run put a sheet-metal question on the view-axes row via 'view, pre, registered'
        home = [rid for rid, w in row_words.items() if len(w & qw) >= 4]
        props.add(home[0] if len(home) == 1 else "?", "tried_rejected", "-", f"{cells[0]} → {cells[1]} ({cells[2]})", path.name)


def run(ws: Path, today: date | None = None, since: date | None = None, do_fetch: bool | None = None) -> tuple[Path, Counter]:
    today = today or date.today()
    cfg = fetch.load_config(ws)["state_diff"]
    rows = ledger.load(ws, today)
    since = since or today - timedelta(days=14)
    props, counts = Proposals(decided(ws)), Counter()
    git_layer(ws, cfg, rows, since, today, props, counts, cfg["git_fetch"] if do_fetch is None else do_fetch)
    if "dated_markdown_logs" in cfg:
        logs_layer(ws, cfg["dated_markdown_logs"], rows, since, props, counts)
    if "attempts_table" in cfg:
        attempts_layer(ws, cfg["attempts_table"], rows, props, counts)
    stale = [r.id for r in rows if r.stale]
    counts["proposals"] = len([l for l in props.lines if l.startswith("[ ]")])
    head = [f"# Ledger proposals — {today} (sources read since {since})", "",
            f"repos read {counts['repos_read']} (missing {counts['repos_missing']}) · commits on watched paths {counts['commits_read']} · "
            f"chat sections {counts['sections_parsed']} · attempts rows {counts['attempts_rows']} · proposals {counts['proposals']} "
            f"· already decided, skipped {props.skipped} · stale rows {', '.join(stale) or 'none'}", "",
            "Flip `[ ]` to `[y]` and edit ledger.md by hand, or `[n]`; decided lines are never proposed again.", ""]
    (ws / "proposals").mkdir(exist_ok=True)
    out = ws / "proposals" / f"{today}.md"
    kept = carry_decisions(out)
    body = head + props.lines + (["", f"Decided earlier today ({len(kept)}), kept:"] + kept if kept else [])
    out.write_text("\n".join(body) + "\n", encoding="utf-8")
    print("\n".join(body))
    return out, counts


def selftest() -> None:
    import os
    import tempfile
    header = "| " + " | ".join(ledger.COLUMNS) + " |\n|" + "---|" * len(ledger.COLUMNS) + "\n"
    rows = ("| 1 | score blind to mirror | cad_score 0.90 · cmd | date:2026-08-21 | 101 | none | - | q1 | me | 2026-09-21 | open |\n"
            "| 2 | drawing reading drops feature positions | reading scorer | - | 143 | none | - | q2 | me | 2026-09-21 | open |\n")
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d) / "radar"
        ws.mkdir()
        repo = Path(d) / "team"
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_DATE": "2026-09-17T10:00:00+0000", "GIT_COMMITTER_DATE": "2026-09-17T10:00:00+0000",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        (repo / "scorer.py").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "scoring: new alignment step"], check=True, env=env)
        (ws / "forbidden_terms.txt").write_text("ACME\n", encoding="utf-8")
        (ws / "ledger.md").write_text(header + rows, encoding="utf-8")
        (ws / "chat.md").write_text("## 2026-09-07 — a paper\n> https://arxiv.org/pdf/2609.03811\nreading drops positions on dense drawings\n"
                                    "they only beat an easy baseline\n## 2026-08-01 — old\nhttps://arxiv.org/abs/2608.00001\n", encoding="utf-8")
        (ws / "ATTEMPTS.md").write_text("### open — 1\n| q | out | where |\n|---|---|---|\n| a | b | c |\n### refused — 1\n\n| question | what came out | where |\n"
                                        "|---|---|---|\n| Can labels beat the selector? | No: 0.61 against 0.70 | `fit.py` |\n### closed\n", encoding="utf-8")
        (ws / "radar.toml").write_text(
            '[arxiv]\nendpoint="x"\ncategories=["cs.CV"]\ncourtesy_seconds=0\npage_size=1\n[match]\nmin_fraction=0.5\n'
            '[semantic_scholar]\ngraph="x"\nrecommendations="x"\nkey_env="K"\nbackoff_seconds=[]\n'
            '[state_diff]\ndead_after_weeks = 8\ngit_fetch = false\n'
            f'[[state_diff.repos]]\nname="team"\npath="../team"\nbranch="main"\nwatch_paths=["scorer.py"]\naffects_rows="cad_score"\n'
            '[state_diff.dated_markdown_logs]\npaths=["chat.md"]\nverdict_pattern="only beat an easy baseline|not better"\n'
            '[state_diff.attempts_table]\npath="ATTEMPTS.md"\nheading="### refused"\n', encoding="utf-8")
        out, counts = run(ws, today=date(2026, 9, 21), since=date(2026, 9, 1), do_fetch=False)
        text = out.read_text(encoding="utf-8")
        assert "[ ] row 1 | baseline_ref | date:2026-08-21 -> STALE (1 commits" in text, text
        assert "row 2 | seed_papers | - -> 2609.03811" in text and "2608.00001" not in text, text      # old section skipped
        assert "row 2 | tried_rejected | - -> paper verdict: they only beat an easy baseline" in text, text
        assert "row ? | tried_rejected | - -> Can labels beat the selector? → No: 0.61 against 0.70 (`fit.py`)" in text, text
        assert "| a | b | c |" not in text and counts["attempts_rows"] == 1, counts        # only the refused table is read
        assert counts["commits_read"] == 1 and counts["sections_parsed"] == 1, counts
        # a decided proposal is not made again
        (ws / "proposals" / "2026-09-20.md").write_text("[n] row 2 | seed_papers | - -> 2609.03811 (> https://arxiv.org/pdf/2609.03811) | source: chat.md §2026-09-07\n", encoding="utf-8")
        out2, _ = run(ws, today=date(2026, 9, 21), since=date(2026, 9, 1), do_fetch=False)
        text2 = out2.read_text(encoding="utf-8")
        assert "2609.03811" not in text2 and "already decided, skipped 1" in text2, text2
        # a same-day re-run keeps the decisions the human already wrote into today's file
        # the line is taken from the output, not typed: the commit sha depends on git and the machine (a typed sha
        # passed on the laptop and failed on a CI runner, 2026-10-01)
        decided_line = "[y]" + next(ln for ln in text2.splitlines() if ln.startswith("[ ] row 1 | baseline_ref"))[3:]
        out2.write_text(out2.read_text(encoding="utf-8").replace("[ ] row 1 | baseline_ref", "[y] row 1 | baseline_ref"), encoding="utf-8")
        out3, _ = run(ws, today=date(2026, 9, 21), since=date(2026, 9, 1), do_fetch=False)
        text3 = out3.read_text(encoding="utf-8")
        assert decided_line in text3 and "[ ] row 1 | baseline_ref" not in text3 and "already decided, skipped 2" in text3, text3
        # a repository whose branch is gone is reported, not fatal
        (ws / "radar.toml").write_text((ws / "radar.toml").read_text(encoding="utf-8").replace('branch="main"', 'branch="origin/gone"'), encoding="utf-8")
        out4, c4 = run(ws, today=date(2026, 9, 21), since=date(2026, 9, 1), do_fetch=False)
        assert c4["repos_unreadable"] == 1 and "its rows were not checked" in out4.read_text(encoding="utf-8"), c4
        # a machine may never propose public_query
        try:
            Proposals(set()).add("1", "public_query", "a", "b", "c")
        except AssertionError:
            pass
        else:
            raise AssertionError("public_query proposal was accepted")
    print("selftest ok: STALE from a commit after the baseline, seed and verdict from a dated chat section (old section skipped), "
          "refused table read (other tables not), decided proposal suppressed, same-day decisions kept, missing branch reported, public_query refused")


if __name__ == "__main__":
    ledger.utf8_stdio()
    argv = sys.argv[1:]
    if argv == ["--selftest"]:
        selftest()
    elif argv and not argv[0].startswith("--"):
        since = date.fromisoformat(argv[argv.index("--since") + 1]) if "--since" in argv else None
        run(Path(argv[0]), since=since, do_fetch=False if "--no-fetch" in argv else None)
    else:
        raise SystemExit(__doc__)
