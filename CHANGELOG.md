# Changelog

## 0.1.0
- Transaction layer: write-ahead journal, hash-based idempotent rollback, atomic per-file rename,
  crash recovery at the start of every run, project lock, plan limits and file-type allowlist.
- Engine verify: `verify.gd` (scripts compile, scenes instantiate with working scripts and parents,
  project.godot parses and its autoloads exist) driven by `GodotVerifier` with timeout, process-group
  kill, scrubbed environment, log scan, and fail-closed output cap. Tested on Godot 4.3.
- Pipeline: Mule validator -> time-slot policy -> bus gates GI_1..GI_3 on the real paths -> transaction
  (verdict re-checked before the first rename) -> registry update.
- Reference handlers: `set_position`, `attach_script` (strict .tscn edits).
