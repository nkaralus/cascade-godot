extends SceneTree
## Shared plumbing for Cascade runtime fixtures. Subclasses override fixture_name() and _run() (a coroutine).
##
##   godot --headless --path <project> --fixed-fps 60 --script <fixture>.gd -- '<spec json>'
##
## Prints exactly one line:  CASCADE_RESULT {"ok":bool,"fixture":str,"ticks":int,"checks":[...],"errors":[...]}
## Exit code 0 = every check passed, 1 = anything else. A fixture that crashes prints no result line, which the
## Python side treats as a failure.
##
## Unlike verify.gd this DOES run game code: _ready and _physics_process execute. Run it sandboxed.
##
## Why a SubViewport: the headless window is clamped to 64x64 and `root.size` cannot be changed, so camera math
## on the root viewport is meaningless. Each scenario gets its own SubViewport of the real project size (and its
## own physics space), which also isolates scenarios from each other.

const MARK := "CASCADE_RESULT "
const MAX_TICKS_TOTAL := 60000

var spec: Dictionary = {}
var checks: Array = []
var errors: Array = []
var ticks_run := 0
var tick_rate := 60.0
var host: SubViewport = null
var _held: Dictionary = {}
var _done := false


func fixture_name() -> String:
	return "fixture"


func _run() -> void:
	fail("fixture did not implement _run")


func _initialize() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() != 1:
		fail("expected exactly one JSON spec argument, got %d" % args.size())
	else:
		var parsed = JSON.parse_string(String(args[0]))
		if typeof(parsed) != TYPE_DICTIONARY:
			fail("spec is not a JSON object")
		else:
			spec = parsed
			tick_rate = float(Engine.physics_ticks_per_second)
			await _run()
	_finish()


# ---- reporting ---------------------------------------------------------------------------------
func fail(msg: String) -> void:
	errors.append(msg)


func check(name: String, ok: bool, detail: String = "", measured: Dictionary = {}) -> void:
	checks.append({"name": name, "ok": ok, "detail": detail, "measured": measured})


func fin(x: float) -> float:
	return x if is_finite(x) else -1.0


func _finish() -> void:
	if _done:
		return
	_done = true
	var ok := errors.is_empty()
	for c in checks:
		if not c["ok"]:
			ok = false
	if checks.is_empty():
		ok = false
		errors.append("fixture ran no checks")
	print(MARK + JSON.stringify({"ok": ok, "fixture": fixture_name(), "ticks": ticks_run,
		"tick_rate": tick_rate, "checks": checks, "errors": errors}))
	quit(0 if ok else 1)


# ---- scenarios -----------------------------------------------------------------------------------
func viewport_size() -> Vector2i:
	var vp = spec.get("viewport", null)
	if vp is Array and vp.size() == 2:
		return Vector2i(int(vp[0]), int(vp[1]))
	return Vector2i(int(ProjectSettings.get_setting("display/window/size/viewport_width", 1152)),
		int(ProjectSettings.get_setting("display/window/size/viewport_height", 648)))


## Fresh SubViewport + freshly instanced scene. Returns null (and records an error) if the scene cannot load.
func begin_scenario(scene_path: String) -> Node:
	release_all()
	if host != null:
		host.queue_free()
		host = null
		await physics_frame
	var res = load("res://" + scene_path)
	if res == null or not (res is PackedScene):
		fail("cannot load scene res://" + scene_path)
		return null
	var inst: Node = res.instantiate()
	if inst == null:
		fail("scene failed to instantiate: " + scene_path)
		return null
	host = SubViewport.new()
	host.size = viewport_size()
	host.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	root.add_child(host)
	host.add_child(inst)
	return inst


## Node by name ("Player" or "%Player": must be unique in the scene) or by path ("Arena/Player").
func find_node(inst: Node, ref: String) -> Node:
	if ref == "":
		fail("empty node reference")
		return null
	if "/" in ref:
		var n := inst.get_node_or_null(NodePath(ref))
		if n == null:
			fail("node path not found: " + ref)
		return n
	var name := ref.trim_prefix("%")
	var found: Array = inst.find_children(name, "", true, false)
	if inst.name == name:
		found.append(inst)
	if found.size() == 0:
		fail("node not found: " + ref)
		return null
	if found.size() > 1:
		fail("node name is ambiguous (%d matches): %s" % [found.size(), ref])
		return null
	return found[0]


func step(n: int = 1) -> void:
	for i in n:
		await physics_frame
		ticks_run += 1
		if ticks_run > MAX_TICKS_TOTAL and not _done:
			fail("tick budget exceeded (%d)" % MAX_TICKS_TOTAL)
			_finish()
			return


# ---- input -----------------------------------------------------------------------------------------
## Press or release actions.
## State goes through Input.action_press/action_release: Input.parse_input_event(InputEventAction) keeps ONLY the
## most recent action, so two held keys (a diagonal) would silently become one. The event is then pushed into the
## scenario's SubViewport so _input/_unhandled_input handlers see it (they do not receive Input-level events there).
func set_actions(actions: Array, pressed: bool) -> bool:
	var ok := true
	for a in actions:
		var action := String(a)
		if not InputMap.has_action(action):
			fail("input action '%s' is not defined in the project's Input Map" % action)
			ok = false
			continue
		if pressed:
			Input.action_press(action, 1.0)
		else:
			Input.action_release(action)
		var ev := InputEventAction.new()
		ev.action = action
		ev.pressed = pressed
		ev.strength = 1.0 if pressed else 0.0
		if host != null:
			host.push_input(ev, true)
		_held[action] = pressed
	return ok


func release_all() -> void:
	var held: Array = []
	for a in _held.keys():
		if _held[a]:
			held.append(a)
	if held.size() > 0:
		set_actions(held, false)


# ---- measurement ---------------------------------------------------------------------------------------
func is_finite_v(v: Vector2) -> bool:
	return is_finite(v.x) and is_finite(v.y)


## Mean global position of a body's enabled collision shapes (its physical centre), else its origin.
func body_center(body: Node2D) -> Vector2:
	var sum := Vector2.ZERO
	var n := 0
	for c in body.get_children():
		if c is CollisionShape2D and not c.disabled:
			sum += c.global_position
			n += 1
	return sum / n if n > 0 else body.global_position


## Deepest overlap (px) between any of the body's shapes and any other physics body in its world.
func penetration_depth(body: CollisionObject2D) -> float:
	var space := body.get_world_2d().direct_space_state
	var worst := 0.0
	for c in body.get_children():
		if c is CollisionShape2D and not c.disabled and c.shape != null:
			var q := PhysicsShapeQueryParameters2D.new()
			q.shape = c.shape
			q.transform = c.global_transform
			q.exclude = [body.get_rid()]
			q.collision_mask = 0xFFFFFFFF
			var pts := space.collide_shape(q, 64)
			var i := 0
			while i + 1 < pts.size():
				worst = max(worst, (pts[i] as Vector2).distance_to(pts[i + 1]))
				i += 2
	return worst
