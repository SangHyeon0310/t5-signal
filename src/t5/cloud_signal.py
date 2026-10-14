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


def fetch_binance(symbol: str, hours: int = H1_COUNT + 2500, get=_get, now_ms: int | None = None) -> pd.DataFrame:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    t, rows = now_ms - hours * 3_600_000, []
    while t < now_ms:
        d = json.loads(get(f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1h&limit=1000&startTime={t}"))
        if not d:
            break
        rows += d
        t = int(d[-1][0]) + 3_600_000
        if len(d) < 1000:
            break
    df = pd.DataFrame([[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])] for r in rows],
                      columns=["start_ms", "open", "high", "low", "close"]).set_index("start_ms")
    return df[~df.index.duplicated(keep="last")].sort_index()


# ST-TRA frozen crypto strategy (SangHyeon0310/ST-TRA research/fasttrack): ema_cross(20,200) long/flat on 4h,
# spot exposure scaled by min(1, 0.40 / realised vol), vol = 180 x 4h log returns annualised with 6*365 bars.
CRYPTO = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}


def st_tra_weights(b: pd.DataFrame) -> pd.Series:
    c = b["close"]
    fast, slow = E.ema(c, 20), E.ema(c, 200)
    sig = (fast > slow).astype(float)
    rv = np.log(c).diff().rolling(180).std() * np.sqrt(6 * 365)
    return (sig * (0.40 / rv).clip(upper=1.0)).fillna(0.0)


def st_tra_weight(b: pd.DataFrame) -> float:
    return float(st_tra_weights(b).iloc[-1])


# ---- display levels: derived from the same rules, they do not change any decision ----
def ema_flip_level(c: pd.Series, fast: int = 20, slow: int = 200) -> float | None:
    """Next 4h close at which EMA(fast) crosses EMA(slow): exit level while long, entry level while flat."""
    a, b = 2 / (fast + 1), 2 / (slow + 1)
    ef, es = float(E.ema(c, fast).iloc[-1]), float(E.ema(c, slow).iloc[-1])
    x = ((1 - b) * es - (1 - a) * ef) / (a - b)
    return x if np.isfinite(x) and x > 0 else None


def donchian_levels(b: pd.DataFrame, entry_n: int = 240, exit_n: int = 30) -> dict:
    """Donchian long state now, its exit level (exit_n low) and entry level (entry_n high) for the next bar."""
    long_now = float(E.sig_donchian(b, entry_n, exit_n).iloc[-1]) > 0
    return {"dc_long": long_now, "dc_exit": float(b["low"].iloc[-exit_n:].min()),
            "dc_entry": float(b["high"].iloc[-entry_n:].max())}


def entry_point(w: pd.Series, close: pd.Series) -> dict | None:
    """Bar where the current holding started (weight went from 0 to > 0): price, time, weight then."""
    on = (w > 0).to_numpy()
    if not len(on) or not on[-1]:
        return None
    i = len(on) - 1
    while i > 0 and on[i - 1]:
        i -= 1
    return {"entry_price": float(close.iloc[i]), "entry_ms": int(w.index[i]), "entry_weight": round(float(w.iloc[i]), 4),
            "entry_before_window": bool(i == 0)}


def compute_crypto(feeds: dict[str, pd.DataFrame], now_ms: int, budget_usdt: float) -> dict:
    out = {}
    for k, h in feeds.items():
        h = h[h.index + 3_600_000 <= now_ms].iloc[-H1_COUNT:]
        b = E.resample(h[["open", "high", "low", "close"]], "4h")
        b = b[b.index + E.TF_MS["4h"] <= now_ms]
        ws = st_tra_weights(b)
        w = float(ws.iloc[-1])
        out[k] = {"weight": round(w, 4), "usdt": round(w * budget_usdt, 2), "price": float(b["close"].iloc[-1]),
                  "bar_ms": int(b.index[-1]), "ema_level": ema_flip_level(b["close"]),
                  **(entry_point(ws, b["close"]) or {})}
    return out


def changed_crypto(prev: dict | None, cur: dict, min_change: float = 0.10) -> list[str]:
    if not prev:
        return []
    return [k for k, t in cur.items() if k in prev and (((prev[k]["weight"] > 0) != (t["weight"] > 0))
                                                        or abs(t["weight"] - prev[k]["weight"]) >= min_change)]


def _kst(ms: int, fmt: str = "%m-%d %H시") -> str:
    return pd.Timestamp(ms, unit="ms").tz_localize("UTC").tz_convert("Asia/Seoul").strftime(fmt)


def _px(x: float | None, nd: int) -> str:
    return "1봉 내 도달 불가" if x is None else f"{x:,.{nd}f}"


def _entry_line(t: dict, price: float, nd: int) -> str:
    if "entry_price" not in t:
        return ""
    when = "기록 이전" if t.get("entry_before_window") else _kst(t["entry_ms"] + E.TF_MS["4h"])
    return f"진입가 {_px(t['entry_price'], nd)} ({when}) · {(price / t['entry_price'] - 1) * 100:+.1f}%"


