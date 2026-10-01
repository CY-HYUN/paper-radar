"""Start a card for one (ledger row, paper) pair from the workspace's template.

    python card.py <workspace> <row_id> <paper_id>        e.g. python card.py workspace 3 ARXIV:2609.07362

Writes cards/<date>_row<id>_<paper>.md with the header, the row's problem and metric line and the paper's title,
date and how the radar found it (from seen.json) filled in. The five sections stay for the human: a card is
written by a person in at most an hour, and a card without a smallest test is not a card (design section 5).
Refuses a paper the radar never surfaced (not in seen.json) and a row that does not exist: a card must trace back
to a candidates file, not to a search someone did on the side.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

import ledger


def new_card(ws: Path, row_id: str, paper_id: str, today: date | None = None) -> Path:
    today = today or date.today()
    rows = {r.id: r for r in ledger.load(ws, today)}
    if row_id not in rows:
        raise ledger.LedgerError(f"row {row_id} is not in the ledger (rows: {', '.join(rows)})")
    seen = json.loads((ws / "seen.json").read_text(encoding="utf-8"))["papers"] if (ws / "seen.json").exists() else {}
    if paper_id not in seen:
        raise ledger.LedgerError(f"{paper_id} was never surfaced by the radar (not in seen.json); cards start from candidates/")
    row, paper = rows[row_id], seen[paper_id]
    template = (ws / "cards" / "CARD_TEMPLATE.md").read_text(encoding="utf-8")
    body = template.split("\n", 1)[1]                        # drop the template's own title line
    head = [f"# Card — row {row_id} · {paper_id} · {today}", "",
            f"Row: {row.problem}", f"Metric: {row.metric_baseline_command}", f"Baseline ref: {row.baseline_ref}",
            f"Paper: {paper.get('title', '?')} · first seen {paper.get('first_seen', '?')} · via {paper.get('via', '?')}", ""]
    slug = re.sub(r"[^A-Za-z0-9.]+", "-", paper_id)
    out = ws / "cards" / f"{today}_row{row_id}_{slug}.md"
    if out.exists():
        raise ledger.LedgerError(f"{out.name} already exists; edit it instead of starting over")
    out.write_text("\n".join(head) + body, encoding="utf-8")
    return out


if __name__ == "__main__":
    ledger.utf8_stdio()
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    ws = Path(sys.argv[1])
    if not ws.is_absolute():
        ws = (Path(__file__).resolve().parent / ws).resolve()
    print(new_card(ws, sys.argv[2], sys.argv[3]))
