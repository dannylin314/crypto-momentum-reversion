#!/usr/bin/env python3
"""Historical backtest of the funding-squeeze strategy on Hyperliquid (stdlib-only).

Replays the EXACT rules the forward paper trader uses, but over months of real
Hyperliquid history, so we get a several-hundred-trade verdict in seconds instead
of weeks. Pulls each coin's hourly funding history + 1h candles, aligns them, and
simulates:

  ENTRY (long): funding <= FUNDING_ENTRY (shorts paying) [+ optional price-up
                confirmation: 24h change > 0]
  EXIT:         first of ATR-scaled take-profit / stop-loss, max-hold timeout, or
                funding flips >= FUNDING_FLIP. Re-entry blocked for a cooldown.

Runs BOTH variants — unfiltered (the pre-fix baseline) and filtered (price-confirm
+ cooldown) — and prints the head-to-head. Fees + slippage modelled per side.

This is the honest arbiter the forward test can't be: same signal, big sample.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

API = "https://api.hyperliquid.xyz/info"

# --- strategy params (mirror paper_trade.py) --------------------------------
FUNDING_ENTRY = -0.0005
FUNDING_FLIP = 0.0005
MAX_HOLD_HRS = 12
TAKER_FEE = 0.0005
SLIPPAGE = 0.0005
ATR_N = 24
ATR_SL_MULT = 3.0
RR = 2.0
SL_FLOOR, SL_CAP = 0.02, 0.10
TP_FLOOR, TP_CAP = 0.04, 0.20
COOLDOWN_HRS = 2
NOTIONAL = 1000.0            # fixed $ per trade, non-compounding, for clean expectancy

# --- backtest scope ---------------------------------------------------------
LOOKBACK_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
N_COINS = int(sys.argv[2]) if len(sys.argv) > 2 else 30
HOUR_MS = 3_600_000


def _post(body: dict, _tries: int = 6) -> object:
    """POST with exponential backoff on 429 (HL rate limit)."""
    import urllib.error
    delay = 0.5
    for attempt in range(_tries):
        req = urllib.request.Request(
            API, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < _tries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
                continue
            raise


def _f(v: object, d: float = 0.0) -> float:
    try:
        out = float(v)  # type: ignore[arg-type]
        return out if out == out else d
    except (TypeError, ValueError):
        return d


def _universe(n: int) -> list[str]:
    """First n perp names from Hyperliquid meta (majors + popular perps)."""
    meta = _post({"type": "meta"})
    names = [a["name"] for a in meta["universe"]]
    return names[:n]


def _candles(coin: str, start_ms: int, end_ms: int) -> list[dict]:
    rows = _post({"type": "candleSnapshot",
                  "req": {"coin": coin, "interval": "1h",
                          "startTime": start_ms, "endTime": end_ms}})
    return rows if isinstance(rows, list) else []


def _funding_map(coin: str, start_ms: int, end_ms: int) -> dict[int, float]:
    """Return {hour_open_ms: funding_rate} from fundingHistory."""
    rows = _post({"type": "fundingHistory",
                  "coin": coin, "startTime": start_ms, "endTime": end_ms})
    out: dict[int, float] = {}
    if isinstance(rows, list):
        for r in rows:
            t = int(r.get("time", 0)) // HOUR_MS * HOUR_MS
            out[t] = _f(r.get("fundingRate"))
    return out


def _build_series(coin: str, start_ms: int, end_ms: int):
    """Aligned arrays (t, close, high, low, funding, atr_pct, chg24) per hourly bar."""
    cnd = _candles(coin, start_ms, end_ms)
    if len(cnd) < ATR_N + 30:
        return None
    fund = _funding_map(coin, start_ms, end_ms)

    ts = [int(c["t"]) // HOUR_MS * HOUR_MS for c in cnd]
    close = [_f(c["c"]) for c in cnd]
    high = [_f(c["h"]) for c in cnd]
    low = [_f(c["l"]) for c in cnd]

    # forward-fill funding onto each bar
    fr: list[float] = []
    last = 0.0
    for t in ts:
        if t in fund:
            last = fund[t]
        fr.append(last)

    # rolling ATR% and 24h change
    atr = [None] * len(close)
    for i in range(len(close)):
        if i < ATR_N or close[i] <= 0:
            continue
        trs = []
        for j in range(i - ATR_N + 1, i + 1):
            tr = high[j] - low[j] if j == 0 else max(
                high[j] - low[j], abs(high[j] - close[j - 1]), abs(low[j] - close[j - 1]))
            trs.append(tr)
        a = (sum(trs) / len(trs)) / close[i]
        atr[i] = a if a > 0 else None
    chg24 = [None] * len(close)
    for i in range(24, len(close)):
        if close[i - 24] > 0:
            chg24[i] = close[i] / close[i - 24] - 1.0
    return {"close": close, "high": high, "low": low, "fund": fr, "atr": atr, "chg": chg24}


def _run_coin(s: dict, price_filter: bool) -> list[dict]:
    """Single-position replay for one coin. Returns list of trade dicts."""
    close, fund, atr, chg = s["close"], s["fund"], s["atr"], s["chg"]
    trades: list[dict] = []
    pos = None
    cooldown_until = -1
    for i in range(len(close)):
        if pos is not None:
            # Funding carry: a long RECEIVES -fundingRate * notional each hour held
            # (negative funding => shorts pay longs). Accrue for each held hour.
            if i > pos["i"]:
                pos["carry"] += -fund[i] * NOTIONAL
            ret = close[i] / pos["entry"] - 1.0
            age = i - pos["i"]
            reason = None
            if ret >= pos["tp"]:
                reason = "take_profit"
            elif ret <= -pos["sl"]:
                reason = "stop_loss"
            elif age >= MAX_HOLD_HRS:
                reason = "max_hold"
            elif fund[i] >= FUNDING_FLIP:
                reason = "funding_flip"
            if reason:
                exit_fill = close[i] * (1 - SLIPPAGE)
                gross = exit_fill / pos["entry"] - 1.0
                price_pnl = NOTIONAL * gross - NOTIONAL * 2 * TAKER_FEE
                pnl = price_pnl + pos["carry"]
                trades.append({"ret": gross, "pnl": pnl, "price_pnl": price_pnl,
                               "carry": pos["carry"], "reason": reason, "held": age})
                pos = None
                cooldown_until = i + COOLDOWN_HRS
                continue
        if pos is None and i >= cooldown_until and atr[i] is not None:
            if fund[i] <= FUNDING_ENTRY and (not price_filter or (chg[i] is not None and chg[i] > 0)):
                entry = close[i] * (1 + SLIPPAGE)
                sl = min(max(ATR_SL_MULT * atr[i], SL_FLOOR), SL_CAP)
                tp = min(max(RR * sl, TP_FLOOR), TP_CAP)
                pos = {"entry": entry, "i": i, "sl": sl, "tp": tp, "carry": 0.0}
    return trades


def _summ(label: str, trades: list[dict]) -> None:
    if not trades:
        print(f"  {label}: (no trades)")
        return
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    aw = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0.0
    al = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0
    by_reason: dict[str, float] = {}
    for t in trades:
        by_reason[t["reason"]] = by_reason.get(t["reason"], 0.0) + t["pnl"]
    reasons = "  ".join(f"{k}:${v:+.0f}" for k, v in sorted(by_reason.items()))
    carry = sum(t.get("carry", 0.0) for t in trades)
    price_only = sum(t.get("price_pnl", t["pnl"]) for t in trades)
    print(f"  {label}: {len(trades):4d} trades | win {len(wins) / len(trades) * 100:4.1f}% | "
          f"avg win ${aw:+6.2f}  avg loss ${al:+6.2f} | total ${total:+9.2f} | "
          f"exp ${total / len(trades):+6.2f}/trade")
    print(f"           price P&L ${price_only:+9.2f}  +  funding carry ${carry:+8.2f}  =  ${total:+9.2f}")
    print(f"           by exit: {reasons}")


def main() -> None:
    end = int(time.time() * 1000)
    start = end - LOOKBACK_DAYS * 24 * HOUR_MS
    coins = _universe(N_COINS)
    print(f"HL funding-squeeze backtest | {LOOKBACK_DAYS}d | {len(coins)} coins | "
          f"${NOTIONAL:.0f}/trade, {TAKER_FEE * 2 * 100:.2f}% fees + {SLIPPAGE * 2 * 100:.2f}% slip round-trip")
    print(f"coins: {', '.join(coins)}\n")

    unfiltered: list[dict] = []
    filtered: list[dict] = []
    used = 0
    for c in coins:
        try:
            s = _build_series(c, start, end)
        except Exception as exc:
            print(f"  [skip {c}: {exc}]", file=sys.stderr)
            continue
        if s is None:
            continue
        used += 1
        unfiltered += _run_coin(s, price_filter=False)
        filtered += _run_coin(s, price_filter=True)
        time.sleep(0.15)   # gentle pacing between coins to respect the rate limit

    print(f"replayed {used}/{len(coins)} coins with usable history\n")
    print("RESULTS (both variants, same data):")
    _summ("unfiltered (baseline)  ", unfiltered)
    _summ("filtered (price+cooldwn)", filtered)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[error] backtest failed: {exc}", file=sys.stderr)
        sys.exit(1)
