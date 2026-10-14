"""Lot sizing for MT5 CFD symbols (XAUUSD) from a target weight (notional / equity).

All inputs come from the broker's symbol specification (MT5 symbol_info):
trade_contract_size, volume_min, volume_step, volume_max. Never hard-code them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolSpec:
    contract_size: float        # oz per 1.00 lot (typical XAUUSD: 100; some "micro" symbols: 1)
    volume_min: float           # e.g. 0.01
    volume_step: float          # e.g. 0.01
    volume_max: float = 100.0

    def notional_per_lot(self, price: float) -> float:
        return self.contract_size * price


def lots_for_weight(weight: float, equity: float, price: float, spec: SymbolSpec) -> float:
    """Largest lot size not exceeding |weight| * equity of notional (rounded DOWN to the step).

    Returns 0 when even the minimum lot would exceed the target (never rounds risk up)."""
    if weight <= 0 or equity <= 0:
        return 0.0
    raw = weight * equity / spec.notional_per_lot(price)
    steps = math.floor(raw / spec.volume_step + 1e-9)
    lots = round(steps * spec.volume_step, 8)
    if lots < spec.volume_min:
        return 0.0
    return min(lots, spec.volume_max)


def weight_step(equity: float, price: float, spec: SymbolSpec) -> float:
    """Weight granularity: how much one volume step changes notional / equity."""
    return spec.volume_step * spec.notional_per_lot(price) / equity


def min_lot_weight(equity: float, price: float, spec: SymbolSpec) -> float:
    """Leverage of the smallest possible position (min lot notional / equity)."""
    return spec.volume_min * spec.notional_per_lot(price) / equity


def min_equity_for_step(max_step: float, price: float, spec: SymbolSpec) -> float:
    """Equity needed so that one volume step changes the weight by at most ``max_step``."""
    return spec.volume_step * spec.notional_per_lot(price) / max_step
