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
       "heartbeat_utc_hour": -1, "crypto_enabled": False}


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


def test_start_message_when_a_channel_is_added_later(tmp_path, monkeypatch):
    f = Feeds()
    st = tmp_path / "s.json"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    T.run(st, CFG, f.now, dry=True, get=f)                       # first runs before any secret existed
    monkeypatch.setenv("NTFY_TOPIC", "t5-test-topic")
    r = T.run(st, CFG, f.now, dry=True, get=f)
    assert len(r["sent"]) == 1 and "시작" in r["sent"][0]
    assert T.run(st, CFG, f.now, dry=True, get=f)["sent"] == []


def test_crypto_sleeve_alerts_and_perf(tmp_path):
    f = Feeds(drift=0.0006)
    cfg = {**CFG, "crypto_enabled": True, "crypto_budget_usdt": 1000.0}
    st = tmp_path / "c.json"
    r = T.run(st, cfg, f.now, dry=True, get=f)
    assert set(r["crypto"]) == {"BTC", "ETH"} and any("코인 신호 시작" in m for m in r["sent"])
    s = json.loads(st.read_text())
    s["last_crypto"]["BTC"]["weight"] = 0.0 if r["crypto"]["BTC"]["weight"] > 0 else 0.9
    s["last_crypto"]["BTC"]["bar_ms"] -= 4 * H
    s["last_crypto"]["ETH"]["bar_ms"] -= 4 * H
    st.write_text(json.dumps(s))
    r2 = T.run(st, cfg, f.now, dry=True, get=f)
    assert any("목표 비중 변경" in m and "BTC" in m for m in r2["sent"])


def test_st_tra_weight_matches_st_tra_package():
    import importlib.util
    import pandas as pd
    root = Path("/home/user/ST-TRA/src")
    if not root.exists():
        pytest.skip("ST-TRA checkout not available")
    sys.path.insert(0, str(root))
    from st_fasttrack import strategies as S
    rng = np.random.default_rng(9)
    c = 30000 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, 1500)))
    b = pd.DataFrame({"open": c, "high": c, "low": c, "close": c}, index=np.arange(1500) * 4 * H)
    want = S.vol_target(b, S.s_ema_cross(b, 20, 200), "4h", 0.40, 180).iloc[-1]
    assert C.st_tra_weight(b) == pytest.approx(float(want))


def test_perf_math():
    hist = [{"bar_ms": 0, "price": 100.0, "H0": 1.0}, {"bar_ms": 1, "price": 110.0, "H0": 0.5}, {"bar_ms": 2, "price": 99.0, "H0": 0.0}]
    p = C.perf(hist, ["H0"])["H0"]
    assert p["hold"] == pytest.approx(-0.01) and p["strategy"] == pytest.approx(1.1 * 0.95 - 1)
