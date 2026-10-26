extends "fixture_base.gd"
## live_camera: is the camera live, does it keep its target on screen with a margin, does the lag stay bounded
## and converge after the target stops, and does the view stay inside the world?
## Screen position is computed from get_screen_center_position() (which includes smoothing and limits).
## "Lag" is measured against where the camera SHOULD be (target + offset, clamped to the camera limits), so a camera
## that is correctly pinned at a world edge is not mistaken for a slow one. Rotation is ignored.


func fixture_name() -> String:
	return "live_camera"


func _run() -> void:
	var ticks := int(spec.get("ticks", 240))
	var warmup := int(spec.get("warmup", 30))
	var margin_req := float(spec.get("margin", 0.0))
	var settle_ticks := int(spec.get("settle_ticks", 120))
	var final_lag_max := float(spec.get("final_lag", 1.0))
	var inst := await begin_scenario(String(spec.get("scene", "")))
	if inst == null:
		return
	var cam: Camera2D = null
	var cref := String(spec.get("camera", ""))
	if cref != "":
		cam = find_node(inst, cref) as Camera2D
	else:
		var cams: Array = inst.find_children("*", "Camera2D", true, false)
		check("camera_present", cams.size() == 1, "found %d Camera2D nodes, need exactly 1 (or name it in the spec)" % cams.size())
		if cams.size() == 1:
			cam = cams[0]
	if cam == null:
		return
	var target := find_node(inst, String(spec.get("target", ""))) as Node2D
	if target == null:
		return
	await step(warmup)
	var active: bool = cam.enabled and cam.is_current()
	check("camera_active", active, "enabled=%s is_current=%s" % [cam.enabled, cam.is_current()])
	if not active:
		return
	var vp := Vector2(host.size)
	var max_lag := float(spec.get("max_lag", 0.25 * minf(vp.x, vp.y)))
	var world = spec.get("world", null)
	var world_rect := Rect2()
	if world is Array and world.size() == 4:
		world_rect = Rect2(float(world[0]), float(world[1]), float(world[2]), float(world[3]))
	if not set_actions(spec.get("actions", []), true):
		return
	var worst_margin := INF
	var worst_lag := 0.0
	var outside := 0.0
	for i in ticks:
		await step(1)
		var c := cam.get_screen_center_position()
		var tp := target.global_position
		if not (is_finite_v(c) and is_finite_v(tp)):
			check("finite_positions", false, "camera or target position became NaN or infinite")
			return
		var scr := (tp - c) * cam.zoom + vp / 2.0
		worst_margin = minf(worst_margin, minf(minf(scr.x, vp.x - scr.x), minf(scr.y, vp.y - scr.y)))
		worst_lag = maxf(worst_lag, c.distance_to(_ideal_center(cam, tp, vp)))
		if world_rect.size != Vector2.ZERO:
			var half := vp / cam.zoom / 2.0
			var view := Rect2(c - half, half * 2.0)
			outside = maxf(outside, maxf(maxf(world_rect.position.x - view.position.x, view.end.x - world_rect.end.x),
				maxf(world_rect.position.y - view.position.y, view.end.y - world_rect.end.y)))
	check("target_on_screen", worst_margin >= margin_req, "closest the target got to a screen edge: %.1f px, need >= %.1f" % [worst_margin, margin_req],
		{"worst_margin": fin(worst_margin)})
	check("tracking_lag", worst_lag <= max_lag, "worst camera lag %.1f px behind where it should be, limit %.1f px" % [worst_lag, max_lag],
		{"worst_lag": fin(worst_lag)})
	if world_rect.size != Vector2.ZERO:
		check("view_in_world", outside <= 0.5, "view extended %.1f px outside the world" % maxf(outside, 0.0), {"outside": fin(outside)})
	set_actions(spec.get("actions", []), false)
	await step(settle_ticks)
	var final_lag := cam.get_screen_center_position().distance_to(_ideal_center(cam, target.global_position, vp))
	check("converges_when_target_stops", final_lag <= final_lag_max, "camera is %.2f px from where it should rest after %d ticks, limit %.2f" % [final_lag, settle_ticks, final_lag_max],
		{"final_lag": fin(final_lag)})


func _ideal_center(cam: Camera2D, target_pos: Vector2, vp: Vector2) -> Vector2:
	var half := vp / cam.zoom / 2.0
	var want := target_pos + cam.offset
	var lo := Vector2(cam.limit_left, cam.limit_top) + half
	var hi := Vector2(cam.limit_right, cam.limit_bottom) - half
	var out := want
	out.x = (lo.x + hi.x) / 2.0 if lo.x > hi.x else clampf(want.x, lo.x, hi.x)
	out.y = (lo.y + hi.y) / 2.0 if lo.y > hi.y else clampf(want.y, lo.y, hi.y)
	return out
