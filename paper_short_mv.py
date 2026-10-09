#!/usr/bin/env python3
"""Multi-venue forward paper-trader for the VELOCITY-SHORT strategy (stdlib-only).

Same edge as paper_short.py (short a thin perp that pumped >= VEL_RUNUP over
VEL_WINDOW_H hours; scale out as it reverts), but sourced from the ILLIQUID TAILS
of Binance-futures and Bybit-linear instead of Hyperliquid. The backtest showed the
edge is an illiquidity premium that lives below ~$2M/day, so we scan only the thin
tail of each venue:
    Binance  quoteVolume  < $2M/day   (BIN_MAX_VOL)
    Bybit    turnover24h  < $1M/day   (BYB_MAX_VOL)

Kept deliberately SEPARATE from the Hyperliquid trader:
  * own ledger      paper_short_mv_state.json
  * own log         short_mv_run.log   (written by tick_once_mv.py)
  * own scheduled task
so the clean HL forward test stays uncontaminated and this adds zero risk to the
live HL tick. All strategy PARAMS are imported from paper_short (single source of
truth) — only the data source and per-venue plumbing differ.

Symbols are namespaced by venue ("BIN:1000PEPEUSDT", "BYB:XYZUSDT") so positions,
cooldowns and the stop-out blacklist never collide across venues. Paper only.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import paper_short as ps   # single source of truth for strategy params + _vol_slip

STATE = Path(__file__).with_name("paper_short_mv_state.json")

# --- venue universe filters (the thin tail is where the edge lives) ----------
BIN_MAX_VOL = 2_000_000.0   # Binance: only perps under $2M/day quote volume
BIN_MIN_VOL = 50_000.0      # ...but skip dead markets (garbage data / unfillable)
BYB_MAX_VOL = 1_000_000.0   # Bybit: only linear perps under $1M/day turnover
BYB_MIN_VOL = 30_000.0
UA = {"User-Agent": "Mozilla/5.0"}
HTTP_TIMEOUT = 20


def _get(url: str):
    """GET JSON with light retries; None on failure (never raises)."""
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.loads(urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read())
        except Exception:
            time.sleep(0.6)
    return None


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# --- market snapshots: {"BIN:SYM"|"BYB:SYM": {mark, chg, vol}} ---------------
def _binance_markets() -> dict:
    out: dict = {}
    data = _get("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if not isinstance(data, list):
        return out
    for t in data:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or "_" in sym:   # skip dated futures
            continue
        vol = _f(t.get("quoteVolume"))
        if not (BIN_MIN_VOL <= vol < BIN_MAX_VOL):
            continue
        out[f"BIN:{sym}"] = {
            "mark": _f(t.get("lastPrice")),
            "chg": _f(t.get("priceChangePercent")) / 100.0,   # percent -> fraction
            "vol": vol,
        }
    return out


def _bybit_markets() -> dict:
    out: dict = {}
    data = _get("https://api.bybit.com/v5/market/tickers?category=linear")
    lst = (((data or {}).get("result") or {}).get("list")) or []
    for t in lst:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        vol = _f(t.get("turnover24h"))
        if not (BYB_MIN_VOL <= vol < BYB_MAX_VOL):
            continue
        out[f"BYB:{sym}"] = {
            "mark": _f(t.get("lastPrice")),
            "chg": _f(t.get("price24hPcnt")),   # already a fraction
            "vol": vol,
        }
    return out


def _markets() -> dict:
    mkt = {}
    mkt.update(_binance_markets())
    mkt.update(_bybit_markets())
    return mkt


def _held_mark(key: str) -> dict | None:
    """Live mark for a HELD symbol regardless of the volume band.

    Open positions must always be managed. Without this, a coin whose 24h volume
    rises above the entry cap (which a pump does) drops out of _markets(), so the
    management loop's `info is None` branch skips it forever — max-hold, stop and
    TP never fire and the short is orphaned at a stale mark. Fetch its ticker
    directly so exits still trigger. None on failure (position just waits a tick).
    """
    try:
        venue, sym = key.split(":", 1)
    except ValueError:
        return None
    if venue == "BIN":
        d = _get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={sym}")
        if not isinstance(d, dict) or d.get("lastPrice") is None:
            return None
        return {"mark": _f(d.get("lastPrice")),
                "chg": _f(d.get("priceChangePercent")) / 100.0,
                "vol": _f(d.get("quoteVolume"))}
    if venue == "BYB":
        d = _get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}")
        lst = (((d or {}).get("result") or {}).get("list")) or []
        if not lst:
            return None
        t = lst[0]
        return {"mark": _f(t.get("lastPrice")),
                "chg": _f(t.get("price24hPcnt")),
                "vol": _f(t.get("turnover24h"))}
    return None


def _velocity(key: str, now: float) -> float | None:
    """Run-up over the last VEL_WINDOW_H hours from the venue's 1h candles."""
    venue, sym = key.split(":", 1)
    w = ps.VEL_WINDOW_H
    limit = w + 5
    closes: list[float] = []
    if venue == "BIN":
        rows = _get(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}"
                    f"&interval=1h&limit={limit}")
        if not isinstance(rows, list) or len(rows) < w + 1:
            return None
        closes = [_f(r[4]) for r in rows]                      # chronological
    elif venue == "BYB":
        r = _get(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}"
                 f"&interval=60&limit={limit}")
        rows = (((r or {}).get("result") or {}).get("list")) or []
        if len(rows) < w + 1:
            return None
        rows = rows[::-1]                                      # newest-first -> chronological
        closes = [_f(row[4]) for row in rows]
    else:
        return None
    now_px, past_px = closes[-1], closes[-(w + 1)]
    if now_px <= 0 or past_px <= 0:
        return None
    return now_px / past_px - 1.0


