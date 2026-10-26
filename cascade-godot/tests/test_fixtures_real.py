"""The four fixtures against the real engine (Godot 4.3, --headless --fixed-fps). Needs GODOT_BIN."""
import struct
import time
import zlib
from pathlib import Path

import pytest

import gamefx as G
from gamefx import godot_procs
from cascade_godot import (CameraFixture, CollideFixture, FixtureVerifier, MoveCase, MoveFixture, SpriteFixture,
                           import_project)

pytestmark = pytest.mark.godot

ARENA, OPEN = "scenes/arena.tscn", "scenes/open.tscn"
MOVE = MoveFixture.eight_way(ARENA, "Player", max_speed=200, ticks=30, min_distance=20)
COLLIDE = CollideFixture(scene=ARENA, mover="Player", statics=("Wall",), actions=("ui_right",), toward=(1, 0),
                         slide_actions=("ui_down",))
CAMERA = CameraFixture(scene=OPEN, target="Player", actions=("ui_right",), margin=24, world=(0, 0, 1000, 1000))
SPRITE = SpriteFixture(scene=ARENA, nodes=("Sprite",))
ALL = [MOVE, COLLIDE, CAMERA, SPRITE]


def fvr(godot_bin, fixtures, **kw):
    kw.setdefault("timeout", 20)
    return FixtureVerifier(fixtures, godot_bin, **kw)


def failing(res):
    return {c.name for c in res.checks if not c.ok}


# ---- the good game passes everything ------------------------------------------------------------------------
def test_good_game_passes_all_four_fixtures(root, godot_bin):
    G.make_game(root, camera="limited")
    t0 = time.monotonic()
    r = fvr(godot_bin, ALL).verify_all(root)
    assert r.ok, r.errors
    assert r.checked == 4 and time.monotonic() - t0 < 20
    names = {(c.fixture, c.name) for c in r.checks}
    for want in [("live_move", "normalization"), ("live_move", "left_up.direction"), ("live_collide", "penetration"),
                 ("live_collide", "no_tunneling"), ("live_collide", "slides_along_obstacle"), ("live_camera", "view_in_world"),
                 ("live_camera", "converges_when_target_stops"), ("live_sprite", "Player/Sprite.size")]:
        assert want in names, want
    assert all(c.ok for c in r.checks)


def test_results_are_deterministic(root, godot_bin):
    G.make_game(root, camera="limited")
    a = fvr(godot_bin, ALL).verify_all(root)
    b = fvr(godot_bin, ALL).verify_all(root)
    assert [(c.name, c.detail) for c in a.checks] == [(c.name, c.detail) for c in b.checks]


def test_a_good_mover_slides_along_the_wall_without_digging_in(root, godot_bin):
    G.make_game(root, camera="limited")
    r = fvr(godot_bin, [COLLIDE]).verify_all(root)
    assert r.ok, r.errors
    slide = next(c.detail for c in r.checks if c.name == "slides_along_obstacle")
    assert "slid 70.71 px" in slide                               # 30 ticks at 200 px/s x 0.707: right+down is normalized while pressed against the wall


def test_diagonal_only_collision_bug_leaves_the_straight_checks_green(root, godot_bin):
    G.make_game(root, player=G.DIAGONAL_GHOST, camera="limited")
    r = fvr(godot_bin, [COLLIDE]).verify_all(root)
    assert failing(r) == {"slide_stays_in_contact", "slide_penetration"}           # phase 1 was genuinely fine


def test_one_broken_case_does_not_contaminate_the_next(root, godot_bin):
    G.make_game(root, player=G.NAN_ON_RIGHT, camera="limited")
    r = fvr(godot_bin, [MOVE]).verify_all(root)
    assert failing(r) == {"right.finite_position"}, failing(r)       # "right" stayed held? then up/down/left would fail too


def test_a_mover_that_ramps_up_is_judged_on_its_steady_speed(root, godot_bin):
    G.make_game(root, player=G.RAMP, camera="limited")
    r = fvr(godot_bin, [MOVE]).verify_all(root)
    assert r.ok, r.errors
    detail = next(c.detail for c in r.checks if c.name == "normalization")
    assert "cardinal 200.0 px/s" in detail and "diagonal 200.0 px/s" in detail     # the ramp is not averaged in


