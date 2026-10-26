#!/usr/bin/env python3
"""Stand-in for the Godot binary. Behaviour chosen with --mode. Never imports anything from the project."""
import json, os, subprocess, sys, time

args = sys.argv[1:]
mode = args[args.index("--mode") + 1] if "--mode" in args else "ok"
pidfile = args[args.index("--pidfile") + 1] if "--pidfile" in args else None
files = args[args.index("--") + 1:] if "--" in args else []
M = "CASCADE_RESULT "


def result(ok=True, checked=None, errors=()):
    print(M + json.dumps({"ok": ok, "checked": len(files) if checked is None else checked, "errors": list(errors)}))


if mode == "ok":
    result()
elif mode == "fail_result":
    result(False, errors=["script does not compile: res://scripts/player.gd"]); sys.exit(1)
elif mode == "no_result":
    print("Godot Engine v4.3 ... but no result line")
elif mode == "crash":
    sys.exit(139)
elif mode == "exit_mismatch":
    result(True); sys.exit(1)
elif mode == "ok_but_nonzero_flag":
    result(False); sys.exit(0)
elif mode == "wrong_count":
    result(True, checked=len(files) + 1)
elif mode == "bad_json":
    print(M + "{not json")
elif mode == "wrong_shape":
    print(M + json.dumps({"ok": "yes", "checked": 1, "errors": []}))
elif mode == "error_log":
    print("SCRIPT ERROR: Parse Error: Expected parameter name.\n          at: GDScript::reload (res://x.gd:2)"); result()
elif mode == "warning_log":
    print("WARNING: Parent path './X' for node 'Player' has vanished when instantiating"); result()
elif mode == "log_after_result":
    result(); print("ERROR: Failed to instantiate an autoload, can't load from path: res://ghost.gd.")
elif mode == "benign_log":
    print("WARNING: something we told the verifier to ignore"); result()
elif mode == "flood":
    sys.stdout.write("x" * 100 + "\n") if False else None
    for _ in range(60000):
        print("noise line " + "n" * 100)
    result()
elif mode == "timeout":
    if pidfile:
        child = subprocess.Popen(["sleep", "60"])
        open(pidfile, "w").write(str(child.pid))
    time.sleep(60)
elif mode == "env":
    result(False, errors=["HOME=" + os.environ.get("HOME", ""), "KEYS=" + ",".join(sorted(os.environ))]); sys.exit(1)
elif mode == "args":
    result(False, errors=["ARGS=" + json.dumps(files)]); sys.exit(1)
