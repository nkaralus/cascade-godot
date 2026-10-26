"""Shared plumbing for running the Godot binary: process control, scrubbed environment, log capture,
result-line parsing, log scanning, and project import. Used by GodotVerifier and FixtureVerifier.

SECURITY: anything that loads a project runs project code (autoloads, script _init). Fixtures run far more of it:
_ready and _physics_process. Use `command_prefix` to run the engine in a sandbox.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .errors import CascadeError

__all__ = ["EngineRun", "EngineRunner", "MARK", "find_godot", "find_result", "log_problems", "import_project"]

MARK = "CASCADE_RESULT "
_LOG_RE = re.compile(r"^(SCRIPT ERROR|ERROR|WARNING|USER ERROR|USER WARNING):")


def find_godot(explicit: Optional[str] = None) -> str:
    cand = explicit or os.environ.get("GODOT_BIN") or shutil.which("godot") or shutil.which("godot4")
    if not cand or not os.access(cand, os.X_OK):
        raise CascadeError("NO_ENGINE", "set GODOT_BIN or pass godot_bin")
    return cand


def find_result(text: str) -> Optional[dict]:
    """The last CASCADE_RESULT line, parsed. None if absent. ValueError if present but not JSON."""
    found = None
    for line in text.splitlines():
        if line.startswith(MARK):
            found = json.loads(line[len(MARK):])
    return found


def log_problems(text: str, ignore: Iterable["re.Pattern[str]"] = (), limit: int = 5) -> List[str]:
    bad = [ln for ln in text.splitlines() if _LOG_RE.match(ln) and not any(p.search(ln) for p in ignore)]
    return [f"engine log: {ln[:300]}" for ln in bad[:limit]]


@dataclass(frozen=True)
class EngineRun:
    text: str
    code: Optional[int]
    timed_out: bool = False
    truncated: bool = False
    duration_s: float = 0.0
    start_error: str = ""

    @property
    def tail(self) -> str:
        return self.text[-2000:]


class EngineRunner:
    def __init__(self, godot_bin: Optional[str] = None, *, timeout: float = 60.0, command_prefix: Sequence[str] = (),
                 max_log_bytes: int = 1 << 20, extra_args: Sequence[str] = ()):
        self.godot = find_godot(godot_bin)
        self.timeout = timeout
        self.prefix = list(command_prefix)
        self.max_log = max_log_bytes
        self.extra = list(extra_args)

    def run(self, root: Path, script: Path, script_args: Sequence[str] = (), *, fixed_fps: Optional[int] = None,
            timeout: Optional[float] = None) -> EngineRun:
        cmd = [*self.prefix, self.godot, "--headless", "--path", str(root), *self.extra]
        if fixed_fps:
            cmd += ["--fixed-fps", str(int(fixed_fps))]
        cmd += ["--script", str(script), "--", *script_args]
        return self._exec(cmd, self.timeout if timeout is None else timeout)

    def _exec(self, cmd: List[str], timeout: float) -> EngineRun:
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="cascade-home-") as home, tempfile.TemporaryFile() as log:
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "LANG": "C",
                   "XDG_DATA_HOME": f"{home}/data", "XDG_CONFIG_HOME": f"{home}/config",
                   "XDG_CACHE_HOME": f"{home}/cache"}
            try:
                proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        env=env, start_new_session=True)
            except OSError as e:
                return EngineRun("", None, start_error=f"cannot start engine: {type(e).__name__}")
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.wait()
            size = log.seek(0, os.SEEK_END)
            log.seek(max(0, size - self.max_log))
            text = log.read().decode("utf-8", errors="replace")
        return EngineRun(text, proc.returncode, timed_out, size > self.max_log, time.monotonic() - t0)


def import_project(root: Path, godot_bin: Optional[str] = None, *, timeout: float = 300.0,
                   command_prefix: Sequence[str] = ()) -> EngineRun:
    """`godot --headless --import`: build the import cache so PNGs and other assets can load.
    Writes `.godot/` and `*.import` files into the project (normal for Godot; ignore them in git as usual)."""
    runner = EngineRunner(godot_bin, timeout=timeout, command_prefix=command_prefix)
    cmd = [*runner.prefix, runner.godot, "--headless", "--path", str(root), "--import"]
    return runner._exec(cmd, timeout)