def test_event_driven_input_is_delivered_including_diagonals(root, godot_bin):
    G.make_game(root, player=G.EVENTS, camera="limited")
    r = fvr(godot_bin, [MOVE]).verify_all(root)
    assert r.ok, r.errors


def test_physics_tick_rate_is_respected(root, godot_bin):
    G.make_game(root, project_extra="\n[physics]\n\ncommon/physics_ticks_per_second=120\n")
    r = fvr(godot_bin, [MOVE]).verify_all(root)
    assert r.ok, r.errors
    speed = next(c for c in r.checks if c.name == "right.speed_bound")
    assert "peak 200." in speed.detail                        # px/s, not px/tick*60


# ---- broken movement ------------------------------------------------------------------------------------------
BROKEN = {
    "unnormalized_diagonal": (G.UNNORMALIZED, [MOVE], {"normalization"}),
    "ghost_ignores_walls": (G.GHOST, [COLLIDE], {"no_tunneling", "penetration", "stops_at_obstacle"}),
    "stuck": (G.STUCK, [MOVE, COLLIDE], {"right.moved", "reaches_obstacle"}),
    "sticks_to_walls": (G.STICKY, [COLLIDE], {"slides_along_obstacle"}),
    "moves_static_bodies": (G.PUSHES_WALL, [COLLIDE], {"static_unmoved"}),
    "digs_into_walls_only_while_sliding": (G.DIAGONAL_GHOST, [COLLIDE], {"slide_stays_in_contact", "slide_penetration"}),
    "too_fast": (G.FAST, [MOVE], {"right.speed_bound"}),
    "inverted_controls": (G.INVERTED, [MOVE], {"right.direction", "left.direction"}),
    "never_stops": (G.DRIFT, [MOVE], {"left.settles", "down.settles", "up.settles"}),   # the wall happens to stop "right"
    "jitters_at_rest": (G.JITTER, [MOVE], {"right.settles"}),
    "goes_quiet_then_drifts_later": (G.LATE_DRIFT, [MOVE], {"down.settles", "up.settles"}),
}


@pytest.mark.parametrize("name", list(BROKEN))
def test_broken_movement_is_caught_by_the_right_check(root, godot_bin, name):
    script, fixtures, expect = BROKEN[name]
    G.make_game(root, player=script, camera="limited")
    r = fvr(godot_bin, fixtures, run_all=True).verify_all(root)
    assert not r.ok
    assert expect <= failing(r), (expect, failing(r), r.errors)


def test_a_clean_diagonal_is_not_blamed_on_the_direction_check(root, godot_bin):
    G.make_game(root, player=G.UNNORMALIZED)
    r = fvr(godot_bin, [MOVE]).verify_all(root)
    assert "right_down.direction" not in failing(r) and "normalization" in failing(r)
    detail = next(c.detail for c in r.checks if c.name == "normalization")
    assert "1.414" in detail


def test_runtime_script_errors_fail_the_fixture(root, godot_bin):
    G.make_game(root, player=G.LOGERR)
    r = fvr(godot_bin, [MOVE]).verify_all(root)
    assert not r.ok and any("engine log: ERROR" in e for e in r.errors)


def test_a_hanging_game_is_killed_and_reported(root, godot_bin):
    G.make_game(root, player=G.HANG)
    t0 = time.monotonic()
    r = fvr(godot_bin, [MOVE], timeout=3).verify_all(root)
    assert not r.ok and "timed out after 3" in r.errors[0] and time.monotonic() - t0 < 15
    time.sleep(0.3)
    assert godot_procs(str(root)) == []                       # nothing left running


