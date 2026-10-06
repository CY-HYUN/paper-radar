"""The weekly collection: new papers against the ledger, no model in the loop.

Three channels, in precision order:
  1. Semantic Scholar: every paper citing one of a row's seed papers (one GET per seed). The most precise
     signal the 2026-09-21 checks found: one seed surfaced two relevant papers the keyword query missed.
  2. arXiv: the window's submissions in the watched categories, matched LOCALLY against each row's
     public_query. The query never leaves the machine; only category names, a date window and paper ids
     go out. Matching is an idf-weighted share of the clause's words present in title+abstract.
  3. Semantic Scholar recommendations (--recommend, monthly): near the seeds, away from the arXiv ids
     named in tried_rejected. The only channel where negative knowledge feeds back.

Everything the fetch writes lives in the workspace, next to the ledger:
  candidates/<ISO week>.md   new ids per row with the channel that found them and the run's denominators
  seen.json                  every id ever surfaced, so nothing is proposed twice
  outbound_requests.txt      every outgoing URL with a timestamp (the leak gate's evidence; .txt because the
                             repository ignores *.log and this file is meant to be committed)
  state/df.json              word frequencies of the last window, reused by eval_golden.py

    python fetch.py <workspace> [--since YYYY-MM-DD] [--recommend] [--dry-run]
    python fetch.py <workspace> --offline      # re-score the cached window (state/window.json) against the current
                                               # ledger and print every row's top_k; nothing sent, nothing written.
                                               # This is how a public_query is tuned without touching arXiv again.
    python fetch.py --selftest
Exit code 1 when any outbound call failed after retries; the candidates that were gathered are still written.
"""
from __future__ import annotations

import http.client
import io
import json
import math
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import ledger
from ledger import LedgerError, leak_hits, load_forbidden

ATOM = "{http://www.w3.org/2005/Atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
STOP = frozenset("a an the of for with and or to in on from by does do is are that this versus vs into using based "
                 "between as at it its their our we not no over under out up down than more less which what how".split())
USER_AGENT = "paper-radar/0.1 (personal research tool; stdlib urllib)"
OUTBOUND_LOG = "outbound_requests.txt"
ARXIV_TAIL = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?$")
# The only request shapes that may leave the machine: a category window or an id list on arXiv, citations of one
# paper id or a recommendations call on Semantic Scholar. Free text (a query, a title, a failure sentence) fits
# none of them, so a future edit that tries to send one is stopped here, not by someone noticing.
ALLOWED_URLS = [
    re.compile(r"^https?://[\w.\-]+/api/query\?(?:search_query=cat%3A[\w.\-]+\+AND\+submittedDate%3A%5B\d{12}\+TO\+\d{12}%5D"
               r"|id_list=[\d.v%2C]+)(?:&(?:start|max_results)=\d+|&sortBy=submittedDate|&sortOrder=descending)*$"),
    re.compile(r"^https://api\.semanticscholar\.org/graph/v1/paper/(?:ARXIV:\d{4}\.\d{4,5}|DOI:[^?\s]+?|[0-9a-f]{40})/citations"
               r"\?fields=title%2Cyear%2CexternalIds%2CpublicationDate&limit=\d+$"),
    re.compile(r"^https://api\.semanticscholar\.org/recommendations/v1/papers\?fields=title%2Cyear%2CexternalIds%2CpublicationDate&limit=\d+$"),
]
ALLOWED_BODY_ID = re.compile(r"^(?:ArXiv:\d{4}\.\d{4,5}|DOI:[^\"]+|[0-9a-f]{40})$")


class LeakError(LedgerError):
    """Raised by the outbound door when a request fails a gate. Never caught inside this module: a leak attempt ends
    the run, whereas a network failure is counted and the run goes on (the selftest checks both behaviours)."""


def check_outbound(url: str, body: str | None) -> None:
    if not any(p.match(url) for p in ALLOWED_URLS):
        raise LeakError(f"outbound URL has a shape the radar never sends (free text?): {url[:160]}")
    if body:
        data = json.loads(body)
        if set(data) - {"positivePaperIds", "negativePaperIds"} or not all(
                ALLOWED_BODY_ID.match(i) for k in data for i in data[k]):
            raise LeakError(f"outbound body carries something other than paper ids: {body[:160]}")


