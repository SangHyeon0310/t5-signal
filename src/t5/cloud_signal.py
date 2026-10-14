"""Free cloud signal (no PC, no MT5): H0/D1 targets from free gold feeds, phone push on change.

Runs hourly on GitHub Actions (.github/workflows/t5-signal.yml).  The user places the order in the MT5 phone
app (semi-automatic).  Feeds, measured against the HistData spot research data 2025-01..2026-09 (H0, 4h):
  - PAXG/USDT (Binance public data, tokenised gold) restricted to gold market hours: 98.0% state agreement
  - GC=F (Yahoo, COMEX front-month futures): 98.3% state agreement
Gold market hours are taken from the GC=F timestamps when both feeds load; otherwise weekends are dropped.
Never places orders; needs no broker login.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import engine as E
from .paper import find_candidate
from .sizing import SymbolSpec, lots_for_weight

H1_COUNT = 6000                       # same window as the Paper runner
UA = {"User-Agent": "Mozilla/5.0 (t5-cloud-signal)"}
STRATEGIES = {"H0": E.H0.name,
              "D1": "mix(0.5:ema(fast=20,slow=200)|LF|vt=0.15;0.5:donchian(entry_n=240,exit_n=30)|LF|vt=0.10)"}


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_gc(get=_get) -> pd.DataFrame:
    d = json.loads(get("https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF?interval=1h&range=730d"))
    g = d["chart"]["result"][0]
    q = g["indicators"]["quote"][0]
    df = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close")},
                      index=pd.Index(np.asarray(g["timestamp"], "int64") * 1000, name="start_ms")).dropna()
    df.index = (df.index // 3_600_000) * 3_600_000
    return df[~df.index.duplicated(keep="last")].sort_index()


def fetch_paxg(hours: int = H1_COUNT + 2500, get=_get, now_ms: int | None = None) -> pd.DataFrame:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    t, rows = now_ms - hours * 3_600_000, []
    while t < now_ms:
        d = json.loads(get(f"https://data-api.binance.vision/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=1000&startTime={t}"))
        if not d:
            break
        rows += d
        t = int(d[-1][0]) + 3_600_000
        if len(d) < 1000:
            break
    df = pd.DataFrame([[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])] for r in rows],
                      columns=["start_ms", "open", "high", "low", "close"]).set_index("start_ms")
    return df[~df.index.duplicated(keep="last")].sort_index()


def market_hours_only(h: pd.DataFrame, mask_index: pd.Index | None) -> pd.DataFrame:
    if mask_index is not None and len(mask_index):
        return h[h.index.isin(mask_index) | (h.index > mask_index.max())]   # newest hours: GC=F may lag slightly
    wd = pd.to_datetime(h.index, unit="ms").weekday
    return h[wd < 5]


def load_feed(get=_get, now_ms: int | None = None) -> tuple[pd.DataFrame, str]:
    """PAXG on gold market hours (GC=F mask), else GC=F, else PAXG weekdays. Returns (H1 bars, feed name)."""
    gc = px = None
    errors = []
    try:
        gc = fetch_gc(get)
    except Exception as e:                                    # noqa: BLE001
        errors.append(f"GC=F: {e}")
    try:
        px = fetch_paxg(get=get, now_ms=now_ms)
    except Exception as e:                                    # noqa: BLE001
        errors.append(f"PAXG: {e}")
    if px is not None and len(px) > 1000:
        name = "PAXG (gold hours from GC=F)" if gc is not None else "PAXG (weekdays)"
        return market_hours_only(px, gc.index if gc is not None else None), name
    if gc is not None and len(gc) > 1000:
        return gc, "GC=F (COMEX futures)"
    raise RuntimeError("no feed: " + "; ".join(errors))


@dataclass(frozen=True)
class Account:
    equity_usd: float = 700.0
    contract_size: float = 100.0      # oz per lot; XAUUSD247-type symbols may be 1 (check in MT5)
    volume_min: float = 0.01
    volume_step: float = 0.01


def compute(h1: pd.DataFrame, now_ms: int, acc: Account) -> dict:
    h1 = h1[h1.index + 3_600_000 <= now_ms].iloc[-H1_COUNT:]
    b = E.resample(h1[["open", "high", "low", "close"]], "4h")
    b = b[b.index + E.TF_MS["4h"] <= now_ms]
    price = float(b["close"].iloc[-1])
    spec = SymbolSpec(acc.contract_size, acc.volume_min, acc.volume_step)
    out = {"bar_ms": int(b.index[-1]), "price": price, "targets": {}}
    for k, name in STRATEGIES.items():
        w = float(find_candidate(name, "4h").weights(b, "4h").iloc[-1])
        lots = lots_for_weight(w, acc.equity_usd, price, spec)
        out["targets"][k] = {"weight": round(w, 4), "lots": lots,
                             "below_min_lot": bool(w > 0 and lots == 0)}
    return out


def changed(prev: dict | None, cur: dict, min_change: float = 0.10) -> list[str]:
    if not prev:
        return []
    msgs = []
    for k, t in cur["targets"].items():
        p = prev.get("targets", {}).get(k)
        if p is None:
            continue
        flip = (p["weight"] > 0) != (t["weight"] > 0)
        if flip or abs(t["weight"] - p["weight"]) >= min_change or t["lots"] != p["lots"]:
            msgs.append(k)
    return msgs


def format_msg(cur: dict, prev: dict | None, keys: list[str], feed: str, acc: Account, title: str) -> str:
    bar = pd.Timestamp(cur["bar_ms"] + E.TF_MS["4h"], unit="ms").tz_localize("UTC").tz_convert("Asia/Seoul")
    lines = [f"[T5 금] {title}", f"4시간봉 마감 {bar:%m-%d %H:%M} KST · 가격 {cur['price']:,.2f} ({feed})"]
    for k in keys or list(cur["targets"]):
        t = cur["targets"][k]
        p = (prev or {}).get("targets", {}).get(k)
        arrow = f"{p['weight'] * 100:.0f}% → " if p else ""
        if t["below_min_lot"]:
            act = f"최소 주문(0.01랏={acc.contract_size * 0.01:g}oz)보다 작아 주문 불가"
        elif p and t["lots"] != p["lots"]:
            d = round(t["lots"] - p["lots"], 2)
            act = f"{'매수' if d > 0 else '매도'} {abs(d):.2f}랏 → 보유 {t['lots']:.2f}랏"
        else:
            act = f"보유 {t['lots']:.2f}랏"
        lines.append(f"{k}: 목표 {arrow}{t['weight'] * 100:.0f}% · {act}")
    lines.append(f"(계좌 ${acc.equity_usd:,.0f} 기준 · 주문은 MT5 앱에서 직접)")
    return "\n".join(lines)


# ---- notification channels (secrets come from the environment, never from the repo) ----
def send_telegram(token: str, chat_id: str, text: str, post=None) -> None:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    (post or (lambda u, d: urllib.request.urlopen(urllib.request.Request(u, data=d), timeout=30).read()))(url, data)


def telegram_commands(token: str, chat_id: str, offset: int, get=_get) -> tuple[list[str], int]:
    d = json.loads(get(f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=0"))
    cmds, new_off = [], offset
    for u in d.get("result", []):
        new_off = max(new_off, int(u["update_id"]) + 1)
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) == str(chat_id) and (m.get("text") or "").startswith("/"):
            cmds.append(m["text"].split()[0].lower())
    return cmds, new_off


def send_ntfy(topic: str, text: str, post=None) -> None:
    url = f"https://ntfy.sh/{urllib.parse.quote(topic)}"
    (post or (lambda u, d: urllib.request.urlopen(urllib.request.Request(u, data=d), timeout=30).read()))(url, text.encode())
