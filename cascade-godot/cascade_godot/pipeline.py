"""Mule frame -> bus gates -> journaled transaction -> engine verify -> registry update.

  run_raw(text)  validate with a Mule validator, then run(frame)
  run(frame)     slot policy -> GI_1/GI_2 -> handler plan -> GI_1..GI_3 on the real paths
                 -> transaction (verdict re-checked right before the first rename)
                 -> registry update after COMMIT

The model never supplies file contents or paths. It picks an op name; `params` and the handler
code come from the harness.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from subnetbus import Action, BusError, PathGuard, Registry, Verdict, check_all, lexical_segments

from .errors import CascadeError, RollbackConflict, TxError
from .pipeline_types import Context, Plan
from .transaction import TransactionManager, TxResult

__all__ = ["Cascade", "Report"]

Handler = Callable[[Mapping, Mapping, Context], Plan]


@dataclass(frozen=True)
class Report:
    status: str
    ok: bool
    detail: str = ""
    errors: Tuple[str, ...] = ()
    verdicts: Tuple[Verdict, ...] = ()
    tx: Optional[TxResult] = None


def _no(status: str, detail: str = "", errors: Sequence[str] = (), verdicts: Sequence[Verdict] = (),
        tx: Optional[TxResult] = None) -> Report:
    return Report(status, False, detail, tuple(errors), tuple(verdicts), tx)


class Cascade:
    def __init__(self, root: "str | os.PathLike", registry: Registry, handlers: Mapping[str, Handler], verifier, *,
                 mule_validator=None, registry_path: "Optional[str | os.PathLike]" = None,
                 execute_slots: Sequence[str] = ("SLOT_3_EXECUTE",), sidecar_bak: bool = False,
                 guard: Optional[PathGuard] = None, mountpoints=None, keep: int = 20, fault=None):
        self.guard = guard or PathGuard(root, mountpoints=mountpoints)
        self.root = self.guard.root
        self.registry = registry
        self.handlers = dict(handlers)
        self.validator = mule_validator
        self.registry_path = registry_path
        self.execute_slots = tuple(execute_slots)
        self.tx = TransactionManager(self.guard, verifier, keep=keep, sidecar_bak=sidecar_bak, fault=fault)

    # ---- entry points -----------------------------------------------------------
    def run_raw(self, raw: str, params: Optional[Mapping] = None, expect: Optional[Mapping] = None) -> Report:
        if self.validator is None:
            raise CascadeError("NO_VALIDATOR", "pass mule_validator= to use run_raw")
        ok, block, errs = self.validator.validate_raw(raw, expect=dict(expect) if expect else None)
        if not ok:
            return _no("REJECTED_BLOCK", "model output failed validation", errs)
        return self.run(block, params)

    def run(self, frame: Mapping, params: Optional[Mapping] = None) -> Report:
        if not isinstance(frame, Mapping) or type(frame.get("op")) is not str:
            return _no("REJECTED_BLOCK", "frame must be a mapping with a string op")
        slot = frame.get("time_slot")
        if slot not in self.execute_slots:
            return _no("REJECTED_SLOT", f"time_slot {slot!r} may not execute writes")
        handler = self.handlers.get(frame["op"])
        if handler is None:
            return _no("UNKNOWN_OP", frame["op"][:40])
        pre = check_all(self.registry, frame, fs_guard=self.guard)
        if not pre.ok:
            return _no("REJECTED_GATE", f"{pre.failed.gate} {pre.failed.code}", [pre.failed.detail], [pre])
        try:
            plan = handler(frame, dict(params or {}), Context(self.root, self.registry, self.guard))
        except (CascadeError, BusError) as e:
            return _no("PLAN_ERROR", e.code, [e.detail])
        except Exception as e:                       # noqa: BLE001 - a buggy handler must not crash the harness
            return _no("PLAN_ERROR", "HANDLER_CRASH", [type(e).__name__])
        if not isinstance(plan, Plan) or not plan.changes:
            return _no("PLAN_ERROR", "BAD_PLAN", ["handler must return a Plan with changes"])
        verdicts = self._gate_plan(frame, plan)
        bad = next((v.failed for v in verdicts if not v.ok), None)
        if bad is not None:
            return _no("REJECTED_GATE", f"{bad.gate} {bad.code}", [bad.detail], verdicts)
        try:
            res = self.tx.run(plan.changes, label=str(frame.get("frame_id", "")), also_verify=plan.also_verify,
                              precommit=lambda: all(v.still_valid(self.registry) for v in verdicts))
        except RollbackConflict as e:
            return _no("NEEDS_ATTENTION", e.code, [e.detail], verdicts)
        except TxError as e:
            return _no("TX_ERROR", e.code, [e.detail], verdicts)
        if res.status == "NOOP":
            return Report("NOOP", True, res.reason, (), tuple(verdicts), res)
        if res.status == "ROLLED_BACK":
            status = res.reason if res.reason in ("VERIFY_FAILED", "STALE_VERDICT") else "ROLLED_BACK"
            errs = res.verify.errors if res.verify else ()
            return _no(status, res.reason, errs, verdicts, res)
        try:
            if plan.on_commit is not None:
                plan.on_commit(self.registry)
                if self.registry_path is not None:
                    self.registry.save(self.registry_path)
        except Exception as e:                       # noqa: BLE001
            code = getattr(e, "code", type(e).__name__)
            return _no("COMMITTED_REGISTRY_ERROR", f"files committed but registry not updated: {code}",
                       [str(e)[:200]], verdicts, res)
        return Report("COMMITTED", True, "", (), tuple(verdicts), res)

    # ---- gates ---------------------------------------------------------------------
    def _exists(self, rel: str) -> bool:
        try:
            lexical_segments(rel)
        except BusError:
            return True                              # invalid path: let GI_2 report it as an overwrite
        return os.path.lexists(self.root / rel)

    def _gate_plan(self, frame: Mapping, plan: Plan) -> List[Verdict]:
        groups: Dict[str, List[str]] = {"overwrite": [], "create": [], "read": list(plan.also_verify)}
        for c in plan.changes:
            groups["overwrite" if self._exists(c.path) else "create"].append(c.path)
        out: List[Verdict] = []
        first = True
        for op, paths in groups.items():
            if not paths:
                continue
            act = Action(refs=plan.refs if first else (), paths=tuple(paths), fs_op=op,
                         proposed_bounds=plan.proposed_bounds if first else None)
            first = False
            v = check_all(self.registry, frame, act, fs_guard=self.guard)
            out.append(v)
            if not v.ok:
                break
        return out
