"""Stage 2 verification: run the project under the real physics loop and judge its behaviour.

  MoveFixture     live_move.gd     it moves, the right way, never too fast, and comes to rest
  CollideFixture  live_collide.gd  bodies stop at obstacles: no tunnelling, penetration <= limit, statics unmoved
  CameraFixture   live_camera.gd   camera is live, keeps the target on screen, lag is bounded, view stays in the world
  SpriteFixture   live_sprite.gd   textures load and are sane, frames/regions valid, nodes actually renderable

Fixtures are configuration written by the HARNESS (like handlers), never by the model. Every value is validated
here before it reaches the engine. Each fixture runs in its own engine process: isolated state, its own timeout.

`--fixed-fps` makes the engine run frames as fast as it can with a fixed delta, so hundreds of physics ticks take
milliseconds and results are deterministic. A fixture that never returns (game code with `while true`) is killed by
the wall-clock timeout, the whole process group, and the transaction rolls back.

SECURITY: fixtures execute game code (_ready, _physics_process). Use `command_prefix` to sandbox the engine.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, fields
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, ClassVar, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from subnetbus import BusError, lexical_segments

from .engine import EngineRunner, find_result, log_problems
from .errors import CascadeError
from .verify import CheckResult, GodotVerifier, StagedVerifier, VerifyResult

__all__ = ["Fixture", "MoveCase", "MoveFixture", "CollideFixture", "CameraFixture", "SpriteFixture",
           "FixtureVerifier", "fixture_from_dict", "load_fixtures", "staged_verifier", "MAX_TICKS_PER_FIXTURE"]

SCRIPT_DIR = Path(__file__).with_name("scripts")
MAX_TICKS_PER_FIXTURE = 20000
_ACTION = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
_SEG = re.compile(r"[A-Za-z0-9_@.-]{1,64}")
_LABEL = re.compile(r"[A-Za-z0-9_-]{1,32}")
_CASE = re.compile(r"[A-Za-z0-9_]{1,32}")


class FixtureError(CascadeError):
    pass


# ---- validation ----------------------------------------------------------------------------------------
def _bad(what: str, why: str):
    raise FixtureError("BAD_FIXTURE", f"{what}: {why}")


def _num(what: str, v: Any, lo: float, hi: float) -> float:
    if type(v) not in (int, float) or not math.isfinite(v) or not lo <= v <= hi:
        _bad(what, f"must be a finite number in [{lo}, {hi}]")
    return float(v)


def _int(what: str, v: Any, lo: int, hi: int) -> int:
    if type(v) is not int or not lo <= v <= hi:
        _bad(what, f"must be an int in [{lo}, {hi}]")
    return v


def _bool(what: str, v: Any) -> bool:
    if type(v) is not bool:
        _bad(what, "must be true or false")
    return v


def _scene(v: Any) -> str:
    try:
        lexical_segments(v)
    except BusError as e:
        _bad("scene", e.code)
    if not v.endswith(".tscn"):
        _bad("scene", "must end with .tscn")
    return v


def _ref(what: str, v: Any) -> str:
    if type(v) is not str or not v:
        _bad(what, "must be a non-empty string")
    body = v[1:] if v.startswith("%") else v
    parts = body.split("/")
    if len(parts) > 8 or any(not _SEG.fullmatch(p) or p in (".", "..") for p in parts):
        _bad(what, f"not a safe node name or path: {v[:40]!r}")
    if v.startswith("%") and len(parts) != 1:
        _bad(what, "a %Name cannot contain a path")
    return v


def _actions(what: str, v: Any, lo: int = 1, hi: int = 4) -> Tuple[str, ...]:
    if not isinstance(v, (list, tuple)) or not lo <= len(v) <= hi or len(set(v)) != len(v):
        _bad(what, f"must be {lo}-{hi} distinct action names")
    for a in v:
        if type(a) is not str or not _ACTION.fullmatch(a):
            _bad(what, f"bad action name {a!r}")
    return tuple(v)


def _vec(what: str, v: Any, lo: float, hi: float, *, unit: bool = False, n: int = 2) -> Tuple[float, ...]:
    if not isinstance(v, (list, tuple)) or len(v) != n:
        _bad(what, f"must be {n} numbers")
    out = tuple(_num(f"{what}[{i}]", x, lo, hi) for i, x in enumerate(v))
    if unit:
        length = math.hypot(*out)
        if length < 1e-9:
            _bad(what, "must not be the zero vector")
        out = tuple(x / length for x in out)
    return out


# ---- fixtures ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, kw_only=True)
class Fixture:
    scene: str
    label: str = ""
    triggers: Tuple[str, ...] = ("*",)           # run when any changed file matches one of these (fnmatch)
    viewport: Optional[Tuple[int, int]] = None   # default: the project's display/window/size
    timeout: Optional[float] = None              # wall-clock seconds; default from FixtureVerifier
    kind: ClassVar[str] = ""

    def __post_init__(self):
        object.__setattr__(self, "scene", _scene(self.scene))
        if self.label and not _LABEL.fullmatch(self.label):
            _bad("label", "use letters, digits, _ and -")
        trig = tuple(self.triggers)
        if not 1 <= len(trig) <= 16 or any(type(t) is not str or not t.isascii() or not 1 <= len(t) <= 128 for t in trig):
            _bad("triggers", "1-16 ASCII patterns")
        object.__setattr__(self, "triggers", trig)
        if self.viewport is not None:
            object.__setattr__(self, "viewport", tuple(int(x) for x in _vec("viewport", self.viewport, 64, 8192)))
        if self.timeout is not None:
            object.__setattr__(self, "timeout", _num("timeout", self.timeout, 0.5, 600))
        self._validate()
        cost = self.tick_cost()
        if cost > MAX_TICKS_PER_FIXTURE:
            _bad("ticks", f"fixture would run {cost} ticks, limit {MAX_TICKS_PER_FIXTURE}")

    def _validate(self) -> None:
        pass

    def tick_cost(self) -> int:
        return 0

    @property
    def name(self) -> str:
        return f"{self.kind}:{self.label}" if self.label else self.kind

    @property
    def script(self) -> Path:
        return SCRIPT_DIR / f"{self.kind}.gd"

    def to_spec(self) -> Dict[str, Any]:
        spec: Dict[str, Any] = {}
        for f in fields(self):
            if f.name in ("label", "triggers", "timeout"):
                continue
            v = getattr(self, f.name)
            if v is not None:
                spec[f.name] = _jsonable(v)
        return spec


def _jsonable(v: Any) -> Any:
    if isinstance(v, (tuple, list)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "__dataclass_fields__"):
        return {f.name: _jsonable(getattr(v, f.name)) for f in fields(v) if getattr(v, f.name) is not None}
    return v


@dataclass(frozen=True)
class MoveCase:
    name: str
    actions: Tuple[str, ...]
    dir: Optional[Tuple[float, float]] = None     # expected direction of travel (normalised for you)

    def __post_init__(self):
        if type(self.name) is not str or not _CASE.fullmatch(self.name):
            _bad("case name", "letters, digits, _ (max 32)")
        object.__setattr__(self, "actions", _actions("case actions", self.actions))
        if self.dir is not None:
            object.__setattr__(self, "dir", _vec("case dir", self.dir, -1e6, 1e6, unit=True))


@dataclass(frozen=True, kw_only=True)
class MoveFixture(Fixture):
    node: str
    cases: Tuple[MoveCase, ...]
    ticks: int = 60
    warmup: int = 3
    min_distance: float = 1.0
    max_speed: Optional[float] = None            # px/s. None: no speed-bound check
    speed_tolerance: float = 0.05
    angle_tolerance_deg: float = 15.0
    normalize: bool = False                      # diagonal (2 actions) speed must match cardinal (1 action) speed
    normalize_tolerance: float = 0.05
    settle_ticks: int = 60
    settle_hold: int = 10
    settle_epsilon: float = 0.05
    kind: ClassVar[str] = "live_move"

    def _validate(self) -> None:
        object.__setattr__(self, "node", _ref("node", self.node))
        cases = tuple(self.cases)
        if not 1 <= len(cases) <= 16 or not all(isinstance(c, MoveCase) for c in cases):
            _bad("cases", "1-16 MoveCase")
        if len({c.name for c in cases}) != len(cases):
            _bad("cases", "case names must be unique")
        object.__setattr__(self, "cases", cases)
        _int("ticks", self.ticks, 1, 3000); _int("warmup", self.warmup, 0, 600)
        _int("settle_ticks", self.settle_ticks, 1, 3000); _int("settle_hold", self.settle_hold, 1, self.settle_ticks)
        _num("min_distance", self.min_distance, 0, 1e6)
        if self.max_speed is not None:
            _num("max_speed", self.max_speed, 0.001, 1e6)
        _num("speed_tolerance", self.speed_tolerance, 0, 10); _num("angle_tolerance_deg", self.angle_tolerance_deg, 0, 180)
        _bool("normalize", self.normalize); _num("normalize_tolerance", self.normalize_tolerance, 0, 10)
        _num("settle_epsilon", self.settle_epsilon, 0, 100)

    def tick_cost(self) -> int:
        return len(self.cases) * (self.warmup + self.ticks + self.settle_ticks + 2)

    @classmethod
    def eight_way(cls, scene: str, node: str, *, left: str = "ui_left", right: str = "ui_right", up: str = "ui_up",
                  down: str = "ui_down", **kw) -> "MoveFixture":
        """Four cardinal and four diagonal cases, with speed normalisation checked."""
        d = {"right": (right, (1, 0)), "left": (left, (-1, 0)), "up": (up, (0, -1)), "down": (down, (0, 1))}
        cases = [MoveCase(n, (a,), v) for n, (a, v) in d.items()]
        for h in ("right", "left"):
            for v in ("up", "down"):
                cases.append(MoveCase(f"{h}_{v}", (d[h][0], d[v][0]), (d[h][1][0], d[v][1][1])))
        kw.setdefault("normalize", True)
        return cls(scene=scene, node=node, cases=tuple(cases), **kw)


@dataclass(frozen=True, kw_only=True)
class CollideFixture(Fixture):
    mover: str
    statics: Tuple[str, ...]
    actions: Tuple[str, ...]
    toward: Tuple[float, float] = (1.0, 0.0)
    ticks: int = 120
    warmup: int = 3
    max_penetration: float = 0.081               # px. Godot's default safe_margin is 0.08
    min_travel: float = 5.0
    stop_window: int = 20
    stop_epsilon: float = 0.5
    static_tolerance: float = 0.001
    slide_actions: Optional[Tuple[str, ...]] = None   # EXTRA actions held once the mover is pressed against the obstacle
    slide_ticks: int = 30                              # keep short enough that the slide stays within the obstacle's length
    min_tangent: float = 5.0
    kind: ClassVar[str] = "live_collide"

    def _validate(self) -> None:
        object.__setattr__(self, "mover", _ref("mover", self.mover))
        statics = tuple(self.statics)
        if not 1 <= len(statics) <= 8:
            _bad("statics", "1-8 node references")
        object.__setattr__(self, "statics", tuple(_ref("statics", s) for s in statics))
        object.__setattr__(self, "actions", _actions("actions", self.actions))
        object.__setattr__(self, "toward", _vec("toward", self.toward, -1e6, 1e6, unit=True))
        if self.slide_actions is not None:
            object.__setattr__(self, "slide_actions", _actions("slide_actions", self.slide_actions))
        _int("ticks", self.ticks, 1, 3000); _int("warmup", self.warmup, 0, 600)
        _int("stop_window", self.stop_window, 1, self.ticks); _int("slide_ticks", self.slide_ticks, 1, 3000)
        _num("max_penetration", self.max_penetration, 0, 100); _num("min_travel", self.min_travel, 0, 1e6)
        _num("stop_epsilon", self.stop_epsilon, 0, 1e6); _num("static_tolerance", self.static_tolerance, 0, 1e6)
        _num("min_tangent", self.min_tangent, 0, 1e6)

    def tick_cost(self) -> int:
        return self.warmup + self.ticks + 2 + (self.slide_ticks if self.slide_actions else 0)


@dataclass(frozen=True, kw_only=True)
class CameraFixture(Fixture):
    target: str
    actions: Tuple[str, ...]
    camera: Optional[str] = None                 # default: the scene's single Camera2D
    ticks: int = 240
    warmup: int = 30
    margin: float = 0.0                          # target must stay at least this many px inside every screen edge
    max_lag: Optional[float] = None              # default: 25% of the smaller viewport side
    settle_ticks: int = 120
    final_lag: float = 1.0
    world: Optional[Tuple[float, float, float, float]] = None     # x, y, w, h: the view must stay inside
    kind: ClassVar[str] = "live_camera"

    def _validate(self) -> None:
        object.__setattr__(self, "target", _ref("target", self.target))
        if self.camera is not None:
            object.__setattr__(self, "camera", _ref("camera", self.camera))
        object.__setattr__(self, "actions", _actions("actions", self.actions, 0, 4))
        _int("ticks", self.ticks, 1, 3000); _int("warmup", self.warmup, 0, 600); _int("settle_ticks", self.settle_ticks, 1, 3000)
        _num("margin", self.margin, 0, 1e5); _num("final_lag", self.final_lag, 0, 1e5)
        if self.max_lag is not None:
            _num("max_lag", self.max_lag, 0, 1e6)
        if self.world is not None:
            w = _vec("world", self.world, -1e7, 1e7, n=4)
            if w[2] <= 0 or w[3] <= 0:
                _bad("world", "width and height must be > 0")
            if self.viewport and (w[2] < self.viewport[0] or w[3] < self.viewport[1]):
                _bad("world", "smaller than the viewport: no camera placement could satisfy it")
            object.__setattr__(self, "world", w)

    def tick_cost(self) -> int:
        return self.warmup + self.ticks + self.settle_ticks + 2


@dataclass(frozen=True, kw_only=True)
class SpriteFixture(Fixture):
    nodes: Tuple[str, ...] = ()                  # default: every Sprite2D and AnimatedSprite2D in the scene
    min_size: Tuple[float, float] = (1.0, 1.0)
    max_size: Tuple[float, float] = (4096.0, 4096.0)
    expect_size: Optional[Tuple[float, float]] = None
    require_visible: bool = True
    min_nodes: int = 1
    warmup: int = 3
    kind: ClassVar[str] = "live_sprite"

    def _validate(self) -> None:
        nodes = tuple(self.nodes)
        if len(nodes) > 64:
            _bad("nodes", "at most 64")
        object.__setattr__(self, "nodes", tuple(_ref("nodes", n) for n in nodes))
        object.__setattr__(self, "min_size", _vec("min_size", self.min_size, 0, 1e6))
        object.__setattr__(self, "max_size", _vec("max_size", self.max_size, 1, 1e6))
        if self.expect_size is not None:
            object.__setattr__(self, "expect_size", _vec("expect_size", self.expect_size, 1, 1e6))
        _bool("require_visible", self.require_visible); _int("min_nodes", self.min_nodes, 0, 64); _int("warmup", self.warmup, 0, 600)

    def tick_cost(self) -> int:
        return self.warmup + 2


_KINDS = {"move": MoveFixture, "collide": CollideFixture, "camera": CameraFixture, "sprite": SpriteFixture}


def fixture_from_dict(d: Mapping[str, Any]) -> Fixture:
    """Build a fixture from JSON-style config: {"type": "move", ...}. Unknown keys are errors."""
    if not isinstance(d, Mapping) or d.get("type") not in _KINDS:
        _bad("fixture", f"needs a 'type' of {sorted(_KINDS)}")
    cls = _KINDS[d["type"]]
    kw = {k: v for k, v in d.items() if k != "type"}
    if cls is MoveFixture and "eight_way" in kw:
        extra = kw.pop("eight_way")
        if not isinstance(extra, Mapping):
            _bad("eight_way", "must be an object of action names (may be empty)")
        return MoveFixture.eight_way(kw.pop("scene", ""), kw.pop("node", ""), **extra, **kw)
    if cls is MoveFixture and "cases" in kw:
        kw["cases"] = tuple(MoveCase(**c) if isinstance(c, Mapping) else c for c in kw["cases"])
    allowed = {f.name for f in fields(cls)}
    unknown = set(kw) - allowed
    if unknown:
        _bad("fixture", f"unknown keys {sorted(unknown)}")
    try:
        return cls(**kw)
    except TypeError as e:
        _bad("fixture", str(e)[:120])


def load_fixtures(path: "str | Path") -> List[Fixture]:
    """Read {"fixtures": [...]} from a JSON file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("fixtures"), list):
        _bad("config", 'expected {"fixtures": [...]}')
    return [fixture_from_dict(x) for x in data["fixtures"]]


