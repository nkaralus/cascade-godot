"""Mule validator -> bus gates -> journaled transaction -> engine verify -> registry.
The `real engine` tests need GODOT_BIN. Everything else runs with a mock engine."""
import json

import pytest

from subnetbus import AABB
from cascade_godot import DEFAULT_HANDLERS, Cascade, FileChange, GodotVerifier, MockVerifier, Plan
from conftest import SCENE, SN, snapshot

mule = pytest.importorskip("mule")

STD = ["untyped_variables", "relative_paths_up_tree", "cross_subnet_queries", "freeform_code_blocks", "synthetic_transactions"]
PINNED = {"subnet": SN, "target": "%Player", "ref": "10.1.1.10"}


def raw(**kw):
    b = {"frame_id": "0x0001", "time_slot": "SLOT_3_EXECUTE", "subnet": SN, "op": "set_position",
         "target": "%Player", "ref": "10.1.1.10", "exclude": STD}
    b.update(kw)
    return json.dumps(b, separators=(",", ":"))


def write_script(frame, params, ctx):
    """Test-local handler: the harness (not the model) decides the file body."""
    return Plan((FileChange.text(params["path"], params["body"]),))


def build(root, reg, verifier, **kw):
    handlers = {**DEFAULT_HANDLERS, "write_script": write_script}
    v = mule.MuleValidator(allowed_ops=set(handlers), require_standard_exclusions=True)
    return Cascade(root, reg, handlers, verifier, mule_validator=v, mountpoints=(), **kw)


P = {"scene": "scenes/main.tscn", "x": 168, "y": 100}


# ---- mock engine ----------------------------------------------------------------------------
def test_good_model_output_is_executed(root, reg):
    r = build(root, reg, MockVerifier()).run_raw(raw(), P, expect=PINNED)
    assert r.status == "COMMITTED" and "position = Vector2(168, 100)" in (root / "scenes/main.tscn").read_text()


@pytest.mark.parametrize("text,status,needle", [
    ("```json\n" + raw() + "\n```", "REJECTED_BLOCK", "one line"),                       # formatting slip
    ("Sure! " + raw(), "REJECTED_BLOCK", "preamble"),
    (raw(note="hi"), "REJECTED_BLOCK", "unexpected key"),
    (raw(op="rm_rf"), "REJECTED_BLOCK", "allowed_ops"),                                  # op not in the handler registry
    (raw(exclude=["untyped_variables"]), "REJECTED_BLOCK", "standard exclusions"),
    (raw(target="%Wall"), "REJECTED_BLOCK", "harness value"),                            # model retargeted: pinned by expect
    (raw(ref="10.1.1.99"), "REJECTED_BLOCK", "harness value"),
    (raw(target="%Foreign", ref="10.1.2.10"), "REJECTED_BLOCK", "outside subnet"),         # Mule's own ref-in-subnet check
])
def test_bad_model_output_never_reaches_the_gates(root, reg, text, status, needle):
    before = snapshot(root)
    v = MockVerifier()
    r = build(root, reg, v).run_raw(text, P, expect=PINNED)
    assert r.status == status and needle in " ".join(r.errors), r.errors
    assert snapshot(root) == before and v.calls == []


def test_syntactically_valid_but_ungrounded_output_dies_at_the_gates(root, reg):
    c = build(root, reg, MockVerifier())
    for text, detail in ((raw(target="%Ghost"), "GI_1 UNKNOWN_TARGET"), (raw(ref="10.1.1.99"), "GI_1 REF_MISMATCH"),
                         (raw(target="%Foreign"), "GI_1 REF_MISMATCH")):                    # a node from another subnet, with a forged ref
        r = c.run_raw(text, P)                                                            # no expect: gates are the only defence
        assert r.status == "REJECTED_GATE" and r.detail == detail


def test_model_cannot_choose_the_time_slot_to_skip_the_clock(root, reg):
    assert build(root, reg, MockVerifier()).run_raw(raw(time_slot="SLOT_5_VERIFY"), P).status == "REJECTED_SLOT"