def _load() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except (ValueError, OSError):
            pass
    return {"equity": ps.START_EQUITY, "positions": {}, "closed": [],
            "cooldowns": {}, "stopped_out": []}


def _save(state: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE)


def _open(state: dict, key: str, mark: float, now: float, slip: float) -> None:
    """Open a SHORT on the namespaced symbol; mirrors paper_short._open sans HL ATR."""
    notional = state["equity"] * ps.RISK_FRAC
    fill = mark * (1 - slip)                       # sell to open, fills lower on thin coins
    qty = notional / fill
    entry_fee = notional * ps.TAKER_FEE
    state["equity"] -= entry_fee
    venue = key.split(":", 1)[0]
    state["positions"][key] = {
        "side": -1, "entry": fill, "qty": qty, "notional": notional, "entry_ts": now,
        "sl_frac": ps.SL_FIXED, "slip": slip, "sig": f"{venue.lower()}·vel{ps.VEL_WINDOW_H}h",
        "rem": 1.0, "tp1_done": False, "tp2_done": False, "low_px": mark, "realized": -entry_fee,
    }


def _close_frac(state: dict, pos: dict, frac: float, mark: float, extra_slip: float = 0.0) -> float:
    fill = mark * (1 + pos.get("slip", ps.SLIPPAGE) + extra_slip)
    units = frac * pos["qty"]
    chunk = units * (pos["entry"] - fill) - units * fill * ps.TAKER_FEE
    state["equity"] += chunk
    pos["realized"] += chunk
    pos["rem"] -= frac
    return fill


def _finalize(state: dict, key: str, pos: dict, now: float, reason: str, fill: float) -> dict:
    notional = pos.get("notional", 0.0)
    trade = {
        "symbol": key, "entry": pos["entry"], "exit": fill,
        "ret_pct": (pos["realized"] / notional * 100) if notional else 0.0,
        "pnl": pos["realized"], "reason": reason,
        "held_hrs": (now - pos["entry_ts"]) / 3600,
        "tp1": pos.get("tp1_done", False), "tp2": pos.get("tp2_done", False),
        "sig": pos.get("sig", "vel"),
    }
    state["closed"].append(trade)
    state["positions"].pop(key, None)
    state.setdefault("cooldowns", {})[key] = now
    if reason == "stop_loss":
        bl = state.setdefault("stopped_out", [])
        if key not in bl:
            bl.append(key)
    return trade


