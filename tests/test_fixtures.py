"""Fixture configuration, selection, and the runner's handling of every way an engine can misbehave.
Uses a fake engine, so no Godot is needed."""
import json
import math
import os
import time
from pathlib import Path

import pytest

from cascade_godot import (CameraFixture, CollideFixture, FixtureVerifier, MockVerifier, MoveCase, MoveFixture,
                           SpriteFixture, StagedVerifier, fixture_from_dict, load_fixtures, staged_verifier)
from cascade_godot.errors import CascadeError

FAKE = str(Path(__file__).with_name("fake_godot.py"))
S = "scenes/arena.tscn"


def mv(**kw):
    return MoveFixture(scene=S, node="Player", cases=(MoveCase("right", ("ui_right",), (1, 0)),), **kw)


def co(**kw):
    return CollideFixture(scene=S, mover="Player", statics=("Wall",), actions=("ui_right",), **kw)


def ca(**kw):
    return CameraFixture(scene=S, target="Player", actions=("ui_right",), **kw)


def sp(**kw):
    return SpriteFixture(scene=S, **kw)


def fv(fixtures, mode, **kw):
    return FixtureVerifier(fixtures, FAKE, extra_args=["--mode", mode], **kw)


# ---- validation -----------------------------------------------------------------------------------
BAD = {
    "scene_traversal": lambda: mv.__wrapped__(scene="../x.tscn") if hasattr(mv, "__wrapped__") else MoveFixture(scene="../x.tscn", node="P", cases=(MoveCase("a", ("ui_right",)),)),
    "scene_not_tscn": lambda: sp(scene="scenes/arena.gd") if False else SpriteFixture(scene="scripts/a.gd"),
    "scene_absolute": lambda: SpriteFixture(scene="/etc/x.tscn"),
    "label_spaces": lambda: sp(label="a b"),
    "viewport_tiny": lambda: sp(viewport=(10, 10)),
    "timeout_zero": lambda: sp(timeout=0),
    "triggers_empty": lambda: sp(triggers=()),
    "triggers_nonascii": lambda: sp(triggers=("\u202e*",)),
    "node_dotdot": lambda: MoveFixture(scene=S, node="../Player", cases=(MoveCase("a", ("ui_right",)),)),
    "node_space": lambda: MoveFixture(scene=S, node="Player 1", cases=(MoveCase("a", ("ui_right",)),)),
    "node_empty": lambda: MoveFixture(scene=S, node="", cases=(MoveCase("a", ("ui_right",)),)),
    "node_pct_path": lambda: MoveFixture(scene=S, node="%A/B", cases=(MoveCase("a", ("ui_right",)),)),
    "no_cases": lambda: MoveFixture(scene=S, node="Player", cases=()),
    "dup_case_names": lambda: MoveFixture(scene=S, node="P", cases=(MoveCase("a", ("ui_right",)), MoveCase("a", ("ui_left",)))),
    "case_bad_name": lambda: MoveCase("a b", ("ui_right",)),
    "case_no_actions": lambda: MoveCase("a", ()),
    "case_dup_actions": lambda: MoveCase("a", ("ui_right", "ui_right")),
    "case_bad_action": lambda: MoveCase("a", ("ui right",)),
    "case_action_injection": lambda: MoveCase("a", ('x"]}',)),
    "case_zero_dir": lambda: MoveCase("a", ("ui_right",), (0, 0)),
    "case_nan_dir": lambda: MoveCase("a", ("ui_right",), (math.nan, 1)),
    "ticks_zero": lambda: mv(ticks=0),
    "ticks_bool": lambda: mv(ticks=True),
    "ticks_float": lambda: mv(ticks=10.5),
    "ticks_huge": lambda: mv(ticks=3001),
    "budget_exceeded": lambda: MoveFixture.eight_way(S, "Player", ticks=2500, settle_ticks=100),
    "max_speed_nan": lambda: mv(max_speed=math.nan),
    "max_speed_inf": lambda: mv(max_speed=math.inf),
    "max_speed_neg": lambda: mv(max_speed=-1),
    "hold_gt_settle": lambda: mv(settle_ticks=5, settle_hold=6),
    "normalize_not_bool": lambda: mv(normalize="yes"),
    "collide_no_statics": lambda: CollideFixture(scene=S, mover="P", statics=(), actions=("ui_right",)),
    "collide_zero_toward": lambda: co(toward=(0, 0)),
    "collide_neg_pen": lambda: co(max_penetration=-0.1),
    "collide_window_gt_ticks": lambda: co(ticks=10, stop_window=11),
    "camera_world_zero": lambda: ca(world=(0, 0, 0, 100)),
    "camera_world_lt_view": lambda: ca(world=(0, 0, 100, 100), viewport=(640, 360)),
    "camera_world_arity": lambda: ca(world=(0, 0, 100)),
    "camera_bad_ref": lambda: ca(camera="../Camera"),
    "sprite_min_size_neg": lambda: sp(min_size=(-1, 1)),
    "sprite_expect_zero": lambda: sp(expect_size=(0, 8)),
    "sprite_too_many_nodes": lambda: sp(nodes=tuple(f"N{i}" for i in range(65))),
}


