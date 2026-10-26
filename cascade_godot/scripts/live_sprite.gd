extends "fixture_base.gd"
## live_sprite: every Sprite2D / AnimatedSprite2D has something valid to draw, and is allowed to be drawn.
## Texture: present, loadable (imported), size within limits. Frames: in range. Region: inside the texture.
## Render state: visible in tree, not transparent, not scaled to nothing. Headless cannot judge pixels.


func fixture_name() -> String:
	return "live_sprite"


func _run() -> void:
	var inst := await begin_scenario(String(spec.get("scene", "")))
	if inst == null:
		return
	await step(int(spec.get("warmup", 3)))
	var nodes: Array = []
	var refs: Array = spec.get("nodes", [])
	if refs.is_empty():
		nodes.append_array(inst.find_children("*", "Sprite2D", true, false))
		nodes.append_array(inst.find_children("*", "AnimatedSprite2D", true, false))
	else:
		for r in refs:
			var n := find_node(inst, String(r))
			if n == null:
				return
			nodes.append(n)
	var min_nodes := int(spec.get("min_nodes", 1))
	check("sprites_present", nodes.size() >= min_nodes, "found %d sprite nodes, need >= %d" % [nodes.size(), min_nodes])
	var minv := _vec(spec.get("min_size", [1, 1]))
	var maxv := _vec(spec.get("max_size", [4096, 4096]))
	var expect = spec.get("expect_size", null)
	var require_visible := bool(spec.get("require_visible", true))
	for n in nodes:
		var label := String(inst.get_path_to(n))
		var tex: Texture2D = null
		if n is Sprite2D:
			tex = n.texture
			if tex == null:
				check(label + ".texture", false, "texture is null (missing file, or not imported: run `godot --import`)")
			else:
				_check_texture(label, tex, minv, maxv, expect)
				var frames: int = n.hframes * n.vframes
				check(label + ".frame", n.frame >= 0 and n.frame < frames, "frame %d, valid range 0..%d" % [n.frame, frames - 1])
				if n.region_enabled:
					var r: Rect2 = n.region_rect
					var inside := r.size.x > 0 and r.size.y > 0 and Rect2(Vector2.ZERO, tex.get_size()).encloses(r)
					check(label + ".region", inside, "region_rect %s must lie inside the %s texture" % [str(r), str(tex.get_size())])
		elif n is AnimatedSprite2D:
			var sf: SpriteFrames = n.sprite_frames
			if sf == null:
				check(label + ".sprite_frames", false, "sprite_frames is null")
			elif not sf.has_animation(n.animation):
				check(label + ".animation", false, "animation '%s' does not exist (have %s)" % [n.animation, str(sf.get_animation_names())])
			else:
				var count := sf.get_frame_count(n.animation)
				check(label + ".frames", count > 0, "animation '%s' has %d frames" % [n.animation, count])
				if count > 0:
					check(label + ".frame", n.frame >= 0 and n.frame < count, "frame %d, valid range 0..%d" % [n.frame, count - 1])
					var ft := sf.get_frame_texture(n.animation, clampi(n.frame, 0, count - 1))
					if ft == null:
						check(label + ".texture", false, "current frame has no texture")
					else:
						_check_texture(label, ft, minv, maxv, expect)
		var ci := n as CanvasItem
		if require_visible:
			check(label + ".visible", ci.is_visible_in_tree(), "node is hidden (itself or an ancestor)")
			check(label + ".opaque", ci.modulate.a > 0.0 and ci.self_modulate.a > 0.0, "alpha is 0 (modulate %.2f, self_modulate %.2f)" % [ci.modulate.a, ci.self_modulate.a])
			var gs: Vector2 = ci.get_global_transform().get_scale()
			check(label + ".scale", is_finite_v(gs) and absf(gs.x) > 0.0001 and absf(gs.y) > 0.0001, "global scale %s" % str(gs))


func _check_texture(label: String, tex: Texture2D, minv: Vector2, maxv: Vector2, expect) -> void:
	var s := tex.get_size()
	check(label + ".size", s.x >= minv.x and s.y >= minv.y and s.x <= maxv.x and s.y <= maxv.y,
		"texture is %s, allowed %s .. %s" % [str(s), str(minv), str(maxv)], {"w": fin(s.x), "h": fin(s.y)})
	if expect is Array and expect.size() == 2:
		check(label + ".expected_size", s == _vec(expect), "texture is %s, expected %s" % [str(s), str(_vec(expect))])


func _vec(a) -> Vector2:
	return Vector2(float(a[0]), float(a[1]))