def test_physical_overlap_is_refused_end_to_end(root, reg):
    before = snapshot(root)
    r = build(root, reg, MockVerifier()).run_raw(raw(), {**P, "x": 190}, expect=PINNED)
    assert r.status == "REJECTED_GATE" and r.detail == "GI_3 SOLID_OVERLAP" and snapshot(root) == before


# ---- real engine ----------------------------------------------------------------------------
@pytest.mark.godot
class TestRealEngine:
    def cascade(self, root, reg, godot_bin, **kw):
        return build(root, reg, GodotVerifier(godot_bin, timeout=60), **kw)

    def test_move_then_attach_script_then_everything_still_loads(self, root, reg, godot_bin, tmp_path):
        c = self.cascade(root, reg, godot_bin, registry_path=tmp_path / "reg.json")
        r = c.run_raw(raw(), P, expect=PINNED)
        assert r.status == "COMMITTED", r
        assert r.tx.verify.checked == 1 and r.tx.verify.engine_exit == 0
        r = c.run_raw(raw(op="attach_script"), {"scene": "scenes/main.tscn", "script": "scripts/player.gd"}, expect=PINNED)
        assert r.status == "COMMITTED", r
        assert r.tx.verify.checked == 2                                                   # scene + the script it depends on
        text = (root / "scenes/main.tscn").read_text()
        assert "position = Vector2(168, 100)" in text and "script = ExtResource(" in text
        assert reg.get("%Player").bounds == AABB(168, 100, 32, 32)

    def test_broken_new_script_is_rolled_back_byte_for_byte(self, root, reg, godot_bin):
        before = snapshot(root)
        c = self.cascade(root, reg, godot_bin)
        r = c.run_raw(raw(op="write_script"), {"path": "scripts/ai/enemy.gd", "body": "extends Node2D\nfunc go( -> int\n\treturn 1 +\n"}, expect=PINNED)
        assert r.status == "VERIFY_FAILED" and any("does not compile" in e for e in r.errors), r
        assert snapshot(root) == before and not (root / "scripts/ai").exists()

    def test_attaching_a_broken_script_restores_the_scene(self, root, reg, godot_bin):
        (root / "scripts/enemy.gd").write_text("extends Node2D\nfunc go( -> int\n")        # already broken on disk
        before = snapshot(root)
        c = self.cascade(root, reg, godot_bin)
        r = c.run_raw(raw(op="attach_script"), {"scene": "scenes/main.tscn", "script": "scripts/enemy.gd"}, expect=PINNED)
        assert r.status == "VERIFY_FAILED", r
        assert snapshot(root) == before and (root / "scenes/main.tscn").read_text() == SCENE

    def test_good_new_script_commits(self, root, reg, godot_bin):
        c = self.cascade(root, reg, godot_bin)
        r = c.run_raw(raw(op="write_script"), {"path": "scripts/ai/enemy.gd", "body": "extends Node2D\nfunc go() -> int:\n\treturn 1\n"}, expect=PINNED)
        assert r.status == "COMMITTED", r
        assert (root / "scripts/ai/enemy.gd").exists()

    def test_broken_autoload_in_project_godot_is_caught(self, root, reg, godot_bin):
        before = snapshot(root)
        h = {"write_script": lambda f, p, c: Plan((FileChange.text("project.godot", 'config_version=5\n[autoload]\nGhost="*res://scripts/ghost.gd"\n'),))}
        v = mule.MuleValidator(allowed_ops=set(h))
        c = Cascade(root, reg, h, GodotVerifier(godot_bin), mule_validator=v, mountpoints=())
        r = c.run_raw(raw(op="write_script"), {}, expect=PINNED)
        assert r.status == "VERIFY_FAILED" and any("missing file" in e for e in r.errors), r
        assert snapshot(root) == before

    def test_crash_during_a_real_verify_recovers(self, root, reg, godot_bin):
        from conftest import SimulatedCrash, crash_at
        before = snapshot(root)
        c = self.cascade(root, reg, godot_bin, fault=crash_at("after_verify"))
        with pytest.raises(SimulatedCrash):
            c.run_raw(raw(), P, expect=PINNED)
        assert snapshot(root) != before
        assert len(c.tx.recover()) == 1 and snapshot(root) == before
