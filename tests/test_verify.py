import os
import sys
import time
from pathlib import Path

import pytest

from cascade_godot import CascadeError, GodotVerifier, MockVerifier, find_godot
from conftest import godot_path

FAKE = str(Path(__file__).with_name("fake_godot.py"))


def _alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"                                   # a zombie is dead, it just has not been reaped


def gv(mode, **kw):
    return GodotVerifier(FAKE, extra_args=["--mode", mode], **kw)


def test_ok(root):
    r = gv("ok").verify(root, ["scripts/player.gd", "scenes/main.tscn"])
    assert r.ok and r.checked == 2 and r.engine_exit == 0


@pytest.mark.parametrize("mode,needle", [
    ("fail_result", "does not compile"), ("no_result", "no result line"), ("crash", "no result line"),
    ("exit_mismatch", "exit code"), ("ok_but_nonzero_flag", "exit code"), ("wrong_count", "checked"),
    ("bad_json", "not valid JSON"), ("wrong_shape", "wrong shape"),
    ("error_log", "engine log: SCRIPT ERROR"), ("warning_log", "engine log: WARNING"),
    ("log_after_result", "engine log: ERROR"),
])
def test_failure_modes_fail_closed(root, mode, needle):
    r = gv(mode).verify(root, ["scripts/player.gd"])
    assert not r.ok and any(needle in e for e in r.errors), r.errors


def test_ignore_patterns_and_fail_on_log_switch(root):
    assert not gv("benign_log").verify(root, ["scripts/player.gd"]).ok
    assert gv("benign_log", ignore_patterns=[r"told the verifier to ignore"]).verify(root, ["scripts/player.gd"]).ok
    assert gv("error_log", fail_on_log=False).verify(root, ["scripts/player.gd"]).ok


def test_timeout_kills_the_whole_process_group(root, tmp_path):
    pidfile = tmp_path / "child.pid"
    v = GodotVerifier(FAKE, extra_args=["--mode", "timeout", "--pidfile", str(pidfile)], timeout=1.0)
    t0 = time.monotonic()
    r = v.verify(root, ["scripts/player.gd"])
    assert not r.ok and "timed out" in r.errors[0] and time.monotonic() - t0 < 10
    pid = int(pidfile.read_text())
    time.sleep(0.2)
    assert not _alive(pid)                                # the grandchild is dead too


def test_environment_is_scrubbed(root, monkeypatch):
    monkeypatch.setenv("SECRET_API_KEY", "hunter2")
    r = gv("env").verify(root, ["scripts/player.gd"])
    home = next(e for e in r.errors if e.startswith("HOME="))
    keys = next(e for e in r.errors if e.startswith("KEYS="))
    assert "cascade-home-" in home and os.environ["HOME"] not in home
    assert "SECRET_API_KEY" not in keys


def test_log_flood_fails_closed_instead_of_hiding_errors(root):
    r = gv("flood", max_log_bytes=50_000).verify(root, ["scripts/player.gd"])
    assert not r.ok and "exceeded" in r.errors[0] and len(r.log_tail) <= 2000
    assert gv("flood", max_log_bytes=50_000_000).verify(root, ["scripts/player.gd"]).ok      # under the cap it is fine


def test_paths_are_passed_as_res_urls_and_unsafe_ones_never_reach_the_engine(root):
    r = gv("args").verify(root, ["scripts/player.gd", "scripts/player.gd"])
    assert 'ARGS=["res://scripts/player.gd"]' in r.errors[0]          # de-duplicated
    for bad in ("../x.gd", "/etc/passwd", "a b.gd", "x.gd\n--quit"):
        r = gv("args").verify(root, [bad])
        assert not r.ok and r.engine_exit is None and "unsafe verify path" in r.errors[0]
    assert not gv("ok").verify(root, []).ok


def test_missing_binary(root):
    with pytest.raises(CascadeError) as e:
        GodotVerifier("/nonexistent/godot")
    assert e.value.code == "NO_ENGINE"
    v = GodotVerifier(FAKE, command_prefix=["/nonexistent/wrapper"])
    r = v.verify(root, ["scripts/player.gd"])
    assert not r.ok and "cannot start engine" in r.errors[0]