# ---- config problems are reported, not crashed on --------------------------------------------------------------------
def test_missing_input_action_scene_and_nodes(root, godot_bin):
    G.make_game(root)
    bad_action = MoveFixture(scene=ARENA, node="Player", cases=(MoveCase("a", ("not_an_action",)),))
    r = fvr(godot_bin, [bad_action]).verify_all(root)
    assert not r.ok and any("not defined in the project's Input Map" in e for e in r.errors)
    r = fvr(godot_bin, [MoveFixture(scene="scenes/nope.tscn", node="Player", cases=MOVE.cases)]).verify_all(root)
    assert not r.ok and any("cannot load scene" in e for e in r.errors)
    r = fvr(godot_bin, [MoveFixture(scene=ARENA, node="Ghost", cases=MOVE.cases)]).verify_all(root)
    assert not r.ok and any("node not found: Ghost" in e for e in r.errors)
    (root / "scenes/dup.tscn").write_text('[gd_scene format=3]\n\n[node name="R" type="Node2D"]\n\n[node name="A" type="Node2D" parent="."]\n\n'
                                          '[node name="Dup" type="Node2D" parent="A"]\n\n[node name="B" type="Node2D" parent="."]\n\n[node name="Dup" type="Node2D" parent="B"]\n')
    r = fvr(godot_bin, [MoveFixture(scene="scenes/dup.tscn", node="Dup", cases=MOVE.cases)]).verify_all(root)
    assert not r.ok and any("ambiguous" in e for e in r.errors)
    ok = fvr(godot_bin, [MoveFixture(scene="scenes/dup.tscn", node="A/Dup", cases=(MoveCase("r", ("ui_right",)),), min_distance=0)]).verify_all(root)
    assert any(c.name == "r.finite_position" for c in ok.checks)               # a path resolves where a bare name is ambiguous


# ---- camera ---------------------------------------------------------------------------------------------------------
CAMERA_CASES = {
    "follow_passes": ("follow", dict(), set()),
    "smooth_passes": ("smooth", dict(), set()),
    "limited_world_passes": ("limited", dict(world=(0, 0, 1000, 1000)), set()),
    "smooth_but_lag_budget_tight": ("smooth", dict(max_lag=20), {"tracking_lag"}),
    "static_loses_target": ("static", dict(), {"target_on_screen", "tracking_lag"}),
    "disabled": ("off", dict(), {"camera_active"}),
    "two_cameras": ("two", dict(), {"camera_present"}),
    "unclamped_view_leaves_world": ("follow", dict(world=(0, 0, 1000, 1000)), {"view_in_world"}),
    "drag_camera_never_recentres": ("drag", dict(max_lag=250), {"converges_when_target_stops"}),
    "drag_camera_with_a_tolerance": ("drag", dict(max_lag=250, final_lag=150), set()),
}


@pytest.mark.parametrize("name", list(CAMERA_CASES))
def test_camera(root, godot_bin, name):
    cam, kw, expect = CAMERA_CASES[name]
    G.make_game(root, camera=cam)
    kw = {"margin": 24, **kw}
    r = fvr(godot_bin, [CameraFixture(scene=OPEN, target="Player", actions=("ui_right",), **kw)]).verify_all(root)
    assert failing(r) == expect or (expect and expect <= failing(r)), (failing(r), r.errors)
    assert r.ok == (not expect)


def test_zoom_changes_what_the_camera_can_see(root, godot_bin):
    G.make_game(root, camera="zoom")
    r = fvr(godot_bin, [CameraFixture(scene=OPEN, target="Player", actions=("ui_right",), world=(0, 0, 1000, 1000))]).verify_all(root)
    assert failing(r) == {"view_in_world"}
    # zoom 2 on a 640x360 view sees 320x180: centred on (100, 100) it hangs 60 px off the left edge (not 220 at zoom 1)
    assert "view extended 60.0 px" in next(c.detail for c in r.checks if c.name == "view_in_world")


def test_zoom_scales_the_on_screen_margin(root, godot_bin):
    G.make_game(root, camera="smooth_zoom")
    r = fvr(godot_bin, [CameraFixture(scene=OPEN, target="Player", actions=("ui_down",), margin=24)]).verify_all(root)
    assert r.ok, r.errors
    # smoothing lags 36.67 px behind; at zoom 2 that is 73.3 screen px below centre of a 360 px view: 360 - (180 + 73.3) = 106.7
    assert "edge: 106.7 px" in next(c.detail for c in r.checks if c.name == "target_on_screen")


