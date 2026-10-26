"""Shared types for handlers and the pipeline (kept separate to avoid import cycles)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

from subnetbus import AABB, BusError, PathGuard, Registry

from .errors import PlanError
from .transaction import FileChange, sha256_bytes

MAX_READ_BYTES = 1 << 20


@dataclass(frozen=True)
class Plan:
    changes: Tuple[FileChange, ...]
    refs: Tuple[str, ...] = ()
    proposed_bounds: Optional[AABB] = None
    also_verify: Tuple[str, ...] = ()
    on_commit: Optional[Callable[[Registry], None]] = None


@dataclass(frozen=True)
class Context:
    root: Path
    registry: Registry
    guard: PathGuard

    def read_text(self, rel: str) -> Tuple[str, str]:
        """(text, sha256) of a project file, read through the path guard."""
        try:
            real = self.guard.check(rel, "read")
        except BusError as e:
            raise PlanError(e.code, e.detail) from None
        if not real.is_file() or real.stat().st_size > MAX_READ_BYTES:
            raise PlanError("UNREADABLE", rel)
        data = real.read_bytes()
        try:
            return data.decode("utf-8"), sha256_bytes(data)
        except UnicodeDecodeError:
            raise PlanError("UNREADABLE", f"{rel}: not UTF-8") from None
