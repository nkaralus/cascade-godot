"""Journaled, hash-verified, crash-safe multi-file transactions.

  plan -> BEGIN(journal) -> back up originals -> stage temp files -> STAGED
       -> precommit check -> APPLYING -> atomic renames -> VERIFYING -> engine check
       -> COMMITTED, or roll back to the exact original bytes.

Guarantees
  * Each file is replaced atomically (temp file in the same directory, fsync, rename, fsync dir).
  * The set of files is all-or-nothing by ROLLBACK: after a crash or failed verify the next run
    restores every file from backups. While a transaction is in flight, a reader (e.g. an open
    editor) can see a partial state.
  * Rollback decides per file from content hashes, so it is idempotent. If a file matches neither
    the original nor the new hash someone else changed it: we stop and raise RollbackConflict.
  * One transaction at a time per project (OS file lock).

Not covered: deletes (v1 writes and creates only), concurrent editors without the lock,
power loss on filesystems that lie about fsync.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from subnetbus import BusError, PathGuard, lexical_segments

from .errors import CascadeError, LockError, RollbackConflict, TxError
from .journal import Journal, fsync_dir
from .verify import VerifyResult

try:                                   # POSIX
    import fcntl
except ImportError:                    # Windows
    fcntl = None                       # type: ignore
    import msvcrt                      # type: ignore

ABSENT = "absent"
MAX_FILES = 16
MAX_FILE_BYTES = 1 << 20
MAX_TOTAL_BYTES = 8 << 20
STATE_DIR = ".cascade"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class FileChange:
    path: str                                  # project-relative
    content: bytes
    expect_sha256: Optional[str] = None        # None: no check. ABSENT: must not exist. else: current sha

    @classmethod
    def text(cls, path: str, text: str, expect_sha256: Optional[str] = None) -> "FileChange":
        return cls(path, text.encode("utf-8"), expect_sha256)


@dataclass(frozen=True)
class TxResult:
    txid: str
    status: str                                # COMMITTED | ROLLED_BACK | NOOP
    reason: str = ""
    verify: Optional[VerifyResult] = None
    files: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in ("COMMITTED", "NOOP")


class _Abort(Exception):
    """Controlled refusal before/around apply. Caught inside run(), triggers rollback."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Lock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: Optional[int] = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise LockError("PROJECT_LOCKED", "another cascade transaction is running") from None
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)                  # closing releases the lock, also on a hard crash
            self.fd = None