class Http:
    """The one outbound door: leak gate on every URL and body, courtesy sleep between arXiv calls, 429 backoff
    for Semantic Scholar, one log line per call. The API key travels in a header, never in the logged URL."""

    def __init__(self, forbidden, log_path: Path, backoff, courtesy: float, key: str | None = None,
                 dry: bool = False, opener=urllib.request.urlopen, sleep=time.sleep, clock=time.monotonic):
        self.forbidden, self.log_path, self.backoff, self.courtesy = forbidden, log_path, list(backoff), courtesy
        self.key, self.dry, self.opener, self.sleep, self.clock = key, dry, opener, sleep, clock
        self.calls, self.retries, self.failed, self.last_arxiv = 0, 0, 0, None

    def get(self, url: str, kind: str, body: str | None = None) -> bytes:
        hits = leak_hits(url + (body or ""), self.forbidden)
        if hits:
            raise LeakError(f"outbound {kind} request carries forbidden terms {hits}; nothing was sent")
        check_outbound(url, body)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%dT%H:%M:%S} {kind} {'POST' if body else 'GET'} {url}"
                    + (f" body={body}" if body else "") + ("  (dry run)" if self.dry else "") + "\n")
        if self.dry:
            return b""
        if kind == "arxiv" and self.last_arxiv is not None:
            wait = self.courtesy - (self.clock() - self.last_arxiv)
            if wait > 0:
                self.sleep(wait)
        headers = {"User-Agent": USER_AGENT}
        if body:
            headers["Content-Type"] = "application/json"
        if kind == "s2" and self.key:
            headers["x-api-key"] = self.key
        waits = [0] + self.backoff
        for i, wait in enumerate(waits):
            if wait:
                self.retries += 1
                self.sleep(wait)
            self.calls += 1
            try:
                req = urllib.request.Request(url, data=body.encode("utf-8") if body else None, headers=headers)
                with self.opener(req, timeout=60) as r:
                    data = r.read()
                if kind == "arxiv":
                    self.last_arxiv = self.clock()
                return data
            except urllib.error.HTTPError as e:
                if e.code != 429 or i == len(waits) - 1:
                    self.failed += 1
                    raise
            except urllib.error.URLError:
                self.failed += 1
                raise
            except (OSError, http.client.HTTPException) as e:
                # urllib wraps only the send in URLError; a timeout, reset or truncated body while READING the
                # response arrives raw (review 2026-09-22). Callers handle URLError, so hand them one.
                self.failed += 1
                raise urllib.error.URLError(e) from e
        raise AssertionError("unreachable")


# ---------------------------------------------------------------- arXiv ------------------------------------------

def parse_entry(e) -> dict:
    raw = e.findtext(ATOM + "id") or ""
    m = ARXIV_TAIL.search(raw)
    prim = e.find(ARXIV_NS + "primary_category")
    return {"id": f"ARXIV:{m.group(1)}" if m else raw,
            "title": " ".join((e.findtext(ATOM + "title") or "").split()),
            "summary": " ".join((e.findtext(ATOM + "summary") or "").split()),
            "published": (e.findtext(ATOM + "published") or "")[:10],
            "primary": prim.get("term", "") if prim is not None else "",
            "categories": [c.get("term", "") for c in e.findall(ATOM + "category")]}


def parse_feed(xml: bytes) -> tuple[int, list[dict]]:
    root = ET.fromstring(xml)
    total = int(root.findtext(OPENSEARCH + "totalResults") or 0)
    entries = [parse_entry(e) for e in root.iter(ATOM + "entry")]
    for en in entries:
        if en["title"] == "Error" or not en["id"].startswith("ARXIV:"):
            raise LedgerError(f"arXiv answered with an error entry: {en['summary'][:200]}")
    return total, entries


def arxiv_query(http: Http, cfg: dict, params: dict) -> tuple[int, list[dict]]:
    url = cfg["endpoint"] + "?" + urllib.parse.urlencode(params)
    return parse_feed(http.get(url, "arxiv"))


def arxiv_window(http: Http, cfg: dict, categories, since: date, until: date) -> list[dict]:
    """Every submission in the categories between the two dates (inclusive), deduplicated across categories."""
    entries: dict[str, dict] = {}
    for cat in categories:
        start, retried = 0, False
        while True:
            total, page = arxiv_query(http, cfg, {
                "search_query": f"cat:{cat} AND submittedDate:[{since:%Y%m%d}0000 TO {until:%Y%m%d}2359]",
                "start": start, "max_results": cfg["page_size"], "sortBy": "submittedDate", "sortOrder": "descending"})
            if not page and start < total and not retried:
                retried = True                      # arXiv sometimes returns an empty page once; ask again
                continue
            for p in page:
                entries.setdefault(p["id"], p)
            start += cfg["page_size"]
            if start >= total or not page:
                break
    return list(entries.values())


