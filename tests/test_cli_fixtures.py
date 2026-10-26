import json

import pytest

import gamefx as G
from cascade_godot.__main__ import main

CONFIG = {"fixtures": [
    {"type": "move", "scene": "scenes/arena.tscn", "node": "Player", "max_speed": 200, "ticks": 30, "min_distance": 20, "eight_way": {}},
    {"type": "collide", "scene": "scenes/arena.tscn", "mover": "Player", "statics": ["Wall"], "actions": ["ui_right"], "slide_actions": ["ui_down"]},
]}


def test_cli_errors_exit_2(tmp_path, capsys):
    cfg = tmp_path / "f.json"
    cfg.write_text("[]")
    assert main(["fixtures", str(tmp_path), str(cfg), "--godot", "/nonexistent/godot"]) == 2
    assert main(["fixtures", str(tmp_path), str(tmp_path / "missing.json")]) == 2


@pytest.mark.godot
def test_cli_runs_fixtures_and_exit_code_follows_the_result(root, godot_bin, tmp_path, capsys):
    cfg = tmp_path / "fixtures.json"
    cfg.write_text(json.dumps(CONFIG))
    G.make_game(root, camera="limited")
    assert main(["fixtures", str(root), str(cfg), "--godot", godot_bin]) == 0
    out = capsys.readouterr().out
    assert "ok   live_move/normalization" in out and "PASS: 2 fixture(s)" in out
    G.make_game(root, player=G.GHOST, camera="limited")
    assert main(["fixtures", str(root), str(cfg), "--godot", godot_bin, "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert not data["ok"] and any(c["name"] == "penetration" and not c["ok"] for c in data["checks"])
