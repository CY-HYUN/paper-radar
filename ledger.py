"""The problem ledger: the only description of "what the company is stuck on" the radar ever reads.

A ledger is a markdown table, one row per open problem, kept in the company workspace (not in this
tool), so the same code serves the next employer with a new file. Columns, in order:

    id | problem | metric_baseline_command | baseline_ref | failing_examples | tried_rejected | seed_papers | public_query | owner | last_verified | status

Rules the parser enforces, because the design (README) says
a ledger that is not enforced rots:
  * at most MAX_ROWS rows; more than that is a backlog, not a ledger
  * a row whose last_verified is older than STALE_DAYS is skipped by the fetch (stale), never deleted
  * baseline_ref pins the number to what produced it: a commit sha of the code that was measured, or
    `date:YYYY-MM-DD` when the sha was not recorded, or `-`. state_diff.py proposes STALE when the scorer
    or config changed after it; last_verified stamps the row, baseline_ref stamps the number (the 2026-09-21
    review found a row "verified" that day whose number predated four scorer commits)
  * public_query is the only string that leaves the machine, so it is checked against the workspace's
    forbidden_terms.txt on every load; one hit stops the run (fail loud, no partial fetch)
  * seed_papers are paper ids with their scheme: a bare arXiv id (2506.01234, 2506.01234v2) is read as
    ARXIV:2506.01234; DOI:10.xxxx/... and S2:<Semantic Scholar paperId> are accepted for papers with no arXiv
    id (a CVPR workshop paper in the golden set has none); anything else is an error. A row with no seed
    fetches by keyword only, which the 2026-09-21 checks showed to be the weak channel

    python ledger.py <workspace_dir>            # print the parsed rows and their state
    python ledger.py --selftest                 # the leak gate must fire on a planted term and pass a clean row
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

MAX_ROWS = 10
STALE_DAYS = 14
COLUMNS = ["id", "problem", "metric_baseline_command", "baseline_ref", "failing_examples", "tried_rejected",
           "seed_papers", "public_query", "owner", "last_verified", "status"]
ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$")
BASELINE_REF = re.compile(r"^(-|[0-9a-f]{7,40}|date:\d{4}-\d{2}-\d{2})$")


def canonical_seed(text: str) -> str:
    """'2510.21862v2' -> 'ARXIV:2510.21862'; 'doi:10.1/x' -> 'DOI:10.1/x'; 'S2:abc' -> 'S2:abc'; else LedgerError."""
    t = text.strip()
    m = ARXIV_ID.match(t)
    if m:
        return f"ARXIV:{m.group(1)}"
    scheme, _, rest = t.partition(":")
    scheme = scheme.upper()
    if scheme == "ARXIV" and ARXIV_ID.match(rest):
        return f"ARXIV:{ARXIV_ID.match(rest).group(1)}"
    if scheme in ("DOI", "S2") and rest:
        return f"{scheme}:{rest}"
    raise LedgerError(f"seed {text!r} is not an arXiv id, DOI:<doi> or S2:<paperId>")


class LedgerError(Exception):
    pass


def utf8_stdio() -> None:
    """Switch stdout and stderr to UTF-8. Windows writes redirected stdout in the locale code page (cp949 here), so a
    scheduled run that prints '·' or '→' would die with UnicodeEncodeError; each script's __main__ calls this first."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class Row:
    id: str
    problem: str
    metric_baseline_command: str
    baseline_ref: str
    failing_examples: str
    tried_rejected: str
    seed_papers: list[str]
    public_query: str
    owner: str
    last_verified: date
    status: str
    stale: bool = field(default=False)

    @property
    def active(self) -> bool:
        return self.status.lower() == "open" and not self.stale


def load_forbidden(workspace: Path) -> list[str]:
    """One term per line; blank lines and '#' comments ignored; matched case-insensitively as substrings."""
    path = workspace / "forbidden_terms.txt"
    if not path.exists():
        raise LedgerError(f"{path} missing: the leak gate needs the list, even if short")
    terms = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    terms = [t for t in terms if t and not t.startswith("#")]
    if not terms:
        raise LedgerError(f"{path} is empty: a gate with nothing to catch is not a gate")
    return terms


def leak_hits(text: str, forbidden: list[str]) -> list[str]:
    low = text.lower()
    return [t for t in forbidden if t.lower() in low]


def parse_table(md: str) -> list[dict[str, str]]:
    rows = []
    header_seen = False
    for ln in md.splitlines():
        if not ln.strip().startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if not header_seen:
            if [c.lower() for c in cells] != COLUMNS:
                raise LedgerError(f"ledger header must be exactly {COLUMNS}, got {cells}")
            header_seen = True
            continue
        if set("".join(cells)) <= set("-: "):
            continue                                    # the markdown separator line
        if len(cells) != len(COLUMNS):
            raise LedgerError(f"row has {len(cells)} cells, expected {len(COLUMNS)}: {ln[:80]}")
        rows.append(dict(zip(COLUMNS, cells)))
    if not header_seen:
        raise LedgerError("no ledger table found")
    return rows


