import os
import stat
import subprocess
import sys
import textwrap

import pytest

from cascade_godot import (ABSENT, FileChange, Journal, LockError, MockVerifier, RollbackConflict,
                           TransactionManager, TxError, sha256_bytes)
from cascade_godot import transaction as tx_mod
from conftest import PLAYER_GD, SimulatedCrash, crash_at, snapshot, stray_temp_files

NEW_PLAYER = "extends Node2D\n\n# changed\nfunc speed() -> int:\n\treturn 250\n"
ENEMY = "extends Node2D\n"


def mgr(guard, verifier=None, **kw):
    return TransactionManager(guard, verifier or MockVerifier(), **kw)


def two_changes():
    return [FileChange.text("scripts/player.gd", NEW_PLAYER), FileChange.text("scripts/ai/deep/enemy.gd", ENEMY)]


# ---- happy path --------------------------------------------------------------------------
def test_commit_applies_all_files_and_verifier_sees_them(root, guard):
    v = MockVerifier()
    m = mgr(guard, v)
    r = m.run(two_changes(), label="0x0001")
    assert r.status == "COMMITTED" and r.ok and set(r.files) == {"scripts/player.gd", "scripts/ai/deep/enemy.gd"}
    assert (root / "scripts/player.gd").read_text() == NEW_PLAYER
    assert (root / "scripts/ai/deep/enemy.gd").read_text() == ENEMY
    assert v.seen[0]["scripts/player.gd"] == NEW_PLAYER.encode()       # engine ran AFTER the renames
    assert stat.S_IMODE(os.stat(root / "scripts/player.gd").st_mode) == 0o755    # mode preserved
    assert stray_temp_files(root) == []
    assert m.status() == [(r.txid, "COMMITTED")]
    assert (root / ".cascade" / ".gdignore").exists()


def test_noop_when_content_is_identical(root, guard):
    v = MockVerifier()
    r = mgr(guard, v).run([FileChange.text("scripts/player.gd", PLAYER_GD)])
    assert r.status == "NOOP" and r.ok and v.calls == []


def test_also_verify_is_passed_to_the_engine(root, guard):
    v = MockVerifier()
    mgr(guard, v).run([FileChange.text("scripts/player.gd", NEW_PLAYER)], also_verify=["scenes/main.tscn"])
    assert v.calls == [("scripts/player.gd", "scenes/main.tscn")]


# ---- rollback ----------------------------------------------------------------------------
def test_verify_failure_restores_every_byte(root, guard):
    before = snapshot(root)
    v = MockVerifier(default_ok=False)
    r = mgr(guard, v).run(two_changes())
    assert r.status == "ROLLED_BACK" and r.reason == "VERIFY_FAILED" and not r.ok
    assert v.seen[0]["scripts/player.gd"] == NEW_PLAYER.encode()       # it really was applied when verified
    assert snapshot(root) == before                                    # files, modes, and created dirs all gone
    assert not (root / "scripts/ai").exists()
    assert stray_temp_files(root) == []


def test_verifier_exception_rolls_back_and_raises(root, guard):
    before = snapshot(root)

    def boom(*a):
        raise RuntimeError("engine exploded")

    with pytest.raises(TxError) as e:
        mgr(guard, MockVerifier(boom)).run(two_changes())
    assert e.value.code == "TX_FAILED"
    assert snapshot(root) == before


def test_precommit_refusal_touches_nothing(root, guard):
    before = snapshot(root)
    v = MockVerifier()
    r = mgr(guard, v).run(two_changes(), precommit=lambda: False)
    assert r.status == "ROLLED_BACK" and r.reason == "STALE_VERDICT"
    assert v.calls == [] and snapshot(root) == before and stray_temp_files(root) == []


def test_failure_between_renames_rolls_back_the_first(root, guard, monkeypatch):
    before = snapshot(root)
    real = os.replace
    n = {"calls": 0}

    def flaky(a, b, *x, **k):
        n["calls"] += 1
        if n["calls"] == 2:
            raise OSError("disk says no")
        return real(a, b, *x, **k)

    monkeypatch.setattr(tx_mod.os, "replace", flaky)
    with pytest.raises(TxError):
        mgr(guard).run(two_changes())
    assert snapshot(root) == before


def test_sidecar_bak_is_created_on_commit_and_removed_on_rollback(root, guard):
    mgr(guard, sidecar_bak=True).run([FileChange.text("scripts/player.gd", NEW_PLAYER)])
    baks = list((root / "scripts").glob("player.gd.*.bak"))
    assert len(baks) == 1 and baks[0].read_text() == PLAYER_GD
    before = snapshot(root)
    mgr(guard, MockVerifier(default_ok=False), sidecar_bak=True).run([FileChange.text("scripts/player.gd", "extends Node\n")])
    assert snapshot(root) == before


