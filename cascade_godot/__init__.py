"""Journaled, verified, rollback-safe execution of validated Mule frames against a Godot project."""
from .errors import CascadeError, LockError, PlanError, RollbackConflict, TxError
from .journal import Journal
from .ops_tscn import DEFAULT_HANDLERS, add_script_resource, attach_script, set_node_property, set_position
from .pipeline import Cascade, Report
from .pipeline_types import Context, Plan
from .transaction import ABSENT, FileChange, TransactionManager, TxResult, sha256_bytes, sha256_file
from .verify import GodotVerifier, MockVerifier, VerifyResult, find_godot

__version__ = "0.1.0"
__all__ = [
    "ABSENT", "Cascade", "CascadeError", "Context", "DEFAULT_HANDLERS", "FileChange", "GodotVerifier", "Journal",
    "LockError", "MockVerifier", "PlanError", "Plan", "Report", "RollbackConflict", "TransactionManager", "TxError",
    "TxResult", "VerifyResult", "add_script_resource", "attach_script", "find_godot", "set_node_property",
    "set_position", "sha256_bytes", "sha256_file",
]
