"""Reference op handlers that edit Godot 4 text scenes (.tscn).

These are deliberately strict. They support scenes written by Godot itself and refuse anything
whose shape they do not understand (multi-line values, ambiguous node names) instead of guessing.
Handlers are HARNESS code: the model only picks the op name; every value comes from `params`.

Assumption: registry bounds (x, y) equal the node's `position`. Change `_bounds_for` if your
scenes use a centred origin.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any, List, Mapping, Optional

from subnetbus import AABB, BusError, lexical_segments

from .errors import PlanError
from .pipeline_types import Context, Plan
from .transaction import FileChange

__all__ = ["set_node_property", "add_script_resource", "set_position", "attach_script", "DEFAULT_HANDLERS"]

_NODE = re.compile(r'^\[node name="([^"]*)"[^\]]*\]\s*$')
_PROP = re.compile(r"^([A-Za-z_][A-Za-z0-9_/]*) = (.*)$")
_EXT = re.compile(r"^\[ext_resource (.*)\]\s*$")
_ATTR = re.compile(r'(\w+)="([^"]*)"')


def _section_end(lines: List[str], start: int) -> int:
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("["):
            return i
    return len(lines)


def set_node_property(text: str, node: str, prop: str, value: str) -> str:
    if "\n" in value or "\r" in value:
        raise PlanError("BAD_VALUE", "value must be one line")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_/]*", prop):
        raise PlanError("BAD_PROPERTY", prop)
    lines = text.split("\n")
    heads = [i for i, ln in enumerate(lines) if (m := _NODE.match(ln)) and m.group(1) == node]
    if not heads:
        raise PlanError("NODE_NOT_FOUND", node)
    if len(heads) > 1:
        raise PlanError("AMBIGUOUS_NODE", f"{node} appears {len(heads)} times")
    start = heads[0]
    end = _section_end(lines, start)
    last_prop = None
    hit = None
    for i in range(start + 1, end):
        ln = lines[i]
        if not ln.strip() or ln.startswith(("#", ";")):
            continue
        m = _PROP.match(ln)
        if not m:
            raise PlanError("UNSUPPORTED_SHAPE", f"node {node}: line {i + 1} is not 'key = value'")
        last_prop = i
        if m.group(1) == prop:
            if hit is not None:
                raise PlanError("UNSUPPORTED_SHAPE", f"node {node}: {prop} appears twice")
            hit = i
    new_line = f"{prop} = {value}"
    if hit is not None:
        lines[hit] = new_line
    else:
        lines.insert((last_prop if last_prop is not None else start) + 1, new_line)
    return "\n".join(lines)


def add_script_resource(text: str, script_rel: str):
    """Ensure an ext_resource for the script exists. Returns (new_text, ext_id)."""
    res = f"res://{script_rel}"
    lines = text.split("\n")
    ext_idx, max_n = [], 0
    for i, ln in enumerate(lines):
        m = _EXT.match(ln)
        if not m:
            continue
        ext_idx.append(i)
        attrs = dict(_ATTR.findall(m.group(1)))
        if attrs.get("path") == res and attrs.get("type") == "Script":
            return text, attrs["id"]
        n = re.match(r"(\d+)_", attrs.get("id", ""))
        max_n = max(max_n, int(n.group(1)) if n else 0)
    new_id = f"{max_n + 1}_{hashlib.sha1(res.encode()).hexdigest()[:5]}"
    line = f'[ext_resource type="Script" path="{res}" id="{new_id}"]'
    if ext_idx:
        lines.insert(ext_idx[-1] + 1, line)
    else:
        head = next((i for i, ln in enumerate(lines) if ln.startswith("[gd_scene")), None)
        if head is None:
            raise PlanError("UNSUPPORTED_SHAPE", "no [gd_scene] header")
        lines[head + 1:head + 1] = ["", line]
    for i, ln in enumerate(lines):
        if ln.startswith("[gd_scene"):
            lines[i] = re.sub(r"load_steps=(\d+)", lambda m: f"load_steps={int(m.group(1)) + 1}", ln, count=1)
            break
    return "\n".join(lines), new_id


# ---- handlers ----------------------------------------------------------------------------
def _num(params: Mapping, key: str) -> float:
    v = params.get(key)
    if type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e7:
        raise PlanError("BAD_PARAMS", f"{key} must be a finite number")
    return v


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def _path(params: Mapping, key: str, suffix: str) -> str:
    v = params.get(key)
    try:
        lexical_segments(v)
    except BusError as e:
        raise PlanError("BAD_PARAMS", f"{key}: {e.code}") from None
    if not v.endswith(suffix):
        raise PlanError("BAD_PARAMS", f"{key} must end with {suffix}")
    return v


def set_position(frame: Mapping, params: Mapping, ctx: Context) -> Plan:
    scene = _path(params, "scene", ".tscn")
    x, y = _num(params, "x"), _num(params, "y")
    node = ctx.registry.get(frame["target"])
    text, sha = ctx.read_text(scene)
    new = set_node_property(text, frame["target"][1:], "position", f"Vector2({_fmt(x)}, {_fmt(y)})")
    bounds: Optional[AABB] = AABB(x, y, node.bounds.w, node.bounds.h) if node and node.bounds else None

    def on_commit(reg) -> None:
        if bounds is not None:
            reg.set_bounds(node.name, bounds)

    return Plan((FileChange.text(scene, new, expect_sha256=sha),), proposed_bounds=bounds, on_commit=on_commit)


def attach_script(frame: Mapping, params: Mapping, ctx: Context) -> Plan:
    scene = _path(params, "scene", ".tscn")
    script = _path(params, "script", ".gd")
    ctx.read_text(script)                                  # the script must already exist
    text, sha = ctx.read_text(scene)
    text, ext_id = add_script_resource(text, script)
    new = set_node_property(text, frame["target"][1:], "script", f'ExtResource("{ext_id}")')
    return Plan((FileChange.text(scene, new, expect_sha256=sha),), also_verify=(script,))


DEFAULT_HANDLERS = {"set_position": set_position, "attach_script": attach_script}
