"""A tiny top-down game, written to disk, with switchable behaviour. Used by the runtime-fixture tests."""
from pathlib import Path

GOOD = """extends CharacterBody2D
@export var speed := 200.0
func _physics_process(_delta: float) -> void:
	var dir := Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down")
	velocity = dir * speed
	move_and_slide()
"""
UNNORMALIZED = """extends CharacterBody2D
@export var speed := 200.0
func _physics_process(_delta: float) -> void:
	velocity = Vector2(Input.get_axis("ui_left", "ui_right"), Input.get_axis("ui_up", "ui_down")) * speed
	move_and_slide()
"""
GHOST = """extends CharacterBody2D
@export var speed := 200.0
func _physics_process(delta: float) -> void:
	global_position += Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down") * speed * delta
"""
STUCK = """extends CharacterBody2D
func _physics_process(_delta: float) -> void:
	var dir := Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down")
	velocity = Vector2.ZERO
	move_and_slide()
"""
FAST = GOOD.replace("200.0", "1000.0")
INVERTED = GOOD.replace("velocity = dir * speed", "velocity = -dir * speed")
DRIFT = """extends CharacterBody2D
func _physics_process(_delta: float) -> void:
	velocity += Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down") * 8.0
	move_and_slide()
"""
JITTER = """extends CharacterBody2D
var t := 0
func _physics_process(_delta: float) -> void:
	var dir := Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down")
	t += 1
	if dir == Vector2.ZERO:
		global_position.x += 1.0 if t % 2 == 0 else -1.0
	else:
		velocity = dir * 200.0
		move_and_slide()
"""
HANG = """extends CharacterBody2D
func _physics_process(_delta: float) -> void:
	while true:
		pass
"""
LOGERR = GOOD.replace("move_and_slide()", "move_and_slide()\n\tget_node(\"NoSuchNode\")")
EVENTS = """extends CharacterBody2D
var held := {"ui_left": false, "ui_right": false, "ui_up": false, "ui_down": false}
func _unhandled_input(event: InputEvent) -> void:
	for a in held.keys():
		if event.is_action(a):
			held[a] = event.is_pressed()
func _physics_process(_delta: float) -> void:
	var v := Vector2(float(held["ui_right"]) - float(held["ui_left"]), float(held["ui_down"]) - float(held["ui_up"]))
	velocity = v.normalized() * 200.0
	move_and_slide()
"""
SYNTAX_ERROR = "extends CharacterBody2D\nfunc _physics_process( -> void\n\tpass +\n"

CAMERAS = {
    "follow": '[node name="Camera" type="Camera2D" parent="Player"]\n',
    "smooth": '[node name="Camera" type="Camera2D" parent="Player"]\nposition_smoothing_enabled = true\nposition_smoothing_speed = 5.0\n',
    "static": '[node name="Camera" type="Camera2D" parent="."]\nposition = Vector2(320, 180)\n',
    "off": '[node name="Camera" type="Camera2D" parent="Player"]\nenabled = false\n',
    "two": '[node name="Camera" type="Camera2D" parent="Player"]\n\n[node name="Camera2" type="Camera2D" parent="."]\n',
    "limited": '[node name="Camera" type="Camera2D" parent="Player"]\nlimit_left = 0\nlimit_top = 0\nlimit_right = 1000\nlimit_bottom = 1000\n',
    "drag": '[node name="Camera" type="Camera2D" parent="Player"]\ndrag_horizontal_enabled = true\ndrag_left_margin = 0.3\ndrag_right_margin = 0.3\n',
    "zoom": '[node name="Camera" type="Camera2D" parent="Player"]\nzoom = Vector2(2, 2)\n',
    "smooth_zoom": '[node name="Camera" type="Camera2D" parent="Player"]\nzoom = Vector2(2, 2)\nposition_smoothing_enabled = true\nposition_smoothing_speed = 5.0\n',
    "limited_zoom": '[node name="Camera" type="Camera2D" parent="Player"]\nzoom = Vector2(2, 2)\nlimit_left = 0\nlimit_top = 0\nlimit_right = 1000\nlimit_bottom = 1000\n',
    "none": "",
}
SPRITES = {
    "good": 'texture = SubResource("T")\n',
    "none": "",
    "hidden": 'visible = false\ntexture = SubResource("T")\n',
    "alpha": 'modulate = Color(1, 1, 1, 0)\ntexture = SubResource("T")\n',
    "region": 'texture = SubResource("T")\nregion_enabled = true\nregion_rect = Rect2(20, 20, 50, 50)\n',
    "frame": 'texture = SubResource("T")\nhframes = 2\nframe = 5\n',
    "huge": 'texture = SubResource("Big")\n',
    "scale0": 'texture = SubResource("T")\nscale = Vector2(0, 0)\n',
}


