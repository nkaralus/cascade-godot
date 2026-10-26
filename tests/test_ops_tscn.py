import pytest

from cascade_godot import PlanError, add_script_resource, set_node_property
from conftest import SCENE


def test_replace_existing_property():
    out = set_node_property(SCENE, "Player", "position", "Vector2(168, 100)")
    assert "[node name=\"Player\" type=\"Node2D\" parent=\".\"]\nposition = Vector2(168, 100)\n" in out
    assert out.replace("Vector2(168, 100)", "Vector2(100, 100)") == SCENE            # nothing else moved


def test_insert_missing_property_after_last_property():
    out = set_node_property(SCENE, "Main", "visible", "false")
    assert '[node name="Main" type="Node2D"]\nvisible = false\n\n[node name="Player"' in out
    out2 = set_node_property(out, "Player", "rotation", "1.5")
    assert 'position = Vector2(100, 100)\nrotation = 1.5\n\n[node name="Wall"' in out2


def test_is_idempotent():
    once = set_node_property(SCENE, "Player", "position", "Vector2(1, 2)")
    assert set_node_property(once, "Player", "position", "Vector2(1, 2)") == once


@pytest.mark.parametrize("node,code", [("Ghost", "NODE_NOT_FOUND"), ("Pla", "NODE_NOT_FOUND")])
def test_node_lookup(node, code):
    with pytest.raises(PlanError) as e:
        set_node_property(SCENE, node, "position", "Vector2(0, 0)")
    assert e.value.code == code


def test_ambiguous_names_are_refused():
    twice = SCENE + '\n[node name="Player" type="Node2D" parent="Wall"]\n'
    with pytest.raises(PlanError) as e:
        set_node_property(twice, "Player", "position", "Vector2(0, 0)")
    assert e.value.code == "AMBIGUOUS_NODE"


def test_unknown_shapes_are_refused_not_guessed():
    multi = SCENE.replace('position = Vector2(100, 100)', 'metadata/x = [\n1,\n2\n]')
    with pytest.raises(PlanError) as e:
        set_node_property(multi, "Player", "position", "Vector2(0, 0)")
    assert e.value.code == "UNSUPPORTED_SHAPE"
    dup = SCENE.replace('position = Vector2(100, 100)', 'position = Vector2(1, 1)\nposition = Vector2(2, 2)')
    with pytest.raises(PlanError):
        set_node_property(dup, "Player", "position", "Vector2(0, 0)")


@pytest.mark.parametrize("prop,value", [("pos ition", "1"), ("", "1"), ("a=b", "1"), ("position", "1\n[node name=\"Evil\"]"),
                                        ("position", "1\r")])
def test_injection_in_property_or_value(prop, value):
    with pytest.raises(PlanError):
        set_node_property(SCENE, "Player", prop, value)


def test_add_script_resource_first_one():
    out, ext = add_script_resource(SCENE, "scripts/player.gd")
    assert ext.startswith("1_")
    assert out.startswith("[gd_scene load_steps=2 format=3]\n\n[ext_resource type=\"Script\" path=\"res://scripts/player.gd\" id=\"%s\"]\n\n[node" % ext)


def test_add_script_resource_reuse_and_second():
    out, ext = add_script_resource(SCENE, "scripts/player.gd")
    again, ext2 = add_script_resource(out, "scripts/player.gd")
    assert again == out and ext2 == ext                                   # reused, no load_steps bump
    out3, ext3 = add_script_resource(out, "scripts/enemy.gd")
    assert ext3.startswith("2_") and "load_steps=3" in out3 and out3.count("[ext_resource") == 2
    assert out3.index("player.gd") < out3.index("enemy.gd") < out3.index("[node")


def test_add_script_needs_a_scene_header():
    with pytest.raises(PlanError):
        add_script_resource('[node name="X" type="Node"]\n', "scripts/a.gd")