# ---- plan checks -------------------------------------------------------------------------
def test_stale_plan_is_refused_before_anything_happens(root, guard):
    m = mgr(guard)
    with pytest.raises(TxError) as e:
        m.run([FileChange.text("scripts/player.gd", NEW_PLAYER, expect_sha256="0" * 64)])
    assert e.value.code == "STALE_PLAN" and m.status() == []
    good = sha256_bytes(PLAYER_GD.encode())
    assert m.run([FileChange.text("scripts/player.gd", NEW_PLAYER, expect_sha256=good)]).ok
    with pytest.raises(TxError):
        m.run([FileChange.text("scripts/player.gd", "x", expect_sha256=ABSENT)])
    assert m.run([FileChange.text("scripts/fresh.gd", ENEMY, expect_sha256=ABSENT)]).ok


REJECTS = [
    ("libevil.so", b"x", "DISALLOWED_FILE_TYPE"), ("run.sh", b"x", "DISALLOWED_FILE_TYPE"),
    ("export_presets.cfg", b"x", "DISALLOWED_FILE_TYPE"), ("project.godot.bak", b"x", "DISALLOWED_FILE_TYPE"),
    ("a.gdextension", b"x", "DISALLOWED_FILE_TYPE"), ("scripts/x.gd.import", b"x", "DISALLOWED_FILE_TYPE"),
    ("../x.gd", b"x", "PATH_TRAVERSAL"), ("/etc/x.gd", b"x", "PATH_ABSOLUTE"), ("C:/x.gd", b"x", "PATH_SCHEME"),
    (".hidden.gd", b"x", "PATH_SEGMENT"), (".cascade/tx/x.gd", b"x", "PATH_SEGMENT"), ("scripts/CON.gd", b"x", "PATH_RESERVED_NAME"),
    ("x.gd", b"a\x00b", "BAD_CONTENT"), ("x.gd", b"\xff\xfe", "BAD_CONTENT"), ("x.tscn", b"not a scene", "BAD_CONTENT"),
    ("x.gd", b"a" * ((1 << 20) + 1), "FILE_TOO_LARGE"),
]


@pytest.mark.parametrize("path,content,code", REJECTS, ids=[r[0] + ":" + r[2] for r in REJECTS])
def test_plan_rejections(root, guard, path, content, code):
    before = snapshot(root)
    with pytest.raises(TxError) as e:
        mgr(guard).run([FileChange(path, content)])
    assert e.value.code == code
    assert snapshot(root) == before


def test_structural_plan_rejections(root, guard, tmp_path):
    m = mgr(guard)
    with pytest.raises(TxError) as e:
        m.run([FileChange.text("scripts/a.gd", "1"), FileChange.text("scripts/a.gd", "2")])
    assert e.value.code == "DUPLICATE_PATH"
    with pytest.raises(TxError) as e:
        m.run([FileChange.text(f"scripts/f{i}.gd", "x") for i in range(17)])
    assert e.value.code == "TOO_MANY_FILES"
    with pytest.raises(TxError) as e:
        m.run([])
    assert e.value.code == "EMPTY_PLAN"
    with pytest.raises(TxError) as e:
        m.run(["scripts/a.gd"])
    assert e.value.code == "BAD_CHANGE"
    (root / "scripts" / "sub.gd").mkdir()
    with pytest.raises(TxError) as e:
        m.run([FileChange.text("scripts/sub.gd", "x")])
    assert e.value.code == "NOT_REGULAR_FILE"
    outside = tmp_path / "outside.gd"
    outside.write_text("secret")
    os.symlink(outside, root / "scripts" / "link.gd")
    with pytest.raises(TxError) as e:
        m.run([FileChange.text("scripts/link.gd", "pwn")])
    assert e.value.code == "NOT_REGULAR_FILE" and outside.read_text() == "secret"
    os.symlink(tmp_path, root / "scripts" / "dirlink")
    with pytest.raises(TxError) as e:
        m.run([FileChange.text("scripts/dirlink/pwn.gd", "pwn")])
    assert e.value.code == "SYMLINK" and not (tmp_path / "pwn.gd").exists()


def test_project_godot_is_writable_but_nothing_else_at_root(root, guard):
    assert mgr(guard).run([FileChange.text("project.godot", "config_version=5\n")]).ok


# ---- crash recovery ----------------------------------------------------------------------
LABELS = ["after_begin", "after_backup", "after_stage", "after_apply:0", "after_apply:1",
          "before_verify", "after_verify", "before_terminal"]


@pytest.mark.parametrize("label", LABELS)
def test_crash_at_every_step_recovers_to_the_original(root, guard, label):
    before = snapshot(root)
    with pytest.raises(SimulatedCrash):
        mgr(guard, fault=crash_at(label)).run(two_changes())
    if label.startswith("after_apply") or label in ("before_verify", "after_verify", "before_terminal"):
        assert snapshot(root) != before                     # sanity: the crash really left partial/applied state
    m = mgr(guard)
    assert m.status()[0][1] == "OPEN"
    done = m.recover()
    assert len(done) == 1 and snapshot(root) == before and stray_temp_files(root) == []
    assert m.status()[0][1] == "ROLLED_BACK"
    assert m.recover() == []                                # idempotent
    assert m.run(two_changes()).status == "COMMITTED"       # and the project is usable again