@pytest.mark.parametrize("name", list(BAD))
def test_bad_fixture_config_is_rejected(name):
    with pytest.raises(CascadeError) as e:
        BAD[name]()
    assert e.value.code == "BAD_FIXTURE"


def test_good_configs_and_specs():
    f = MoveFixture.eight_way(S, "Player", max_speed=200, ticks=30)
    assert f.name == "live_move" and len(f.cases) == 8 and f.normalize
    names = {c.name: c for c in f.cases}
    assert names["right_down"].actions == ("ui_right", "ui_down")
    assert names["right_down"].dir == pytest.approx((math.sqrt(0.5), math.sqrt(0.5)))
    assert names["left_up"].dir == pytest.approx((-math.sqrt(0.5), -math.sqrt(0.5)))
    spec = f.to_spec()
    json.dumps(spec)                                                                   # JSON-able, no tuples/dataclasses left
    assert spec["cases"][0] == {"name": "right", "actions": ["ui_right"], "dir": [1.0, 0.0]}
    assert "label" not in spec and "triggers" not in spec and "timeout" not in spec    # runner-only fields stay in Python
    assert "max_speed" in spec and "expect_size" not in sp().to_spec()                # None values are omitted
    assert sp(label="hud").name == "live_sprite:hud"
    assert ca(world=(0, 0, 2000, 2000), viewport=(640, 360)).to_spec()["world"] == [0, 0, 2000, 2000]


def test_fixture_from_dict_and_files(tmp_path):
    cfg = {"fixtures": [
        {"type": "move", "scene": S, "node": "Player", "max_speed": 200, "eight_way": {}},
        {"type": "move", "scene": S, "node": "Player", "cases": [{"name": "r", "actions": ["ui_right"], "dir": [1, 0]}]},
        {"type": "collide", "scene": S, "mover": "Player", "statics": ["Wall"], "actions": ["ui_right"]},
        {"type": "camera", "scene": S, "target": "Player", "actions": ["ui_right"], "world": [0, 0, 2000, 2000]},
        {"type": "sprite", "scene": S, "triggers": ["scenes/*"]},
    ]}
    p = tmp_path / "fixtures.json"
    p.write_text(json.dumps(cfg))
    fx = load_fixtures(p)
    assert [f.kind for f in fx] == ["live_move", "live_move", "live_collide", "live_camera", "live_sprite"]
    assert len(fx[0].cases) == 8 and fx[4].triggers == ("scenes/*",)
    for bad, why in (({"type": "nope"}, "needs a 'type'"), ({"scene": S}, "needs a 'type'"),
                     ({"type": "sprite", "scene": S, "evil": 1}, "unknown keys"), ({"type": "sprite"}, "scene"), ([], "needs a 'type'"),
                     ({"type": "move", "scene": S, "node": "P", "eight_way": 5}, "eight_way")):
        with pytest.raises(CascadeError, match=why):
            fixture_from_dict(bad)
    p.write_text("[]")
    with pytest.raises(CascadeError):
        load_fixtures(p)


# ---- selection ------------------------------------------------------------------------------------------
def test_triggers_select_fixtures(root):
    a, b, c = sp(triggers=("scripts/*",), label="a"), sp(triggers=("scenes/*.tscn",), label="b"), sp(label="c")
    v = fv([a, b, c], "fx_ok")
    assert [f.label for f in v.triggered(["scripts/player.gd"])] == ["a", "c"]
    assert [f.label for f in v.triggered(["scenes/main.tscn"])] == ["b", "c"]
    assert [f.label for f in v.triggered(["project.godot"])] == ["c"]
    r = v.verify(root, ["scripts/player.gd"])
    assert r.ok and r.checked == 2 and r.stage == "fixtures"


def test_nothing_triggered_is_a_pass_that_ran_nothing(root):
    r = fv([sp(triggers=("scenes/*",))], "fx_fail").verify(root, ["scripts/player.gd"])
    assert r.ok and r.checked == 0 and r.checks == ()


def test_spec_reaches_the_engine_exactly_as_validated(root):
    r = fv([MoveFixture.eight_way(S, "Player", ticks=30, max_speed=200)], "fx_echo_spec").verify(root, ["scripts/player.gd"])
    spec = json.loads(r.checks[0].detail)
    assert spec == {"scene": S, "ticks": 30, "normalize": True, "ncases": 8}


