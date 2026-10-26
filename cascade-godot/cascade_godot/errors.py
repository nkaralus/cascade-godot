"""Error types. Every error carries a machine-readable `code`."""
from __future__ import annotations


class CascadeError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class TxError(CascadeError):
    pass


class LockError(TxError):
    pass


class RollbackConflict(TxError):
    """A file changed under us after the crash/failure. We refuse to overwrite it."""
    def __init__(self, txid: str, conflicts):
        super().__init__("ROLLBACK_CONFLICT", f"tx {txid}: {', '.join(conflicts)}")
        self.txid = txid
        self.conflicts = tuple(conflicts)


class PlanError(CascadeError):
    """A handler could not build a safe plan."""
