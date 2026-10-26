"""Stage 2 inside the full pipeline: a harness handler rewrites a game file, stage 1 says "it loads",
stage 2 RUNS the game and may still say no. A "no" at either stage rolls everything back."""
import json
import time

import pytest

import gamefx as G
from gamefx import godot_procs
from cascade_godot import Cascade, FileChange, Plan, staged_verifier
from conftest import SN, frame, snapshot

mule = pytest.importorskip("mule")
pytestmark = pytest.mark.godot

STD = ["untyped_variables", "relative_paths_up_tree", "cross_subnet_queries", "freeform_code_blocks", "synthetic_transactions"]
PIN = {"subnet": SN, "target": "%Player", "ref": "10.1.1.10"}


def raw(op):
    return json.dumps({"frame_id": "0x0001", "time_slot": "SLOT_3_EXECUTE", "subnet": SN, "op": op, "target": "%Player",
                       "ref": "10.1.1.10", "exclude": STD}, separators=(",", ":"))


HANDLERS = {
    "write_player": lambda f, p, c: Plan((FileChange.text("scripts/player.gd", p["body"]),)),
    "write_scene": lambda f, p, c: Plan((FileChange.text(p["path"], p["body"]),)),
}


def build(root, reg, godot_bin, fixtures=None, **kw):
    ver = staged_verifier(godot_bin, fixtures if fixtures is not None else G.standard_fixtures(), timeout=30, fixture_timeout=kw.pop("fixture_timeout", 10))
    return Cascade(root, reg, HANDLERS, ver, mule_validator=mule.MuleValidator(allowed_ops=set(HANDLERS)), mountpoints=(), **kw)


@pytest.fixture
def game(root):
    G.make_game(root, camera="limited")
    return root


def test_a_good_change_runs_both_stages_and_commits(game, reg, godot_bin):
    body = G.GOOD + "# tuned\n"
    r = build(game, reg, godot_bin).run_raw(raw("write_player"), {"body": body}, expect=PIN)
    assert r.status == "COMMITTED", (r.errors, r.detail)
    v = r.tx.verify
    assert v.stage == "load+fixtures" and {c.fixture for c in v.checks} == {"live_move", "live_collide", "live_camera", "live_sprite"}
    assert (game / "scripts/player.gd").read_text() == body


BAD = {
    "unnormalized_diagonal": (G.UNNORMALIZED, "live_move/normalization"),
    "ghost_through_walls": (G.GHOST, "live_collide/penetration"),
    "inverted_controls": (G.INVERTED, "live_move/right.direction"),
    "never_stops": (G.DRIFT, "live_move/left.settles"),
}


@pytest.mark.parametrize("name", list(BAD))
def test_a_game_that_loads_but_misbehaves_is_rolled_back(game, reg, godot_bin, name):
    body, needle = BAD[name]
    before = snapshot(game)
    r = build(game, reg, godot_bin).run_raw(raw("write_player"), {"body": body}, expect=PIN)
    assert r.status == "VERIFY_FAILED" and not r.ok
    assert r.tx.verify.stage == "fixtures"                     # stage 1 PASSED: this is what stage 1 cannot see
    assert any(needle in e for e in r.errors), r.errors
    assert snapshot(game) == before                            # byte-for-byte, modes included
    assert r.tx.status == "ROLLED_BACK"


def test_syntax_errors_stop_at_stage_one_and_no_game_code_runs(game, reg, godot_bin):
    before = snapshot(game)
    r = build(game, reg, godot_bin).run_raw(raw("write_player"), {"body": G.SYNTAX_ERROR}, expect=PIN)
    assert r.status == "VERIFY_FAILED" and r.tx.verify.stage == "load" and r.tx.verify.checks == ()
    assert snapshot(game) == before


def test_a_hanging_script_times_out_is_killed_and_rolled_back(game, reg, godot_bin):
    before = snapshot(game)
    t0 = time.monotonic()
    r = build(game, reg, godot_bin, fixture_timeout=3).run_raw(raw("write_player"), {"body": G.HANG}, expect=PIN)
    assert r.status == "VERIFY_FAILED" and any("timed out after 3" in e for e in r.errors), r.errors
    assert time.monotonic() - t0 < 20 and snapshot(game) == before
    time.sleep(0.3)
    assert godot_procs(str(game)) == []                        # the stuck engine did not outlive the transaction


def test_a_scene_change_that_breaks_the_camera_is_caught(game, reg, godot_bin):
    before = snapshot(game)
    r = build(game, reg, godot_bin).run_raw(raw("write_scene"), {"path": "scenes/open.tscn", "body": G.arena(camera="off", wall=False)}, expect=PIN)
    assert r.status == "VERIFY_FAILED" and any("live_camera/camera_active" in e for e in r.errors), r.errors
    assert snapshot(game) == before


def test_triggers_run_only_the_fixtures_a_change_can_affect(game, reg, godot_bin):
    fx = G.standard_fixtures(triggers_by_kind={"move": ("scripts/*",), "collide": ("scripts/*",), "camera": ("scenes/*",), "sprite": ("scenes/*",)})
    c = build(game, reg, godot_bin, fixtures=fx)
    r = c.run_raw(raw("write_player"), {"body": G.GOOD + "# a\n"}, expect=PIN)
    assert r.ok and {x.fixture for x in r.tx.verify.checks} == {"live_move", "live_collide"}
    r = c.run_raw(raw("write_scene"), {"path": "scenes/open.tscn", "body": G.arena(camera="limited", wall=False) + "\n"}, expect=PIN)
    assert r.ok and {x.fixture for x in r.tx.verify.checks} == {"live_camera", "live_sprite"}


def test_a_crash_while_the_game_is_running_recovers(game, reg, godot_bin):
    from conftest import SimulatedCrash, crash_at
    before = snapshot(game)
    c = build(game, reg, godot_bin, fault=crash_at("after_verify"))
    with pytest.raises(SimulatedCrash):
        c.run_raw(raw("write_player"), {"body": G.GOOD + "# x\n"}, expect=PIN)
    assert snapshot(game) != before
    assert len(c.tx.recover()) == 1 and snapshot(game) == before
