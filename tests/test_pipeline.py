import os

import pytest

from subnetbus import AABB, Registry
from cascade_godot import (DEFAULT_HANDLERS, Cascade, FileChange, MockVerifier, Plan, PlanError, TxError, set_node_property)
from conftest import SCENE, SN, frame, snapshot


def make(root, reg, verifier=None, handlers=None, **kw):
    return Cascade(root, reg, handlers or DEFAULT_HANDLERS, verifier or MockVerifier(), mountpoints=(), **kw)


P = {"scene": "scenes/main.tscn", "x": 168, "y": 100}


def test_set_position_commits_and_updates_the_registry(root, reg, tmp_path):
    v = MockVerifier()
    rp = tmp_path / "state_registry.json"
    c = make(root, reg, v, registry_path=rp)
    rev = reg.revision
    r = c.run(frame(), P)
    assert r.status == "COMMITTED" and r.ok
    assert 'position = Vector2(168, 100)' in (root / "scenes/main.tscn").read_text()
    assert reg.get("%Player").bounds == AABB(168, 100, 32, 32) and reg.revision > rev
    assert Registry.load(rp).get("%Player").bounds == AABB(168, 100, 32, 32)          # persisted
    assert v.calls == [("scenes/main.tscn",)] and [x.ok for x in r.verdicts] == [True]


def test_overlap_is_stopped_before_any_disk_access(root, reg):
    before = snapshot(root)
    v = MockVerifier()
    r = make(root, reg, v).run(frame(), {**P, "x": 190})
    assert r.status == "REJECTED_GATE" and r.detail == "GI_3 SOLID_OVERLAP" and "%Wall" in r.errors[0]
    assert snapshot(root) == before and v.calls == []
    tx_dir = root / ".cascade/tx"
    assert not tx_dir.exists() or not any(tx_dir.iterdir())            # no transaction was even opened


@pytest.mark.parametrize("fr,status,detail", [
    (frame(time_slot="SLOT_1_FETCH"), "REJECTED_SLOT", ""), (frame(time_slot=None), "REJECTED_SLOT", ""),
    (frame(op="format_disk"), "UNKNOWN_OP", ""),
    (frame(target="%Ghost"), "REJECTED_GATE", "GI_1 UNKNOWN_TARGET"),
    (frame(ref="10.1.1.14"), "REJECTED_GATE", "GI_1 REF_MISMATCH"),
    (frame(subnet="10.9.9.0/24"), "REJECTED_GATE", "GI_1 UNKNOWN_SUBNET"),
    (frame(target="%Foreign", ref="10.1.2.10"), "REJECTED_GATE", "GI_2 CROSS_SUBNET_TARGET"),
    ("not a frame", "REJECTED_BLOCK", ""), ({"op": 5}, "REJECTED_BLOCK", ""),
])
def test_frame_rejections(root, reg, fr, status, detail):
    before = snapshot(root)
    r = make(root, reg).run(fr, P)
    assert r.status == status and not r.ok and (not detail or r.detail == detail)
    assert snapshot(root) == before


@pytest.mark.parametrize("params,code", [
    ({**P, "scene": "../x.tscn"}, "BAD_PARAMS"), ({**P, "scene": "/etc/x.tscn"}, "BAD_PARAMS"), ({**P, "scene": "scenes/x.gd"}, "BAD_PARAMS"),
    ({**P, "x": float("nan")}, "BAD_PARAMS"), ({**P, "x": True}, "BAD_PARAMS"), ({**P, "x": "168"}, "BAD_PARAMS"),
    ({"scene": "scenes/main.tscn"}, "BAD_PARAMS"), ({**P, "scene": "scenes/missing.tscn"}, "UNREADABLE"),
])
def test_bad_params_become_plan_errors(root, reg, params, code):
    r = make(root, reg).run(frame(), params)
    assert r.status == "PLAN_ERROR" and r.detail == code


def test_a_crashing_handler_does_not_crash_the_harness(root, reg):
    def bad(fr, params, ctx):
        raise KeyError("secret internals")
    r = make(root, reg, handlers={"set_position": bad}).run(frame(), P)
    assert r.status == "PLAN_ERROR" and r.detail == "HANDLER_CRASH" and "secret" not in str(r)
    r = make(root, reg, handlers={"set_position": lambda f, p, c: "nope"}).run(frame(), P)
    assert r.status == "PLAN_ERROR" and r.detail == "BAD_PLAN"
    r = make(root, reg, handlers={"set_position": lambda f, p, c: Plan(())}).run(frame(), P)
    assert r.status == "PLAN_ERROR"


