"""End-to-end demo in a throwaway project.  GODOT_BIN=/path/to/godot python examples/demo.py
Without GODOT_BIN it uses a mock engine (loads nothing, so the 'broken script' case will not fail)."""
import json
import os
import tempfile
from pathlib import Path

import mule
from subnetbus import AABB, Registry
from cascade_godot import DEFAULT_HANDLERS, Cascade, FileChange, GodotVerifier, MockVerifier, Plan

SN = "10.1.1.0/24"
STD = ["untyped_variables", "relative_paths_up_tree", "cross_subnet_queries", "freeform_code_blocks", "synthetic_transactions"]
SCENE = '[gd_scene format=3]\n\n[node name="Main" type="Node2D"]\n\n[node name="Player" type="Node2D" parent="."]\nposition = Vector2(100, 100)\n\n[node name="Wall" type="Node2D" parent="."]\nposition = Vector2(200, 100)\n'

root = Path(tempfile.mkdtemp(prefix="cascade-demo-")) / "game"
(root / "scenes").mkdir(parents=True); (root / "scripts").mkdir()
(root / "project.godot").write_text('config_version=5\n[application]\nconfig/name="demo"\n')
(root / "scenes/main.tscn").write_text(SCENE)
(root / "scripts/player.gd").write_text("extends Node2D\n")

reg = Registry()
reg.add_subnet(SN, world=AABB(0, 0, 1000, 1000))
reg.allocate("%Player", SN, ip="10.1.1.10", bounds=AABB(100, 100, 32, 32))
reg.allocate("%Wall", SN, ip="10.1.1.14", bounds=AABB(200, 100, 32, 200))

handlers = {**DEFAULT_HANDLERS, "write_script": lambda f, p, c: Plan((FileChange.text(p["path"], p["body"]),))}
engine = GodotVerifier(os.environ["GODOT_BIN"]) if os.environ.get("GODOT_BIN") else MockVerifier()
cascade = Cascade(root, reg, handlers, engine, registry_path=root.parent / "state_registry.json",
                  mule_validator=mule.MuleValidator(allowed_ops=set(handlers), require_standard_exclusions=True))
pin = {"subnet": SN, "target": "%Player", "ref": "10.1.1.10"}


def model(op, **kw):                                  # what a local model would emit
    return json.dumps({"frame_id": "0x0001", "time_slot": "SLOT_3_EXECUTE", "subnet": SN, "op": op,
                       "target": "%Player", "ref": "10.1.1.10", "exclude": STD, **kw}, separators=(",", ":"))


runs = [
    ("move next to the wall", model("set_position"), {"scene": "scenes/main.tscn", "x": 168, "y": 100}),
    ("move INTO the wall", model("set_position"), {"scene": "scenes/main.tscn", "x": 190, "y": 100}),
    ("model wraps JSON in fences", "```json\n" + model("set_position") + "\n```", {}),
    ("model invents an op", model("delete_everything"), {}),
    ("write a script with a syntax error", model("write_script"), {"path": "scripts/ai/enemy.gd", "body": "extends Node2D\nfunc go( -> int\n"}),
    ("write a good script", model("write_script"), {"path": "scripts/ai/enemy.gd", "body": "extends Node2D\n"}),
]
for name, raw, params in runs:
    r = cascade.run_raw(raw, params, expect=pin)
    print(f"{name:38} {r.status:16} {r.detail[:40]}")
print("\nproject:", root)
print("journal :", cascade.tx.status())
