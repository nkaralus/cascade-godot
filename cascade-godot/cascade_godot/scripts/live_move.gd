extends "fixture_base.gd"
## live_move: does the thing move the way the game says it should?
## Per case (held actions): it moves, in the expected direction, never faster than max_speed, and comes to rest
## after release. With normalize=true it also checks diagonal speed against cardinal speed (8-way input).


func fixture_name() -> String:
	return "live_move"


func _run() -> void:
	var cases: Array = spec.get("cases", [])
	if cases.is_empty():
		fail("spec has no cases")
		return
	var ticks := int(spec.get("ticks", 60))
	var warmup := int(spec.get("warmup", 3))
	var min_distance := float(spec.get("min_distance", 1.0))
	var angle_tol := float(spec.get("angle_tolerance_deg", 15.0))
	var speed_tol := float(spec.get("speed_tolerance", 0.05))
	var max_speed = spec.get("max_speed", null)
	var settle_ticks := int(spec.get("settle_ticks", 60))
	var settle_hold := int(spec.get("settle_hold", 10))
	var settle_eps := float(spec.get("settle_epsilon", 0.05))
	var steady_by_actions := {}

	for c in cases:
		var cname := String(c.get("name", "case"))
		var actions: Array = c.get("actions", [])
		var inst := await begin_scenario(String(spec.get("scene", "")))
		if inst == null:
			return
		var node := find_node(inst, String(spec.get("node", ""))) as Node2D
		if node == null:
			return
		await step(warmup)
		var start := node.global_position
		if not set_actions(actions, true):
			return
		var prev := start
		var speeds: Array = []
		var finite := true
		for i in ticks:
			await step(1)
			var p := node.global_position
			if not is_finite_v(p):
				finite = false
				break
			speeds.append(prev.distance_to(p) * tick_rate)
			prev = p
		check(cname + ".finite_position", finite, "" if finite else "position became NaN or infinite")
		if not finite:
			continue
		var d := prev - start
		var dist := d.length()
		check(cname + ".moved", dist >= min_distance, "moved %.2f px, need >= %.2f" % [dist, min_distance],
			{"distance": fin(dist)})
		var dir = c.get("dir", null)
		if dir is Array and dir.size() == 2 and dist > 0.0001:
			var ang := absf(rad_to_deg(d.angle_to(Vector2(float(dir[0]), float(dir[1])))))
			check(cname + ".direction", ang <= angle_tol, "%.1f deg off the expected direction, tolerance %.1f" % [ang, angle_tol],
				{"angle_deg": fin(ang)})
		var peak := 0.0
		for s in speeds:
			peak = maxf(peak, s)
		if max_speed != null:
			var limit := float(max_speed) * (1.0 + speed_tol)
			check(cname + ".speed_bound", peak <= limit, "peak %.1f px/s, limit %.1f px/s" % [peak, limit], {"peak_speed": fin(peak)})
		var steady := 0.0
		var half: int = speeds.size() / 2
		for i in range(half, speeds.size()):
			steady += speeds[i]
		steady = steady / maxf(1.0, float(speeds.size() - half))
		if not steady_by_actions.has(actions.size()):
			steady_by_actions[actions.size()] = []
		steady_by_actions[actions.size()].append(steady)
		# release and watch it come to rest
		set_actions(actions, false)
		var quiet := 0
		var last_disp := 0.0
		var rest_prev := node.global_position
		for i in settle_ticks:
			await step(1)
			var q := node.global_position
			last_disp = rest_prev.distance_to(q)
			rest_prev = q
			quiet = quiet + 1 if last_disp < settle_eps else 0
		check(cname + ".settles", quiet >= settle_hold,
			"quiet for the last %d ticks (need %d); last tick moved %.3f px" % [quiet, settle_hold, last_disp],
			{"quiet_ticks": quiet, "last_displacement": fin(last_disp)})

	if bool(spec.get("normalize", false)):
		if not (steady_by_actions.has(1) and steady_by_actions.has(2)):
			check("normalization", false, "needs cardinal (1 action) and diagonal (2 actions) cases")
			return
		var card := _mean(steady_by_actions[1])
		var diag := _mean(steady_by_actions[2])
		var tol := float(spec.get("normalize_tolerance", 0.05))
		var ratio := diag / card if card > 0.0001 else -1.0
		check("normalization", ratio > 0.0 and absf(ratio - 1.0) <= tol,
			"diagonal/cardinal speed ratio %.3f, expected 1.000 +/- %.3f (cardinal %.1f px/s, diagonal %.1f px/s)" % [ratio, tol, card, diag],
			{"ratio": fin(ratio), "cardinal": fin(card), "diagonal": fin(diag)})


func _mean(a: Array) -> float:
	var s := 0.0
	for v in a:
		s += v
	return s / maxf(1.0, float(a.size()))
