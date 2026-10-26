"""Engine verification: does the project still load after the write?

  GodotVerifier  runs `godot --headless --script verify.gd` (real engine)
  MockVerifier   scripted results for tests; records what was on disk at verify time

SECURITY: loading a project executes its autoloads and script _init() code. Run the engine
sandboxed (container, bwrap, firejail) via `command_prefix`. The verifier scrubs the
environment and gives the engine a throwaway HOME so it cannot touch the real one.
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
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from subnetbus import BusError, lexical_segments

from .errors import CascadeError

__all__ = ["VerifyResult", "GodotVerifier", "MockVerifier", "find_godot", "VERIFY_SCRIPT"]

VERIFY_SCRIPT = Path(__file__).with_name("scripts") / "verify.gd"
MARK = "CASCADE_RESULT "
_LOG_RE = re.compile(r"^(SCRIPT ERROR|ERROR|WARNING|USER ERROR|USER WARNING):")


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    errors: Tuple[str, ...] = ()
    checked: int = 0
    duration_s: float = 0.0
    engine_exit: Optional[int] = None
    log_tail: str = ""


def find_godot(explicit: Optional[str] = None) -> str:
    cand = explicit or os.environ.get("GODOT_BIN") or shutil.which("godot") or shutil.which("godot4")
    if not cand or not os.access(cand, os.X_OK):
        raise CascadeError("NO_ENGINE", "set GODOT_BIN or pass godot_bin")
    return cand


def _fail(*errs: str, **kw) -> VerifyResult:
    return VerifyResult(False, tuple(errs), **kw)


class GodotVerifier:
    def __init__(self, godot_bin: Optional[str] = None, *, timeout: float = 60.0,
                 command_prefix: Sequence[str] = (), ignore_patterns: Iterable[str] = (),
                 fail_on_log: bool = True, max_log_bytes: int = 1 << 20,
                 extra_args: Sequence[str] = (), script: Optional[Path] = None):
        self.godot = find_godot(godot_bin)
        self.timeout = timeout
        self.prefix = list(command_prefix)
        self.ignore = [re.compile(p) for p in ignore_patterns]
        self.fail_on_log = fail_on_log
        self.max_log = max_log_bytes
        self.extra = list(extra_args)
        self.script = Path(script or VERIFY_SCRIPT)

    def verify(self, root: Path, files: Sequence[str]) -> VerifyResult:
        t0 = time.monotonic()
        files = list(dict.fromkeys(files))
        if not files:
            return _fail("nothing to verify")
        for f in files:
            try:
                lexical_segments(f)
            except BusError as e:
                return _fail(f"unsafe verify path {f!r}: {e.code}")
        cmd = [*self.prefix, self.godot, "--headless", "--path", str(root), *self.extra,
               "--script", str(self.script), "--", *[f"res://{f}" for f in files]]
        with tempfile.TemporaryDirectory(prefix="cascade-home-") as home, tempfile.TemporaryFile() as log:
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "LANG": "C",
                   "XDG_DATA_HOME": f"{home}/data", "XDG_CONFIG_HOME": f"{home}/config",
                   "XDG_CACHE_HOME": f"{home}/cache"}
            try:
                proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        env=env, start_new_session=True)
            except OSError as e:
                return _fail(f"cannot start engine: {type(e).__name__}")
            timed_out = False
            try:
                proc.wait(timeout=self.timeout)
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
        dur = time.monotonic() - t0
        tail = text[-2000:]
        if timed_out:
            return _fail(f"engine timed out after {self.timeout}s", duration_s=dur, log_tail=tail)
        if size > self.max_log:                      # the discarded head could hide errors: fail closed
            return _fail(f"engine log exceeded {self.max_log} bytes", engine_exit=proc.returncode,
                         duration_s=dur, log_tail=tail)
        return self._judge(text, proc.returncode, len(files), dur, tail)

    def _judge(self, text: str, code: int, n_files: int, dur: float, tail: str) -> VerifyResult:
        result = None
        for line in text.splitlines():
            if line.startswith(MARK):
                try:
                    result = json.loads(line[len(MARK):])
                except ValueError:
                    return _fail("engine result line is not valid JSON", engine_exit=code, duration_s=dur, log_tail=tail)
        if result is None:
            return _fail(f"engine produced no result line (exit {code})", engine_exit=code, duration_s=dur, log_tail=tail)
        if (type(result) is not dict or type(result.get("ok")) is not bool or type(result.get("checked")) is not int
                or type(result.get("errors")) is not list or not all(type(e) is str for e in result["errors"])):
            return _fail("engine result has the wrong shape", engine_exit=code, duration_s=dur, log_tail=tail)
        errors: List[str] = [e[:300] for e in result["errors"][:20]]
        if result["ok"] != (code == 0):
            errors.append(f"engine result says ok={result['ok']} but exit code is {code}")
        if result["checked"] != n_files:
            errors.append(f"engine checked {result['checked']} files, expected {n_files}")
        if result["ok"] and code == 0 and not errors and self.fail_on_log:
            bad = [ln for ln in text.splitlines()
                   if _LOG_RE.match(ln) and not any(p.search(ln) for p in self.ignore)]
            errors.extend(f"engine log: {ln[:300]}" for ln in bad[:5])
        return VerifyResult(not errors and result["ok"], tuple(errors), result["checked"], dur, code, tail)


class MockVerifier:
    """Test double. `decide(root, files)` may return bool, VerifyResult, or raise."""

    def __init__(self, decide: Optional[Callable] = None, *, default_ok: bool = True):
        self.decide = decide
        self.default_ok = default_ok
        self.calls: List[Tuple[str, ...]] = []
        self.seen: List[Dict[str, Optional[bytes]]] = []     # file contents as the engine would see them

    def verify(self, root: Path, files: Sequence[str]) -> VerifyResult:
        files = tuple(files)
        self.calls.append(files)
        self.seen.append({f: (Path(root) / f).read_bytes() if (Path(root) / f).is_file() else None for f in files})
        out = self.decide(root, files) if self.decide else self.default_ok
        if isinstance(out, VerifyResult):
            return out
        return VerifyResult(bool(out), () if out else ("mock verifier said no",), len(files))
