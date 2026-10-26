# cascade-godot

Run a validated Mule frame against a Godot 4 project **without ever leaving it half-written or broken.**

```
 model text ─► MuleValidator ─► slot policy ─► GI_1/GI_2 ─► handler (harness code) ─► GI_1..GI_3 on real paths
   (mule-protocol)               SLOT_3 only    exists?       builds FileChange[]       overlap? symlink? ../ ?
                                                same subnet?
                                                                                              │
          ┌───────────────────────────────────────────────────────────────────────────────────┘
          ▼
   BEGIN ─► backup ─► stage temp files ─► STAGED ─► verdict still valid? ─► APPLYING ─► atomic renames
                                                                                              │
                                                              VERIFYING ◄─────────────────────┘
                                                                  │  godot --headless --script verify.gd
                                                    ┌─────────────┴─────────────┐
                                                  pass                        fail / crash / timeout
                                                    ▼                           ▼
                                               COMMITTED                   ROLLED_BACK  (byte-for-byte)
                                          registry updated              registry untouched
```

The model picks an **op name**. It never supplies paths or file contents. Those come from handlers (your code).

## Quickstart
```bash
pip install -e ../engine-telemetry-bus -e ../mule-protocol -e ".[dev]"
GODOT_BIN=/path/to/Godot_v4.3-stable pytest -q       # without GODOT_BIN the 16 real-engine tests skip
GODOT_BIN=/path/to/Godot_v4.3-stable python examples/demo.py
```
```python
from mule import MuleValidator
from cascade_godot import Cascade, DEFAULT_HANDLERS, GodotVerifier
cascade = Cascade(project_dir, registry, DEFAULT_HANDLERS, GodotVerifier("/path/to/godot"),
                  mule_validator=MuleValidator(allowed_ops=set(DEFAULT_HANDLERS)))
report = cascade.run_raw(model_text, params={"scene": "scenes/main.tscn", "x": 168, "y": 100},
                         expect={"target": "%Player", "ref": "10.1.1.10", "subnet": "10.1.1.0/24"})
report.status      # COMMITTED | REJECTED_BLOCK | REJECTED_GATE | VERIFY_FAILED | STALE_VERDICT | ...
```
After a crash: `python -m cascade_godot status PROJECT` and `python -m cascade_godot recover PROJECT`
(the next `run` also recovers by itself).

## What the engine check proves (`verify.gd`)
Every `.gd` compiles. Every `.tscn` loads, instantiates, has no node whose script failed to load or compile,
no node with a missing parent. `project.godot` parses, is config_version 5, and each autoload exists.
Any `ERROR:` / `WARNING:` line in the engine log also fails the check (`ignore_patterns` to allow known noise).
It does **not** run `_ready()` or play the game. Behaviour fixtures (movement, collision, camera) are the next layer.

## Rules
| | |
|---|---|
| Files | `.gd`, `.tscn`, and `project.godot` only. No dotfiles, no `.so/.gdextension/.sh/.cfg`, UTF-8, no NUL, 1 MiB each, 16 files |
| Writes | temp file in the same directory, fsync, rename, fsync directory. Mode preserved |
| Rollback | decided per file from SHA-256, so it is idempotent. A file that matches neither old nor new content is **never overwritten**: `NEEDS_ATTENTION` |
| Concurrency | one transaction per project (OS lock). `expect_sha256` rejects plans built on stale reads |
| Journal | `.cascade/` inside the project (has a `.gdignore`). Last 20 transactions kept |

## Read this before you run it
- **The engine executes project code.** Loading a project runs its autoloads, and loading scripts runs `_init()`.
  Run verification in a sandbox: `GodotVerifier(..., command_prefix=[...])` (bubblewrap, firejail, container).
  The prefix hook is tested; specific sandbox recipes are not.
- **Not atomic across files while in flight.** Atomicity is by rollback. An editor open on the project can see a partial state.
- **Tested on Godot 4.3 on Linux.** 4.4+ and Windows/macOS are untested. Locking has a Windows branch that has never run.
- **Writes and creates only.** No deletes or moves yet.
- Registry drift: if a commit succeeds but saving the registry fails you get `COMMITTED_REGISTRY_ERROR`. Re-sync it.
- Add `.cascade/` to the game project's `.gitignore`.