# ---- engine misbehaviour ----------------------------------------------------------------------------------
@pytest.mark.parametrize("mode,needle", [
    ("fx_fail", "live_sprite/b: went wrong"), ("fx_zero_checks", "no checks"), ("fx_claims_ok", "claims ok"),
    ("fx_wrong_name", "wrong shape"), ("fx_bad_exit", "exit code"), ("fx_errors_listed", "node not found"),
    ("fx_error_log", "engine log: SCRIPT ERROR"), ("fx_no_result", "no result line"), ("fx_bad_json", "not valid JSON"),
    ("fx_malformed_check", "malformed check"), ("fx_silent_fail", "without saying why"),
])
def test_every_failure_mode_fails_closed(root, mode, needle):
    r = fv([sp()], mode).verify(root, ["scenes/arena.tscn"])
    assert not r.ok and any(needle in e for e in r.errors), r.errors


def test_passing_run_reports_every_check(root):
    r = fv([sp()], "fx_ok").verify(root, ["x"])
    assert r.ok and [(c.fixture, c.name, c.ok) for c in r.checks] == [("live_sprite", "a", True), ("live_sprite", "b", True)]


def test_log_flood_fails_closed(root):
    r = fv([sp()], "fx_flood", max_log_bytes=50_000).verify(root, ["x"])
    assert not r.ok and "engine log exceeded 50000 bytes" in r.errors[0]
    assert fv([sp()], "fx_flood", max_log_bytes=50_000_000).verify(root, ["x"]).ok


def test_log_scan_can_be_relaxed(root):
    assert not fv([sp()], "fx_error_log").verify(root, ["x"]).ok
    assert fv([sp()], "fx_error_log", fail_on_log=False).verify(root, ["x"]).ok
    assert fv([sp()], "fx_error_log", ignore_patterns=[r"Invalid access"]).verify(root, ["x"]).ok


def test_stops_at_first_failing_fixture_unless_run_all(root):
    fx = [sp(label="one"), sp(label="two")]
    r = fv(fx, "fx_fail").verify(root, ["x"])
    assert r.checked == 1 and len(r.errors) == 1
    r = fv(fx, "fx_fail", run_all=True).verify(root, ["x"])
    assert r.checked == 2 and len(r.errors) == 2 and not r.ok


def test_timeout_kills_the_process_group_and_names_the_fixture(root, tmp_path):
    pidfile = tmp_path / "child.pid"
    v = FixtureVerifier([sp(timeout=1.0)], FAKE, extra_args=["--mode", "fx_timeout", "--pidfile", str(pidfile)])
    t0 = time.monotonic()
    r = v.verify(root, ["x"])
    assert not r.ok and "live_sprite: timed out after 1.0s" in r.errors[0] and time.monotonic() - t0 < 10
    pid = int(pidfile.read_text())
    time.sleep(0.2)
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        state = "Z"
    assert state == "Z"


def test_per_fixture_timeout_overrides_default(root):
    v = FixtureVerifier([sp(timeout=1.0)], FAKE, timeout=30, extra_args=["--mode", "fx_timeout"])
    t0 = time.monotonic()
    assert not v.verify(root, ["x"]).ok and time.monotonic() - t0 < 8


def test_runner_arguments_are_validated():
    with pytest.raises(CascadeError):
        FixtureVerifier(["not a fixture"], FAKE)
    for kw in ({"fps": 0}, {"fps": 5000}, {"timeout": 0}):
        with pytest.raises(CascadeError):
            FixtureVerifier([sp()], FAKE, **kw)


# ---- staged verification ------------------------------------------------------------------------------------
def test_stage_two_never_runs_when_stage_one_fails(root):
    one, two = MockVerifier(default_ok=False, stage_name="load"), MockVerifier(stage_name="fixtures")
    r = StagedVerifier(one, two).verify(root, ["scripts/player.gd"])
    assert not r.ok and r.stage == "load" and two.calls == [] and r.errors == ("[load] mock verifier said no",)


def test_stage_two_runs_and_results_merge(root):
    one = MockVerifier(stage_name="load")
    two = FixtureVerifier([sp()], FAKE, extra_args=["--mode", "fx_fail"])
    r = StagedVerifier(one, two).verify(root, ["scripts/player.gd"])
    assert not r.ok and r.stage == "fixtures" and r.errors == ("[fixtures] live_sprite/b: went wrong",)
    assert [c.name for c in r.checks] == ["a", "b"] and r.checked == 2                     # 1 file + 1 fixture
    ok = StagedVerifier(one, FixtureVerifier([sp()], FAKE, extra_args=["--mode", "fx_ok"])).verify(root, ["x"])
    assert ok.ok and ok.stage == "load+fixtures" and len(ok.checks) == 2


def test_staged_needs_a_stage():
    with pytest.raises(ValueError):
        StagedVerifier()


def test_staged_verifier_helper_builds_both_stages():
    assert len(staged_verifier(FAKE, [sp()]).stages) == 2 and len(staged_verifier(FAKE).stages) == 1