# ---- the runner ---------------------------------------------------------------------------------------------
class FixtureVerifier:
    stage_name = "fixtures"

    def __init__(self, fixtures: Sequence[Fixture], godot_bin: Optional[str] = None, *, fps: int = 60,
                 timeout: float = 30.0, command_prefix: Sequence[str] = (), ignore_patterns: Iterable[str] = (),
                 fail_on_log: bool = True, max_log_bytes: int = 1 << 20, extra_args: Sequence[str] = (),
                 run_all: bool = False, runner: Optional[EngineRunner] = None):
        if not all(isinstance(f, Fixture) for f in fixtures):
            raise FixtureError("BAD_FIXTURE", "fixtures must be Fixture instances")
        self.fixtures = list(fixtures)
        self.fps = _int("fps", fps, 10, 1000)
        self.timeout = _num("timeout", timeout, 0.5, 600)
        self.runner = runner or EngineRunner(godot_bin, timeout=timeout, command_prefix=command_prefix,
                                             max_log_bytes=max_log_bytes, extra_args=extra_args)
        self.ignore = [re.compile(p) for p in ignore_patterns]
        self.fail_on_log = fail_on_log
        self.run_all = run_all

    def triggered(self, files: Sequence[str]) -> List[Fixture]:
        return [f for f in self.fixtures if any(fnmatchcase(p, pat) for p in files for pat in f.triggers)]

    def verify(self, root: Path, files: Sequence[str]) -> VerifyResult:
        return self._run(root, self.triggered(files))

    def verify_all(self, root: Path) -> VerifyResult:
        """Every fixture, ignoring triggers (CLI and manual checks)."""
        return self._run(root, self.fixtures)

    def _run(self, root: Path, selected: Sequence[Fixture]) -> VerifyResult:
        errors: List[str] = []
        checks: List[CheckResult] = []
        dur = 0.0
        ran = 0
        code: Optional[int] = None
        tail = ""
        for fx in selected:
            ran += 1
            res = self._run_one(root, fx)
            dur += res.duration_s
            code, tail = res.engine_exit, res.log_tail
            checks.extend(res.checks)
            if not res.ok:
                errors.extend(res.errors)
                if not self.run_all:
                    break
        return VerifyResult(not errors, tuple(errors), ran, dur, code, tail, self.stage_name, tuple(checks))

    def _run_one(self, root: Path, fx: Fixture) -> VerifyResult:
        spec = json.dumps(fx.to_spec(), separators=(",", ":"), ensure_ascii=True)
        timeout = fx.timeout or self.timeout
        run = self.runner.run(root, fx.script, [spec], fixed_fps=self.fps, timeout=timeout)
        mk = lambda *e, **kw: VerifyResult(False, tuple(f"{fx.name}: {x}" for x in e), 1, run.duration_s, run.code, run.tail, self.stage_name, **kw)
        if run.start_error:
            return mk(run.start_error)
        if run.timed_out:
            return mk(f"timed out after {timeout}s (game code may be stuck in a loop)")
        if run.truncated:
            return mk(f"engine log exceeded {self.runner.max_log} bytes")
        try:
            result = find_result(run.text)
        except ValueError:
            return mk("result line is not valid JSON")
        if result is None:
            return mk(f"produced no result line (exit {run.code}): the fixture or the game code crashed")
        shape_err = _shape_error(result, fx.kind)
        if shape_err:
            return mk(shape_err)
        out: List[str] = []
        checks = tuple(CheckResult(fx.name, c["name"], c["ok"], c["detail"][:300]) for c in result["checks"])
        out.extend(f"{fx.name}/{c.name}: {c.detail}" for c in checks if not c.ok)
        out.extend(f"{fx.name}: {e[:300]}" for e in result["errors"][:10])
        if result["ok"] != (run.code == 0):
            out.append(f"{fx.name}: result says ok={result['ok']} but exit code is {run.code}")
        if result["ok"] and not out and self.fail_on_log:
            out.extend(f"{fx.name}: {p}" for p in log_problems(run.text, self.ignore))
        if not result["ok"] and not out:
            out.append(f"{fx.name}: failed without saying why")
        return VerifyResult(not out and result["ok"], tuple(out), 1, run.duration_s, run.code, run.tail, self.stage_name, checks)


