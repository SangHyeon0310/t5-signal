"""Hourly free cloud signal for the phone (GitHub Actions). No orders, no broker login.

Usage: python tools/t5_cloud_signal.py --state <state.json> [--config config/signal_cloud.json] [--dry]
Secrets (environment, set as GitHub repository secrets): TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and/or NTFY_TOPIC.
Telegram commands (answered at the next hourly run): /status
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from t5 import cloud_signal as C  # noqa: E402


def notify(text: str, dry: bool, sent: list) -> None:
    sent.append(text)
    if dry:
        return
    tok, chat, topic = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"), os.environ.get("NTFY_TOPIC")
    if tok and chat:
        C.send_telegram(tok, chat, text)
    if topic:
        C.send_ntfy(topic, text)


def run(state_path: Path, cfg: dict, now_ms: int, dry: bool = False, get=C._get) -> dict:
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    acc = C.Account(cfg["equity_usd"], cfg["contract_size"], cfg["volume_min"], cfg["volume_step"])
    sent: list[str] = []
    cmds = []
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat and not dry:
        try:
            cmds, state["tg_offset"] = C.telegram_commands(tok, chat, int(state.get("tg_offset", 0)), get)
        except Exception as e:                                   # noqa: BLE001
            print("telegram getUpdates failed:", e)
    day = now_ms // 86_400_000
    try:
        h1, feed = C.load_feed(get=get, now_ms=now_ms)
        cur = C.compute(h1, now_ms, acc)
    except Exception as e:                                       # noqa: BLE001
        if state.get("error_day") != day:
            notify(f"[T5 금] 시세를 받지 못했습니다: {e}\n다음 실행(1시간 뒤)에 다시 시도합니다.", dry, sent)
            state["error_day"] = day
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return {"error": str(e), "sent": sent}
    prev = state.get("last")
    new_bar = not prev or cur["bar_ms"] > prev["bar_ms"]
    channels = bool((tok and chat) or os.environ.get("NTFY_TOPIC"))
    if not prev or (channels and not state.get("channels_ok")):      # first message once a channel is configured
        notify(C.format_msg(cur, None, [], feed, acc, "클라우드 신호 시작"), dry, sent)
        state["channels_ok"] = channels
    elif new_bar:
        keys = C.changed(prev, cur, cfg.get("min_change", 0.10))
        if keys:
            notify(C.format_msg(cur, prev, keys, feed, acc, "목표 비중 변경"), dry, sent)
    hb = cfg.get("heartbeat_utc_hour", 22)
    if hb is not None and hb >= 0 and time.gmtime(now_ms / 1000).tm_hour == hb and state.get("heartbeat_day") != day:
        notify(C.format_msg(cur, None, [], feed, acc, "매일 점검: 정상 작동"), dry, sent)
        state["heartbeat_day"] = day
    if any(c in ("/status", "/start") for c in cmds):
        notify(C.format_msg(cur, None, [], feed, acc, "현재 상태"), dry, sent)
    if new_bar or not prev:
        hist = state.get("history", [])
        hist.append({"bar_ms": cur["bar_ms"], "price": cur["price"],
                     **{k: v["weight"] for k, v in cur["targets"].items()}, "feed": feed})
        state["history"] = hist[-500:]
        state["last"] = cur
    state["last_run_ms"] = now_ms
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return {"bar_ms": cur["bar_ms"], "feed": feed, "targets": cur["targets"], "sent": sent}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--config", type=Path, default=ROOT / "config" / "signal_cloud.json")
    ap.add_argument("--dry", action="store_true", help="compute and print, send nothing")
    a = ap.parse_args(argv)
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and not chat and not a.dry:                           # setup helper: tell the user their chat ID
        try:
            d = json.loads(C._get(f"https://api.telegram.org/bot{tok}/getUpdates?timeout=0"))
            ids = sorted({str(u["message"]["chat"]["id"]) for u in d.get("result", []) if "message" in u})
            for cid in ids:
                C.send_telegram(tok, cid, f"[T5] 이 채팅의 ID: {cid}\nGitHub Secret TELEGRAM_CHAT_ID에 이 숫자를 등록하세요.")
            print("telegram chat ids:", ids or "none yet - send /start to your bot first")
        except Exception as e:                                   # noqa: BLE001
            print("telegram setup helper failed:", e)
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    res = run(a.state, cfg, int(time.time() * 1000), a.dry)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0                                                      # failures are reported by message, not by a red run


if __name__ == "__main__":
    raise SystemExit(main())
