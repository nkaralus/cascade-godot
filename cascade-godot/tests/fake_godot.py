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


# ---- runtime-fixture modes (selected when the script is a live_*.gd) ----------------------------------
if mode.startswith("fx_"):
    script = args[args.index("--script") + 1]
    kind = os.path.basename(script)[:-3]
    spec = json.loads(files[0]) if files else {}
    chk = lambda n, ok=True, d="": {"name": n, "ok": ok, "detail": d, "measured": {}}
    out = {"ok": True, "fixture": kind, "ticks": 10, "tick_rate": 60.0, "checks": [chk("a"), chk("b")], "errors": []}
    code = 0
    if mode == "fx_fail":
        out["checks"][1] = chk("b", False, "went wrong"); out["ok"] = False; code = 1
    elif mode == "fx_zero_checks":
        out["checks"] = []
    elif mode == "fx_claims_ok":
        out["checks"][1] = chk("b", False, "went wrong")
    elif mode == "fx_wrong_name":
        out["fixture"] = "live_other"
    elif mode == "fx_bad_exit":
        code = 1
    elif mode == "fx_errors_listed":
        out["errors"] = ["node not found: Player"]; out["ok"] = False; code = 1
    elif mode == "fx_error_log":
        print("SCRIPT ERROR: Invalid access to property")
    elif mode == "fx_no_result":
        print("silence"); sys.exit(139)
    elif mode == "fx_bad_json":
        print(M + "{nope"); sys.exit(0)
    elif mode == "fx_flood":
        for _ in range(60000):
            print("noise line " + "n" * 100)
    elif mode == "fx_silent_fail":
        out["checks"] = []; out["ok"] = False; code = 1
    elif mode == "fx_malformed_check":
        out["checks"] = [{"name": "a", "ok": "yes", "detail": ""}]
    elif mode == "fx_echo_spec":
        out["checks"] = [chk("spec", False, json.dumps({"scene": spec.get("scene"), "ticks": spec.get("ticks"), "normalize": spec.get("normalize"), "ncases": len(spec.get("cases", []))}))]; out["ok"] = False; code = 1
    elif mode == "fx_timeout":
        if pidfile:
            child = subprocess.Popen(["sleep", "60"]); open(pidfile, "w").write(str(child.pid))
        time.sleep(60)
    print(M + json.dumps(out)); sys.exit(code)

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