def load(workspace: Path, today: date | None = None) -> list[Row]:
    today = today or date.today()
    forbidden = load_forbidden(workspace)
    raw = parse_table((workspace / "ledger.md").read_text(encoding="utf-8"))
    if len(raw) > MAX_ROWS:
        raise LedgerError(f"{len(raw)} rows; the ledger caps at {MAX_ROWS} so one person can keep it current")
    rows, leaks = [], []
    for r in raw:
        seeds = [canonical_seed(s) for s in r["seed_papers"].split(",") if s.strip() and s.strip() != "-"]
        if not BASELINE_REF.match(r["baseline_ref"]):
            raise LedgerError(f"row {r['id']}: baseline_ref must be a commit sha, date:YYYY-MM-DD or -, got {r['baseline_ref']!r}")
        hits = leak_hits(r["public_query"], forbidden)
        if hits:
            leaks.append((r["id"], hits))
        verified = datetime.strptime(r["last_verified"], "%Y-%m-%d").date()
        rows.append(Row(r["id"], r["problem"], r["metric_baseline_command"], r["baseline_ref"], r["failing_examples"],
                        r["tried_rejected"], seeds, r["public_query"], r["owner"], verified, r["status"],
                        stale=(today - verified).days > STALE_DAYS))
    if leaks:
        raise LedgerError("public_query carries forbidden terms, nothing was sent: " +
                          "; ".join(f"row {i}: {h}" for i, h in leaks))
    return rows


def selftest() -> None:
    import tempfile
    header = "| " + " | ".join(COLUMNS) + " |\n|" + "---|" * len(COLUMNS) + "\n"
    clean = "| 1 | score blind to mirror | cad_score 0.90 · cmd | date:2026-08-21 | 101 | none | 2510.21862v2, DOI:10.1/abc, s2:0123abcd | rigid-alignment CAD metric blind to reflection | me | 2026-09-21 | open |\n"
    leaky = "| 2 | x | y | - | z | none | - | drawings from ACME Corp part 999 | me | 2026-09-21 | open |\n"
    stale = "| 3 | x | y | 094c447 | z | none | - | old query | me | 2026-08-01 | open |\n"
    badseed = "| 4 | x | y | - | z | none | Khan2025 | q | me | 2026-09-21 | open |\n"
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "forbidden_terms.txt").write_text("# planted\nACME\npart 999\n", encoding="utf-8")
        (ws / "ledger.md").write_text(header + clean + stale, encoding="utf-8")
        rows = load(ws, today=date(2026, 9, 21))
        assert [r.id for r in rows] == ["1", "3"] and rows[0].active and rows[1].stale and not rows[1].active
        assert rows[0].seed_papers == ["ARXIV:2510.21862", "DOI:10.1/abc", "S2:0123abcd"], rows[0].seed_papers
        assert rows[0].baseline_ref == "date:2026-08-21" and rows[1].baseline_ref == "094c447"
        (ws / "ledger.md").write_text(header + clean + badseed, encoding="utf-8")
        try:
            load(ws, today=date(2026, 9, 21))
        except LedgerError as e:
            assert "Khan2025" in str(e), e
        else:
            raise AssertionError("a non-id seed was accepted")
        (ws / "ledger.md").write_text(header + clean + leaky, encoding="utf-8")
        try:
            load(ws, today=date(2026, 9, 21))
        except LedgerError as e:
            assert "row 2" in str(e) and "ACME" in str(e), e
        else:
            raise AssertionError("the leak gate did not fire on a planted term")
        (ws / "ledger.md").write_text(header + clean * (MAX_ROWS + 1), encoding="utf-8")
        try:
            load(ws, today=date(2026, 9, 21))
        except LedgerError as e:
            assert "caps at" in str(e)
        else:
            raise AssertionError("the row cap did not fire")
    print("selftest ok: clean row loads with three seed schemes, stale row marked, bad seed rejected, planted term blocked, row cap enforced")


if __name__ == "__main__":
    utf8_stdio()
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        selftest()
    elif len(sys.argv) == 2:
        for row in load(Path(sys.argv[1])):
            state = "stale" if row.stale else row.status
            print(f"[{row.id}] {state:<6} {row.problem[:46]:<46} ref={row.baseline_ref:<16} seeds={len(row.seed_papers)} q={row.public_query[:60]!r}")
    else:
        raise SystemExit(__doc__)
