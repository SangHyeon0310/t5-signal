"""Shared copies across ST-TRA, T5 and t5-signal must stay identical.

If this fails you changed a shared file: make the same change in the other repositories
(ST-TRA src/st_fasttrack/notify.py == T5 src/t5/notify.py; T5 src/t5/cloud_signal.py == t5-signal src/t5/cloud_signal.py),
then update the hash here and in their copies of this test.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = {
    "src/t5/cloud_signal.py": "a73d5118b4f2b11e50065e0740d232db702b2cd0abf7c05b2d2640bcf19ccaed",
}


def test_shared_files_match_pinned_hash():
    for rel, h in PINNED.items():
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == h, rel
