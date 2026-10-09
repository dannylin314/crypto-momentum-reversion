#!/usr/bin/env python3
"""Forward paper-trading simulator for the Hyperliquid squeeze signal (stdlib-only).

Trades ONE thesis live, tick by tick, and tracks P&L so you can watch the signal's
real-time performance accumulate:

  ENTRY  (long)  — funding <= FUNDING_ENTRY (shorts paying = squeeze fuel) AND
                   24h price change > MIN_ENTRY_CHG (price CONFIRMING up, not a
                   falling knife) AND the symbol is not in its post-close re-entry
                   cooldown. The price + cooldown gates were added after the
                   forward test showed losses clustered on falling-price entries
                   and same-symbol re-buys right after a stop.
  EXIT           — first of: take-profit, stop-loss (both VOLATILITY-SCALED per
                   coin via ATR — quiet names get tight levels, wild memecoins
                   wide, at a fixed 2:1 reward:risk), max-hold timeout, or funding
                   flips positive past FUNDING_FLIP (thesis invalidated).

Shorting crowded-longs is OFF by default: in observation those kept trending UP
(TRUMP +104%, PURR +33%), so fading them bled. Flip ENABLE_SHORTS to test it.

This is a FORWARD test — it starts flat now and builds from here; it does NOT
replay past catches (use the backtest for history). Fees + slippage are modelled
(taker 0.05% + 0.05% slip per side) so the equity curve is honest.

State (equity, open positions, closed trades) persists in ``paper_state.json``.
Run once per tick (loop it like the scanner). Prints a P&L report each run.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

API = "https://api.hyperliquid.xyz/info"
STATE = Path(__file__).with_name("paper_state.json")

# --- strategy params (tune to taste) ----------------------------------------
START_EQUITY = float(os.getenv("PAPER_START", "10000"))
RISK_FRAC = float(os.getenv("PAPER_RISK_FRAC", "0.10"))   # notional per trade = equity * frac
MAX_POSITIONS = int(os.getenv("PAPER_MAX_POS", "5"))
FUNDING_ENTRY = float(os.getenv("PAPER_FUNDING_ENTRY", "-0.0005"))  # <= this => long
FUNDING_FLIP = float(os.getenv("PAPER_FUNDING_FLIP", "0.0005"))     # >= this => exit
TAKE_PROFIT = float(os.getenv("PAPER_TP", "0.08"))        # +8% (fixed fallback)
STOP_LOSS = float(os.getenv("PAPER_SL", "0.04"))          # -4% (fixed fallback)
MAX_HOLD_HRS = float(os.getenv("PAPER_MAX_HOLD_HRS", "12"))
TAKER_FEE = 0.0005
SLIPPAGE = 0.0005
ENABLE_SHORTS = os.getenv("PAPER_ENABLE_SHORTS", "0") == "1"

# --- volatility-scaled stops/targets (ATR-based) ----------------------------
# Each position's stop = ATR_SL_MULT * ATR% (of price), target = RR * stop, so a
# quiet coin gets a tight stop and a wild memecoin a wide one — both at the same
# reward:risk. Falls back to the fixed TAKE_PROFIT/STOP_LOSS above if candles are
# unavailable. Defaults land a "typical" coin near the old 4%/8% for continuity.
ATR_INTERVAL = os.getenv("PAPER_ATR_INTERVAL", "1h")
ATR_N = int(os.getenv("PAPER_ATR_N", "24"))              # candles in the ATR window
ATR_SL_MULT = float(os.getenv("PAPER_ATR_SL_MULT", "3.0"))  # stop = mult * ATR%
RR = float(os.getenv("PAPER_RR", "2.0"))                 # target = RR * stop
SL_FLOOR, SL_CAP = 0.02, 0.10                            # clamp stop to [2%, 10%]
TP_FLOOR, TP_CAP = 0.04, 0.20                            # clamp target to [4%, 20%]
_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
                "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

# --- entry-quality filters (added after forward-test analysis) ---------------
# 1. Price confirmation: only long a squeeze whose 24h price change clears this,
#    i.e. the squeeze is firing UP, not a falling knife. The ledger showed the
#    losers clustered on negative-price entries (GAS -6.8%, MOVE/ACE re-buys into
#    declines); winners were rising-price squeezes.
# 2. Re-entry cooldown: after ANY close, don't re-open the same symbol for this
#    many hours. Kills the churn (MOVE was traded 6x, ACE 4x — mostly re-buys
#    into the same fade right after a stop).
MIN_ENTRY_CHG = float(os.getenv("PAPER_MIN_ENTRY_CHG", "0.0"))   # 24h change must exceed this
REENTRY_COOLDOWN_HRS = float(os.getenv("PAPER_COOLDOWN_HRS", "2.0"))


def _post(body: dict) -> object:
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def _f(v: object, d: float = 0.0) -> float:
    try:
        out = float(v)  # type: ignore[arg-type]
        return out if out == out else d
    except (TypeError, ValueError):
        return d


def _load() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except (ValueError, OSError):
            pass
    return {"equity": START_EQUITY, "positions": {}, "closed": [], "cooldowns": {}}


def _save(state: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE)


def _market() -> dict[str, dict]:
    """Return {symbol: {funding, mark, chg}} from Hyperliquid (chg = 24h price %)."""
    meta, ctxs = _post({"type": "metaAndAssetCtxs"})
    out: dict[str, dict] = {}
    for asset, ctx in zip(meta["universe"], ctxs):
        mark = _f(ctx.get("markPx"))
        prev = _f(ctx.get("prevDayPx"))
        if mark > 0:
            out[asset["name"]] = {
                "funding": _f(ctx.get("funding")),
                "mark": mark,
                "chg": (mark / prev - 1.0) if prev > 0 else 0.0,
                "vol": _f(ctx.get("dayNtlVlm")),   # 24h notional $ volume (liquidity proxy)
            }
    return out


def _atr_pct(symbol: str, now: float) -> float | None:
    """Average True Range as a fraction of price over ATR_N candles; None on failure.

    Pulls recent OHLC from Hyperliquid's candleSnapshot and computes classic ATR
    (mean true range), normalized by the last close so it is comparable across
    coins of very different price. Fail-soft: any error returns None and the
    caller uses the fixed stop/target.
    """
    span = _INTERVAL_MS.get(ATR_INTERVAL, 3_600_000)
    end = int(now * 1000)
    start = end - (ATR_N + 2) * span
    try:
        rows = _post({"type": "candleSnapshot",
                      "req": {"coin": symbol, "interval": ATR_INTERVAL,
                              "startTime": start, "endTime": end}})
    except Exception:
        return None
    if not isinstance(rows, list) or len(rows) < 3:
        return None
    trs: list[float] = []
    prev_close = None
    for c in rows[-(ATR_N + 1):]:
        if not isinstance(c, dict):
            continue
        hi, lo, cl = _f(c.get("h")), _f(c.get("l")), _f(c.get("c"))
        if hi <= 0 or lo <= 0 or cl <= 0:
            continue
        tr = hi - lo if prev_close is None else max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
        prev_close = cl
    if not trs or prev_close is None or prev_close <= 0:
        return None
    pct = (sum(trs) / len(trs)) / prev_close
    return pct if pct == pct and pct > 0 else None


def _scaled_levels(symbol: str, now: float) -> tuple[float, float, float | None]:
    """Return (tp_frac, sl_frac, atr_pct) — ATR-scaled, clamped, fixed fallback."""
    atr = _atr_pct(symbol, now)
    if atr is None:
        return TAKE_PROFIT, STOP_LOSS, None
    sl = min(max(ATR_SL_MULT * atr, SL_FLOOR), SL_CAP)
    tp = min(max(RR * sl, TP_FLOOR), TP_CAP)
    return tp, sl, atr


def _open(state: dict, symbol: str, side: int, mark: float, now: float) -> dict:
    """Open a paper position; entry fee is paid immediately from equity.

    Stamps the ATR-scaled take-profit / stop-loss onto the position so each name
    exits on levels sized to its own volatility. Returns the level dict for the
    OPEN event line.
    """
    notional = state["equity"] * RISK_FRAC
    fill = mark * (1 + side * SLIPPAGE)          # pay up on entry
    qty = notional / fill
    state["equity"] -= notional * TAKER_FEE       # entry fee
    tp_frac, sl_frac, atr = _scaled_levels(symbol, now)
    state["positions"][symbol] = {
        "side": side, "entry": fill, "qty": qty, "notional": notional, "entry_ts": now,
        "tp_frac": tp_frac, "sl_frac": sl_frac, "atr_pct": atr,
        "filtered": True,   # opened under the price-confirm + cooldown gates
    }
    return {"tp_frac": tp_frac, "sl_frac": sl_frac, "atr_pct": atr}


def _close(state: dict, symbol: str, mark: float, now: float, reason: str) -> dict:
    """Close a paper position, bank realized P&L (net of exit fee)."""
    pos = state["positions"].pop(symbol)
    side = pos["side"]
    fill = mark * (1 - side * SLIPPAGE)           # give up on exit
    gross = pos["qty"] * (fill - pos["entry"]) * side
    exit_fee = pos["qty"] * fill * TAKER_FEE
    pnl = gross - exit_fee
    state["equity"] += pnl
    ret = (fill / pos["entry"] - 1) * side
    trade = {
        "symbol": symbol, "side": side, "entry": pos["entry"], "exit": fill,
        "ret_pct": ret * 100, "pnl": pnl, "reason": reason,
        "held_hrs": (now - pos["entry_ts"]) / 3600,
        "filtered": pos.get("filtered", False),   # False = pre-fix (unfiltered) baseline
    }
    state["closed"].append(trade)
    state.setdefault("cooldowns", {})[symbol] = now   # start re-entry cooldown
    return trade


def tick() -> dict:
    """Run one paper-trading tick: mark positions, exit, enter. Returns a report."""
    state = _load()
    now = time.time()
    mkt = _market()
    events: list[str] = []

    # 1. Manage open positions (exits)
    for symbol in list(state["positions"].keys()):
        info = mkt.get(symbol)
        if info is None:
            continue
        pos = state["positions"][symbol]
        side = pos["side"]
        mark = info["mark"]
        ret = (mark / pos["entry"] - 1) * side
        age_hrs = (now - pos["entry_ts"]) / 3600
        tp = pos.get("tp_frac", TAKE_PROFIT)   # backward-compat for pre-ATR positions
        sl = pos.get("sl_frac", STOP_LOSS)
        reason = None
        if ret >= tp:
            reason = "take_profit"
        elif ret <= -sl:
            reason = "stop_loss"
        elif age_hrs >= MAX_HOLD_HRS:
            reason = "max_hold"
        elif side == 1 and info["funding"] >= FUNDING_FLIP:
            reason = "funding_flip"
        elif side == -1 and info["funding"] <= -FUNDING_FLIP:
            reason = "funding_flip"
        if reason:
            t = _close(state, symbol, mark, now, reason)
            events.append(f"CLOSE {symbol} {t['ret_pct']:+.1f}% (${t['pnl']:+.2f}) [{reason}]")

    # 2. Entries (long squeeze-up; optional short crowded-long)
    #    Gated by: not already held, not in re-entry cooldown, and price CONFIRMING
    #    the squeeze direction (up for longs, down for shorts).
    cooldowns = state.setdefault("cooldowns", {})
    cd_secs = REENTRY_COOLDOWN_HRS * 3600
    candidates: list[tuple[str, int, float]] = []
    for symbol, info in mkt.items():
        if symbol in state["positions"]:
            continue
        last_close = cooldowns.get(symbol)
        if last_close is not None and (now - last_close) < cd_secs:
            continue  # re-entry cooldown active
        if info["funding"] <= FUNDING_ENTRY and info["chg"] > MIN_ENTRY_CHG:
            candidates.append((symbol, 1, info["funding"]))
        elif ENABLE_SHORTS and info["funding"] >= -FUNDING_ENTRY and info["chg"] < -MIN_ENTRY_CHG:
            candidates.append((symbol, -1, info["funding"]))
    # Strongest signal first (most negative for longs); fill open slots.
    candidates.sort(key=lambda c: c[2] * c[1])
    for symbol, side, funding in candidates:
        if len(state["positions"]) >= MAX_POSITIONS:
            break
        lv = _open(state, symbol, side, mkt[symbol]["mark"], now)
        atr_txt = f"ATR {lv['atr_pct'] * 100:.1f}%" if lv["atr_pct"] is not None else "ATR n/a (fixed)"
        events.append(
            f"OPEN  {symbol} {'LONG' if side == 1 else 'SHORT'} @ {mkt[symbol]['mark']:.4g} "
            f"(funding {funding:+.4%}, {atr_txt} -> TP {lv['tp_frac'] * 100:.1f}% / SL {lv['sl_frac'] * 100:.1f}%)"
        )

    # 3. Build report
    unrealized = 0.0
    open_rows = []
    for symbol, pos in state["positions"].items():
        info = mkt.get(symbol)
        mark = info["mark"] if info else pos["entry"]
        ret = (mark / pos["entry"] - 1) * pos["side"]
        u = pos["qty"] * (mark - pos["entry"]) * pos["side"]
        unrealized += u
        open_rows.append((symbol, pos["side"], ret * 100, u, (now - pos["entry_ts"]) / 3600))

    _save(state)

    closed = state["closed"]
    wins = [t for t in closed if t["pnl"] > 0]
    total = state["equity"] + unrealized
    return {
        "events": events, "equity": state["equity"], "unrealized": unrealized,
        "total": total, "start": START_EQUITY, "open_rows": open_rows,
        "n_closed": len(closed), "n_wins": len(wins),
        "realized_pnl": state["equity"] - START_EQUITY,
    }


def stats() -> None:
    """Print expectancy split by cohort: pre-fix (unfiltered) vs post-fix (gated)."""
    closed = _load().get("closed", [])

    def summarize(label: str, trades: list[dict]) -> None:
        if not trades:
            print(f"  {label}: (no trades yet)")
            return
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        total = sum(t["pnl"] for t in trades)
        aw = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0.0
        al = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0
        print(
            f"  {label}: {len(trades):2d} trades | win {len(wins) / len(trades) * 100:3.0f}% | "
            f"avg win ${aw:+6.2f}  avg loss ${al:+6.2f} | "
            f"total ${total:+8.2f} | exp ${total / len(trades):+6.2f}/trade"
        )

    pre = [t for t in closed if not t.get("filtered")]
    post = [t for t in closed if t.get("filtered")]
    print("PAPER STATS  (pre-fix = unfiltered baseline | post-fix = price-confirm + cooldown)")
    summarize("pre-fix ", pre)
    summarize("post-fix", post)
    summarize("all     ", closed)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        stats()
        sys.exit(0)
    try:
        r = tick()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for e in r["events"]:
            print(e, flush=True)
        ret_pct = (r["total"] / r["start"] - 1) * 100
        wr = (r["n_wins"] / r["n_closed"] * 100) if r["n_closed"] else 0.0
        print(
            f"[{ts}] total ${r['total']:,.2f} ({ret_pct:+.2f}%) | "
            f"realized ${r['realized_pnl']:+,.2f} | unrealized ${r['unrealized']:+,.2f} | "
            f"open {len(r['open_rows'])} | closed {r['n_closed']} (win {wr:.0f}%)",
            flush=True,
        )
        for sym, side, retp, u, age in r["open_rows"]:
            print(f"    {sym:10s} {'LONG ' if side == 1 else 'SHORT'} {retp:+6.1f}%  ${u:+8.2f}  {age:4.1f}h", flush=True)
    except Exception as exc:  # noqa: BLE001 - top-level guard for loop use
        print(f"[error] paper tick failed: {exc}", file=sys.stderr)
        sys.exit(1)
