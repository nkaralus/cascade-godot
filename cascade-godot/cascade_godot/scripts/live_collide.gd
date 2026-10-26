extends "fixture_base.gd"
## live_collide: drive a mover into static bodies under real physics ticks and judge the response.
## Penetration is measured EVERY tick through the physics server, so a mover that ignores collisions
## (position += ...) is caught even if it never "looks" wrong at the end.


func fixture_name() -> String:
	return "live_collide"


func _run() -> void:
	var ticks := int(spec.get("ticks", 120))
	var max_pen := float(spec.get("max_penetration", 0.081))
	var min_travel := float(spec.get("min_travel", 5.0))
	var stop_window := int(spec.get("stop_window", 20))
	var stop_eps := float(spec.get("stop_epsilon", 0.5))
	var static_tol := float(spec.get("static_tolerance", 0.001))
	var toward_a: Array = spec.get("toward", [1, 0])
	var toward := Vector2(float(toward_a[0]), float(toward_a[1])).normalized()

	var inst := await begin_scenario(String(spec.get("scene", "")))
	if inst == null:
		return
	var mover := find_node(inst, String(spec.get("mover", ""))) as CollisionObject2D
	if mover == null:
		fail("mover must be a CollisionObject2D")
		return
	var statics: Array = []
	for ref in spec.get("statics", []):
		var s := find_node(inst, String(ref)) as Node2D
		if s == null:
			return
		statics.append(s)
	if statics.is_empty():
		fail("spec lists no static bodies")
		return
	await step(int(spec.get("warmup", 3)))

	var start_center := body_center(mover)
	var start_pos := mover.global_position
	var side0: Array = []
	var static_start: Array = []
	for s in statics:
		side0.append(signf((start_center - body_center(s)).dot(toward)))
		static_start.append(s.global_position)
	if not set_actions(spec.get("actions", []), true):
		return

	var worst_pen := 0.0
	var tunneled := ""
	var travel := 0.0
	var history: Array = []
	for i in ticks:
		await step(1)
		var c := body_center(mover)
		if not is_finite_v(c):
			check("finite_position", false, "mover position became NaN or infinite")
			return
		worst_pen = maxf(worst_pen, penetration_depth(mover))
		for k in statics.size():
			var side := signf((c - body_center(statics[k])).dot(toward))
			if side != 0.0 and side0[k] != 0.0 and side != side0[k] and tunneled == "":
				tunneled = "%s (tick %d)" % [statics[k].name, i]
		travel = maxf(travel, (mover.global_position - start_pos).dot(toward))
		history.append(mover.global_position.dot(toward))

	check("reaches_obstacle", travel >= min_travel, "mover travelled %.2f px toward the obstacle, need >= %.2f" % [travel, min_travel],
		{"travel": fin(travel)})
	check("no_tunneling", tunneled == "", "mover passed through " + tunneled if tunneled != "" else "")
	check("penetration", worst_pen <= max_pen, "deepest overlap %.4f px, limit %.4f px" % [worst_pen, max_pen],
		{"max_penetration": fin(worst_pen)})
	var window: int = mini(stop_window, history.size() - 1)
	var late_motion := absf(history[history.size() - 1] - history[history.size() - 1 - window]) if window > 0 else 0.0
	check("stops_at_obstacle", late_motion <= stop_eps, "moved %.3f px toward the obstacle in the last %d ticks, limit %.3f" % [late_motion, window, stop_eps],
		{"late_motion": fin(late_motion)})
	var drift := 0.0
	for k in statics.size():
		drift = maxf(drift, statics[k].global_position.distance_to(static_start[k]))
	check("static_unmoved", drift <= static_tol, "static body moved %.5f px, limit %.5f" % [drift, static_tol], {"drift": fin(drift)})

	var slide = spec.get("slide_actions", null)
	if slide is Array and slide.size() > 0:
		# Phase 2, same scenario: still pressed against the obstacle, ADD the tangent input and watch what happens.
		var before := mover.global_position
		var depth_before := toward_progress(mover, toward)
		var slide_pen := 0.0
		if not set_actions(slide, true):
			return
		for i in int(spec.get("slide_ticks", 30)):
			await step(1)
			slide_pen = maxf(slide_pen, penetration_depth(mover))
		var moved := mover.global_position - before
		var tangent := absf(moved.dot(Vector2(-toward.y, toward.x)))
		var into := maxf(0.0, toward_progress(mover, toward) - depth_before)
		var min_t := float(spec.get("min_tangent", 5.0))
		check("slides_along_obstacle", tangent >= min_t, "slid %.2f px along the obstacle, need >= %.2f" % [tangent, min_t], {"tangent": fin(tangent)})
		check("slide_stays_in_contact", into <= stop_eps, "pushed %.3f px further into the obstacle while sliding, limit %.3f" % [into, stop_eps], {"into": fin(into)})
		check("slide_penetration", slide_pen <= max_pen, "deepest overlap while sliding %.4f px, limit %.4f px" % [slide_pen, max_pen], {"max_penetration": fin(slide_pen)})


func toward_progress(mover: Node2D, toward: Vector2) -> float:
	return mover.global_position.dot(toward)
