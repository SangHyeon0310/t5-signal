"""Append-only hash-chained JSONL journal (ported from ST-TRA paper.Journal)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

GENESIS = "sha256:" + "0" * 64


class Journal:
    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def digest(prev: str, body: dict) -> str:
        raw = prev + json.dumps(body, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()

    def append(self, seq_prev: tuple[int, str], body: dict) -> tuple[int, str]:
        n, prev = seq_prev
        body = {"seq": n + 1, **body}
        h = self.digest(prev, body)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"prev": prev, "hash": h, "body": body}, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())                              # durable before any order is sent
        return n + 1, h

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines()]

    def verify(self) -> int:
        prev, n = GENESIS, 0
        for rec in self.records():
            n += 1
            if rec["prev"] != prev or rec["body"]["seq"] != n or self.digest(prev, rec["body"]) != rec["hash"]:
                raise ValueError(f"journal chain broken at line {n}")
            prev = rec["hash"]
        return n


def write_atomic(path: Path, text: str) -> None:
    """tmp + fsync + replace, so a power loss leaves either the old or the new file."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)