def arxiv_by_ids(http: Http, cfg: dict, bare_ids) -> list[dict]:
    _, entries = arxiv_query(http, cfg, {"id_list": ",".join(bare_ids), "max_results": len(bare_ids)})
    return entries


# ---------------------------------------------------------------- matching ---------------------------------------

def words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in STOP or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith("s"):
            w = w[:-1]                              # ponytail: crude stemming, a stemmer if precision is measured poor
        out.add(w)
    return out


def df_table(entries) -> dict:
    df = Counter()
    for e in entries:
        df.update(words(e["title"] + " " + e["summary"]))
    return {"_N": len(entries), **df}


def idf(w: str, df: dict) -> float:
    return math.log((df["_N"] + 1) / (df.get(w, 0) + 1))


def anchor_pattern(anchors) -> re.Pattern | None:
    """Whole-word or whole-phrase match on the raw text, case-insensitive. A bare token gate ('drawing') admitted
    'drawing on prior work' and tactile-drawing accessibility papers (2026-09-21 tuning pass); phrases such as
    'engineering drawing' and the acronym 'CAD' as a word carry the domain, so the gate reads the text, not the bag."""
    if not anchors:
        return None
    return re.compile("|".join(r"\b" + re.escape(a) + r"\b" for a in anchors), re.I)


def match(public_query: str, paper_words: set[str], df: dict) -> tuple[float, str]:
    """Best clause of the row's query (clauses split on ';'): the idf-weighted share of its words in the paper.
    Tried and reverted on 2026-09-21: requiring the clause's rarest window word to be present. It cost two golden
    hits (K5 fell from rank 1 to 19, IterCAD from 3 to 597) and still let three off-topic papers into row 3's top
    three, because the rarest word in a week ('extracting') is not the domain word ('drawing'). Rank plus a floor
    is the whole rule; a clause of common words is a ledger defect, not a matcher one."""
    best = (0.0, "")
    for clause in public_query.split(";"):
        cw = words(clause)
        if not cw:
            continue
        total = sum(idf(w, df) for w in cw)
        got = sum(idf(w, df) for w in cw & paper_words)
        score = got / total if total else 0.0
        if score > best[0]:
            best = (score, clause.strip())
    return best


# ---------------------------------------------------------------- Semantic Scholar --------------------------------

def s2_path_id(seed: str) -> str:
    scheme, _, ident = seed.partition(":")
    return {"ARXIV": f"ARXIV:{ident}", "DOI": f"DOI:{ident}", "S2": ident}[scheme]


def s2_body_id(seed: str) -> str:
    scheme, _, ident = seed.partition(":")
    # 'ArXiv:' and 'DOI:' prefixes verified live 2026-09-21; a bare paperId in the recommendations body is [unverified]
    return {"ARXIV": f"ArXiv:{ident}", "DOI": f"DOI:{ident}", "S2": ident}[scheme]


def s2_paper(p: dict) -> dict:
    ext = p.get("externalIds") or {}
    if ext.get("ArXiv"):
        pid = "ARXIV:" + ARXIV_TAIL.search(ext["ArXiv"]).group(1)
    elif ext.get("DOI"):
        pid = f"DOI:{ext['DOI']}"
    else:
        pid = f"S2:{p.get('paperId')}"
    return {"id": pid, "title": p.get("title") or "", "summary": "",
            "published": p.get("publicationDate") or str(p.get("year") or "")}


def s2_citations(http: Http, cfg: dict, seed: str) -> list[dict]:
    # ponytail: one page of 1000 citers per seed; page with offset/next when a seed passes 1000
    url = f"{cfg['graph']}/paper/{urllib.parse.quote(s2_path_id(seed), safe=':/')}/citations?" + urllib.parse.urlencode(
        {"fields": "title,year,externalIds,publicationDate", "limit": 1000})
    data = json.loads(http.get(url, "s2") or b"{}")
    return [s2_paper(item.get("citingPaper") or {}) for item in data.get("data", [])]


def s2_recommend(http: Http, cfg: dict, positives, negatives) -> list[dict]:
    body = json.dumps({"positivePaperIds": [s2_body_id(p) for p in positives],
                       "negativePaperIds": [s2_body_id(n) for n in negatives]})
    # the first live call (2026-09-21) at limit 100 put 92 unassigned papers in one week's file: a digest, the thing
    # the design exists to avoid. The API ranks by relevance, so a small limit keeps the head.
    url = cfg["recommendations"] + "?" + urllib.parse.urlencode({"fields": "title,year,externalIds,publicationDate",
                                                                  "limit": cfg.get("recommend_limit", 10)})
    data = json.loads(http.get(url, "s2", body=body) or b"{}")
    return [s2_paper(p) for p in data.get("recommendedPapers", [])]