def arena(*, camera="follow", sprite="good", wall=True, player_script="res://scripts/player.gd") -> str:
    parts = ['[gd_scene load_steps=6 format=3]\n',
             f'[ext_resource type="Script" path="{player_script}" id="1_p"]\n',
             '[sub_resource type="RectangleShape2D" id="Rp"]\nsize = Vector2(32, 32)\n',
             '[sub_resource type="RectangleShape2D" id="Rw"]\nsize = Vector2(32, 200)\n',
             '[sub_resource type="PlaceholderTexture2D" id="T"]\nsize = Vector2(32, 32)\n',
             '[sub_resource type="PlaceholderTexture2D" id="Big"]\nsize = Vector2(9000, 32)\n',
             '[node name="Arena" type="Node2D"]\n',
             '[node name="Player" type="CharacterBody2D" parent="."]\nposition = Vector2(100, 100)\nscript = ExtResource("1_p")\n',
             '[node name="PlayerShape" type="CollisionShape2D" parent="Player"]\nshape = SubResource("Rp")\n']
    if sprite != "none":
        parts.append('[node name="Sprite" type="Sprite2D" parent="Player"]\n' + SPRITES[sprite])
    if camera != "none":
        parts.append(CAMERAS[camera])
    if wall:
        parts.append('[node name="Wall" type="StaticBody2D" parent="."]\nposition = Vector2(300, 100)\n')
        parts.append('[node name="WallShape" type="CollisionShape2D" parent="Wall"]\nshape = SubResource("Rw")\n')
    return "\n".join(parts) + "\n"


def make_game(root: Path, *, player=GOOD, camera="follow", sprite="good", wall=True, project_extra="") -> Path:
    (root / "scenes").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "project.godot").write_text('config_version=5\n\n[application]\nconfig/name="fx"\n\n[display]\n\nwindow/size/viewport_width=640\nwindow/size/viewport_height=360\n' + project_extra)
    (root / "scripts/player.gd").write_text(player)
    (root / "scenes/arena.tscn").write_text(arena(camera=camera, sprite=sprite, wall=wall))
    (root / "scenes/open.tscn").write_text(arena(camera=camera, sprite=sprite, wall=False))
    return root


def godot_procs(marker: str):
    """PIDs of live (non-zombie) Godot processes whose command line mentions `marker`."""
    out = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            state = (p / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if marker in cmd and "godot" in cmd.lower() and state != "Z":
            out.append(p.name)
    return out


def standard_fixtures(*, triggers_by_kind=None):
    from cascade_godot import CameraFixture, CollideFixture, MoveFixture, SpriteFixture
    t = triggers_by_kind or {}
    kw = lambda k: {"triggers": t[k]} if k in t else {}
    return [
        MoveFixture.eight_way("scenes/arena.tscn", "Player", max_speed=200, ticks=30, min_distance=20, **kw("move")),
        CollideFixture(scene="scenes/arena.tscn", mover="Player", statics=("Wall",), actions=("ui_right",), toward=(1, 0),
                       slide_actions=("ui_down",), **kw("collide")),
        CameraFixture(scene="scenes/open.tscn", target="Player", actions=("ui_right",), margin=24, world=(0, 0, 1000, 1000), **kw("camera")),
        SpriteFixture(scene="scenes/arena.tscn", nodes=("Sprite",), **kw("sprite")),
    ]

RAMP = """extends CharacterBody2D
func _physics_process(_delta: float) -> void:
	var dir := Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down")
	velocity = velocity.move_toward(dir * 200.0, 20.0)
	move_and_slide()
"""
LATE_DRIFT = """extends CharacterBody2D
var idle := 0
func _physics_process(_delta: float) -> void:
	var dir := Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down")
	idle = idle + 1 if dir == Vector2.ZERO else 0
	velocity = dir * 200.0 if dir != Vector2.ZERO else (Vector2(-120, 0) if idle > 20 else Vector2.ZERO)
	move_and_slide()
"""

STICKY = """extends CharacterBody2D
func _physics_process(delta: float) -> void:
	move_and_collide(Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down") * 200.0 * delta)
"""
PUSHES_WALL = GOOD.replace("move_and_slide()", "move_and_slide()\n\tget_parent().get_node(\"Wall\").global_position.x += 0.5")

DIAGONAL_GHOST = """extends CharacterBody2D
func _physics_process(delta: float) -> void:
	var dir := Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down")
	if dir.x != 0.0 and dir.y != 0.0:
		global_position += dir * 200.0 * delta        # diagonal movement forgets about collisions
	else:
		velocity = dir * 200.0
		move_and_slide()
"""
NAN_ON_RIGHT = GOOD.replace("move_and_slide()", "move_and_slide()\n\tif dir.x > 0.5 and dir.y == 0.0:\n\t\tglobal_position = Vector2(NAN, NAN)")