class TransactionManager:
    def __init__(self, guard: PathGuard, verifier, *, keep: int = 20, sidecar_bak: bool = False,
                 extra_suffixes: Sequence[str] = (), fault: Optional[Callable[[str], None]] = None):
        self.guard = guard
        self.root = guard.root
        self.verifier = verifier
        self.keep = keep
        self.sidecar_bak = sidecar_bak
        self.suffixes = (".gd", ".tscn", *extra_suffixes)
        self.fault = fault
        self.state = self.root / STATE_DIR
        self.txroot = self.state / "tx"

    # ---- public ------------------------------------------------------------
    def recover(self) -> List[str]:
        """Roll back every unfinished transaction. Returns the txids it rolled back."""
        with self._locked():
            return self._recover_locked()

    def status(self) -> List[Tuple[str, str]]:
        if not self.txroot.is_dir():
            return []
        return [(d.name, Journal(d).state()) for d in sorted(self.txroot.iterdir()) if d.is_dir()]

    def run(self, changes: Sequence[FileChange], *, label: str = "",
            also_verify: Sequence[str] = (), precommit: Optional[Callable[[], bool]] = None) -> TxResult:
        with self._locked():
            self._recover_locked()
            txid = f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:12]}"
            files, created_dirs, contents = self._plan(changes, txid)
            if not files:
                return TxResult(txid, "NOOP", "all files already have the requested content")
            verify_list = self._verify_list([f["rel"] for f in files], also_verify)
            txdir = self.txroot / txid
            txdir.mkdir(parents=True)
            journal = Journal(txdir)
            journal.append({"t": "BEGIN", "v": 1, "txid": txid, "label": str(label)[:64],
                            "ts": datetime.now(timezone.utc).isoformat(), "files": files,
                            "created_dirs": created_dirs})
            self._f("after_begin")
            rels = tuple(f["rel"] for f in files)
            try:
                self._backup(txdir, files)
                self._f("after_backup")
                self._stage(files, created_dirs, contents)
                journal.append({"t": "STAGED"})
                self._f("after_stage")
                if precommit is not None and not precommit():
                    raise _Abort("STALE_VERDICT")
                journal.append({"t": "APPLYING"})
                self._apply(files)
                journal.append({"t": "VERIFYING"})
                self._f("before_verify")
                result = self.verifier.verify(self.root, verify_list)
                self._f("after_verify")
            except _Abort as a:
                self._rollback(txdir, a.reason)
                return TxResult(txid, "ROLLED_BACK", a.reason, files=rels)
            except RollbackConflict:
                raise
            except Exception as e:             # noqa: BLE001 - anything unexpected must still roll back
                self._rollback(txdir, f"ERROR:{type(e).__name__}")
                raise TxError("TX_FAILED", f"{type(e).__name__} (rolled back)") from e
            if not result.ok:
                self._rollback(txdir, "VERIFY_FAILED")
                return TxResult(txid, "ROLLED_BACK", "VERIFY_FAILED", result, rels)
            self._f("before_terminal")
            journal.append({"t": "COMMITTED"})
            self._prune()
            return TxResult(txid, "COMMITTED", "", result, rels)

    # ---- locking / faults ---------------------------------------------------
    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.state.mkdir(exist_ok=True)
        gi = self.state / ".gdignore"
        if not gi.exists():
            gi.write_bytes(b"")
        self.txroot.mkdir(exist_ok=True)
        with _Lock(self.state / "lock"):
            yield

    def _f(self, name: str) -> None:
        if self.fault:
            self.fault(name)

    # ---- planning -------------------------------------------------------------
    def _allowed(self, rel: str) -> bool:
        return rel == "project.godot" or rel.endswith(self.suffixes)

    def _verify_list(self, rels: Sequence[str], extra: Sequence[str]) -> List[str]:
        out = list(dict.fromkeys([*rels, *extra]))
        for r in out:
            try:
                lexical_segments(r)
            except BusError as e:
                raise TxError("BAD_VERIFY_PATH", f"{r!r}: {e.code}") from None
            if not self._allowed(r):
                raise TxError("DISALLOWED_FILE_TYPE", r)
        return out

    def _plan(self, changes: Sequence[FileChange], txid: str) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, bytes]]:
        if not changes:
            raise TxError("EMPTY_PLAN")
        if len(changes) > MAX_FILES:
            raise TxError("TOO_MANY_FILES", str(MAX_FILES))
        files: List[Dict[str, Any]] = []
        contents: Dict[str, bytes] = {}
        seen = set()
        total = 0
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        short = txid[-12:]
        new_dirs: List[str] = []
        for ch in changes:
            if not isinstance(ch, FileChange) or type(ch.content) is not bytes or type(ch.path) is not str:
                raise TxError("BAD_CHANGE", "changes must be FileChange(path: str, content: bytes)")
            try:
                lexical_segments(ch.path)
            except BusError as e:
                raise TxError(e.code, e.detail) from None
            if not self._allowed(ch.path):
                raise TxError("DISALLOWED_FILE_TYPE", ch.path)
            if len(ch.content) > MAX_FILE_BYTES:
                raise TxError("FILE_TOO_LARGE", ch.path)
            total += len(ch.content)
            if total > MAX_TOTAL_BYTES:
                raise TxError("TX_TOO_LARGE", str(MAX_TOTAL_BYTES))
            if b"\x00" in ch.content:
                raise TxError("BAD_CONTENT", f"{ch.path}: NUL byte")
            try:
                ch.content.decode("utf-8")
            except UnicodeDecodeError:
                raise TxError("BAD_CONTENT", f"{ch.path}: not UTF-8") from None
            if ch.path.endswith(".tscn") and not ch.content.startswith(b"[gd_scene"):
                raise TxError("BAD_CONTENT", f"{ch.path}: not a scene file")
            target = self.root / ch.path
            exists = os.path.lexists(target)
            if exists:
                st = os.lstat(target)
                if not stat.S_ISREG(st.st_mode):
                    raise TxError("NOT_REGULAR_FILE", ch.path)
            try:
                real = self.guard.check(ch.path, "overwrite" if exists else "create")
            except BusError as e:
                raise TxError(e.code, e.detail) from None
            if real in seen:
                raise TxError("DUPLICATE_PATH", ch.path)
            seen.add(real)
            cur = sha256_file(real) if exists else None
            if ch.expect_sha256 is not None:
                want = None if ch.expect_sha256 == ABSENT else ch.expect_sha256
                if cur != want:
                    raise TxError("STALE_PLAN", f"{ch.path} changed since it was read")
            new_sha = sha256_bytes(ch.content)
            if cur == new_sha:
                continue                                         # no-op
            i = len(files)
            rec = {"rel": ch.path, "existed": exists, "orig_sha": cur, "new_sha": new_sha,
                   "mode": (stat.S_IMODE(os.lstat(real).st_mode) if exists else 0o644),
                   "backup": f"orig/{i}" if exists else None,
                   "tmp": f".{real.name}.cascade-{short}.tmp", "rtmp": f".{real.name}.cascade-{short}.restore",
                   "sidecar": (f"{real.name}.{ts}-{short[-6:]}.bak" if exists and self.sidecar_bak else None)}
            files.append(rec)
            contents[ch.path] = ch.content
            p = real.parent
            missing = []
            while not p.exists():
                missing.append(p)
                p = p.parent
            for d in reversed(missing):
                rel = str(d.relative_to(self.root))
                if rel not in new_dirs:
                    new_dirs.append(rel)
        return files, new_dirs, contents

    # ---- phases ------------------------------------------------------------------
    def _backup(self, txdir: Path, files: List[Dict[str, Any]]) -> None:
        (txdir / "orig").mkdir()
        for f in files:
            if not f["existed"]:
                continue
            src = self.root / f["rel"]
            dst = txdir / f["backup"]
            shutil.copyfile(src, dst)
            with open(dst, "rb") as fh:
                os.fsync(fh.fileno())
            if sha256_file(dst) != f["orig_sha"]:
                raise TxError("BACKUP_MISMATCH", f["rel"])
            if f["sidecar"]:
                side = src.parent / f["sidecar"]
                shutil.copyfile(src, side)
                with open(side, "rb") as fh:
                    os.fsync(fh.fileno())
        fsync_dir(txdir / "orig")
        fsync_dir(txdir)

    def _stage(self, files: List[Dict[str, Any]], created_dirs: List[str], contents: Dict[str, bytes]) -> None:
        for d in created_dirs:
            os.mkdir(self.root / d, 0o755)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        parents = set()
        for f in files:
            target = self.root / f["rel"]
            tmp = target.parent / f["tmp"]
            fd = os.open(tmp, flags, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(contents[f["rel"]])
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, f["mode"])
            parents.add(target.parent)
        for p in parents:
            fsync_dir(p)

    def _apply(self, files: List[Dict[str, Any]]) -> None:
        parents = set()
        for i, f in enumerate(files):
            target = self.root / f["rel"]
            if os.path.islink(target):
                raise TxError("SYMLINK_TARGET", f["rel"])
            os.replace(target.parent / f["tmp"], target)
            parents.add(target.parent)
            self._f(f"after_apply:{i}")
        for p in parents:
            fsync_dir(p)

    # ---- rollback / recovery -----------------------------------------------------
    def _recover_locked(self) -> List[str]:
        done: List[str] = []
        if not self.txroot.is_dir():
            return done
        for d in sorted(self.txroot.iterdir()):
            if not d.is_dir():
                continue
            j = Journal(d)
            st = j.state()
            if st == "EMPTY":
                shutil.rmtree(d, ignore_errors=True)       # crashed before BEGIN reached disk: nothing to undo
            elif st == "OPEN":
                self._rollback(d, "RECOVERED_AFTER_CRASH")
                done.append(d.name)
        return done

    def _rollback(self, txdir: Path, reason: str) -> None:
        journal = Journal(txdir)
        begin = journal.read()[0]
        txid = begin["txid"]
        conflicts: List[str] = []
        for f in reversed(begin["files"]):
            target = self.root / f["rel"]
            cur = sha256_file(target)
            if cur == f["new_sha"]:                            # we applied it: undo
                if f["existed"]:
                    backup = txdir / f["backup"]
                    if sha256_file(backup) != f["orig_sha"]:
                        journal.append({"t": "NEEDS_ATTENTION", "reason": "BACKUP_CORRUPT", "conflicts": [f["rel"]]})
                        raise TxError("BACKUP_CORRUPT", f"tx {txid}: {f['rel']}")
                    rtmp = target.parent / f["rtmp"]
                    shutil.copyfile(backup, rtmp)
                    with open(rtmp, "rb") as fh:
                        os.fsync(fh.fileno())
                    os.chmod(rtmp, f["mode"])
                    os.replace(rtmp, target)
                else:
                    os.unlink(target)
            elif cur != f["orig_sha"]:                         # neither old nor new: not ours to touch
                conflicts.append(f["rel"])
            for name in (f["tmp"], f["rtmp"]):
                try:
                    os.unlink(target.parent / name)
                except FileNotFoundError:
                    pass
            if f["sidecar"] and f["rel"] not in conflicts:
                try:
                    os.unlink(target.parent / f["sidecar"])
                except FileNotFoundError:
                    pass
            fsync_dir(target.parent)
        for d in reversed(begin.get("created_dirs", [])):
            try:
                os.rmdir(self.root / d)
            except OSError:
                pass
        if conflicts:
            journal.append({"t": "NEEDS_ATTENTION", "reason": reason, "conflicts": conflicts})
            raise RollbackConflict(txid, conflicts)
        journal.append({"t": "ROLLED_BACK", "reason": reason})

    def _prune(self) -> None:
        done = [d for d in sorted(self.txroot.iterdir()) if d.is_dir() and Journal(d).state() in ("COMMITTED", "ROLLED_BACK")]
        for d in done[:-self.keep] if self.keep > 0 else done:
            shutil.rmtree(d, ignore_errors=True)