def test_gates_still_judge_the_real_paths_even_if_a_handler_is_careless(root, reg):
    before = snapshot(root)
    for path, detail in (("../outside.gd", "GI_2 PATH_TRAVERSAL"), ("/etc/cron.d/x.gd", "GI_2 PATH_ABSOLUTE"), (".cascade/x.gd", "GI_2 PATH_SEGMENT")):
        h = {"set_position": lambda f, p, c, path=path: Plan((FileChange.text(path, "x"),))}
        r = make(root, reg, handlers=h).run(frame(), P)
        assert r.status == "REJECTED_GATE" and r.detail == detail, (path, r)
    h = {"set_position": lambda f, p, c: Plan((FileChange.text("scripts/a.gd", "x"),), refs=("%Foreign",))}
    assert make(root, reg, handlers=h).run(frame(), P).detail == "GI_2 CROSS_SUBNET_REF"
    h = {"set_position": lambda f, p, c: Plan((FileChange.text("scripts/a.gd", "x"),), also_verify=("../x.gd",))}
    assert make(root, reg, handlers=h).run(frame(), P).detail == "GI_2 PATH_TRAVERSAL"
    assert snapshot(root) == before


def test_symlink_in_project_is_refused_by_gate_three(root, reg, tmp_path):
    os.symlink(tmp_path, root / "scripts" / "dirlink")
    h = {"set_position": lambda f, p, c: Plan((FileChange.text("scripts/dirlink/x.gd", "x"),))}
    r = make(root, reg, handlers=h).run(frame(), P)
    assert r.status == "REJECTED_GATE" and r.detail == "GI_3 SYMLINK" and not (tmp_path / "x.gd").exists()


def test_disallowed_file_type_reaches_the_transaction_and_is_refused(root, reg):
    h = {"set_position": lambda f, p, c: Plan((FileChange.text("scripts/payload.so", "x"),))}
    r = make(root, reg, handlers=h).run(frame(), P)
    assert r.status == "TX_ERROR" and r.detail == "DISALLOWED_FILE_TYPE"


def test_verify_failure_rolls_back_files_and_leaves_the_registry_alone(root, reg):
    before, bounds, rev = snapshot(root), reg.get("%Player").bounds, reg.revision
    r = make(root, reg, MockVerifier(default_ok=False)).run(frame(), P)
    assert r.status == "VERIFY_FAILED" and not r.ok and r.tx.status == "ROLLED_BACK"
    assert snapshot(root) == before and reg.get("%Player").bounds == bounds and reg.revision == rev


def test_registry_changing_after_the_gates_aborts_the_commit(root, reg):
    before = snapshot(root)
    v = MockVerifier()
    intruder = lambda name: reg.allocate("%Intruder", SN, bounds=AABB(600, 600, 10, 10)) if name == "after_stage" else None
    r = make(root, reg, v, fault=intruder).run(frame(), P)
    assert r.status == "STALE_VERDICT" and v.calls == [] and snapshot(root) == before


def test_concurrent_file_edit_between_plan_and_commit_is_caught(root, reg):
    def sneaky(fr, params, ctx):
        plan = DEFAULT_HANDLERS["set_position"](fr, params, ctx)
        (root / "scenes/main.tscn").write_text(SCENE + "\n# edited by the editor\n")      # lands after the read
        return plan
    r = make(root, reg, handlers={"set_position": sneaky}).run(frame(), P)
    assert r.status == "TX_ERROR" and r.detail == "STALE_PLAN"
    assert (root / "scenes/main.tscn").read_text().endswith("# edited by the editor\n")


def test_on_commit_failure_is_loud(root, reg):
    def h(fr, params, ctx):
        def boom(_reg):
            raise RuntimeError("disk full")
        return Plan((FileChange.text("scripts/new.gd", "extends Node\n"),), on_commit=boom)
    r = make(root, reg, handlers={"set_position": h}).run(frame(), P)
    assert r.status == "COMMITTED_REGISTRY_ERROR" and not r.ok and r.tx.status == "COMMITTED"
    assert (root / "scripts/new.gd").exists()


def test_attach_script(root, reg):
    v = MockVerifier()
    r = make(root, reg, v).run(frame(op="attach_script"), {"scene": "scenes/main.tscn", "script": "scripts/player.gd"})
    assert r.status == "COMMITTED", r
    text = (root / "scenes/main.tscn").read_text()
    assert 'path="res://scripts/player.gd"' in text and 'load_steps=2' in text
    assert '[node name="Player" type="Node2D" parent="."]\nposition = Vector2(100, 100)\nscript = ExtResource(' in text
    assert v.calls == [("scenes/main.tscn", "scripts/player.gd")]
    assert make(root, reg, v).run(frame(op="attach_script"), {"scene": "scenes/main.tscn", "script": "scripts/player.gd"}).status == "NOOP"


def test_attach_script_requires_an_existing_script(root, reg):
    r = make(root, reg).run(frame(op="attach_script"), {"scene": "scenes/main.tscn", "script": "scripts/ghost.gd"})
    assert r.status == "PLAN_ERROR" and r.detail == "UNREADABLE"


def test_run_raw_needs_a_validator(root, reg):
    with pytest.raises(Exception) as e:
        make(root, reg).run_raw("{}")
    assert e.value.code == "NO_VALIDATOR"
