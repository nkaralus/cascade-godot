"""Journaled, verified, rollback-safe execution of validated Mule frames against a Godot project."""
from .errors import CascadeError, LockError, PlanError, RollbackConflict, TxError
from .journal import Journal
from .ops_tscn import DEFAULT_HANDLERS, add_script_resource, attach_script, set_node_property, set_position
from .pipeline import Cascade, Report
from .pipeline_types import Context, Plan
from .transaction import ABSENT, FileChange, TransactionManager, TxResult, sha256_bytes, sha256_file
from .engine import EngineRunner, find_godot, import_project
from .fixtures import (CameraFixture, CollideFixture, Fixture, FixtureVerifier, MoveCase, MoveFixture, SpriteFixture,
                       fixture_from_dict, load_fixtures, staged_verifier)
from .verify import CheckResult, GodotVerifier, MockVerifier, StagedVerifier, VerifyResult

__version__ = "0.2.0"
__all__ = [
    "CameraFixture", "CollideFixture", "EngineRunner", "Fixture", "FixtureVerifier", "MoveCase", "MoveFixture", "SpriteFixture",
    "StagedVerifier", "CheckResult", "fixture_from_dict", "import_project", "load_fixtures", "staged_verifier",
    "ABSENT", "Cascade", "CascadeError", "Context", "DEFAULT_HANDLERS", "FileChange", "GodotVerifier", "Journal",
    "LockError", "MockVerifier", "PlanError", "Plan", "Report", "RollbackConflict", "TransactionManager", "TxError",
    "TxResult", "VerifyResult", "add_script_resource", "attach_script", "find_godot", "set_node_property",
    "set_position", "sha256_bytes", "sha256_file",
]
