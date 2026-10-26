extends SceneTree
## Cascade headless verifier.
## Usage: godot --headless --path <project> --script res://<this file> -- res://a.gd res://b.tscn res://project.godot
## Prints exactly one line:  CASCADE_RESULT {"ok":bool,"checked":int,"errors":[...]}
## Exit code 0 = ok, 1 = failed. It never adds scenes to the tree, so no _ready() code runs.
## NOTE: the engine still runs project autoloads and script _init() on load. Run it sandboxed.

const MARK := "CASCADE_RESULT "
var errors: Array = []


func _init() -> void:
	var checked := 0
	for raw in OS.get_cmdline_user_args():
		var p := String(raw)
		if not p.begins_with("res://") or ".." in p:
			errors.append("bad path argument: " + p)
			continue
		checked += 1
		if p == "res://project.godot":
			_check_project()
		elif p.ends_with(".gd"):
			_check_script(p)
		elif p.ends_with(".tscn"):
			_check_scene(p)
		else:
			errors.append("unsupported file type: " + p)
	if checked == 0:
		errors.append("no files were given to verify")
	print(MARK + JSON.stringify({"ok": errors.is_empty(), "checked": checked, "errors": errors}))
	quit(0 if errors.is_empty() else 1)


func _check_script(p: String) -> void:
	if not FileAccess.file_exists(p):
		errors.append("missing file: " + p)
		return
	var s = load(p)
	if s == null or not (s is GDScript):
		errors.append("not a loadable GDScript: " + p)
	elif not s.can_instantiate():
		errors.append("script does not compile: " + p)


func _check_scene(p: String) -> void:
	if not FileAccess.file_exists(p):
		errors.append("missing file: " + p)
		return
	var ps = load(p)
	if ps == null or not (ps is PackedScene):
		errors.append("not a loadable PackedScene: " + p)
		return
	var st: SceneState = ps.get_state()
	for i in st.get_node_count():
		for j in st.get_node_property_count(i):
			if st.get_node_property_name(i, j) == "script":
				var sc = st.get_node_property_value(i, j)
				if sc == null:
					errors.append("%s: node '%s' has a script that failed to load" % [p, st.get_node_name(i)])
				elif sc is GDScript and not sc.can_instantiate():
					errors.append("%s: node '%s' script does not compile (%s)" % [p, st.get_node_name(i), sc.resource_path])
	var known := {".": true}
	for i in range(1, st.get_node_count()):
		var np := String(st.get_node_path(i))
		var parent := np.get_base_dir()
		if not known.has(parent):
			errors.append("%s: node '%s' has a missing parent '%s'" % [p, st.get_node_name(i), parent])
		known[np] = true
	var inst = ps.instantiate()
	if inst == null:
		errors.append("scene failed to instantiate: " + p)
		return
	var total := 0
	var stack: Array = [inst]
	while stack.size() > 0:
		var n = stack.pop_back()
		total += 1
		for c in n.get_children():
			stack.append(c)
	if total != st.get_node_count():
		errors.append("%s: scene has %d nodes but %d instantiated" % [p, st.get_node_count(), total])
	inst.free()


func _check_project() -> void:
	var cf := ConfigFile.new()
	var err := cf.load("res://project.godot")
	if err != OK:
		errors.append("project.godot does not parse (error %d)" % err)
		return
	if int(cf.get_value("", "config_version", 0)) != 5:
		errors.append("project.godot config_version must be 5 (Godot 4.x)")
	if cf.has_section("autoload"):
		for key in cf.get_section_keys("autoload"):
			var target := String(cf.get_value("autoload", key)).lstrip("*")
			if not ResourceLoader.exists(target):
				errors.append("autoload '%s' points at a missing file: %s" % [key, target])
