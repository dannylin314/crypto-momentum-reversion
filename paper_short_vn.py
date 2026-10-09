#!/usr/bin/env python3
"""Vol-NORMALIZED velocity-short paper trader (forward test of the z>=4 filter).

Identical to paper_short.py in EVERY way (same Hyperliquid universe, same scale-out
exit, same params imported from paper_short) EXCEPT the entry gate adds a
vol-normalization filter: the 4h pump must ALSO be >= ZMIN sigma relative to the
coin's own trailing 4h-return volatility. Backtests suggested this culls "30% moves
that aren't actually anomalous for that coin" (which revert weakly in poor regimes);
this trader forward-tests whether that holds live, RUNNING ALONGSIDE the plain HL
trader on the same signal stream so their forward results are directly comparable.

Kept fully SEPARATE (own ledger paper_short_vn_state.json, own log short_vn_run.log,
own scheduled task HypeShortPaperVN) so it never touches the validated HL trader.
Same read-only rule: NEVER run this manually once the task is live. Paper only.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import paper_trade as pt     # HL market/candle/ATR helpers
import paper_short as ps     # single source of truth for strategy params + _vol_slip

STATE = Path(__file__).with_name("paper_short_vn_state.json")
VW = 72          # trailing window (hours) of overlapping 4h returns for sigma
ZMIN = 4.0       # pump must be >= this many sigma for the coin (the vol-norm filter)


def _pump_velocity_z(symbol: str, now: float):
    """Return (vel, z) for the coin: 4h run-up and its sigma-normalized z, or (None,None)."""
    span = 3_600_000
    end = int(now * 1000)
    w = ps.VEL_WINDOW_H
    start = end - (VW + w + 3) * span
    try:
        rows = pt._post({"type": "candleSnapshot",
                         "req": {"coin": symbol, "interval": "1h",
                                 "startTime": start, "endTime": end}})
    except Exception:
        return None, None
    if not isinstance(rows, list) or len(rows) < w + 8:
        return None, None
    closes = [pt._f(c.get("c")) for c in rows]
    now_px, past_px = closes[-1], closes[-(w + 1)]
    if now_px <= 0 or past_px <= 0:
        return None, None
    vel = now_px / past_px - 1.0
    rr = [closes[i] / closes[i - w] - 1.0 for i in range(w, len(closes) - 1) if closes[i - w] > 0]
    if len(rr) < 8:
        return vel, None
    sd = statistics.pstdev(rr)
    z = (vel / sd) if sd > 0 else None
    return vel, z


def _load() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except (ValueError, OSError):
            pass
    return {"equity": ps.START_EQUITY, "positions": {}, "closed": [], "cooldowns": {}, "stopped_out": []}


def _save(state: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE)


def _open(state: dict, symbol: str, mark: float, now: float, slip: float) -> dict:
    notional = state["equity"] * ps.RISK_FRAC
    fill = mark * (1 - slip)
    qty = notional / fill
    entry_fee = notional * ps.TAKER_FEE
    state["equity"] -= entry_fee
    atr = pt._atr_pct(symbol, now)
    state["positions"][symbol] = {
        "side": -1, "entry": fill, "qty": qty, "notional": notional, "entry_ts": now,
        "sl_frac": ps.SL_FIXED, "atr_pct": atr, "slip": slip, "sig": f"vel{ps.VEL_WINDOW_H}h_z{int(ZMIN)}",
        "rem": 1.0, "tp1_done": False, "tp2_done": False, "low_px": mark, "realized": -entry_fee,
    }
    return {"sl_frac": ps.SL_FIXED, "atr_pct": atr, "slip": slip}


def _close_frac(state: dict, pos: dict, frac: float, mark: float, extra_slip: float = 0.0) -> float:
    fill = mark * (1 + pos.get("slip", ps.SLIPPAGE) + extra_slip)
    units = frac * pos["qty"]
    chunk = units * (pos["entry"] - fill) - units * fill * ps.TAKER_FEE
    state["equity"] += chunk
    pos["realized"] += chunk
    pos["rem"] -= frac
    return fill


def _finalize(state: dict, symbol: str, pos: dict, now: float, reason: str, fill: float) -> dict:
    notional = pos.get("notional", 0.0)
    trade = {
        "symbol": symbol, "entry": pos["entry"], "exit": fill,
        "ret_pct": (pos["realized"] / notional * 100) if notional else 0.0,
        "pnl": pos["realized"], "reason": reason,
        "held_hrs": (now - pos["entry_ts"]) / 3600,
        "tp1": pos.get("tp1_done", False), "tp2": pos.get("tp2_done", False),
        "sig": pos.get("sig", "vel_z"),
    }
    state["closed"].append(trade)
    state["positions"].pop(symbol, None)
    state.setdefault("cooldowns", {})[symbol] = now
    if reason == "stop_loss":
        bl = state.setdefault("stopped_out", [])
        if symbol not in bl:
            bl.append(symbol)
    return trade


def tick() -> dict:
    """One vol-norm short-paper tick over the HL universe. Mirrors paper_short.tick."""
    state = _load()
    now = time.time()
    mkt = pt._market()
    events: list[str] = []

    # 1. Manage open shorts — SCALE-OUT exits (identical to paper_short)
    for symbol in list(state["positions"].keys()):
        info = mkt.get(symbol)
        if info is None:
            continue
        pos = state["positions"][symbol]
        pos.setdefault("rem", 1.0)
        pos.setdefault("tp1_done", False)
        pos.setdefault("tp2_done", False)
        pos.setdefault("realized", 0.0)
        mark = info["mark"]
        pos["low_px"] = min(pos.get("low_px", mark), mark)
        fall = 1.0 - mark / pos["entry"]
        rise = mark / pos["entry"] - 1.0
        age_hrs = (now - pos["entry_ts"]) / 3600
        sl = pos.get("sl_frac", ps.SL_FIXED)

        if rise >= sl:
            fill = _close_frac(state, pos, pos["rem"], mark, extra_slip=ps.STOP_SLIP)
            t = _finalize(state, symbol, pos, now, "stop_loss", fill)
            events.append(f"CLOSE {symbol} ${t['pnl']:+.2f} [stop_loss]")
            continue
        if age_hrs >= ps.MAX_HOLD_HRS:
            fill = _close_frac(state, pos, pos["rem"], mark)
            t = _finalize(state, symbol, pos, now, "max_hold", fill)
            events.append(f"CLOSE {symbol} ${t['pnl']:+.2f} [max_hold]")
            continue
        if not pos["tp1_done"] and fall >= ps.TP1_PCT:
            _close_frac(state, pos, 1 / 3, mark)
            pos["tp1_done"] = True
            events.append(f"TP1   {symbol} banked 1/3 (price {fall * 100:+.0f}%)")
        if not pos["tp2_done"] and fall >= ps.TP2_PCT:
            _close_frac(state, pos, 1 / 3, mark)
            pos["tp2_done"] = True
            events.append(f"TP2   {symbol} banked 1/3 (price {fall * 100:+.0f}%)")
        if pos["tp2_done"] and pos["rem"] > 1e-9 and mark >= pos["low_px"] * (1 + ps.TRAIL_PCT):
            fill = _close_frac(state, pos, pos["rem"], mark)
            t = _finalize(state, symbol, pos, now, "trail", fill)
            events.append(f"CLOSE {symbol} ${t['pnl']:+.2f} [trail]")
            continue

    # 2. Entries: FAST pump (>= VEL_RUNUP) AND vol-normalized z >= ZMIN
    cooldowns = state.setdefault("cooldowns", {})
    cd_secs = ps.COOLDOWN_HRS * 3600
    blacklist = state.setdefault("stopped_out", [])
    blacklist[:] = [s for s in blacklist if mkt.get(s, {}).get("chg", 0.0) >= ps.PREFILTER_24H]

    pre = []
    for symbol, info in mkt.items():
        if symbol in state["positions"] or symbol in blacklist:
            continue
        last_close = cooldowns.get(symbol)
        if last_close is not None and (now - last_close) < cd_secs:
            continue
        if info["chg"] >= ps.PREFILTER_24H:
            pre.append((symbol, info["chg"]))
    pre.sort(key=lambda c: c[1], reverse=True)

    candidates = []
    for symbol, _ in pre[:ps.MAX_VEL_CHECKS]:
        vel, z = _pump_velocity_z(symbol, now)
        if vel is not None and vel >= ps.VEL_RUNUP and z is not None and z >= ZMIN:
            candidates.append((symbol, vel, z))
    candidates.sort(key=lambda c: c[1], reverse=True)
    for symbol, vel, z in candidates:
        if len(state["positions"]) >= ps.MAX_POSITIONS:
            break
        slip = ps._vol_slip(mkt[symbol].get("vol", 0.0))
        _open(state, symbol, mkt[symbol]["mark"], now, slip)
        events.append(
            f"OPEN  {symbol} SHORT @ {mkt[symbol]['mark']:.4g} "
            f"({ps.VEL_WINDOW_H}h pump {vel * 100:+.0f}%, z={z:.1f}sigma, vol ${mkt[symbol].get('vol', 0) / 1e6:.1f}M "
            f"-> slip {slip * 100:.2f}% | scale-out {ps.TP1_PCT * 100:.0f}/{ps.TP2_PCT * 100:.0f}%+trail, SL {ps.SL_FIXED * 100:.0f}%)"
        )

    # 3. Report
    unrealized = 0.0
    open_rows = []
    for symbol, pos in state["positions"].items():
        info = mkt.get(symbol)
        mark = info["mark"] if info else pos["entry"]
        rem = pos.get("rem", 1.0)
        short_ret = pos["entry"] / mark - 1.0
        u = rem * pos["qty"] * (pos["entry"] - mark)
        unrealized += u
        open_rows.append((symbol, short_ret * 100, u, (now - pos["entry_ts"]) / 3600))

    _save(state)

    closed = state["closed"]
    wins = [t for t in closed if t["pnl"] > 0]
    total = state["equity"] + unrealized
    return {
        "events": events, "equity": state["equity"], "unrealized": unrealized,
        "total": total, "start": ps.START_EQUITY, "open_rows": open_rows,
        "n_closed": len(closed), "n_wins": len(wins),
        "realized_pnl": state["equity"] - ps.START_EQUITY,
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
            f"[{ts}] SHORT-VN ${r['total']:,.2f} ({ret_pct:+.2f}%) | "
            f"real ${r['realized_pnl']:+,.2f} unreal ${r['unrealized']:+,.2f} | "
            f"open {len(r['open_rows'])} closed {r['n_closed']} (win {wr:.0f}%)",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[error] vn short paper tick failed: {exc}", file=sys.stderr)
        sys.exit(1)
