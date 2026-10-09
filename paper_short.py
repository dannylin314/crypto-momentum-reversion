#!/usr/bin/env python3
"""Forward paper-trader for the SHORT-THE-RUNUP strategy (stdlib-only).

The strategy the backtest validated (the first with a real, stress-tested edge):
SHORT a memecoin after an aggressive 24h run-up, betting on mean reversion.

  ENTRY (short) — a FAST pump: price up >= VEL_RUNUP within the last VEL_WINDOW_H
                  hours (parabolic velocity). The backtest showed the fast version
                  survives realistic slippage while the slow 24h version doesn't.
                  A cheap 24h pre-filter bounds the short-window candle fetches.
                  Not already held / not in cooldown / not blacklisted.
  EXIT          — SCALE-OUT: bank 1/3 at TP1 (-8%), 1/3 at TP2 (-15%), final 1/3
                  on a trailing stop (exits on a TRAIL% bounce off its low). Whole
                  position dumped on the wide 30% hard stop or the 12h timeout.
                  Beat a single fixed TP in-sample (+14% exp, same worst loss).
                  5% size + wide stop for the parabola tail; stop fills pay extra
                  adverse slippage. A stopped-out coin is blacklisted from re-entry
                  until its pump breaks (below RUNUP), so we don't re-short a runaway.

Own ledger in ``paper_short_state.json``. Reuses the HL market/ATR helpers from
paper_trade.py. Fees + slippage modelled. Run once per tick (watch.py loops it).

Reality check: this edge is thin (~+$7-15/trade in backtest) and slippage-
sensitive — a coin that keeps ripping can blow through the stop. Paper only.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import paper_trade as pt   # reuse _post / _market / _atr_pct / _f

STATE = Path(__file__).with_name("paper_short_state.json")

# --- strategy params (SCALE-OUT exit, validated by backtest) -----------------
# Scale out in thirds as the short works: 1/3 at TP1, 1/3 at TP2, final 1/3 on a
# trailing stop (banks the reversion instead of round-tripping to the 12h timeout).
# In-sample this beat a single fixed TP: +14% expectancy, higher win, same worst
# loss. The pivot-low target was dropped — the backtest showed price ~never
# reaches it, so the trailing runner does the work. Wide 30% hard stop + 5% size
# for the parabola tail. 12h window kept (the sweet spot for capital efficiency).
START_EQUITY = float(os.getenv("PSHORT_START", "10000"))
RISK_FRAC = float(os.getenv("PSHORT_RISK_FRAC", "0.05"))
MAX_POSITIONS = int(os.getenv("PSHORT_MAX_POS", "5"))
RUNUP = float(os.getenv("PSHORT_RUNUP", "0.30"))           # legacy 24h threshold
# VELOCITY entry: short a coin that pumped >= VEL_RUNUP within the last
# VEL_WINDOW_H hours — a FAST parabolic move. The backtest showed the fast version
# survives realistic slippage (+$16-57/trade) while the slow 24h version does not.
# A cheap 24h pre-filter limits how many coins we pull short-window candles for.
VEL_WINDOW_H = int(os.getenv("PSHORT_VEL_WINDOW", "4"))     # pump window (hours) — 4h beat 6h on every axis (OOS, slip cushion, breadth)
VEL_RUNUP = float(os.getenv("PSHORT_VEL_RUNUP", "0.30"))    # min run-up over the window
PREFILTER_24H = float(os.getenv("PSHORT_PREFILTER", "0.15"))   # 24h gate before candle fetch
MAX_VEL_CHECKS = int(os.getenv("PSHORT_MAX_VEL_CHECKS", "15"))  # cap candle fetches per tick
TP1_PCT = float(os.getenv("PSHORT_TP1", "0.10"))           # bank 1/3 once price falls 10% (wider ladder: +15-20% exp, OOS-robust)
TP2_PCT = float(os.getenv("PSHORT_TP2", "0.25"))           # bank 1/3 once price falls 25% (deep reversions reward patience)
TRAIL_PCT = float(os.getenv("PSHORT_TRAIL", "0.05"))       # runner exits on a 5% bounce off its low
SL_FIXED = float(os.getenv("PSHORT_SL", "0.30"))           # wide hard stop (whole position)
MAX_HOLD_HRS = float(os.getenv("PSHORT_MAX_HOLD_HRS", "12"))
COOLDOWN_HRS = float(os.getenv("PSHORT_COOLDOWN_HRS", "2"))
TAKER_FEE = 0.0005
SLIPPAGE = 0.0005
STOP_SLIP = float(os.getenv("PSHORT_STOP_SLIP", "0.015"))  # extra adverse slip on stops
# Volume-scaled slippage: thin coins fill far worse. The backtest showed a FLAT
# 0.05% slip is fiction here — realistic per-coin slip (median ~0.7%) flips the
# edge negative. This makes the forward test honest instead of optimistic.
SLIP_BASE = float(os.getenv("PSHORT_SLIP_BASE", "0.0005"))  # floor (liquid coins)
SLIP_REF = float(os.getenv("PSHORT_SLIP_REF", "5000000"))   # $vol at which slip = base
SLIP_CAP = float(os.getenv("PSHORT_SLIP_CAP", "0.05"))      # cap (thinnest coins)


def _vol_slip(vol: float) -> float:
    """Per-coin slippage scaling inversely with 24h $ volume (thin = worse)."""
    if vol <= 0:
        return SLIP_CAP
    return min(SLIP_CAP, max(SLIP_BASE, SLIP_BASE * SLIP_REF / vol))


def _pump_velocity(symbol: str, now: float) -> float | None:
    """Run-up over the last VEL_WINDOW_H hours from 1h candles; None on failure."""
    span = 3_600_000
    end = int(now * 1000)
    start = end - (VEL_WINDOW_H + 3) * span
    try:
        rows = pt._post({"type": "candleSnapshot",
                         "req": {"coin": symbol, "interval": "1h",
                                 "startTime": start, "endTime": end}})
    except Exception:
        return None
    if not isinstance(rows, list) or len(rows) < VEL_WINDOW_H + 1:
        return None
    closes = [pt._f(c.get("c")) for c in rows]
    now_px, past_px = closes[-1], closes[-(VEL_WINDOW_H + 1)]
    if now_px <= 0 or past_px <= 0:
        return None
    return now_px / past_px - 1.0


def _load() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except (ValueError, OSError):
            pass
    return {"equity": START_EQUITY, "positions": {}, "closed": [], "cooldowns": {}, "stopped_out": []}


def _save(state: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE)


def _levels(symbol: str, now: float) -> tuple[float, float | None]:
    """Hard-stop fraction (fixed) + ATR (display only)."""
    atr = pt._atr_pct(symbol, now)
    return SL_FIXED, atr


def _open(state: dict, symbol: str, mark: float, now: float, slip: float) -> dict:
    """Open a SHORT; entry fee paid immediately. Sets up scale-out tracking.

    `slip` is the coin's volume-scaled slippage, stored and used on every fill.
    """
    notional = state["equity"] * RISK_FRAC
    fill = mark * (1 - slip)                    # sell to open, fill lower (worse on thin coins)
    qty = notional / fill
    entry_fee = notional * TAKER_FEE
    state["equity"] -= entry_fee
    sl_frac, atr = _levels(symbol, now)
    state["positions"][symbol] = {
        "side": -1, "entry": fill, "qty": qty, "notional": notional, "entry_ts": now,
        "sl_frac": sl_frac, "atr_pct": atr, "slip": slip, "sig": f"vel{VEL_WINDOW_H}h",
        "rem": 1.0, "tp1_done": False, "tp2_done": False, "low_px": mark, "realized": -entry_fee,
    }
    return {"sl_frac": sl_frac, "atr_pct": atr, "slip": slip}


def _close_frac(state: dict, pos: dict, frac: float, mark: float, extra_slip: float = 0.0) -> float:
    """Buy back `frac` of the short at current mark; bank into equity + pos realized."""
    fill = mark * (1 + pos.get("slip", SLIPPAGE) + extra_slip)
    units = frac * pos["qty"]
    chunk = units * (pos["entry"] - fill) - units * fill * TAKER_FEE   # short profit - exit fee
    state["equity"] += chunk
    pos["realized"] += chunk
    pos["rem"] -= frac
    return fill


def _finalize(state: dict, symbol: str, pos: dict, now: float, reason: str, fill: float) -> dict:
    """Record the completed trade, set cooldown, blacklist on stop."""
    notional = pos.get("notional", 0.0)
    trade = {
        "symbol": symbol, "entry": pos["entry"], "exit": fill,
        "ret_pct": (pos["realized"] / notional * 100) if notional else 0.0,
        "pnl": pos["realized"], "reason": reason,
        "held_hrs": (now - pos["entry_ts"]) / 3600,
        "tp1": pos.get("tp1_done", False), "tp2": pos.get("tp2_done", False),
        "sig": pos.get("sig", "runup24"),
    }
    state["closed"].append(trade)
    state["positions"].pop(symbol, None)
    state.setdefault("cooldowns", {})[symbol] = now
    if reason == "stop_loss":
        bl = state.setdefault("stopped_out", [])   # don't re-short the same runaway
        if symbol not in bl:
            bl.append(symbol)
    return trade


def tick() -> dict:
    """Run one short-paper tick: mark, exit, enter. Returns a report."""
    state = _load()
    now = time.time()
    mkt = pt._market()
    events: list[str] = []

    # 1. Manage open shorts — SCALE-OUT exits (1/3 at TP1, 1/3 at TP2, runner trails)
    for symbol in list(state["positions"].keys()):
        info = mkt.get(symbol)
        if info is None:
            continue
        pos = state["positions"][symbol]
        # migrate any pre-scale-out position to the new schema
        pos.setdefault("rem", 1.0)
        pos.setdefault("tp1_done", False)
        pos.setdefault("tp2_done", False)
        pos.setdefault("realized", 0.0)
        mark = info["mark"]
        pos["low_px"] = min(pos.get("low_px", mark), mark)
        fall = 1.0 - mark / pos["entry"]         # + = price fell (short profit)
        rise = mark / pos["entry"] - 1.0         # + = price rose (short loss)
        age_hrs = (now - pos["entry_ts"]) / 3600
        sl = pos.get("sl_frac", SL_FIXED)

        # hard stop / max-hold: dump ALL remaining and finalize
        if rise >= sl:
            fill = _close_frac(state, pos, pos["rem"], mark, extra_slip=STOP_SLIP)
            t = _finalize(state, symbol, pos, now, "stop_loss", fill)
            events.append(f"CLOSE {symbol} ${t['pnl']:+.2f} [stop_loss]")
            continue
        if age_hrs >= MAX_HOLD_HRS:
            fill = _close_frac(state, pos, pos["rem"], mark)
            t = _finalize(state, symbol, pos, now, "max_hold", fill)
            events.append(f"CLOSE {symbol} ${t['pnl']:+.2f} [max_hold]")
            continue
        # partial take-profits (bank a third each)
        if not pos["tp1_done"] and fall >= TP1_PCT:
            _close_frac(state, pos, 1 / 3, mark)
            pos["tp1_done"] = True
            events.append(f"TP1   {symbol} banked 1/3 (price {fall * 100:+.0f}%)")
        if not pos["tp2_done"] and fall >= TP2_PCT:
            _close_frac(state, pos, 1 / 3, mark)
            pos["tp2_done"] = True
            events.append(f"TP2   {symbol} banked 1/3 (price {fall * 100:+.0f}%)")
        # trailing runner (final third, only after TP2): exit on a bounce off the low
        if pos["tp2_done"] and pos["rem"] > 1e-9 and mark >= pos["low_px"] * (1 + TRAIL_PCT):
            fill = _close_frac(state, pos, pos["rem"], mark)
            t = _finalize(state, symbol, pos, now, "trail", fill)
            events.append(f"CLOSE {symbol} ${t['pnl']:+.2f} [trail]")
            continue

    # 2. Entries: short FAST pumps (>= VEL_RUNUP over VEL_WINDOW_H hours).
    cooldowns = state.setdefault("cooldowns", {})
    cd_secs = COOLDOWN_HRS * 3600
    # Un-blacklist coins whose pump has clearly faded (24h change below the pre-filter).
    blacklist = state.setdefault("stopped_out", [])
    blacklist[:] = [sym for sym in blacklist if mkt.get(sym, {}).get("chg", 0.0) >= PREFILTER_24H]

    # Cheap 24h pre-filter → then pull short-window candles only for the top movers.
    pre = []
    for symbol, info in mkt.items():
        if symbol in state["positions"] or symbol in blacklist:
            continue
        last_close = cooldowns.get(symbol)
        if last_close is not None and (now - last_close) < cd_secs:
            continue
        if info["chg"] >= PREFILTER_24H:
            pre.append((symbol, info["chg"]))
    pre.sort(key=lambda c: c[1], reverse=True)

    candidates = []
    for symbol, _ in pre[:MAX_VEL_CHECKS]:               # bound candle fetches per tick
        vel = _pump_velocity(symbol, now)
        if vel is not None and vel >= VEL_RUNUP:
            candidates.append((symbol, vel))
    candidates.sort(key=lambda c: c[1], reverse=True)   # fastest pump first
    for symbol, vel in candidates:
        if len(state["positions"]) >= MAX_POSITIONS:
            break
        slip = _vol_slip(mkt[symbol].get("vol", 0.0))
        lv = _open(state, symbol, mkt[symbol]["mark"], now, slip)
        events.append(
            f"OPEN  {symbol} SHORT @ {mkt[symbol]['mark']:.4g} "
            f"({VEL_WINDOW_H}h pump {vel * 100:+.0f}%, vol ${mkt[symbol].get('vol', 0) / 1e6:.1f}M "
            f"-> slip {slip * 100:.2f}% | scale-out {TP1_PCT * 100:.0f}/{TP2_PCT * 100:.0f}%+trail, "
            f"SL {lv['sl_frac'] * 100:.0f}%)"
        )

    # 3. Report
    unrealized = 0.0
    open_rows = []
    for symbol, pos in state["positions"].items():
        info = mkt.get(symbol)
        mark = info["mark"] if info else pos["entry"]
        rem = pos.get("rem", 1.0)
        short_ret = pos["entry"] / mark - 1.0                 # price move (display)
        u = rem * pos["qty"] * (pos["entry"] - mark)          # only the open remainder
        unrealized += u
        open_rows.append((symbol, short_ret * 100, u, (now - pos["entry_ts"]) / 3600))

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


if __name__ == "__main__":
    try:
        r = tick()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for e in r["events"]:
            print(e, flush=True)
        ret_pct = (r["total"] / r["start"] - 1) * 100
        wr = (r["n_wins"] / r["n_closed"] * 100) if r["n_closed"] else 0.0
        print(
            f"[{ts}] SHORT total ${r['total']:,.2f} ({ret_pct:+.2f}%) | "
            f"realized ${r['realized_pnl']:+,.2f} | unrealized ${r['unrealized']:+,.2f} | "
            f"open {len(r['open_rows'])} | closed {r['n_closed']} (win {wr:.0f}%)",
            flush=True,
        )
        for sym, retp, u, age in r["open_rows"]:
            print(f"    {sym:10s} SHORT {retp:+6.1f}%  ${u:+8.2f}  {age:4.1f}h", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] short paper tick failed: {exc}", file=sys.stderr)
        sys.exit(1)
