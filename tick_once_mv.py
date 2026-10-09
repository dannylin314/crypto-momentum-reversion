#!/usr/bin/env python3
"""One durable tick for the MULTI-VENUE short paper account + log.

Run every ~2 min by Windows Task Scheduler (separate task from the HL trader).
Appends a timestamped line (plus any OPEN/CLOSE events) to short_mv_run.log.
"""
from __future__ import annotations

import time
from pathlib import Path

import paper_short_mv

LOG = Path(__file__).with_name("short_mv_run.log")


def main() -> None:
    r = paper_short_mv.tick()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    ret = (r["total"] / r["start"] - 1) * 100
    wr = (r["n_wins"] / r["n_closed"] * 100) if r["n_closed"] else 0.0
    lines = [
        f"[{ts}] SHORT-MV ${r['total']:,.2f} ({ret:+.2f}%) | real ${r['realized_pnl']:+,.2f} "
        f"unreal ${r['unrealized']:+,.2f} | open {len(r['open_rows'])} closed {r['n_closed']} "
        f"(win {wr:.0f}%) | universe {r['n_markets']}"
    ]
    for e in r["events"]:
        lines.append(f"    {e}")
    with LOG.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # never let one bad tick kill the schedule
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] tick error: {exc}\n")