def test_command_prefix_is_used_for_sandboxing(root):
    r = GodotVerifier(FAKE, command_prefix=["/usr/bin/env", "FROM_PREFIX=1"], extra_args=["--mode", "ok"]).verify(root, ["scripts/player.gd"])
    assert r.ok


def test_mock_verifier_records_state(root):
    v = MockVerifier(lambda r, f: f[0] != "scripts/bad.gd")
    assert v.verify(root, ["scripts/player.gd"]).ok and not v.verify(root, ["scripts/bad.gd"]).ok
    assert v.calls == [("scripts/player.gd",), ("scripts/bad.gd",)] and v.seen[1] == {"scripts/bad.gd": None}


# ---- the real engine ----------------------------------------------------------------------
@pytest.mark.godot
class TestRealEngine:
    GOOD_SCENE = ('[gd_scene load_steps=2 format=3]\n\n[ext_resource type="Script" path="res://scripts/player.gd" id="1_a"]\n\n'
                  '[node name="Main" type="Node2D"]\n\n[node name="Player" type="Node2D" parent="."]\nscript = ExtResource("1_a")\n')

    def v(self, godot_bin):
        return GodotVerifier(godot_bin, timeout=60)

    def test_good_project_passes(self, root, godot_bin):
        (root / "scenes/good.tscn").write_text(self.GOOD_SCENE)
        r = self.v(godot_bin).verify(root, ["scripts/player.gd", "scenes/main.tscn", "scenes/good.tscn", "project.godot"])
        assert r.ok, r.errors
        assert r.checked == 4 and r.engine_exit == 0

    def test_script_syntax_error_fails(self, root, godot_bin):
        (root / "scripts/player.gd").write_text("extends Node2D\nfunc broken( -> int\n\treturn 1 +\n")
        r = self.v(godot_bin).verify(root, ["scripts/player.gd"])
        assert not r.ok and any("does not compile" in e for e in r.errors)
        assert r.engine_exit == 1                           # the engine itself reports failure, not only the JSON

    def test_scene_whose_script_is_broken_fails(self, root, godot_bin):
        (root / "scenes/good.tscn").write_text(self.GOOD_SCENE)
        (root / "scripts/player.gd").write_text("extends Node2D\nfunc broken( -> int\n")
        r = self.v(godot_bin).verify(root, ["scenes/good.tscn"])
        assert not r.ok and any("script does not compile" in e for e in r.errors)

    def test_scene_with_vanished_parent_fails(self, root, godot_bin):
        (root / "scenes/orphan.tscn").write_text('[gd_scene format=3]\n\n[node name="Main" type="Node2D"]\n\n[node name="P" type="Node2D" parent="NoSuch"]\n')
        r = self.v(godot_bin).verify(root, ["scenes/orphan.tscn"])
        assert not r.ok and any("missing parent" in e for e in r.errors)

    def test_missing_script_reference_fails(self, root, godot_bin):
        (root / "scenes/ghost.tscn").write_text('[gd_scene load_steps=2 format=3]\n\n[ext_resource type="Script" path="res://scripts/ghost.gd" id="1_a"]\n\n[node name="Main" type="Node2D"]\nscript = ExtResource("1_a")\n')
        assert not self.v(godot_bin).verify(root, ["scenes/ghost.tscn"]).ok

    @pytest.mark.parametrize("content,needle", [
        ("config_version=5\n[application\nconfig/name=\n", "does not parse"),
        ("config_version=4\n", "config_version"),
        ('config_version=5\n[autoload]\nGhost="*res://ghost.gd"\n', "missing file"),
    ])
    def test_bad_project_godot_fails(self, root, godot_bin, content, needle):
        (root / "project.godot").write_text(content)
        r = self.v(godot_bin).verify(root, ["project.godot"])
        assert not r.ok and any(needle in e for e in r.errors), r.errors

    def test_missing_file_fails(self, root, godot_bin):
        r = self.v(godot_bin).verify(root, ["scripts/nope.gd"])
        assert not r.ok and any("missing file" in e for e in r.errors)

    def test_find_godot_honours_env(self, godot_bin, monkeypatch):
        monkeypatch.setenv("GODOT_BIN", godot_bin)
        assert find_godot() == godot_bin
