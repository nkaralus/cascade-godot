# Changelog

## 0.2.0
- Runtime fixtures (stage 2): `live_move`, `live_collide`, `live_camera`, `live_sprite`, validated Python configs,
  `FixtureVerifier`, `StagedVerifier`, `staged_verifier()`; JSON config loader; `fixtures` and `import` CLI commands.
- `engine.py`: shared process control, result parsing, log scan, `import_project`. `GodotVerifier` now uses it.
- `VerifyResult` gains `stage` and `checks`.
- Tested against Godot 4.3: 276 tests, 75 of them against the real engine.


## 0.1.0
- Transaction layer: write-ahead journal, hash-based idempotent rollback, atomic per-file rename,
  crash recovery at the start of every run, project lock, plan limits and file-type allowlist.
- Engine verify: `verify.gd` (scripts compile, scenes instantiate with working scripts and parents,
  project.godot parses and its autoloads exist) driven by `GodotVerifier` with timeout, process-group
  kill, scrubbed environment, log scan, and fail-closed output cap. Tested on Godot 4.3.
- Pipeline: Mule validator -> time-slot policy -> bus gates GI_1..GI_3 on the real paths -> transaction
  (verdict re-checked before the first rename) -> registry update.
- Reference handlers: `set_position`, `attach_script` (strict .tscn edits).
