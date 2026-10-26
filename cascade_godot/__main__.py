"""CLI.  python -m cascade_godot status|recover|verify ...    Exit: 0 ok, 1 failed, 2 error."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from subnetbus import PathGuard

from . import CascadeError, GodotVerifier, TransactionManager, VerifyResult


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="cascade_godot")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("status", "recover"):
        s = sub.add_parser(name)
        s.add_argument("root")
    s = sub.add_parser("verify")
    s.add_argument("root"); s.add_argument("files", nargs="+")
    s.add_argument("--godot"); s.add_argument("--timeout", type=float, default=60.0)
    a = p.parse_args(argv)
    try:
        if a.cmd == "verify":
            r: VerifyResult = GodotVerifier(a.godot, timeout=a.timeout).verify(Path(a.root).resolve(), a.files)
            print(json.dumps({"ok": r.ok, "checked": r.checked, "errors": list(r.errors), "seconds": round(r.duration_s, 2)}))
            return 0 if r.ok else 1
        tm = TransactionManager(PathGuard(Path(a.root).resolve()), verifier=None)
        if a.cmd == "status":
            rows = tm.status()
            print("\n".join(f"{t}  {s}" for t, s in rows) or "no transactions")
            return 1 if any(s in ("OPEN", "NEEDS_ATTENTION") for _, s in rows) else 0
        done = tm.recover()
        print("rolled back: " + (", ".join(done) if done else "nothing to do"))
        return 0
    except CascadeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:                                   # noqa: BLE001
        print(f"error: {e.__class__.__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
