"""Append-only, fsynced write-ahead journal (one JSON object per line).

Records of one transaction, in order:
  BEGIN        full plan: every file, its hashes, backup name, temp name, created dirs
  STAGED       every temp file is written and fsynced
  APPLYING     renames are about to start
  VERIFYING    renames done, engine check running
  COMMITTED | ROLLED_BACK | NEEDS_ATTENTION      (terminal)

Recovery never trusts the middle records. It decides per file from content hashes,
so rollback is idempotent and safe to re-run after any crash.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from .errors import TxError

TERMINAL = frozenset({"COMMITTED", "ROLLED_BACK", "NEEDS_ATTENTION"})


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class Journal:
    def __init__(self, tx_dir: Path):
        self.dir = Path(tx_dir)
        self.path = self.dir / "journal.jsonl"

    def append(self, rec: Dict[str, Any]) -> None:
        line = json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n"
        new = not self.path.exists()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        if new:
            fsync_dir(self.dir)

    def read(self) -> List[Dict[str, Any]]:
        """All complete records. A torn final line (crash mid-write) is ignored; corruption elsewhere raises."""
        try:
            data = self.path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            return []
        except UnicodeDecodeError:
            raise TxError("JOURNAL_CORRUPT", str(self.path)) from None
        parts = data.split("\n")
        out: List[Dict[str, Any]] = []
        for i, line in enumerate(parts):
            if not line:
                continue
            try:
                rec = json.loads(line)
                if type(rec) is not dict or "t" not in rec:
                    raise ValueError
            except ValueError:
                if i == len(parts) - 1:        # no trailing newline: torn write
                    break
                raise TxError("JOURNAL_CORRUPT", f"{self.path}: line {i + 1}") from None
            out.append(rec)
        return out

    def state(self) -> str:
        recs = self.read()
        if not recs:
            return "EMPTY"
        last = recs[-1]["t"]
        return last if last in TERMINAL else "OPEN"
