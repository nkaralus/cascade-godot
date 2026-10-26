import hashlib
import os
import shutil
from pathlib import Path

import pytest

from subnetbus import AABB, PathGuard, Registry

SN = "10.1.1.0/24"

SCENE = """[gd_scene load_steps=1 format=3]

[node name="Main" type="Node2D"]

[node name="Player" type="Node2D" parent="."]
position = Vector2(100, 100)

[node name="Wall" type="Node2D" parent="."]
position = Vector2(200, 100)
"""
PLAYER_GD = "extends Node2D\n\nfunc speed() -> int:\n\treturn 200\n"
PROJECT = 'config_version=5\n\n[application]\nconfig/name="t"\n'


class SimulatedCrash(BaseException):
    """BaseException on purpose: nothing in the code under test may catch it and tidy up."""


def snapshot(root: Path) -> dict:
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in (".cascade", ".godot")]
        for d in dns:
            out["d:" + os.path.relpath(os.path.join(dp, d), root)] = None
        for f in fns:
            p = os.path.join(dp, f)
            out["f:" + os.path.relpath(p, root)] = (hashlib.sha256(open(p, "rb").read()).hexdigest(), oct(os.stat(p).st_mode & 0o777))
    return out


def stray_temp_files(root: Path):
    return [p for p in root.rglob("*") if ".cascade-" in p.name and ".cascade" not in p.parts[len(root.parts):-1]]


def crash_at(label):
    def fault(name):
        if name == label:
            raise SimulatedCrash(name)
    return fault


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "proj"
    (r / "scenes").mkdir(parents=True)
    (r / "scripts").mkdir()
    (r / "project.godot").write_text(PROJECT)
    (r / "scenes" / "main.tscn").write_text(SCENE)
    (r / "scripts" / "player.gd").write_text(PLAYER_GD)
    os.chmod(r / "scripts" / "player.gd", 0o755)
    return r


@pytest.fixture
def guard(root):
    return PathGuard(root, mountpoints=())


@pytest.fixture
def reg():
    r = Registry()
    r.add_subnet(SN, label="main", world=AABB(0, 0, 1000, 1000))
    r.add_subnet("10.1.2.0/24")
    r.allocate("%Player", SN, ip="10.1.1.10", bounds=AABB(100, 100, 32, 32))
    r.allocate("%Wall", SN, ip="10.1.1.14", bounds=AABB(200, 100, 32, 200))
    r.allocate("%Foreign", "10.1.2.0/24", ip="10.1.2.10")
    return r


def frame(**kw):
    f = {"frame_id": "0x0001", "time_slot": "SLOT_3_EXECUTE", "subnet": SN, "op": "set_position",
         "target": "%Player", "ref": "10.1.1.10", "exclude": ["untyped_variables"]}
    f.update(kw)
    return f


def godot_path():
    p = os.environ.get("GODOT_BIN")
    return p if p and os.access(p, os.X_OK) else None


@pytest.fixture
def godot_bin():
    p = godot_path()
    if not p:
        pytest.skip("set GODOT_BIN to run real-engine tests")
    return p
