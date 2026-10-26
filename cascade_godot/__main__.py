"""CLI.  python -m cascade_godot status|recover|verify|fixtures|import ...    Exit: 0 ok, 1 failed, 2 error."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from subnetbus import PathGuard

from . import CascadeError, FixtureVerifier, GodotVerifier, TransactionManager, VerifyResult, import_project, load_fixtures


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="cascade_godot")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("status", "recover"):
        s = sub.add_parser(name)
        s.add_argument("root")
    s = sub.add_parser("verify")
    s.add_argument("root"); s.add_argument("files", nargs="+")
    s.add_argument("--godot"); s.add_argument("--timeout", type=float, default=60.0)
    s = sub.add_parser("fixtures", help="run runtime fixtures from a JSON config against a project")
    s.add_argument("root"); s.add_argument("config"); s.add_argument("--godot"); s.add_argument("--timeout", type=float, default=30.0)
    s.add_argument("--fail-fast", action="store_true"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("import", help="godot --headless --import (needed before textures such as PNGs can load)")
    s.add_argument("root"); s.add_argument("--godot"); s.add_argument("--timeout", type=float, default=300.0)
    a = p.parse_args(argv)
    try:
        if a.cmd == "verify":
            r: VerifyResult = GodotVerifier(a.godot, timeout=a.timeout).verify(Path(a.root).resolve(), a.files)
            print(json.dumps({"ok": r.ok, "checked": r.checked, "errors": list(r.errors), "seconds": round(r.duration_s, 2)}))
            return 0 if r.ok else 1
        if a.cmd == "fixtures":
            r = FixtureVerifier(load_fixtures(a.config), a.godot, timeout=a.timeout, run_all=not a.fail_fast).verify_all(Path(a.root).resolve())
            if a.json:
                print(json.dumps({"ok": r.ok, "fixtures": r.checked, "seconds": round(r.duration_s, 2), "errors": list(r.errors),
                                  "checks": [{"fixture": c.fixture, "name": c.name, "ok": c.ok, "detail": c.detail} for c in r.checks]}))
            else:
                for c in r.checks:
                    print(f"{'ok  ' if c.ok else 'FAIL'} {c.fixture}/{c.name}" + (f"  {c.detail}" if not c.ok else ""))
                for e in r.errors:
                    if not any(e.startswith(f"{c.fixture}/{c.name}:") for c in r.checks if not c.ok):
                        print(f"FAIL {e}")
                print(f"{'PASS' if r.ok else 'FAIL'}: {r.checked} fixture(s) in {r.duration_s:.1f}s")
            return 0 if r.ok else 1
        if a.cmd == "import":
            run = import_project(Path(a.root).resolve(), a.godot, timeout=a.timeout)
            print(f"import {'timed out' if run.timed_out else 'exit ' + str(run.code)} in {run.duration_s:.1f}s")
            return 0 if run.code == 0 and not run.timed_out else 1
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