def test_zoom_and_limits_together_use_the_zoomed_view_for_the_resting_position(root, godot_bin):
    G.make_game(root, camera="limited_zoom")
    r = fvr(godot_bin, [CameraFixture(scene=OPEN, target="Player", actions=("ui_right",), world=(0, 0, 1000, 1000))]).verify_all(root)
    assert r.ok, r.errors                                      # pinned to the limit at x=160 (not 320): lag against the clamped ideal is 0


def test_drag_margin_lag_is_exactly_the_margin(root, godot_bin):
    G.make_game(root, camera="drag")
    r = fvr(godot_bin, [CameraFixture(scene=OPEN, target="Player", actions=("ui_right",), max_lag=250, final_lag=150)]).verify_all(root)
    assert "lag 96.0 px" in next(c.detail for c in r.checks if c.name == "tracking_lag")          # 0.3 x half of 640


def test_smoothing_lag_is_measured_not_guessed(root, godot_bin):
    G.make_game(root, camera="smooth")
    r = fvr(godot_bin, [CameraFixture(scene=OPEN, target="Player", actions=("ui_right",))]).verify_all(root)
    lag = next(c.detail for c in r.checks if c.name == "tracking_lag")
    assert "36.7 px" in lag                                    # v / smoothing_speed = 200 / 5 ... measured, as in the probes


# ---- sprites -------------------------------------------------------------------------------------------------------------
SPRITE_CASES = {
    "good": ("good", dict(), set(), None),
    "hidden": ("hidden", dict(), {"Player/Sprite.visible"}, None),
    "transparent": ("alpha", dict(), {"Player/Sprite.opaque"}, None),
    "region_outside_texture": ("region", dict(), {"Player/Sprite.region"}, None),
    "texture_too_big": ("huge", dict(), {"Player/Sprite.size"}, None),
    "scaled_to_nothing": ("scale0", dict(), {"Player/Sprite.scale"}, None),
    "wrong_size": ("good", dict(expect_size=(64, 64)), {"Player/Sprite.expected_size"}, None),
    "no_texture": ("none", dict(), {"sprites_present"}, None),
    "bad_frame_is_an_engine_error": ("frame", dict(), set(), "engine log: ERROR"),
}


@pytest.mark.parametrize("name", list(SPRITE_CASES))
def test_sprite(root, godot_bin, name):
    variant, kw, expect, log_needle = SPRITE_CASES[name]
    G.make_game(root, sprite=variant)
    r = fvr(godot_bin, [SpriteFixture(scene=ARENA, **kw)]).verify_all(root)     # auto-discovers sprite nodes
    assert failing(r) == expect, (failing(r), r.errors)
    if log_needle:
        assert any(log_needle in e for e in r.errors)
    assert r.ok == (not expect and not log_needle)


def _png(w, h, rgba):
    raw = b"".join(b"\x00" + bytes(rgba) * w for _ in range(h))

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def test_png_textures_need_an_import_and_the_fixture_says_so(root, godot_bin):
    G.make_game(root)
    (root / "icon.png").write_bytes(_png(8, 8, (255, 0, 0, 255)))
    (root / "scenes/png.tscn").write_text('[gd_scene load_steps=2 format=3]\n\n[ext_resource type="Texture2D" path="res://icon.png" id="1_t"]\n\n'
                                          '[node name="Root" type="Node2D"]\n\n[node name="Icon" type="Sprite2D" parent="."]\ntexture = ExtResource("1_t")\n')
    fx = SpriteFixture(scene="scenes/png.tscn", expect_size=(8, 8))
    before = fvr(godot_bin, [fx]).verify_all(root)
    assert not before.ok and "Icon.texture" in failing(before)
    assert any("not imported" in c.detail for c in before.checks if c.name == "Icon.texture")
    run = import_project(root, godot_bin, timeout=120)
    assert run.code == 0 and not run.timed_out
    after = fvr(godot_bin, [fx]).verify_all(root)
    assert after.ok, after.errors