# ---------------------------------------------------------------- the run ----------------------------------------

def load_config(ws: Path) -> dict:
    return tomllib.loads((ws / "radar.toml").read_text(encoding="utf-8"))


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def negatives_from(rows) -> list[str]:
    return sorted({f"ARXIV:{m}" for r in rows for m in re.findall(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b", r.tried_rejected)})


def run(ws: Path, today: date | None = None, since: date | None = None, recommend: bool = False, dry: bool = False,
        offline: bool = False, opener=urllib.request.urlopen, sleep=time.sleep, clock=time.monotonic,
        extra_rows=()) -> Counter:
    import os
    today = today or date.today()
    cfg = load_config(ws)
    rows = ledger.load(ws, today)                       # the leak gate on public_query fires here
    active = [r for r in rows if r.active] + list(extra_rows)   # extra_rows: this run's current problems (current.py)
    seen = read_json(ws / "seen.json", {"meta": {}, "papers": {}})
    if since is None:
        # the window starts where the last SUCCESSFUL arXiv window ended, so a refused week is fetched next time
        # instead of being skipped for good (review 2026-09-22); last_run keeps counting runs
        last = seen["meta"].get("arxiv_last_ok") or seen["meta"].get("last_run")
        since = date.fromisoformat(last) if last else today - timedelta(days=7)
    s2cfg, axcfg = cfg["semantic_scholar"], cfg["arxiv"]
    http = Http(load_forbidden(ws), ws / OUTBOUND_LOG, s2cfg["backoff_seconds"], axcfg["courtesy_seconds"],
                key=os.environ.get(s2cfg["key_env"]) or None, dry=dry, opener=opener, sleep=sleep, clock=clock)
    counts: Counter = Counter()
    found: dict[str, dict[str, dict]] = defaultdict(dict)          # row id -> paper id -> {paper, via[]}

    def add(row_id: str, paper: dict, via: str) -> None:
        if paper["id"] in seen["papers"] and not offline:       # tuning wants to see everything the query ranks
            counts["already_seen"] += 1
            return
        slot = found[row_id].setdefault(paper["id"], {"paper": paper, "via": []})
        slot["via"].append(via)

    # A seed the radar has not watched before brings every paper that ever cited it: 1,002 for one demo row on the
    # first run (2026-10-01), a digest instead of news. Per seed and run, the newest citers_per_seed unseen citers are
    # listed; the rest are recorded as seen and counted in the file, so no later week lists them and none vanish
    # silently. The cap acts only when more unseen citers than that arrive for one seed in one run.
    per_seed = s2cfg.get("citers_per_seed", 10)
    held: dict[str, dict[str, tuple[dict, str]]] = defaultdict(dict)   # row id -> paper id -> (paper, via), not listed
    for r in active if not offline else []:             # channel 1: the seed graph
        for seed in r.seed_papers:
            try:
                citers = s2_citations(http, s2cfg, seed)
            except (urllib.error.URLError, json.JSONDecodeError) as e:
                counts["failed_calls"] += 1
                print(f"[fail] citations of {seed}: {e}", file=sys.stderr)
                continue
            counts["citers"] += len(citers)
            citers = sorted((p for p in citers if p["id"] != seed), key=lambda p: p["published"], reverse=True)
            over = {p["id"] for p in [p for p in citers if p["id"] not in seen["papers"]][per_seed:]}
            for p in citers:
                if p["id"] in over:
                    held[r.id].setdefault(p["id"], (p, f"cites {seed}"))
                else:
                    add(r.id, p, f"cites {seed}")

    if offline:                                          # channel 2 from the cache: query tuning without network
        entries = read_json(ws / "state" / "window.json", None)
        if entries is None:
            raise LedgerError("no cached window in state/window.json: run a normal fetch first")
    else:
        entries = []
        if not dry:
            try:
                entries = arxiv_window(http, axcfg, axcfg["categories"], since, today)          # channel 2: the window
            except LeakError:
                raise                                                                           # a gate fired: stop
            except (urllib.error.URLError, LedgerError, ET.ParseError) as e:
                # the first scheduled run (2026-09-21) died here with HTTP 406 and left no candidates file at all;
                # a failed channel is counted and reported, the other channels' work is still written
                counts["failed_calls"] += 1
                counts["arxiv_failed"] = 1
                print(f"[fail] arXiv window: {e}", file=sys.stderr)
            else:
                (ws / "state").mkdir(exist_ok=True)
                (ws / "state" / "window.json").write_text(json.dumps(entries), encoding="utf-8")
                (ws / "state" / "df.json").write_text(json.dumps(df_table(entries)), encoding="utf-8")
    counts["arxiv_entries"] = len(entries)
    df = df_table(entries)
    # per row: everything above the floor, ranked, top_k kept. The golden replay (2026-09-21) ranked the pulled
    # paper 1st, 1st and 3rd in its week at scores 0.39-0.49, while a fixed 0.5 cut let 19 off-topic papers through:
    # rank is the signal, the floor only removes noise.
    floor, top_k = cfg["match"]["min_fraction"], cfg["match"]["top_k"]
    # optional domain gate: a paper must contain at least one anchor word before it is ranked at all. Added after the
    # first tuning pass (2026-09-21): rewritten queries still let a theorem-autoformalization paper and a tomato-harvesting
    # paper into rows 4 and 6, because idf weights common words, not domain words. Anchors are a workspace choice
    # (radar.toml), measured every week by what the top three look like.
    anchors = anchor_pattern(cfg["match"].get("anchors", []))
    texts = [(e, e["title"] + " " + e["summary"]) for e in entries]
    scored_words = [(e, words(t)) for e, t in texts if anchors is None or anchors.search(t)]
    counts["passed_anchor_gate"] = len(scored_words)
    for r in active:
        pool = scored_words
        if r.anchors:                                   # a current problem carries its own gate: an agent-context problem
            own = anchor_pattern(r.anchors)             # is not about CAD, and the CAD anchors would hide its papers
            pool = [(e, words(t)) for e, t in texts if own.search(t)]
        ranked = sorted(((match(r.public_query, pw, df), e) for e, pw in pool), key=lambda t: t[0][0], reverse=True)
        above = [(sc, e) for sc, e in ranked if sc[0] >= floor]
        counts[f"above_floor_row_{r.id}"] = len(above)
        for (score, clause), e in above[:top_k]:
            add(r.id, e, f"matched {score:.2f} (rank {above.index(((score, clause), e)) + 1} of {len(above)} above floor) on '{clause[:50]}'")

    if recommend and not offline and any(r.seed_papers for r in active):    # channel 3: monthly, negatives fed back
        positives = sorted({s for r in active for s in r.seed_papers})
        negatives = negatives_from(rows)
        try:
            recs = s2_recommend(http, s2cfg, positives, negatives)
            counts["recommended"] = len(recs)
            for p in recs:
                add("?", p, f"recommended near {len(positives)} seeds, away from {len(negatives)} rejected")
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            counts["failed_calls"] += 1
            print(f"[fail] recommendations: {e}", file=sys.stderr)

    week = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"
    mode = ", DRY RUN" if dry else ", OFFLINE re-score of the cached window" if offline else ""
    arxiv_note = " (arXiv FAILED, see stderr)" if counts["arxiv_failed"] else ""
    anchor_note = f", {counts['passed_anchor_gate']} past the anchor gate" if anchors else ""
    lines = [f"# Candidates — {week} (run {today}, window {since}..{today}{mode})", "",
             f"arXiv entries {counts['arxiv_entries']:,}{arxiv_note} in {', '.join(axcfg['categories'])}{anchor_note} · seed citers {counts['citers']} · "
             f"outbound calls {http.calls} (429 retries {http.retries}, failed {counts['failed_calls']}) · "
             f"already seen, skipped {counts['already_seen']} · ledger rows active {len(active) - len(extra_rows)} of {len(rows)}"
             f" · current problems {len(extra_rows)}", ""]
    new_total = 0
    for r in active + [None]:
        rid = r.id if r else "?"
        items = found.get(rid, {})
        title = f"row {r.id} — {r.problem}" if r else "unassigned (recommendations)"
        lines.append(f"## {title} ({len(items)} new)")
        for pid, slot in sorted(items.items(), key=lambda kv: kv[1]["paper"]["published"], reverse=True):
            p = slot["paper"]
            lines.append(f"- {pid} · {p['published'] or '?'} · {p['title'][:120]} · via {'; '.join(slot['via'])}")
            seen["papers"][pid] = {"first_seen": str(today), "row": rid, "via": slot["via"][0], "title": p["title"][:120]}
            new_total += 1
        rest = {pid: v for pid, v in held.get(rid, {}).items() if pid not in items}
        if rest:
            counts["held_back"] += len(rest)
            seeds = sorted({via.removeprefix("cites ") for _, via in rest.values()})
            lines.append(f"- +{len(rest)} older papers citing {', '.join(seeds)} not listed (newest {per_seed} per seed "
                         f"kept); recorded in seen.json with their titles, so no later week lists them")
            for pid, (p, via) in rest.items():
                seen["papers"][pid] = {"first_seen": str(today), "row": rid, "via": f"{via} (held back over the per-seed cap)",
                                       "title": p["title"][:120]}
        lines.append("")
    out = ws / "candidates" / f"{week}.md"
    if not dry and not offline:                          # a dry run only shows what would go out; offline only prints
        (ws / "candidates").mkdir(exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        seen["meta"] = {"last_run": str(today), "runs": seen["meta"].get("runs", 0) + 1,
                        "arxiv_last_ok": seen["meta"].get("arxiv_last_ok") if counts["arxiv_failed"] else str(today)}
        (ws / "seen.json").write_text(json.dumps(seen, indent=1, ensure_ascii=False), encoding="utf-8")
    counts["new"] = new_total
    print("\n".join(lines if offline else lines[:3]))
    print(f"new candidates {new_total} -> {out}" if not (dry or offline)
          else f"dry run: nothing written except {OUTBOUND_LOG}" if dry else "offline: nothing sent, nothing written")
    return counts


# ---------------------------------------------------------------- selftest ---------------------------------------

def _atom(entries) -> bytes:
    body = "".join(
        f'<entry><id>http://arxiv.org/abs/{i}</id><title>{t}</title><summary>{s}</summary>'
        f'<published>2026-09-18T00:00:00Z</published><arxiv:primary_category term="cs.CV"/><category term="cs.CV"/></entry>'
        for i, t, s in entries)
    return (f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" '
            f'xmlns:arxiv="http://arxiv.org/schemas/atom"><opensearch:totalResults>{len(entries)}</opensearch:totalResults>'
            f'{body}</feed>').encode()


def selftest() -> None:
    import tempfile
    header = "| " + " | ".join(ledger.COLUMNS) + " |\n|" + "---|" * len(ledger.COLUMNS) + "\n"
    row = ("| 1 | score blind to mirror | cad_score · cmd | date:2026-08-21 | 101 | tried 2401.00001 already | 2510.21862 | "
           "rigid alignment cad metric blind to reflection mirror | me | 2026-09-21 | open |\n")
    atom = _atom([("2609.00001v1", "A reflection-blind rigid alignment metric for CAD", "we show the cad metric is blind to mirror symmetry"),
                  ("2609.00002v1", "Medical image segmentation", "a transformer for tumour segmentation in MRI"),
                  ("2609.00003v1", "Seen before", "rigid alignment cad metric blind to reflection mirror")])
    s2 = json.dumps({"data": [{"citingPaper": {"paperId": "abc", "externalIds": {"ArXiv": "2609.00010"}, "title": "Citer A", "publicationDate": "2026-09-10"}},
                              {"citingPaper": {"paperId": "def0", "externalIds": {"DOI": "10.1/x"}, "title": "Citer B (DOI only)", "year": 2026}}]}).encode()
    state = {"s2_calls": 0}

    def opener(req, timeout):
        url = req.full_url
        if "semanticscholar" in url:
            state["s2_calls"] += 1
            if state["s2_calls"] == 1:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
            return io.BytesIO(s2)
        assert "cat:cs.CV" in urllib.parse.unquote_plus(url) and "20260914" in url, url
        return io.BytesIO(atom)

    toml = ('[arxiv]\nendpoint = "http://example.test/api/query"\ncategories = ["cs.CV"]\ncourtesy_seconds = 3\npage_size = 200\n'
            '[match]\nmin_fraction = 0.3\ntop_k = 3\n[semantic_scholar]\ngraph = "https://api.semanticscholar.org/graph/v1"\n'
            'recommendations = "https://api.semanticscholar.org/recommendations/v1/papers"\nkey_env = "PAPER_RADAR_TEST_KEY"\n'
            'backoff_seconds = [6, 12, 24]\n')
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "forbidden_terms.txt").write_text("ACME\n", encoding="utf-8")
        (ws / "ledger.md").write_text(header + row, encoding="utf-8")
        (ws / "radar.toml").write_text(toml, encoding="utf-8")
        (ws / "seen.json").write_text(json.dumps({"meta": {}, "papers": {"ARXIV:2609.00003": {}}}), encoding="utf-8")
        slept = []
        counts = run(ws, today=date(2026, 9, 21), since=date(2026, 9, 14), opener=opener, sleep=slept.append, clock=lambda: 0.0)
        text = (ws / "candidates" / "2026-W39.md").read_text(encoding="utf-8")
        assert "ARXIV:2609.00001" in text and "matched" in text, text
        assert "ARXIV:2609.00010" in text and "DOI:10.1/x" in text and "cites ARXIV:2510.21862" in text, text
        assert "2609.00002" not in text and "2609.00003" not in text, text
        assert counts["new"] == 3 and counts["already_seen"] == 1 and counts["failed_calls"] == 0, counts
        assert slept == [6], slept                                  # one 429, one backoff wait, then success
        seen = read_json(ws / "seen.json", None)
        assert len(seen["papers"]) == 4 and seen["meta"]["last_run"] == "2026-09-21", seen
        assert negatives_from(ledger.load(ws, date(2026, 9, 21))) == ["ARXIV:2401.00001"]
        log = (ws / OUTBOUND_LOG).read_text(encoding="utf-8")
        assert log.count("\n") == 2 and "x-api-key" not in log, log       # one S2 URL, one arXiv URL; the key never
        # offline re-score: every byte in the workspace is the same afterwards and the network door is never opened
        before = {p: p.read_bytes() for p in ws.rglob("*") if p.is_file()}

        def no_network(req, timeout):
            raise AssertionError(f"offline mode opened the network door: {req.full_url}")
        off = run(ws, today=date(2026, 9, 21), since=date(2026, 9, 14), offline=True, opener=no_network, sleep=slept.append, clock=lambda: 0.0)
        assert off["arxiv_entries"] == 3 and off["new"] == 2 and off["above_floor_row_1"] == 2, off   # cached window; seen ids shown too
        assert {p: p.read_bytes() for p in ws.rglob("*") if p.is_file()} == before, "offline mode changed a file"
        # an arXiv edge refusal (HTTP 406, seen from the scheduled task 2026-09-21) is counted, not fatal: the seed
        # channel's candidates are still written and the exit code says the week is incomplete
        def arxiv_down(req, timeout):
            if "semanticscholar" in req.full_url:
                return io.BytesIO(s2)
            raise urllib.error.HTTPError(req.full_url, 406, "Not Acceptable", None, None)
        (ws / "ledger.md").write_text(header + row, encoding="utf-8")
        down = run(ws, today=date(2026, 9, 28), since=date(2026, 9, 21), opener=arxiv_down, sleep=slept.append, clock=lambda: 0.0)
        assert down["failed_calls"] == 1 and down["arxiv_failed"] == 1 and down["arxiv_entries"] == 0, down
        text = (ws / "candidates" / "2026-W40.md").read_text(encoding="utf-8")
        assert "arXiv FAILED" in text and "Citer A" not in text and "seed citers 2" in text, text   # citers already seen; channel ran
        meta = read_json(ws / "seen.json", None)["meta"]
        assert meta["last_run"] == "2026-09-28" and meta["arxiv_last_ok"] == "2026-09-21", meta  # the refused week stays owed
        # a socket error while reading the body (not a URLError) is counted like any other failed call
        def body_dies(req, timeout):
            if "semanticscholar" in req.full_url:
                raise ConnectionResetError("reset while reading")
            return io.BytesIO(atom)
        dead = run(ws, today=date(2026, 9, 29), since=date(2026, 9, 21), opener=body_dies, sleep=slept.append, clock=lambda: 0.0)
        assert dead["failed_calls"] == 1 and dead["arxiv_failed"] == 0 and dead["arxiv_entries"] == 3, dead
        # the leak gate on the outbound door: a planted term in a category name must stop the run before any call
        (ws / "radar.toml").write_text(toml.replace('["cs.CV"]', '["cs.CV", "ACME"]'), encoding="utf-8")
        logged_before = (ws / OUTBOUND_LOG).read_text(encoding="utf-8").count("\n")
        try:
            run(ws, today=date(2026, 9, 21), since=date(2026, 9, 14), opener=opener, sleep=slept.append, clock=lambda: 0.0)
        except LeakError as e:
            assert "ACME" in str(e), e
        else:
            raise AssertionError("the outbound leak gate did not fire")
        log = (ws / OUTBOUND_LOG).read_text(encoding="utf-8")
        assert log.count("\n") == logged_before + 2 and "ACME" not in log, "expected the S2 call and the clean cs.CV page before the gate, nothing after"
        logged_before = log.count("\n")
        # the shape gate: free text in a query, and a body with anything but ids, never leave
        http = Http(["ACME"], ws / OUTBOUND_LOG, [], 0, opener=opener, sleep=slept.append, clock=lambda: 0.0)
        for url, body in [("http://example.test/api/query?search_query=all%3Areading+drops+positions&max_results=5", None),
                          ("https://api.semanticscholar.org/graph/v1/paper/search?query=rigid+alignment", None),
                          ("https://api.semanticscholar.org/recommendations/v1/papers?fields=title%2Cyear%2CexternalIds%2CpublicationDate&limit=100",
                           json.dumps({"positivePaperIds": ["ArXiv:2510.21862"], "note": "reading drops positions"})),
                          ("https://api.semanticscholar.org/recommendations/v1/papers?fields=title%2Cyear%2CexternalIds%2CpublicationDate&limit=100",
                           json.dumps({"positivePaperIds": ["2510.21862"]}))]:
            try:
                http.get(url, "s2", body)
            except LeakError:
                pass
            else:
                raise AssertionError(f"shape gate let through {url} {body}")
        assert (ws / OUTBOUND_LOG).read_text(encoding="utf-8").count("\n") == logged_before, "a refused request must not be logged as sent"
        for url in ["http://example.test/api/query?id_list=2510.21862%2C2602.18296&max_results=2",
                    "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1038/s41586-026-11044-y/citations?fields=title%2Cyear%2CexternalIds%2CpublicationDate&limit=1000"]:
            check_outbound(url, None)                       # the shapes the radar does send still pass
        # the per-seed cap, run at two values on the same five citers: it must change what is listed
        five = json.dumps({"data": [{"citingPaper": {"paperId": f"p{i}", "externalIds": {"ArXiv": f"2609.0010{i}"},
                                                     "title": f"Citer {i}", "publicationDate": f"2026-09-0{i}"}}
                                    for i in range(1, 6)]}).encode()

        def citers_only(req, timeout):
            return io.BytesIO(five if "semanticscholar" in req.full_url else _atom([]))

        def capped(cap: int, w: Path, today: date) -> tuple[Counter, str, dict]:
            if not w.exists():
                w.mkdir()
                (w / "forbidden_terms.txt").write_text("ACME\n", encoding="utf-8")
                (w / "ledger.md").write_text(header + row, encoding="utf-8")
                (w / "radar.toml").write_text(toml + f"citers_per_seed = {cap}\n", encoding="utf-8")
            c = run(w, today=today, since=today - timedelta(days=7), opener=citers_only, sleep=slept.append, clock=lambda: 0.0)
            week = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"
            return c, (w / "candidates" / f"{week}.md").read_text(encoding="utf-8"), read_json(w / "seen.json", None)["papers"]

        c, text, seen = capped(2, ws / "cap2", date(2026, 9, 21))
        assert c["new"] == 2 and c["held_back"] == 3, c
        assert "ARXIV:2609.00105" in text and "ARXIV:2609.00104" in text and "ARXIV:2609.00103" not in text, text
        assert "+3 older papers citing ARXIV:2510.21862" in text, text
        assert len(seen) == 5 and sum("held back" in v["via"] for v in seen.values()) == 3, seen
        c, text, _ = capped(2, ws / "cap2", date(2026, 9, 28))            # next week: none of the five comes back
        assert c["new"] == 0 and c["held_back"] == 0 and c["already_seen"] == 5, c
        c, text, seen = capped(10, ws / "cap10", date(2026, 9, 21))
        assert c["new"] == 5 and c["held_back"] == 0 and "older papers citing" not in text, (c, text)
    print("selftest ok: window matched 1 of 3, seen id skipped, S2 citers (arXiv and DOI-only) kept, one 429 backoff, "
          "negatives parsed, key absent from the log, offline re-score sent and wrote nothing, "
          "forbidden-term gate fired on a planted term, shape gate refused 4 free-text requests, "
          "per-seed cap 2 listed the newest 2 of 5 and recorded 3 (none back next week), cap 10 listed all 5")


if __name__ == "__main__":
    ledger.utf8_stdio()
    argv = sys.argv[1:]
    if argv == ["--selftest"]:
        selftest()
    elif argv and not argv[0].startswith("--"):
        ws = Path(argv[0])
        since = date.fromisoformat(argv[argv.index("--since") + 1]) if "--since" in argv else None
        c = run(ws, since=since, recommend="--recommend" in argv, dry="--dry-run" in argv, offline="--offline" in argv)
        raise SystemExit(1 if c["failed_calls"] else 0)
    else:
        raise SystemExit(__doc__)
