"""Gold (XAUUSD) bar backtest: signed weights, spread cost, calendar-time swap.

Chronology (same as ST-TRA): weight w[t] is decided at the close of bar t and held from the
open of bar t+1 to the open of bar t+2.  Swap is charged on calendar time held (weekends
included, which approximates the broker's triple-swap day).
Protocol: research/GOLD_PROTOCOL_V1.md
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Callable

import numpy as np
import pandas as pd

TF_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
YEAR_MS = 365.25 * 86_400_000
# trading bars per year differ from crypto: gold trades ~5 days x ~23h
BARS_PER_YEAR = {"4h": 6 * 260, "1d": 260}


@dataclass(frozen=True)
class Costs:
    side_bps: float = 3.0            # half spread + slippage per side (no commission at the user's broker)
    swap_long: float = 0.04          # annual financing paid by a long (fraction of notional)
    swap_short: float = 0.01         # annual financing paid by a short

    def stressed(self) -> "Costs":
        return Costs(self.side_bps * 2, 0.08, 0.03)


def resample(h: pd.DataFrame, tf: str) -> pd.DataFrame:
    if tf == "1h":
        return h
    ms = TF_MS[tf]
    key = (h.index.to_numpy() // ms) * ms
    g = h.groupby(key)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last()})
    out.index.name = "start_ms"
    return out


def run(b: pd.DataFrame, w: pd.Series, costs: Costs = Costs()) -> pd.DataFrame:
    o = b["open"]
    pos = w.shift(1).fillna(0.0)
    r = o.shift(-1) / o - 1.0
    dt_years = (pd.Series(b.index, index=b.index).shift(-1) - b.index.to_numpy()) / YEAR_MS
    dw = pos.diff().fillna(pos)
    cost = dw.abs() * costs.side_bps / 1e4
    swap = -(pos.clip(lower=0) * costs.swap_long + (-pos).clip(lower=0) * costs.swap_short) * dt_years
    gross = pos * r
    out = pd.DataFrame({"pos": pos, "gross": gross, "cost": cost, "swap": swap, "net": gross - cost + swap,
                        "turnover": dw.abs()})
    return out.iloc[:-1]


def daily_returns(res: pd.DataFrame, col: str = "net") -> pd.Series:
    day = (res.index.to_numpy() // TF_MS["1d"]) * TF_MS["1d"]
    return (1 + res[col]).groupby(day).prod() - 1


# -- signals (ported from ST-TRA strategies.py; values in {-1, 0, 1}) --------------------------
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def sig_ema(b, fast, slow):
    f, s = ema(b["close"], fast), ema(b["close"], slow)
    return pd.Series(np.where(f > s, 1.0, np.where(f < s, -1.0, 0.0)), index=b.index).where(s.notna(), 0.0)


def sig_tsmom(b, lookback):
    return np.sign(b["close"] / b["close"].shift(lookback) - 1).fillna(0.0)


def sig_donchian(b, entry_n, exit_n):
    up_e = b["high"].rolling(entry_n).max().shift(1).to_numpy()
    lo_e = b["low"].rolling(entry_n).min().shift(1).to_numpy()
    up_x = b["high"].rolling(exit_n).max().shift(1).to_numpy()
    lo_x = b["low"].rolling(exit_n).min().shift(1).to_numpy()
    c = b["close"].to_numpy()
    state, out = 0.0, np.zeros(len(c))
    for i in range(len(c)):
        if (state > 0 and c[i] < lo_x[i]) or (state < 0 and c[i] > up_x[i]):
            state = 0.0
        if state == 0.0:
            state = 1.0 if c[i] > up_e[i] else -1.0 if c[i] < lo_e[i] else 0.0
        out[i] = state
    return pd.Series(out, index=b.index)


def sig_adx(b, n, threshold):
    up, dn = b["high"].diff(), -b["low"].diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=b.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=b.index)
    prev = b["close"].shift(1)
    tr = pd.concat([b["high"] - b["low"], (b["high"] - prev).abs(), (b["low"] - prev).abs()], axis=1).max(axis=1)
    atr = wilder(tr, n)
    pdi, mdi = 100 * wilder(pdm, n) / atr, 100 * wilder(mdm, n) / atr
    adx = wilder(100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan), n)
    return pd.Series(np.where(adx > threshold, np.where(pdi > mdi, 1.0, -1.0), 0.0), index=b.index)


def sig_hold(b):
    return pd.Series(1.0, index=b.index)


@dataclass(frozen=True)
class Candidate:
    family: str
    params: tuple
    fn: Callable = field(compare=False, repr=False)
    side: str = "LF"                  # LF long/flat, LS long/short
    vt: float | None = None           # annual vol target
    cap: float = 2.0

    @property
    def name(self) -> str:
        p = ",".join(f"{k}={v}" for k, v in self.params)
        v = f"|vt={self.vt:.2f}" if self.vt else ""
        return f"{self.family}({p})|{self.side}{v}"

    def weights(self, b: pd.DataFrame, tf: str) -> pd.Series:
        s = self.fn(b, **dict(self.params)).fillna(0.0)
        if self.side == "LF":
            s = s.clip(lower=0.0)
        if self.vt:
            look = 180 if tf == "4h" else 30
            rv = np.log(b["close"]).diff().rolling(look).std() * np.sqrt(BARS_PER_YEAR[tf])
            s = (s * (self.vt / rv).clip(upper=self.cap)).fillna(0.0)
        else:
            s = s.clip(-1.0, 1.0)
        return s.clip(-self.cap, self.cap)


def _grid(fam, fn, **axes):
    keys = list(axes)
    return [(fam, tuple(zip(keys, v)), fn) for v in product(*axes.values())]


def families(tf: str) -> list[Candidate]:
    """Pre-declared trial set (GOLD_PROTOCOL_V1 section 3)."""
    if tf == "1d":
        base = (_grid("ema", sig_ema, fast=[10, 20, 30], slow=[50, 100, 200])
                + _grid("tsmom", sig_tsmom, lookback=[20, 60, 120, 200])
                + _grid("donchian", sig_donchian, entry_n=[20, 55, 100], exit_n=[10, 20, 50])
                + _grid("adx", sig_adx, n=[14, 20], threshold=[20.0, 25.0]))
    else:
        base = (_grid("ema", sig_ema, fast=[20, 50], slow=[100, 200, 400])
                + _grid("tsmom", sig_tsmom, lookback=[60, 180, 360])
                + _grid("donchian", sig_donchian, entry_n=[60, 120, 240], exit_n=[30, 60])
                + _grid("adx", sig_adx, n=[14, 28], threshold=[20.0, 25.0]))
    out = []
    for fam, params, fn in base:
        p = dict(params)
        if p.get("fast", 0) >= p.get("slow", 10**9) or p.get("exit_n", 0) >= p.get("entry_n", 10**9):
            continue
        for side in ("LF", "LS"):
            for vt in (0.10, 0.15):
                out.append(Candidate(fam, params, fn, side, vt))
    out += [Candidate("hold", (), sig_hold, "LF", None), Candidate("hold", (), sig_hold, "LF", 0.10),
            Candidate("hold", (), sig_hold, "LF", 0.15)]
    return out


H0 = Candidate("ema", (("fast", 20), ("slow", 200)), sig_ema, "LF", 0.15)   # ST-TRA frozen signal, gold vol target
BUY_HOLD = Candidate("hold", (), sig_hold, "LF", None)