def format_crypto(cur: dict, prev: dict | None, keys: list[str], budget: float, title: str) -> str:
    any_bar = max(v["bar_ms"] for v in cur.values())
    lines = [f"[ST-TRA 코인] {title} · 4h 마감 {_kst(any_bar + E.TF_MS['4h'])}"]
    for k in keys or list(cur):
        t, p = cur[k], (prev or {}).get(k)
        hold = t["weight"] > 0
        head = f"■ {k} {'롱 보유' if hold else '관망'} · 목표 {t['weight'] * 100:.0f}%"
        if p and keys:
            d = t["usdt"] - p["usdt"]
            head += f" · {'▲ 매수' if d > 0 else '▼ 매도'} {abs(d):,.0f} USDT"
        lines.append(head)
        if hold:
            lines.append(f"현재가 {_px(t['price'], 0)} · {_entry_line(t, t['price'], 0)}")
            amt = f"보유금액 {t['usdt']:,.0f} USDT"
            if "entry_weight" in t:
                amt += f" · 진입금액 {t['entry_weight'] * budget:,.0f} USDT"
            lines.append(amt)
            lines.append(f"손절가 {_px(t.get('ema_level'), 0)} (4h 종가 기준) · 목표가 없음")
        else:
            lines.append(f"현재가 {_px(t['price'], 0)} · 매수 진입가 {_px(t.get('ema_level'), 0)} 이상 (4h 종가)")
    lines.append(f"예산 {budget:,.0f} USDT · 현물 롱 전용")
    return "\n".join(lines)


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
    ema_level = ema_flip_level(b["close"])
    ema_long = bool(E.ema(b["close"], 20).iloc[-1] > E.ema(b["close"], 200).iloc[-1])
    dc = donchian_levels(b)
    for k, name in STRATEGIES.items():
        ws = find_candidate(name, "4h").weights(b, "4h")
        w = float(ws.iloc[-1])
        lots = lots_for_weight(w, acc.equity_usd, price, spec)
        out["targets"][k] = {"weight": round(w, 4), "lots": lots,
                             "below_min_lot": bool(w > 0 and lots == 0),
                             "ema_level": ema_level, "ema_long": ema_long, **(dc if k == "D1" else {}),
                             **(entry_point(ws, b["close"]) or {})}
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
    price = cur["price"]
    lines = [f"[T5 금] {title} · 4h 마감 {_kst(cur['bar_ms'] + E.TF_MS['4h'])}", f"현재가 {price:,.2f} ({feed.split(' ')[0]})"]
    for k in keys or list(cur["targets"]):
        t = cur["targets"][k]
        p = (prev or {}).get("targets", {}).get(k)
        hold = t["weight"] > 0
        if t["below_min_lot"]:
            size = f"최소 {acc.volume_min:g}랏({acc.contract_size * acc.volume_min:g}oz) 미만 → 주문 불가"
        else:
            size = f"{t['lots']:.2f}랏"
        head = f"■ {k} {'롱' if hold else '관망'} {size} · 목표 {t['weight'] * 100:.0f}%"
        if p and t["lots"] != p["lots"]:
            d = round(t["lots"] - p["lots"], 2)
            head += f" · {'▲ 매수' if d > 0 else '▼ 매도'} {abs(d):.2f}랏"
        lines.append(head)
        # D1 = half EMA leg + half Donchian leg: show the exit of each leg that is long, the entry of each that is not.
        legs = [("EMA", t.get("ema_long", hold), t.get("ema_level"), t.get("ema_level"))]
        if k == "D1":
            legs.append(("돈치안", t.get("dc_long", False), t.get("dc_exit"), t.get("dc_entry")))
        named = len(legs) > 1
        stops = [f"{n + ' ' if named else ''}{_px(x, 2)}" for n, on, x, _ in legs if on]
        entries = [f"{n + ' ' if named else ''}{_px(e, 2)}" for n, on, _, e in legs if not on]
        if hold:
            lines.append(_entry_line(t, price, 2))
            if stops:
                lines.append(f"손절가 {' / '.join(stops)} (4h 종가 기준) · 목표가 없음")
        if entries:
            lines.append(f"{'추가 ' if hold else ''}매수 진입가 {' / '.join(entries)} 돌파 (4h 종가)")
    lines.append(f"계좌 ${acc.equity_usd:,.0f} · 롱 전용")
    return "\n".join(l for l in lines if l)


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


def perf(history: list[dict], keys: list[str], price_key=lambda k: "price") -> dict:
    """Hypothetical return of following each key's weight bar by bar (no costs) vs holding, since the first record."""
    out = {}
    for k in keys:
        rows = [h for h in history if k in h and price_key(k) in h]
        if len(rows) < 2:
            continue
        eq = bh = 1.0
        for a, b in zip(rows, rows[1:]):
            r = b[price_key(k)] / a[price_key(k)] - 1
            eq *= 1 + a[k] * r
            bh *= 1 + r
        out[k] = {"strategy": round(eq - 1, 4), "hold": round(bh - 1, 4), "bars": len(rows), "since_ms": rows[0]["bar_ms"]}
    return out


def format_perf(gold: dict, crypto: dict) -> str:
    lines = ["[T5·ST-TRA] 신호 성과 (클라우드 기록 시작 이후, 비용 제외 · 가상 계산)"]
    for name, d in (("금", gold), ("코인", crypto)):
        for k, v in d.items():
            since = pd.Timestamp(v["since_ms"], unit="ms").strftime("%m-%d")
            lines.append(f"{name} {k}: 신호 {v['strategy'] * 100:+.2f}% · 단순 보유 {v['hold'] * 100:+.2f}% ({since}부터 {v['bars']}봉)")
    if len(lines) == 1:
        lines.append("아직 기록이 2개 이상 쌓이지 않았습니다.")
    return "\n".join(lines)
