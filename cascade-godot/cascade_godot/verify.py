"""Engine verification, stage 1: does the project still load after the write?

  GodotVerifier  runs `godot --headless --script verify.gd` (real engine)
  MockVerifier   scripted results for tests; records what was on disk at verify time
  StagedVerifier chains stages (load check, then runtime fixtures) and stops at the first failing one

SECURITY: loading a project executes its autoloads and script _init() code. Run the engine
sandboxed (container, bwrap, firejail) via `command_prefix`. The verifier scrubs the
environment and gives the engine a throwaway HOME so it cannot touch the real one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from subnetbus import BusError, lexical_segments

from .engine import EngineRun, EngineRunner, find_godot, find_result, log_problems

__all__ = ["VerifyResult", "CheckResult", "GodotVerifier", "MockVerifier", "StagedVerifier", "find_godot", "VERIFY_SCRIPT"]

VERIFY_SCRIPT = Path(__file__).with_name("scripts") / "verify.gd"


@dataclass(frozen=True)
class CheckResult:
    fixture: str
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    errors: Tuple[str, ...] = ()
    checked: int = 0
    duration_s: float = 0.0
    engine_exit: Optional[int] = None
    log_tail: str = ""
    stage: str = ""                          # "load", "fixtures", or "load+fixtures" when every stage passed
    checks: Tuple[CheckResult, ...] = ()     # per-check detail from runtime fixtures


def _fail(*errs: str, **kw) -> VerifyResult:
    return VerifyResult(False, tuple(errs), **kw)


class GodotVerifier:
    stage_name = "load"

    def __init__(self, godot_bin: Optional[str] = None, *, timeout: float = 60.0,
                 command_prefix: Sequence[str] = (), ignore_patterns: Iterable[str] = (),
                 fail_on_log: bool = True, max_log_bytes: int = 1 << 20,
                 extra_args: Sequence[str] = (), script: Optional[Path] = None):
        self.runner = EngineRunner(godot_bin, timeout=timeout, command_prefix=command_prefix,
                                   max_log_bytes=max_log_bytes, extra_args=extra_args)
        self.godot = self.runner.godot
        self.timeout = timeout
        self.ignore = [re.compile(p) for p in ignore_patterns]
        self.fail_on_log = fail_on_log
        self.max_log = max_log_bytes
        self.script = Path(script or VERIFY_SCRIPT)

    def verify(self, root: Path, files: Sequence[str]) -> VerifyResult:
        files = list(dict.fromkeys(files))
        if not files:
            return _fail("nothing to verify", stage=self.stage_name)
        for f in files:
            try:
                lexical_segments(f)
            except BusError as e:
                return _fail(f"unsafe verify path {f!r}: {e.code}", stage=self.stage_name)
        run = self.runner.run(root, self.script, [f"res://{f}" for f in files])
        if run.start_error:
            return _fail(run.start_error, stage=self.stage_name)
        if run.timed_out:
            return _fail(f"engine timed out after {self.timeout}s", duration_s=run.duration_s, log_tail=run.tail, stage=self.stage_name)
        if run.truncated:                            # the discarded head could hide errors: fail closed
            return _fail(f"engine log exceeded {self.max_log} bytes", engine_exit=run.code,
                         duration_s=run.duration_s, log_tail=run.tail, stage=self.stage_name)
        return self._judge(run, len(files))

    def _judge(self, run: EngineRun, n_files: int) -> VerifyResult:
        code, dur, tail = run.code, run.duration_s, run.tail
        common = dict(engine_exit=code, duration_s=dur, log_tail=tail, stage=self.stage_name)
        try:
            result = find_result(run.text)
        except ValueError:
            return _fail("engine result line is not valid JSON", **common)
        if result is None:
            return _fail(f"engine produced no result line (exit {code})", **common)
        if (type(result) is not dict or type(result.get("ok")) is not bool or type(result.get("checked")) is not int
                or type(result.get("errors")) is not list or not all(type(e) is str for e in result["errors"])):
            return _fail("engine result has the wrong shape", **common)
        errors: List[str] = [e[:300] for e in result["errors"][:20]]
        if result["ok"] != (code == 0):
            errors.append(f"engine result says ok={result['ok']} but exit code is {code}")
        if result["checked"] != n_files:
            errors.append(f"engine checked {result['checked']} files, expected {n_files}")
        if result["ok"] and code == 0 and not errors and self.fail_on_log:
            errors.extend(log_problems(run.text, self.ignore))
        return VerifyResult(not errors and result["ok"], tuple(errors), result["checked"], dur, code, tail, self.stage_name)


class MockVerifier:
    """Test double. `decide(root, files)` may return bool, VerifyResult, or raise."""

    def __init__(self, decide: Optional[Callable] = None, *, default_ok: bool = True, stage_name: str = "mock"):
        self.stage_name = stage_name
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


class StagedVerifier:
    """Run verification stages in order; stop at the first failure.

    Stage 1 (GodotVerifier) proves the project loads. Stage 2 (FixtureVerifier) then RUNS it. Running is
    slower and executes game code, so it only happens once loading is known to work."""

    def __init__(self, *stages):
        if not stages:
            raise ValueError("StagedVerifier needs at least one stage")
        self.stages = stages

    def verify(self, root: Path, files: Sequence[str]) -> VerifyResult:
        errors: List[str] = []
        checks: List[CheckResult] = []
        checked, dur, last = 0, 0.0, None
        names: List[str] = []
        for st in self.stages:
            r = st.verify(root, files)
            name = getattr(st, "stage_name", type(st).__name__)
            names.append(name)
            checked += r.checked
            dur += r.duration_s
            checks.extend(r.checks)
            last = r
            if not r.ok:
                errors.extend(f"[{name}] {e}" for e in r.errors)
                return VerifyResult(False, tuple(errors), checked, dur, r.engine_exit, r.log_tail, name, tuple(checks))
        return VerifyResult(True, (), checked, dur, last.engine_exit, last.log_tail, "+".join(names), tuple(checks))


