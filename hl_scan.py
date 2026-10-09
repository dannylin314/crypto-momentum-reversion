#!/usr/bin/env python3
"""Hyperliquid opportunity scanner (stdlib-only, no codebase deps).

Polls Hyperliquid's PUBLIC info API and flags three real, transparent edges:

  1. NEW LISTING   - a perp symbol that appeared since the last snapshot
                     (earliest catchable window, measured in minutes).
  2. FUNDING EXTREME - |hourly funding| beyond a threshold => crowded book,
                     squeeze fuel. Sign tells you which side is trapped.
  3. VOLUME / OI SURGE - 24h notional volume (or open interest) spiking versus
                     its own recent baseline => attention inflection.

Why standalone: common ccxt/okx crypto loaders do NOT support Hyperliquid
(the ccxt perp path is Binance-only), so this hits the HL info API directly.

API (verify against https://hyperliquid.gitbook.io - endpoints drift):
  POST https://api.hyperliquid.xyz/info   Content-Type: application/json
    {"type":"meta"}               -> {"universe":[{"name":"BTC",...}, ...]}
    {"type":"metaAndAssetCtxs"}   -> [meta, [ctx,...]] where ctx has:
        funding, openInterest, dayNtlVlm (24h notional vol), prevDayPx,
        markPx, oraclePx, midPx  (index-aligned to meta["universe"])

State (new-listing diff + volume baseline) persists next to this file.
Alerts print to stdout; set HL_WEBHOOK_URL for a POST (Slack/Discord/generic).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

API = "https://api.hyperliquid.xyz/info"
STATE = Path(__file__).with_name("hl_state.json")

# --- thresholds (tune to taste) ---------------------------------------------
FUNDING_ABS = float(os.getenv("HL_FUNDING_ABS", "0.0005"))   # per-hour; ~0.05%
VOL_SURGE_X = float(os.getenv("HL_VOL_SURGE_X", "3.0"))       # 24h vol vs baseline
OI_SURGE_X = float(os.getenv("HL_OI_SURGE_X", "2.0"))         # OI vs baseline
BASELINE_ALPHA = 0.2                                          # EWMA smoothing


def _post(body: dict) -> object:
    """POST a JSON body to the HL info endpoint and return parsed JSON."""
    req = urllib.request.Request(
        API,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (fixed host)
        return json.loads(resp.read().decode())


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except (ValueError, OSError):
            pass
    return {"universe": [], "vol_ewma": {}, "oi_ewma": {}}


def _save_state(state: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE)


def _webhook_url() -> str:
    """Resolve the alert webhook from env, else a local gitignored file.

    Keeps the credential out of shell history / command logs: drop the URL into
    ``hl_webhook.txt`` next to this script (one line) and it is read each run.
    """
    env = os.getenv("HL_WEBHOOK_URL", "").strip()
    if env:
        return env
    f = Path(__file__).with_name("hl_webhook.txt")
    if f.exists():
        return f.read_text().strip()
    return ""


def _alert(kind: str, msg: str) -> None:
    line = f"[{kind}] {msg}"
    print(line, flush=True)
    hook = _webhook_url()
    if not hook:
        return
    try:
        req = urllib.request.Request(
            hook, data=json.dumps({"text": line}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=10).close()  # noqa: S310
    except Exception as exc:  # a broken webhook must never kill the scan
        print(f"[warn] webhook failed: {exc}", file=sys.stderr)


def _f(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
        return out if out == out else default  # drop NaN
    except (TypeError, ValueError):
        return default


def scan() -> int:
    """Run one scan pass. Returns the number of alerts emitted."""
    state = _load_state()
    prev_universe = set(state.get("universe", []))
    vol_ewma: dict = state.get("vol_ewma", {})
    oi_ewma: dict = state.get("oi_ewma", {})

    meta, ctxs = _post({"type": "metaAndAssetCtxs"})
    names = [a["name"] for a in meta["universe"]]
    alerts = 0

    # 1. New listings (skip the very first run so we don't alert the whole board)
    if prev_universe:
        for new in sorted(set(names) - prev_universe):
            _alert("NEW-LISTING", f"{new} perp is now live on Hyperliquid")
            alerts += 1

    for name, ctx in zip(names, ctxs):
        funding = _f(ctx.get("funding"))
        vol = _f(ctx.get("dayNtlVlm"))
        oi = _f(ctx.get("openInterest"))
        mark = _f(ctx.get("markPx"))
        prev = _f(ctx.get("prevDayPx"))
        chg = (mark / prev - 1.0) * 100 if prev > 0 else 0.0

        # 2. Funding extreme
        if abs(funding) >= FUNDING_ABS:
            side = "shorts paying (squeeze-up fuel)" if funding < 0 else "longs paying (top-heavy)"
            _alert("FUNDING", f"{name} hourly funding {funding:+.4%} - {side} | 24h {chg:+.1f}%")
            alerts += 1

        # 3. Volume / OI surge vs each symbol's own EWMA baseline
        base_v = vol_ewma.get(name)
        if base_v and base_v > 0 and vol >= VOL_SURGE_X * base_v:
            _alert("VOL-SURGE", f"{name} 24h vol ${vol:,.0f} = {vol / base_v:.1f}x baseline | {chg:+.1f}%")
            alerts += 1
        base_oi = oi_ewma.get(name)
        if base_oi and base_oi > 0 and oi >= OI_SURGE_X * base_oi:
            _alert("OI-SURGE", f"{name} OI {oi:,.0f} = {oi / base_oi:.1f}x baseline | funding {funding:+.4%}")
            alerts += 1

        # update baselines (EWMA)
        vol_ewma[name] = vol if not base_v else (1 - BASELINE_ALPHA) * base_v + BASELINE_ALPHA * vol
        oi_ewma[name] = oi if not base_oi else (1 - BASELINE_ALPHA) * base_oi + BASELINE_ALPHA * oi

    _save_state({"universe": names, "vol_ewma": vol_ewma, "oi_ewma": oi_ewma})
    return alerts


if __name__ == "__main__":
    try:
        n = scan()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] scan complete - {n} alert(s)", flush=True)
    except Exception as exc:  # noqa: BLE001 - top-level guard for cron use
        print(f"[error] scan failed: {exc}", file=sys.stderr)
        sys.exit(1)
