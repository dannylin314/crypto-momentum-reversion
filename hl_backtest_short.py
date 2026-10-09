#!/usr/bin/env python3
"""Strategy comparison backtest on Hyperliquid history (stdlib-only).

Fetches each perp's candles + funding ONCE, then replays several strategies on
the same data so they're directly comparable:

  * squeeze-long        — the funding-squeeze longs (with funding carry).
  * short-runup >=X%     — SHORT a coin after a >=X% 24h run-up, betting on mean
                          reversion. ATR-scaled stop/target, funding carry (a
                          short RECEIVES positive funding). Tested at several X.

Reuses the data + params from hl_backtest.py. Fees + slippage per side.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import hl_backtest as hb

CACHE = Path(__file__).with_name("bt_series_cache.json")

RUNUPS = [0.25, 0.30, 0.40, 0.50]     # 24h run-up thresholds to short
LOOKBACK_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
N_COINS = int(sys.argv[2]) if len(sys.argv) > 2 else 232
# Extra adverse slippage on a STOP exit: a shorted memecoin that keeps pumping
# gaps through the stop and fills far worse than the clean 0.05%. This stresses
# the biggest real-world risk (getting run over). Tune with argv[3].
def _argf(i: int, default: float) -> float:
    """Parse a positional float arg, ignoring non-numeric tokens like 'sweep'."""
    if len(sys.argv) > i:
        try:
            return float(sys.argv[i])
        except ValueError:
            pass
    return default


STOP_SLIP = _argf(3, 0.015)


def _run_short(s: dict, runup: float, require_pos_funding: bool = False,
               stop_slip: float = STOP_SLIP) -> list[dict]:
    """Short a coin when 24h change >= runup; mean-reversion exit. ATR-scaled.

    ``require_pos_funding``: only short when funding >= 0 (crowded long = top-heavy
    confirmation, and a positive-funding short earns carry instead of paying it).
    Stop exits pay an extra ``stop_slip`` to model gapping through the stop.
    """
    close, fund, atr, chg = s["close"], s["fund"], s["atr"], s["chg"]
    trades: list[dict] = []
    pos = None
    cooldown_until = -1
    for i in range(len(close)):
        if pos is not None:
            if i > pos["i"]:
                pos["carry"] += fund[i] * hb.NOTIONAL   # short RECEIVES positive funding
            move = close[i] / pos["entry"] - 1.0         # price move since entry
            age = i - pos["i"]
            reason = None
            if -move >= pos["tp"]:        # price fell by tp => short take-profit
                reason = "take_profit"
            elif move >= pos["sl"]:       # price rose by sl => short stop-loss
                reason = "stop_loss"
            elif age >= hb.MAX_HOLD_HRS:
                reason = "max_hold"
            if reason:
                slip = hb.SLIPPAGE + (stop_slip if reason == "stop_loss" else 0.0)
                exit_fill = close[i] * (1 + slip)             # buy to close, pay up (worse on stops)
                gross = -(exit_fill / pos["entry"] - 1.0)     # short return
                price_pnl = hb.NOTIONAL * gross - hb.NOTIONAL * 2 * hb.TAKER_FEE
                pnl = price_pnl + pos["carry"]
                trades.append({"ret": gross, "pnl": pnl, "price_pnl": price_pnl,
                               "carry": pos["carry"], "reason": reason, "held": age})
                pos = None
                cooldown_until = i + hb.COOLDOWN_HRS
                continue
        if pos is None and i >= cooldown_until and atr[i] is not None and chg[i] is not None:
            if chg[i] >= runup and (not require_pos_funding or fund[i] >= 0.0):
                entry = close[i] * (1 - hb.SLIPPAGE)          # sell to open, fill lower
                sl = min(max(hb.ATR_SL_MULT * atr[i], hb.SL_FLOOR), hb.SL_CAP)
                tp = min(max(hb.RR * sl, hb.TP_FLOOR), hb.TP_CAP)
                pos = {"entry": entry, "i": i, "sl": sl, "tp": tp, "carry": 0.0}
    return trades


SWEEP_SLIPS = [0.0, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]


def _run_short_fixed(s: dict, runup: float, sl_frac: float | None, tp_frac: float,
                     stop_slip: float = STOP_SLIP, max_hold: float | None = None) -> list[dict]:
    """Short-runup with EXPLICIT sl/tp fractions (sl_frac/max_hold=None => disabled)."""
    if max_hold is None:
        max_hold = hb.MAX_HOLD_HRS
    close, fund, atr, chg = s["close"], s["fund"], s["atr"], s["chg"]
    trades: list[dict] = []
    pos = None
    cooldown_until = -1
    for i in range(len(close)):
        if pos is not None:
            if i > pos["i"]:
                pos["carry"] += fund[i] * hb.NOTIONAL
            short_ret = -(close[i] / pos["entry"] - 1.0)   # + if price fell (profit)
            age = i - pos["i"]
            reason = None
            if short_ret >= tp_frac:
                reason = "take_profit"
            elif sl_frac is not None and short_ret <= -sl_frac:
                reason = "stop_loss"
            elif max_hold != float("inf") and age >= max_hold:
                reason = "max_hold"
            if reason:
                slip = hb.SLIPPAGE + (stop_slip if reason == "stop_loss" else 0.0)
                exit_fill = close[i] * (1 + slip)
                gross = -(exit_fill / pos["entry"] - 1.0)
                pnl = hb.NOTIONAL * gross - hb.NOTIONAL * 2 * hb.TAKER_FEE + pos["carry"]
                trades.append({"pnl": pnl, "reason": reason, "held": age})
                pos = None
                cooldown_until = i + hb.COOLDOWN_HRS
                continue
        if pos is None and i >= cooldown_until and atr[i] is not None and chg[i] is not None:
            if chg[i] >= runup:
                pos = {"entry": close[i] * (1 - hb.SLIPPAGE), "i": i, "carry": 0.0}
    # close any position still open at end of data (mark to last close)
    if pos is not None:
        i = len(close) - 1
        exit_fill = close[i] * (1 + hb.SLIPPAGE)
        gross = -(exit_fill / pos["entry"] - 1.0)
        pnl = hb.NOTIONAL * gross - hb.NOTIONAL * 2 * hb.TAKER_FEE + pos["carry"]
        trades.append({"pnl": pnl, "reason": "end_of_data", "held": i - pos["i"]})
    return trades


def _maxhold_sweep(series: dict) -> None:
    """Does removing the 12h timeout help? Compare max-hold values incl. none (inf)."""
    runup, tp, sl = 0.30, 0.20, 0.30
    print(f"MAX-HOLD SWEEP (short >= {runup * 100:.0f}%, TP {tp * 100:.0f}% / SL {sl * 100:.0f}%, "
          f"{STOP_SLIP * 100:.1f}% stop-slip):\n")
    print(f"    {'max-hold':>9} | {'trades':>6} | {'win%':>5} | {'total':>10} | {'exp':>8} | "
          f"{'avg hold':>8} | {'worst':>9} | exits")
    for mh in [12, 24, 48, 72, float("inf")]:
        trades: list[dict] = []
        for s in series.values():
            trades += _run_short_fixed(s, runup, sl, tp, max_hold=mh)
        if not trades:
            continue
        wins = sum(1 for t in trades if t["pnl"] > 0)
        total = sum(t["pnl"] for t in trades)
        worst = min(t["pnl"] for t in trades)
        avg_hold = sum(t["held"] for t in trades) / len(trades)
        byr: dict[str, int] = {}
        for t in trades:
            byr[t["reason"]] = byr.get(t["reason"], 0) + 1
        exits = " ".join(f"{k}:{v}" for k, v in sorted(byr.items()))
        lbl = "none" if mh == float("inf") else f"{mh:.0f}h"
        print(f"    {lbl:>9} | {len(trades):6d} | {wins / len(trades) * 100:4.1f}% | "
              f"${total:+9.2f} | ${total / len(trades):+7.2f} | {avg_hold:6.1f}h | ${worst:+8.2f} | {exits}")
    print()


def _wide_sl_sweep(series: dict) -> None:
    """Does a super-wide (or no) stop help? Watch win% rise but the worst loss balloon."""
    runup, tp = 0.40, 0.20
    print(f"WIDE-SL TEST (short >= {runup * 100:.0f}% run-up, fixed TP {tp * 100:.0f}%, "
          f"{STOP_SLIP * 100:.1f}% stop-slip):\n")
    print(f"    {'stop':>5} | {'trades':>6} | {'win%':>5} | {'total':>10} | {'exp':>8} | "
          f"{'worst 1 loss':>12} | exits")
    for sl in [0.10, 0.15, 0.20, 0.30, 0.50, None]:
        trades: list[dict] = []
        for s in series.values():
            trades += _run_short_fixed(s, runup, sl, tp)
        if not trades:
            continue
        wins = sum(1 for t in trades if t["pnl"] > 0)
        total = sum(t["pnl"] for t in trades)
        worst = min(t["pnl"] for t in trades)
        byr: dict[str, int] = {}
        for t in trades:
            byr[t["reason"]] = byr.get(t["reason"], 0) + 1
        exits = " ".join(f"{k}:{v}" for k, v in sorted(byr.items()))
        lbl = "none" if sl is None else f"{sl * 100:.0f}%"
        print(f"    {lbl:>5} | {len(trades):6d} | {wins / len(trades) * 100:4.1f}% | "
              f"${total:+9.2f} | ${total / len(trades):+7.2f} | ${worst:+11.2f} | {exits}")
    print()


def _sweep(series: dict) -> None:
    """Show how expectancy decays as stop-slippage rises — find where the edge breaks."""
    print("STOP-SLIPPAGE SWEEP (where does the short-runup edge break?):\n")
    for r in (0.30, 0.40):
        print(f"  short >= {r * 100:.0f}% run-up:")
        print(f"    {'stop-slip':>9} | {'trades':>6} | {'win%':>5} | {'total':>10} | {'exp/trade':>9}")
        for ss in SWEEP_SLIPS:
            trades: list[dict] = []
            for s in series.values():
                trades += _run_short(s, r, stop_slip=ss)
            if not trades:
                continue
            wins = sum(1 for t in trades if t["pnl"] > 0)
            total = sum(t["pnl"] for t in trades)
            flag = "  <- breaks even" if total < 0 else ""
            print(f"    {ss * 100:8.1f}% | {len(trades):6d} | {wins / len(trades) * 100:4.1f}% | "
                  f"${total:+9.2f} | ${total / len(trades):+8.2f}{flag}")
        print()


def _pivot_low(low: list, entry_i: int, lookback: int) -> float | None:
    """Lowest low in the `lookback` bars BEFORE entry (pre-pump support target)."""
    window = low[max(0, entry_i - lookback):entry_i]
    return min(window) if window else None


def _vol_slip(vol: float, base: float = 0.0005, ref: float = 5_000_000.0, cap: float = 0.05) -> float:
    """Per-coin slippage that scales inversely with 24h $ volume (thin = worse).

    Liquid coins (vol >= ref) floor at `base` (5 bps); thinner coins ramp up
    inverse-linearly to `cap` (5%). A coarse spread/depth proxy from volume.
    """
    if vol <= 0:
        return cap
    return min(cap, max(base, base * ref / vol))


def _run_short_scaleout(s: dict, runup: float, sl_frac: float, tp1: float, tp2: float,
                        pivot_lookback: int = 72, trail: float = 0.05,
                        stop_slip: float = STOP_SLIP, max_hold: float | None = None,
                        slip: float | None = None, liq: float = float("inf"),
                        chg_window: int | None = None, confirm_downbar: bool = False,
                        blacklist_after_stop: bool = False,
                        prior_range: tuple = (float("-inf"), float("inf"))) -> list[dict]:
    """3-way scale-out short: 1/3 at tp1, 1/3 at tp2, final 1/3 runs to a pivot low
    with a trailing stop. Hard stop / max-hold close all remaining at once.
    `slip` overrides the flat base slippage (e.g. volume-scaled per coin).
    `liq` = adverse move (price rise) at which an isolated-margin position is
    liquidated (~1/leverage); pre-empts the stop when tighter."""
    if max_hold is None:
        max_hold = hb.MAX_HOLD_HRS
    if slip is None:
        slip = hb.SLIPPAGE
    close, low, atr, chg = s["close"], s["low"], s["atr"], s["chg"]
    trades: list[dict] = []
    pos = None
    cooldown_until = -1
    blacklisted = False
    N = hb.NOTIONAL
    for i in range(len(close)):
        if pos is not None:
            e = pos["entry"]
            pos["low_px"] = min(pos["low_px"], low[i])
            short_ret = -(close[i] / e - 1.0)          # + when price fell (profit)
            age = i - pos["i"]

            def _bank(frac: float, px: float, extra_slip: float = 0.0) -> None:
                fill = px * (1 + slip + extra_slip)
                gross = -(fill / e - 1.0)
                pos["pnl"] += N * frac * gross - N * frac * hb.TAKER_FEE   # exit fee on the frac

            rise = -short_ret                          # price rise = short loss
            # liquidation (leverage): isolated margin wiped before the stop is reached
            if rise >= liq:
                _bank(pos["rem"], close[i], stop_slip)  # liquidated at a bad fill
                trades.append({"pnl": pos["pnl"], "reason": "liquidation", "held": age})
                pos = None
                cooldown_until = i + hb.COOLDOWN_HRS
                continue
            # hard stop or max-hold: dump ALL remaining
            if short_ret <= -sl_frac or (max_hold != float("inf") and age >= max_hold):
                reason = "stop_loss" if short_ret <= -sl_frac else "max_hold"
                _bank(pos["rem"], close[i], stop_slip if reason == "stop_loss" else 0.0)
                trades.append({"pnl": pos["pnl"], "reason": reason, "held": age})
                pos = None
                cooldown_until = i + hb.COOLDOWN_HRS
                if blacklist_after_stop and reason == "stop_loss":
                    blacklisted = True     # don't re-short this runaway until the pump breaks
                continue
            # scale-out TPs
            if not pos["tp1"] and short_ret >= tp1:
                _bank(1 / 3, close[i]); pos["rem"] -= 1 / 3; pos["tp1"] = True
            if not pos["tp2"] and short_ret >= tp2:
                _bank(1 / 3, close[i]); pos["rem"] -= 1 / 3; pos["tp2"] = True
            # runner (final third) after tp2: exit on pivot target or trailing bounce
            if pos["tp2"] and pos["rem"] > 1e-9:
                pivot_ret = (e - pos["pivot"]) / e if pos["pivot"] else 999.0
                if short_ret >= pivot_ret:
                    _bank(pos["rem"], close[i]); pos["rem"] = 0.0
                    trades.append({"pnl": pos["pnl"], "reason": "pivot", "held": age})
                    pos = None; cooldown_until = i + hb.COOLDOWN_HRS; continue
                if close[i] >= pos["low_px"] * (1 + trail):     # bounced off the low
                    _bank(pos["rem"], close[i]); pos["rem"] = 0.0
                    trades.append({"pnl": pos["pnl"], "reason": "trail", "held": age})
                    pos = None; cooldown_until = i + hb.COOLDOWN_HRS; continue
        if pos is None and i >= cooldown_until and atr[i] is not None:
            if chg_window is None:
                cv = chg[i]
            elif i >= chg_window and close[i - chg_window] > 0:
                cv = close[i] / close[i - chg_window] - 1.0     # run-up over a custom window
            else:
                cv = None
            if blacklisted and cv is not None and cv < runup:
                blacklisted = False        # pump broke — a fresh setup is allowed again
            downbar_ok = (not confirm_downbar) or (i > 0 and close[i] < close[i - 1])
            ok = cv is not None and cv >= runup and downbar_ok and not blacklisted
            if ok and prior_range != (float("-inf"), float("inf")):
                w = chg_window if chg_window else 24        # 24h trend BEFORE the pump window
                j = i - w
                prior = (close[j] / close[j - 24] - 1.0) if (j - 24 >= 0 and close[j - 24] > 0) else None
                ok = prior is not None and prior_range[0] <= prior <= prior_range[1]
            if ok:
                e = close[i] * (1 - slip)
                pos = {"entry": e, "i": i, "rem": 1.0, "pnl": -N * hb.TAKER_FEE,  # entry fee once
                       "tp1": False, "tp2": False, "low_px": low[i],
                       "pivot": _pivot_low(low, i, pivot_lookback)}
    if pos is not None:                                  # close leftover at data end
        i = len(close) - 1
        gross = -((close[i] * (1 + hb.SLIPPAGE)) / pos["entry"] - 1.0)
        pos["pnl"] += N * pos["rem"] * gross - N * pos["rem"] * hb.TAKER_FEE
        trades.append({"pnl": pos["pnl"], "reason": "end_of_data", "held": i - pos["i"]})
    return trades


def _scaleout_compare(series: dict) -> None:
    """Baseline single-TP vs the 3-way scale-out (trailing runner to pivot low)."""
    runup, sl = 0.30, 0.30
    print(f"EXIT COMPARISON (short >= {runup * 100:.0f}%, SL {sl * 100:.0f}%, {STOP_SLIP * 100:.1f}% stop-slip):\n")

    def show(label: str, trades: list[dict]) -> None:
        if not trades:
            print(f"  {label}: (none)"); return
        wins = sum(1 for t in trades if t["pnl"] > 0)
        total = sum(t["pnl"] for t in trades)
        worst = min(t["pnl"] for t in trades)
        avg_hold = sum(t["held"] for t in trades) / len(trades)
        byr: dict[str, int] = {}
        for t in trades:
            byr[t["reason"]] = byr.get(t["reason"], 0) + 1
        exits = " ".join(f"{k}:{v}" for k, v in sorted(byr.items()))
        print(f"  {label}: {len(trades)} trades | win {wins / len(trades) * 100:.0f}% | "
              f"total ${total:+.0f} | exp ${total / len(trades):+.2f}/trade | avg {avg_hold:.1f}h | worst ${worst:+.0f}")
        print(f"           exits: {exits}")

    base, scaled = [], []
    for s in series.values():
        base += _run_short_fixed(s, runup, sl, 0.20)               # current: single fixed 20% TP
        scaled += _run_short_scaleout(s, runup, sl, 0.08, 0.15)    # 3-way scale-out + trailing to pivot
    show("baseline (single 20% TP)", base)
    print()
    show("scale-out (8/15/pivot+trail)", scaled)
    print()


def _fmt(label: str, trades: list[dict]) -> None:
    if not trades:
        print(f"  {label}: (none)"); return
    wins = sum(1 for t in trades if t["pnl"] > 0)
    total = sum(t["pnl"] for t in trades)
    worst = min(t["pnl"] for t in trades)
    avg = sum(t["held"] for t in trades) / len(trades)
    byr: dict[str, int] = {}
    for t in trades:
        byr[t["reason"]] = byr.get(t["reason"], 0) + 1
    ex = " ".join(f"{k}:{v}" for k, v in sorted(byr.items()))
    print(f"  {label}: {len(trades)} tr | win {wins / len(trades) * 100:.0f}% | total ${total:+.0f} | "
          f"exp ${total / len(trades):+.2f} | avg {avg:.1f}h | worst ${worst:+.0f}")
    print(f"           {ex}")


def _scaleout_hold_sweep(series: dict) -> None:
    """Scale-out exit across max-hold values — does more room let the runner reach the pivot?"""
    runup, sl = 0.30, 0.30
    print(f"SCALE-OUT x MAX-HOLD  (short >= {runup * 100:.0f}%, SL {sl * 100:.0f}%, {STOP_SLIP * 100:.1f}% stop-slip):\n")
    base = []
    for s in series.values():
        base += _run_short_fixed(s, runup, sl, 0.20, max_hold=12)
    _fmt("baseline single-TP 12h ", base)
    print()
    for mh in [12, 24, 48, 72, float("inf")]:
        trades = []
        for s in series.values():
            trades += _run_short_scaleout(s, runup, sl, 0.08, 0.15, max_hold=mh)
        lbl = "none" if mh == float("inf") else f"{mh:.0f}h"
        _fmt(f"scale-out hold={lbl:>4} ", trades)
    print()


def _liquidity_test(series: dict) -> None:
    """Does filtering out thin coins (by 24h $ volume) improve the scale-out edge?

    Uses CURRENT dayNtlVlm as a per-coin liquidity proxy (one API call) — a coarse
    static tier, not volume-at-entry, but enough to answer 'do the liquid names do
    better?'. Partitions the cached coins by volume threshold and re-runs scale-out.
    """
    meta, ctxs = hb._post({"type": "metaAndAssetCtxs"})
    vol = {a["name"]: hb._f(c.get("dayNtlVlm")) for a, c in zip(meta["universe"], ctxs)}
    runup, sl = 0.30, 0.30
    print("LIQUIDITY FILTER TEST (scale-out short >= 30%, SL 30%; min 24h $ volume):\n")
    for thr in [0, 500_000, 1_000_000, 5_000_000, 20_000_000, 50_000_000]:
        sub = {c: s for c, s in series.items() if vol.get(c, 0.0) >= thr}
        trades: list[dict] = []
        for s in sub.values():
            trades += _run_short_scaleout(s, runup, sl, 0.08, 0.15)
        _fmt(f"vol >= ${thr / 1e6:>5.1f}M ({len(sub):3d} coins)", trades)
    print()


def _volslip_test(series: dict) -> None:
    """Does the scale-out edge survive REALISTIC (volume-scaled) execution?"""
    meta, ctxs = hb._post({"type": "metaAndAssetCtxs"})
    vol = {a["name"]: hb._f(c.get("dayNtlVlm")) for a, c in zip(meta["universe"], ctxs)}
    runup, sl = 0.30, 0.30
    print("VOLUME-SCALED SLIPPAGE (scale-out short >= 30%): flat 0.05% vs per-coin vol-slip\n")

    flat, scaled = [], []
    slip_samples = []
    for c, s in series.items():
        vs = _vol_slip(vol.get(c, 0.0))
        slip_samples.append(vs)
        flat += _run_short_scaleout(s, runup, sl, 0.08, 0.15)                 # flat 5bps
        scaled += _run_short_scaleout(s, runup, sl, 0.08, 0.15, slip=vs)      # realistic
    _fmt("flat 0.05% slip     ", flat)
    print()
    _fmt("volume-scaled slip  ", scaled)
    ss = sorted(slip_samples)
    print(f"\n  per-coin slip: median {ss[len(ss) // 2] * 100:.2f}%  "
          f"min {ss[0] * 100:.2f}%  max {ss[-1] * 100:.2f}%  (over {len(ss)} coins)")
    print()


def _tpsl_grid(series: dict) -> None:
    """Grid-search TP1/TP2 x hard-SL under REALISTIC (volume-scaled) slippage.

    The honest test: is there ANY tp/sl combo whose edge survives real execution
    costs? Run under flat slippage it would just reproduce the fiction, so this
    charges each coin its volume-scaled slip.
    """
    meta, ctxs = hb._post({"type": "metaAndAssetCtxs"})
    vol = {a["name"]: hb._f(c.get("dayNtlVlm")) for a, c in zip(meta["universe"], ctxs)}
    slip = {c: _vol_slip(vol.get(c, 0.0)) for c in series}
    runup = 0.30
    tp_pairs = [(0.05, 0.10), (0.08, 0.15), (0.10, 0.20), (0.15, 0.30)]
    sls = [0.20, 0.30, 0.40, 0.50]
    print("TP/SL GRID under REALISTIC (volume-scaled) slippage — scale-out short >= 30%")
    print("cells = exp $/trade (win%); positive = survives real costs\n")
    print("  SL \\ TP1/TP2 |" + "|".join(f"  {int(a * 100)}/{int(b * 100)}%  " for a, b in tp_pairs))
    print("  " + "-" * 60)
    for sl in sls:
        cells = []
        for tp1, tp2 in tp_pairs:
            trades: list[dict] = []
            for c, s in series.items():
                trades += _run_short_scaleout(s, runup, sl, tp1, tp2, slip=slip[c])
            n = len(trades)
            tot = sum(t["pnl"] for t in trades)
            w = sum(1 for t in trades if t["pnl"] > 0)
            cells.append(f"${tot / n:+6.2f}({w / n * 100:2.0f}%)" if n else "   n/a   ")
        print(f"  {sl * 100:>3.0f}%         |" + "|".join(cells))
    print()


def _leverage_test(series: dict) -> None:
    """Leverage on the best config (50% SL, 8/15% TP, vol-slip), WITH liquidation.

    Return-on-capital scales with L, but isolated-margin liquidation at ~1/L
    pre-empts the wide stop — converting it into an effective tight stop, which
    the grid showed loses. Shows exp per-notional AND on-capital per trade.
    """
    meta, ctxs = hb._post({"type": "metaAndAssetCtxs"})
    vol = {a["name"]: hb._f(c.get("dayNtlVlm")) for a, c in zip(meta["universe"], ctxs)}
    slip = {c: _vol_slip(vol.get(c, 0.0)) for c in series}
    runup, sl, tp1, tp2 = 0.30, 0.50, 0.08, 0.15
    print("LEVERAGE TEST — best config (50% SL, 8/15% TP, realistic slippage)")
    print("liquidation modelled at ~90%/L adverse move (isolated margin)\n")
    print(f"    {'lev':>4} | {'liq@':>5} | {'exp/notional':>12} | {'on-capital/trade':>16} | {'win%':>5} | liquidations")
    for L in [1, 2, 3, 5, 10]:
        liq = float("inf") if L <= 1 else 0.90 / L
        trades: list[dict] = []
        for c, s in series.items():
            trades += _run_short_scaleout(s, runup, sl, tp1, tp2, slip=slip[c], liq=liq)
        n = len(trades)
        tot = sum(t["pnl"] for t in trades)
        w = sum(1 for t in trades if t["pnl"] > 0)
        nliq = sum(1 for t in trades if t["reason"] == "liquidation")
        exp_notional = tot / n
        on_capital = exp_notional / hb.NOTIONAL * L * 100        # margin = notional/L
        liq_txt = "none" if L <= 1 else f"{liq * 100:.0f}%"
        print(f"    {L:>3}x | {liq_txt:>5} | ${exp_notional:>+10.2f} | {on_capital:>+14.2f}% | "
              f"{w / n * 100:>4.0f}% | {nliq}/{n}")
    print()


def _pump_variations(series: dict) -> None:
    """Can a bigger/faster pump produce reversions that clear REALISTIC slippage?"""
    meta, ctxs = hb._post({"type": "metaAndAssetCtxs"})
    vol = {a["name"]: hb._f(c.get("dayNtlVlm")) for a, c in zip(meta["universe"], ctxs)}
    slip = {c: _vol_slip(vol.get(c, 0.0)) for c in series}
    sl, tp1, tp2 = 0.50, 0.08, 0.15    # best exit config from the grid

    def run(runup, window):
        trades = []
        for c, s in series.items():
            trades += _run_short_scaleout(s, runup, sl, tp1, tp2, slip=slip[c], chg_window=window)
        return trades

    print("SHORT-AFTER-PUMP VARIATIONS under realistic (volume-scaled) slippage")
    print("scale-out exit, 50% SL. + = clears real costs\n")
    print("A) bigger pump — 24h run-up threshold:")
    for thr in [0.30, 0.50, 0.75, 1.00, 1.50]:
        _fmt(f"   24h >= {thr * 100:>4.0f}%", run(thr, None))
    print("\nB) faster pump — run-up over a shorter window (parabolic velocity), thr = 40%:")
    for win in [4, 8, 12, 24]:
        _fmt(f"   {win:>2}h >= 40%", run(0.40, win))
    print("\nC) fast AND big — >= 60% over 8h:")
    _fmt("   8h >= 60%", run(0.60, 8))
    _fmt("   6h >= 80%", run(0.80, 6))
    print()


def _velocity_grid(series: dict) -> None:
    """Fine grid: pump threshold x window, under realistic slippage. Robust ridge or luck?"""
    meta, ctxs = hb._post({"type": "metaAndAssetCtxs"})
    vol = {a["name"]: hb._f(c.get("dayNtlVlm")) for a, c in zip(meta["universe"], ctxs)}
    slip = {c: _vol_slip(vol.get(c, 0.0)) for c in series}
    sl, tp1, tp2 = 0.50, 0.08, 0.15
    windows = [4, 6, 8, 12, 24]
    thrs = [0.30, 0.40, 0.50, 0.60]
    print("VELOCITY GRID under realistic (volume-scaled) slippage — pump >= THR over WINDOW")
    print("cells = exp $/trade (win%, n_trades); + = clears real costs\n")
    print("  win\\thr |" + "|".join(f"   >={int(t * 100)}%    " for t in thrs))
    print("  " + "-" * 62)
    for w in windows:
        cells = []
        for thr in thrs:
            trades: list[dict] = []
            for c, s in series.items():
                trades += _run_short_scaleout(s, thr, sl, tp1, tp2, slip=slip[c], chg_window=w)
            n = len(trades)
            if not n:
                cells.append("   n/a    ")
                continue
            tot = sum(t["pnl"] for t in trades)
            wn = sum(1 for t in trades if t["pnl"] > 0)
            cells.append(f"${tot / n:+6.2f}({wn / n * 100:2.0f}%,{n:3d})")
        print(f"  {w:>3}h    |" + "|".join(cells))
    print()


def main() -> None:
    end = int(time.time() * 1000)
    start = end - LOOKBACK_DAYS * 24 * hb.HOUR_MS
    coins = hb._universe(N_COINS)
    print(f"strategy comparison | {LOOKBACK_DAYS}d candles (~21d funding) | {len(coins)} coins | "
          f"${hb.NOTIONAL:.0f}/trade, {hb.TAKER_FEE * 2 * 100:.2f}% fees + {hb.SLIPPAGE * 2 * 100:.2f}% slip\n")

    cache: dict[str, dict] = {}
    if CACHE.exists() and "refresh" not in sys.argv:
        try:
            cache = json.loads(CACHE.read_text())
        except Exception:
            cache = {}
    if "cachedonly" in sys.argv:                     # instant: use whatever is cached
        series = {c: v for c, v in cache.items() if v is not None}
        print(f"using {len(series)} cached coins (no fetch)\n")
        if "scaleout" in sys.argv:
            _scaleout_compare(series); return
        if "scalehold" in sys.argv:
            _scaleout_hold_sweep(series); return
        if "liqtest" in sys.argv:
            _liquidity_test(series); return
        if "volslip" in sys.argv:
            _volslip_test(series); return
        if "tpslgrid" in sys.argv:
            _tpsl_grid(series); return
        if "leverage" in sys.argv:
            _leverage_test(series); return
        if "velgrid" in sys.argv:
            _velocity_grid(series); return
        if "pump" in sys.argv:
            _pump_variations(series); return
        if "maxhold" in sys.argv:
            _maxhold_sweep(series); return
        if "widesl" in sys.argv:
            _wide_sl_sweep(series); return
        if "sweep" in sys.argv:
            _sweep(series); return
    series: dict[str, dict] = {}
    fetched = 0
    for c in coins:
        if c in cache:
            series[c] = cache[c]
            continue
        try:
            s = hb._build_series(c, start, end)
        except Exception as exc:
            print(f"  [skip {c}: {exc}]", file=sys.stderr)
            continue
        if s is not None:
            series[c] = s
            cache[c] = s
            fetched += 1
        if fetched and fetched % 15 == 0:           # checkpoint so a timeout resumes
            CACHE.write_text(json.dumps(cache))
        time.sleep(0.1)
    CACHE.write_text(json.dumps(cache))
    print(f"replayed {len(series)}/{len(coins)} coins ({fetched} fresh, {len(series) - fetched} cached)\n")

    if "sweep" in sys.argv:
        _sweep(series)
        return
    if "widesl" in sys.argv:
        _wide_sl_sweep(series)
        return
    if "maxhold" in sys.argv:
        _maxhold_sweep(series)
        return
    if "scaleout" in sys.argv:
        _scaleout_compare(series)
        return

    print(f"stop-slippage stress: {STOP_SLIP * 100:.1f}% extra on stop fills\n")
    print("RESULTS:")
    # short-the-runup: plain vs positive-funding confirmation, at several thresholds
    for r in RUNUPS:
        plain, confirmed = [], []
        for s in series.values():
            plain += _run_short(s, r, require_pos_funding=False)
            confirmed += _run_short(s, r, require_pos_funding=True)
        hb._summ(f"short >= {r * 100:3.0f}%              ", plain)
        hb._summ(f"short >= {r * 100:3.0f}% +pos-funding  ", confirmed)
        print()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[error] backtest failed: {exc}", file=sys.stderr)
        sys.exit(1)
