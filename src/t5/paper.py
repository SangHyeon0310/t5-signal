"""Forward Paper runner for MT5 gold (no orders). Port of ST-TRA PaperRunner incl. 2026-10-14 fixes:
state saved right after START; every step event carries a state snapshot so a mid-step crash is rebuilt.

Fill model: target decided at a bar close is filled at the next bar open, rounded DOWN to the broker
lot step (``sizing.lots_for_weight``), paying ``side_bps``; swap accrues on calendar time held.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from . import engine as E
from .journal import GENESIS, Journal, write_atomic
from .sizing import SymbolSpec, lots_for_weight


@dataclass(frozen=True)
class PaperConfig:
    run_id: str
    symbol: str
    tf: str
    candidate: str
    notional_usd: float = 10_000.0
    side_bps: float = 3.0
    swap_long: float = 0.04
    contract_size: float = 100.0
    volume_min: float = 0.01
    volume_step: float = 0.01

    def config_hash(self) -> str:
        return "sha256:" + hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    @property
    def spec(self) -> SymbolSpec:
        return SymbolSpec(self.contract_size, self.volume_min, self.volume_step)


@dataclass
class PaperState:
    cash: float
    units: float = 0.0                       # ounces
    pending_target: float | None = None
    pending_decided_at: int | None = None
    last_bar_ms: int | None = None
    last_open_ms: int | None = None
    fills: int = 0
    swap_paid: float = 0.0
    events: int = 0
    last_hash: str = GENESIS
    extra: dict = field(default_factory=dict)


SNAP = ("cash", "units", "pending_target", "pending_decided_at", "last_bar_ms", "last_open_ms", "fills", "swap_paid")


@dataclass(frozen=True)
class Mix:
    """Fixed-weight mix of pre-declared candidates on one account (net weight = sum of parts).

    Name format: ``mix(0.5:<candidate name>;0.5:<candidate name>)``.  GOLD_PROTOCOL_V2 D1 is
    ``mix(0.5:ema(fast=20,slow=200)|LF|vt=0.15;0.5:donchian(entry_n=240,exit_n=30)|LF|vt=0.10)``.
    The research mixed daily returns (daily rebalance); netting one position per bar is the tradable equivalent.
    """
    parts: tuple

    def weights(self, b: pd.DataFrame, tf: str) -> pd.Series:
        return sum(k * c.weights(b, tf) for k, c in self.parts)


def find_candidate(name: str, tf: str):
    if name.startswith("mix(") and name.endswith(")"):
        parts = []
        for item in name[4:-1].split(";"):
            k, sub = item.split(":", 1)
            parts.append((float(k), find_candidate(sub, tf)))
        if abs(sum(k for k, _ in parts) - 1.0) > 1e-9:
            raise ValueError(f"mix weights must sum to 1: {name}")
        return Mix(tuple(parts))
    for c in E.families(tf):
        if c.name == name:
            return c
    raise KeyError(f"candidate not pre-declared: {name} @ {tf}")


class PaperRunner:
    def __init__(self, cfg: PaperConfig, workdir: Path):
        self.cfg, self.dir = cfg, workdir
        self.cand = find_candidate(cfg.candidate, cfg.tf)
        workdir.mkdir(parents=True, exist_ok=True)
        self.state_path = workdir / f"{cfg.run_id}.state.json"
        self.journal = Journal(workdir / f"{cfg.run_id}.journal.jsonl")

    # -- persistence ------------------------------------------------------------------
    def save(self, st: PaperState) -> None:
        write_atomic(self.state_path, json.dumps(asdict(st), indent=2))

    def _append(self, st: PaperState, body: dict, snap: bool = True) -> None:
        if snap:
            body = {**body, "snap": {k: getattr(st, k) for k in SNAP}}
        st.events, st.last_hash = self.journal.append((st.events, st.last_hash), body)

    def load(self, now_ms: int) -> PaperState:
        if self.state_path.exists():
            st = PaperState(**json.loads(self.state_path.read_text()))
            n = self.journal.verify()
            if n > st.events:                                  # crashed mid-step: rebuild from last snapshot
                rec = self.journal.records()[-1]
                if "snap" not in rec["body"]:
                    raise ValueError("state/journal mismatch; refusing to continue")
                st = PaperState(**rec["body"]["snap"], events=n, last_hash=rec["hash"], extra=st.extra)
                self.save(st)
            elif n != st.events:
                raise ValueError("state/journal mismatch; refusing to continue")
            return st
        if self.journal.path.exists() and self.journal.path.stat().st_size > 0:
            raise ValueError("journal exists without state; refusing to start a second chain")
        st = PaperState(cash=self.cfg.notional_usd)
        self._append(st, {"type": "START", "config": asdict(self.cfg), "config_hash": self.cfg.config_hash(),
                          "now_ms": now_ms}, snap=False)
        self.save(st)
        return st

    # -- main -------------------------------------------------------------------------
    def step(self, bars: pd.DataFrame, now_ms: int) -> PaperState:
        """``bars``: CLOSED UTC bars of cfg.tf with open/high/low/close (oldest first)."""
        if bars.empty:
            raise RuntimeError("no closed bars")
        st = self.load(now_ms)
        new = bars.iloc[-1:] if st.last_bar_ms is None else bars[bars.index > st.last_bar_ms]
        if new.empty:
            return st
        w = self.cand.weights(bars, self.cfg.tf)               # causal: w[t] uses bars <= t
        for bar_ms, bar in new.iterrows():
            o = float(bar["open"])
            if st.last_open_ms is not None and st.units > 0:   # swap on calendar time held since last open
                dt = (int(bar_ms) - st.last_open_ms) / E.YEAR_MS
                fee = st.units * o * self.cfg.swap_long * dt
                st.cash -= fee
                st.swap_paid += fee
            st.last_open_ms = int(bar_ms)
            if st.pending_target is not None:
                self._fill(st, int(bar_ms), o, now_ms)
            target = float(w.loc[bar_ms])
            st.pending_target, st.pending_decided_at = target, int(bar_ms)
            st.last_bar_ms = int(bar_ms)
            close = float(bar["close"])
            self._append(st, {"type": "DECISION", "bar_ms": int(bar_ms), "close": close, "target": target,
                              "equity_mark": st.cash + st.units * close, "catch_up": len(new) > 1, "now_ms": now_ms})
        self.save(st)
        return st

    def _fill(self, st: PaperState, bar_ms: int, o: float, now_ms: int) -> None:
        equity = st.cash + st.units * o
        lots = lots_for_weight(st.pending_target, equity, o, self.cfg.spec)
        target_units = lots * self.cfg.contract_size
        delta = target_units - st.units
        decided = st.pending_decided_at
        st.pending_target, st.pending_decided_at = None, None
        if abs(delta) < 1e-12:
            return
        px = o * (1 + (1 if delta > 0 else -1) * self.cfg.side_bps / 1e4)
        st.units += delta
        st.cash -= delta * px
        st.fills += 1
        self._append(st, {"type": "PAPER_FILL", "bar_ms": bar_ms, "decided_at": decided, "ref_open": o,
                          "fill_px": px, "units_delta": delta, "lots": lots, "cash": st.cash, "units": st.units,
                          "now_ms": now_ms})
