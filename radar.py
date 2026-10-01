"""One entry point, for the scheduler and for people.

    python radar.py weekly <workspace>   fetch (with Semantic Scholar recommendations when it is the first run of the
                                         month) then the ledger proposals. What the scheduled task runs.
    python radar.py tune   <workspace>   re-score the cached arXiv window against the current ledger and print every
                                         row's top_k (fetch.run offline=True; fetch.py --selftest asserts that mode
                                         opens no connection and changes no file). Edit a public_query, run again,
                                         until the top three are the papers you would open.
    python radar.py check  <workspace>   every selftest, then a dry fetch (shows the URLs that would go out) and a
                                         no-fetch proposal pass against the workspace. Run after any change.

Every weekly run appends one line to <workspace>/runs.txt (date, denominators, exit): the evidence that the job is
alive, and what the design's section 13.5 reads three weeks at a time. A ledger that fails its gate is recorded
there as BLOCKED and exits 2, so a scheduler shows red instead of a quiet week.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import fetch
import ledger
import state_diff

HERE = Path(__file__).resolve().parent


def record(ws: Path, line: str) -> None:
    with (ws / "runs.txt").open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M} {line}\n")


def weekly(ws: Path) -> int:
    today = date.today()
    try:
        counts = fetch.run(ws, recommend=today.day <= 7)
        _, sd = state_diff.run(ws)
    except ledger.LedgerError as e:
        record(ws, f"weekly BLOCKED {e}")
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 2
    except Exception as e:                          # a crash must leave a line too, or it reads as a quiet week
        record(ws, f"weekly CRASHED {type(e).__name__}: {str(e)[:200]}")
        raise
    record(ws, f"weekly arxiv={counts['arxiv_entries']} citers={counts['citers']} recommended={counts['recommended']} "
               f"new={counts['new']} failed_calls={counts['failed_calls']} proposals={sd['proposals']} "
               f"commits={sd['commits_read']} sections={sd['sections_parsed']} attempts={sd['attempts_rows']}")
    return 1 if counts["failed_calls"] else 0


def tune(ws: Path) -> int:
    fetch.run(ws, offline=True)
    return 0


def check(ws: Path) -> int:
    for script in ("ledger.py", "fetch.py", "state_diff.py"):
        r = subprocess.run([sys.executable, str(HERE / script), "--selftest"], capture_output=True, text=True, encoding="utf-8")
        print((r.stdout.strip().splitlines() or [""])[-1] if r.returncode == 0 else r.stderr)
        if r.returncode:
            return r.returncode
    rows = ledger.load(ws)
    print(f"ledger: {len(rows)} rows, {sum(r.active for r in rows)} active, {sum(r.stale for r in rows)} stale, "
          f"{sum(1 for r in rows if not r.seed_papers)} without seeds, "
          f"{sum(1 for r in rows if 'to fill' in r.metric_baseline_command or 'none yet' in r.metric_baseline_command)} without a command")
    fetch.run(ws, dry=True)
    out, sd = state_diff.run(ws, do_fetch=False)
    print(f"check ok: proposals {sd['proposals']} -> {out}")
    return 0


if __name__ == "__main__":
    ledger.utf8_stdio()
    if len(sys.argv) != 3 or sys.argv[1] not in ("weekly", "tune", "check"):
        raise SystemExit(__doc__)
    workspace = Path(sys.argv[2])
    if not workspace.is_absolute():
        workspace = (HERE / workspace).resolve()
    raise SystemExit({"weekly": weekly, "tune": tune, "check": check}[sys.argv[1]](workspace))
