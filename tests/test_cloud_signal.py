import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from t5 import cloud_signal as C  # noqa: E402
import t5_cloud_signal as T  # noqa: E402

H = 3_600_000
T0 = 1_760_000_000_000 // H * H


class Feeds:
    """Fake Yahoo + Binance endpoints serving one synthetic gold path (weekends closed on 'GC=F')."""

    def __init__(self, n=9000, drift=0.0003, seed=3):
        rng = np.random.default_rng(seed)
        self.c = 2000 * np.exp(np.cumsum(rng.normal(drift, 0.003, n)))
        self.t = T0 + np.arange(n) * H
        self.now = int(self.t[-1]) + H + 60_000
        self.fail_paxg = self.fail_gc = False

    def _bar(self, i):
        c = float(self.c[i]); o = float(self.c[i - 1]) if i else c
        return o, max(o, c) * 1.0005, min(o, c) * 0.9995, c

    def __call__(self, url):
        vis = [i for i in range(len(self.t)) if self.t[i] + H <= self.now]
        if "yahoo" in url:
            if self.fail_gc:
                raise OSError("yahoo down")
            idx = [i for i in vis if (self.t[i] // H // 24 + 4) % 7 < 5]          # drop weekends
            q = {k: [] for k in ("open", "high", "low", "close")}
            for i in idx:
                for k, v in zip(q, self._bar(i)):
                    q[k].append(v)
            return json.dumps({"chart": {"result": [{"timestamp": [int(self.t[i] // 1000) for i in idx],
                                                      "indicators": {"quote": [q]}}]}}).encode()
        if self.fail_paxg:
            raise OSError("binance blocked")
        start = int(url.split("startTime=")[1])
        rows = [[int(self.t[i]), *map(str, self._bar(i))] for i in vis if self.t[i] >= start][:1000]
        return json.dumps(rows).encode()


CFG = {"equity_usd": 10_000.0, "contract_size": 1.0, "volume_min": 0.01, "volume_step": 0.01, "min_change": 0.10,
       "heartbeat_utc_hour": -1}


def test_first_run_then_quiet_then_change(tmp_path):
    f = Feeds()
    st = tmp_path / "s.json"
    r = T.run(st, CFG, f.now, dry=True, get=f)
    assert r["feed"].startswith("PAXG") and len(r["sent"]) == 1 and "시작" in r["sent"][0]
    assert T.run(st, CFG, f.now, dry=True, get=f)["sent"] == []                    # same bar: silent
    hist = json.loads(st.read_text())["history"]
    assert len(hist) == 1
    s = json.loads(st.read_text())
    s["last"]["targets"]["H0"]["weight"] = 0.0 if r["targets"]["H0"]["weight"] > 0 else 0.9
    s["last"]["bar_ms"] -= 4 * H                                                  # pretend the previous bar differed
    st.write_text(json.dumps(s))
    r3 = T.run(st, CFG, f.now, dry=True, get=f)
    assert len(r3["sent"]) == 1 and "목표 비중 변경" in r3["sent"][0] and "H0" in r3["sent"][0]


def test_fallback_feeds_and_error_notice_once_per_day(tmp_path):
    f = Feeds()
    f.fail_paxg = True
    r = T.run(tmp_path / "a.json", CFG, f.now, dry=True, get=f)
    assert r["feed"].startswith("GC=F")
    f.fail_gc = True
    st = tmp_path / "b.json"
    assert "error" in T.run(st, CFG, f.now, dry=True, get=f)
    assert T.run(st, CFG, f.now, dry=True, get=f)["sent"] == []                    # error notice only once a day


def test_targets_match_engine_on_same_bars():
    from t5 import engine as E
    f = Feeds(drift=0.0005)
    h1, _ = C.load_feed(get=f, now_ms=f.now)
    cur = C.compute(h1, f.now, C.Account(10_000, 1.0))
    b = E.resample(h1[h1.index + H <= f.now].iloc[-C.H1_COUNT:], "4h")
    b = b[b.index + 4 * H <= f.now]
    assert cur["targets"]["H0"]["weight"] == pytest.approx(float(E.H0.weights(b, "4h").iloc[-1]), abs=1e-4)  # stored rounded


def test_small_account_message_says_below_min_lot():
    cur = {"bar_ms": T0, "price": 4000.0, "targets": {"H0": {"weight": 0.8, "lots": 0.0, "below_min_lot": True}}}
    msg = C.format_msg(cur, None, [], "GC=F", C.Account(700, 100.0), "x")
    assert "주문 불가" in msg and "1oz" in msg
