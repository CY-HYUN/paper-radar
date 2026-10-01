"""Replay the golden set: for each dated moment where a measured failure led to a paper, would the radar have
surfaced that paper? Two channels are scored. Every request goes through fetch.Http, whose shape gate
(fetch.ALLOWED_URLS, proven to refuse free text in fetch.py --selftest) admits only category windows, id lists
and citation lookups, so the failure sentences below stay on this machine by construction:

  window  the arXiv category window of the week the paper was submitted (3 days either side), matched with the
          row's public_query exactly as fetch.py does; reports how many candidates that row would have received
          that week (min(top_k, papers above the floor)) and where the paper ranks (hit@k = rank <= k and above the floor)
  graph   whether the paper cites one of the row's OTHER seeds (Semantic Scholar citations of that seed)

Events whose paper has no arXiv id, or whose problem has no ledger row, are reported as such, never dropped.
Caveat carried from the design (section 6): the row queries are today's, written after the events, so a hit is
an upper bound on what the radar would have done at the time.

    python eval_golden.py <workspace>
Writes golden/replay_<date>.md in the workspace and prints the same table.
"""
from __future__ import annotations

import json
import sys
import urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path

import fetch
import ledger


def replay(ws: Path, today: date | None = None) -> str:
    today = today or date.today()
    cfg = fetch.load_config(ws)
    rows = {r.id: r for r in ledger.load(ws, today)}
    gold = json.loads((ws / "golden" / "events.json").read_text(encoding="utf-8"))
    http = fetch.Http(ledger.load_forbidden(ws), ws / fetch.OUTBOUND_LOG, cfg["semantic_scholar"]["backoff_seconds"],
                      cfg["arxiv"]["courtesy_seconds"])
    watched = set(cfg["arxiv"]["categories"])
    thr, top_k = cfg["match"]["min_fraction"], cfg["match"]["top_k"]
    window_cache: dict[tuple[str, str], dict] = {}
    citers_cache: dict[str, set[str]] = {}
    out = [f"# Golden replay — {today}", "",
           f"rows {len(rows)} · watched categories {', '.join(sorted(watched))} · floor {thr} · top_k {top_k} · queries are today's (hindsight caveat)", "",
           "| event | date | direction | paper | row | primary cat | window N | row candidates | rank | hit@1 | hit@3 | graph |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    hits1 = hits3 = scored = 0
    for ev in gold["events"]:
        row = rows.get(ev["row"]) if ev["row"] else None
        if not ev["arxiv"]:
            out.append(f"| {ev['id']} | {ev['date']} | {ev['direction']} | {ev['paper'][:40]} | {ev['row'] or 'none'} | no arXiv id | - | - | - | - | - | - |")
            continue
        for bare in ev["arxiv"]:
            target = f"ARXIV:{bare}"
            if row is None:
                out.append(f"| {ev['id']} | {ev['date']} | {ev['direction']} | {bare} | none (lane not in ledger) | - | - | - | - | - | - | - |")
                continue
            key = (bare, row.id)
            if key not in window_cache:
                window_cache[key] = score_window(http, cfg, bare, row, thr, top_k)
            w = window_cache[key]
            graph = "-"
            others = [s for s in row.seed_papers if s != target]
            if others:
                found = []
                for seed in others:
                    if seed not in citers_cache:
                        try:
                            citers_cache[seed] = {p["id"] for p in fetch.s2_citations(http, cfg["semantic_scholar"], seed)}
                        except urllib.error.URLError as e:
                            citers_cache[seed] = set()
                            print(f"[fail] citations of {seed}: {e}", file=sys.stderr)
                    if target in citers_cache[seed]:
                        found.append(seed)
                graph = f"cites {', '.join(found)}" if found else f"no ({len(others)} other seeds)"
            scored += 1
            hits1 += w["hit1"]
            hits3 += w["hit3"]
            out.append(f"| {ev['id']} | {ev['date']} | {ev['direction']} | {bare} | {row.id} | {w['primary']}{'' if w['watched'] else ' (NOT watched)'} | "
                       f"{w['n']} | {w['candidates']} | {w['rank']} (score {w['score']:.2f}) | {'yes' if w['hit1'] else 'no'} | {'yes' if w['hit3'] else 'no'} | {graph} |")
    out += ["", f"scored (paper, row) pairs {scored} · hit@1 {hits1} · hit@3 {hits3} · outbound calls {http.calls} (429 retries {http.retries}, failed {http.failed})", "",
            "Negatives (documented, not scored):"]
    for n in gold.get("negatives", []):
        out.append(f"- {n['id']} {n['date']} row {n['row']} {', '.join(n['arxiv'])}: {n['what']}")
    text = "\n".join(out) + "\n"
    (ws / "golden" / f"replay_{today}.md").write_text(text, encoding="utf-8")
    print(text)
    return text


def score_window(http: fetch.Http, cfg: dict, bare: str, row, thr: float, top_k: int) -> dict:
    """Fetch the target's metadata, then its primary category's window around its submission, and rank it."""
    axcfg = cfg["arxiv"]
    meta = fetch.arxiv_by_ids(http, axcfg, [bare])
    if not meta:
        return {"primary": "?", "watched": False, "n": 0, "candidates": 0, "rank": "not on arXiv", "score": 0.0, "hit1": 0, "hit3": 0}
    target = meta[0]
    day = date.fromisoformat(target["published"])
    entries = fetch.arxiv_window(http, axcfg, [target["primary"]], day - timedelta(days=3), day + timedelta(days=3))
    if target["id"] not in {e["id"] for e in entries}:
        entries.append(target)                          # the id_list copy fills a gap the window search may leave
    df = fetch.df_table(entries)
    anchors = fetch.anchor_pattern(cfg["match"].get("anchors", []))           # the same domain gate fetch.py applies
    scored = sorted(((fetch.match(row.public_query, fetch.words(text), df)[0] if anchors is None or anchors.search(text) else 0.0, e["id"])
                     for e in entries for text in [e["title"] + " " + e["summary"]]), reverse=True)
    rank = next(i for i, (_, pid) in enumerate(scored, 1) if pid == target["id"])
    score = next(s for s, pid in scored if pid == target["id"])
    above = sum(1 for s, _ in scored if s >= thr)
    return {"primary": target["primary"], "watched": target["primary"] in cfg["arxiv"]["categories"], "n": len(entries),
            "candidates": min(above, top_k), "rank": rank, "score": score,
            "hit1": int(rank <= 1 and score >= thr), "hit3": int(rank <= min(3, top_k) and score >= thr)}


if __name__ == "__main__":
    ledger.utf8_stdio()
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    ws = Path(sys.argv[1])
    replay(ws if ws.is_absolute() else (Path(__file__).resolve().parent / ws).resolve())