@pytest.mark.parametrize("label", ["after_stage", "after_apply:0", "before_verify"])
def test_next_run_recovers_implicitly(root, guard, label):
    before = snapshot(root)
    with pytest.raises(SimulatedCrash):
        mgr(guard, fault=crash_at(label)).run(two_changes())
    r = mgr(guard).run([FileChange.text("scripts/other.gd", ENEMY)])
    assert r.status == "COMMITTED"
    after = snapshot(root)
    after.pop("f:scripts/other.gd")
    assert after == before


def test_recovery_refuses_to_clobber_a_file_someone_else_changed(root, guard):
    with pytest.raises(SimulatedCrash):
        mgr(guard, fault=crash_at("after_apply:1")).run(two_changes())
    (root / "scripts/player.gd").write_text("# edited by a human in the editor\n")
    m = mgr(guard)
    with pytest.raises(RollbackConflict) as e:
        m.recover()
    assert e.value.conflicts == ("scripts/player.gd",)
    assert (root / "scripts/player.gd").read_text() == "# edited by a human in the editor\n"
    assert not (root / "scripts/ai/deep/enemy.gd").exists()     # the file we created was still undone
    assert m.status()[0][1] == "NEEDS_ATTENTION"
    assert m.run([FileChange.text("scripts/other.gd", ENEMY)]).ok     # does not wedge the project forever


def test_torn_journal_tail_still_recovers(root, guard):
    before = snapshot(root)
    with pytest.raises(SimulatedCrash):
        mgr(guard, fault=crash_at("after_stage")).run(two_changes())
    jp = next((root / ".cascade/tx").iterdir()) / "journal.jsonl"
    with open(jp, "ab") as f:
        f.write(b'{"t":"APPLY')
    assert len(mgr(guard).recover()) == 1 and snapshot(root) == before


def test_corrupt_journal_stops_everything(root, guard):
    with pytest.raises(SimulatedCrash):
        mgr(guard, fault=crash_at("after_stage")).run(two_changes())
    jp = next((root / ".cascade/tx").iterdir()) / "journal.jsonl"
    with open(jp, "ab") as f:
        f.write(b"garbage\n")
        f.write(b'{"t":"STAGED"}\n')
    with pytest.raises(TxError) as e:
        mgr(guard).run(two_changes())
    assert e.value.code == "JOURNAL_CORRUPT"


def test_empty_tx_dir_from_crash_before_begin_is_cleaned(root, guard):
    (root / ".cascade/tx/0000000000001-aaaaaaaaaaaa").mkdir(parents=True)
    assert mgr(guard).run(two_changes()).ok
    assert [s for _, s in mgr(guard).status()] == ["COMMITTED"]


def test_hard_process_death_mid_verify(root, guard, tmp_path):
    before = snapshot(root)
    code = textwrap.dedent("""
        import os, sys
        from subnetbus import PathGuard
        from cascade_godot import TransactionManager, FileChange
        class V:
            def verify(self, root, files): os._exit(9)
        TransactionManager(PathGuard(sys.argv[1], mountpoints=()), V()).run(
            [FileChange.text("scripts/player.gd", "extends Node2D\\n# changed\\n"), FileChange.text("scripts/new.gd", "extends Node\\n")])
    """)
    p = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True)
    assert p.returncode == 9
    assert (root / "scripts/player.gd").read_text().endswith("# changed\n")      # really applied
    m = mgr(guard)
    assert m.status()[0][1] == "OPEN"
    assert len(m.recover()) == 1 and snapshot(root) == before


def test_lock_blocks_a_second_transaction(root, guard):
    a, b = mgr(guard), mgr(guard)
    with a._locked():
        with pytest.raises(LockError):
            b.run(two_changes())
        with pytest.raises(LockError):
            b.recover()
    assert b.run(two_changes()).ok


def test_archive_is_pruned(root, guard):
    m = mgr(guard, keep=3)
    for i in range(6):
        assert m.run([FileChange.text("scripts/player.gd", f"extends Node2D\n# v{i}\n")]).ok
    assert len(m.status()) == 3


@pytest.mark.parametrize("extra,code", [("libevil.so", "DISALLOWED_FILE_TYPE"), ("../x.gd", "BAD_VERIFY_PATH"),
                                        ("/etc/x.gd", "BAD_VERIFY_PATH")])
def test_also_verify_paths_are_checked_too(root, guard, extra, code):
    before = snapshot(root)
    with pytest.raises(TxError) as e:
        mgr(guard).run([FileChange.text("scripts/player.gd", NEW_PLAYER)], also_verify=[extra])
    assert e.value.code == code and snapshot(root) == before