def tick() -> dict:
    """One multi-venue short-paper tick: mark, exit, enter. Returns a report."""
    state = _load()
    now = time.time()
    mkt = _markets()
    events: list[str] = []

    # Ensure every HELD position is marked even if its coin has left the entry
    # universe (volume rose above the cap), so exits still fire. See _held_mark.
    for key in state["positions"]:
        if key not in mkt:
            hm = _held_mark(key)
            if hm is not None:
                mkt[key] = hm

    # 1. Manage open shorts — SCALE-OUT exits (identical logic to paper_short)
    for key in list(state["positions"].keys()):
        info = mkt.get(key)
        if info is None or info["mark"] <= 0:
            continue
        pos = state["positions"][key]
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
            t = _finalize(state, key, pos, now, "stop_loss", fill)
            events.append(f"CLOSE {key} ${t['pnl']:+.2f} [stop_loss]")
            continue
        if age_hrs >= ps.MAX_HOLD_HRS:
            fill = _close_frac(state, pos, pos["rem"], mark)
            t = _finalize(state, key, pos, now, "max_hold", fill)
            events.append(f"CLOSE {key} ${t['pnl']:+.2f} [max_hold]")
            continue
        if not pos["tp1_done"] and fall >= ps.TP1_PCT:
            _close_frac(state, pos, 1 / 3, mark)
            pos["tp1_done"] = True
            events.append(f"TP1   {key} banked 1/3 (price {fall * 100:+.0f}%)")
        if not pos["tp2_done"] and fall >= ps.TP2_PCT:
            _close_frac(state, pos, 1 / 3, mark)
            pos["tp2_done"] = True
            events.append(f"TP2   {key} banked 1/3 (price {fall * 100:+.0f}%)")
        if pos["tp2_done"] and pos["rem"] > 1e-9 and mark >= pos["low_px"] * (1 + ps.TRAIL_PCT):
            fill = _close_frac(state, pos, pos["rem"], mark)
            t = _finalize(state, key, pos, now, "trail", fill)
            events.append(f"CLOSE {key} ${t['pnl']:+.2f} [trail]")
            continue

    # 2. Entries: short FAST pumps (>= VEL_RUNUP over VEL_WINDOW_H hours)
    cooldowns = state.setdefault("cooldowns", {})
    cd_secs = ps.COOLDOWN_HRS * 3600
    blacklist = state.setdefault("stopped_out", [])
    blacklist[:] = [k for k in blacklist if mkt.get(k, {}).get("chg", 0.0) >= ps.PREFILTER_24H]

    pre = []
    for key, info in mkt.items():
        if key in state["positions"] or key in blacklist:
            continue
        last_close = cooldowns.get(key)
        if last_close is not None and (now - last_close) < cd_secs:
            continue
        if info["chg"] >= ps.PREFILTER_24H:
            pre.append((key, info["chg"]))
    pre.sort(key=lambda c: c[1], reverse=True)

    candidates = []
    for key, _ in pre[:ps.MAX_VEL_CHECKS]:
        vel = _velocity(key, now)
        if vel is not None and vel >= ps.VEL_RUNUP:
            candidates.append((key, vel))
    candidates.sort(key=lambda c: c[1], reverse=True)
    for key, vel in candidates:
        if len(state["positions"]) >= ps.MAX_POSITIONS:
            break
        slip = ps._vol_slip(mkt[key].get("vol", 0.0))
        _open(state, key, mkt[key]["mark"], now, slip)
        events.append(
            f"OPEN  {key} SHORT @ {mkt[key]['mark']:.4g} "
            f"({ps.VEL_WINDOW_H}h pump {vel * 100:+.0f}%, vol ${mkt[key].get('vol', 0) / 1e6:.2f}M "
            f"-> slip {slip * 100:.2f}% | scale-out {ps.TP1_PCT * 100:.0f}/{ps.TP2_PCT * 100:.0f}%+trail, "
            f"SL {ps.SL_FIXED * 100:.0f}%)"
        )

    # 3. Report
    unrealized = 0.0
    open_rows = []
    for key, pos in state["positions"].items():
        info = mkt.get(key)
        mark = info["mark"] if info and info["mark"] > 0 else pos["entry"]
        rem = pos.get("rem", 1.0)
        short_ret = pos["entry"] / mark - 1.0
        u = rem * pos["qty"] * (pos["entry"] - mark)
        unrealized += u
        open_rows.append((key, short_ret * 100, u, (now - pos["entry_ts"]) / 3600))

    _save(state)

    closed = state["closed"]
    wins = [t for t in closed if t["pnl"] > 0]
    total = state["equity"] + unrealized
    return {
        "events": events, "equity": state["equity"], "unrealized": unrealized,
        "total": total, "start": ps.START_EQUITY, "open_rows": open_rows,
        "n_closed": len(closed), "n_wins": len(wins),
        "realized_pnl": state["equity"] - ps.START_EQUITY,
        "n_markets": len(mkt),
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
            f"[{ts}] SHORT-MV ${r['total']:,.2f} ({ret_pct:+.2f}%) | "
            f"real ${r['realized_pnl']:+,.2f} unreal ${r['unrealized']:+,.2f} | "
            f"open {len(r['open_rows'])} closed {r['n_closed']} (win {wr:.0f}%) | "
            f"universe {r['n_markets']}",
            flush=True,
        )
        for key, retp, u, age in r["open_rows"]:
            print(f"    {key:16s} SHORT {retp:+6.1f}%  ${u:+8.2f}  {age:4.1f}h", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] mv short paper tick failed: {exc}", file=sys.stderr)
        sys.exit(1)