def _shape_error(r: Any, kind: str) -> str:
    if type(r) is not dict or type(r.get("ok")) is not bool or r.get("fixture") != kind:
        return "result has the wrong shape"
    if type(r.get("ticks")) is not int or type(r.get("errors")) is not list or not all(type(e) is str for e in r["errors"]):
        return "result has the wrong shape"
    cs = r.get("checks")
    if type(cs) is not list or (not cs and r["ok"]):
        return "result contains no checks"
    for c in cs:
        if (type(c) is not dict or type(c.get("name")) is not str or type(c.get("ok")) is not bool
                or type(c.get("detail")) is not str):
            return "result has a malformed check"
    if r["ok"] and (not all(c["ok"] for c in cs) or r["errors"]):
        return "result claims ok but contains failures"
    return ""


def staged_verifier(godot_bin: Optional[str], fixtures: Sequence[Fixture] = (), *, timeout: float = 60.0,
                    fixture_timeout: float = 30.0, command_prefix: Sequence[str] = (),
                    ignore_patterns: Iterable[str] = (), **kw) -> StagedVerifier:
    """Stage 1: does it load (verify.gd).  Stage 2: does it behave (fixtures). Stage 2 only runs if stage 1 passes."""
    load = GodotVerifier(godot_bin, timeout=timeout, command_prefix=command_prefix, ignore_patterns=ignore_patterns)
    if not fixtures:
        return StagedVerifier(load)
    return StagedVerifier(load, FixtureVerifier(fixtures, godot_bin, timeout=fixture_timeout, command_prefix=command_prefix,
                                                ignore_patterns=ignore_patterns, **kw))
