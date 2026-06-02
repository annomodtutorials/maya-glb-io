"""Apply decoded glTF animations to Maya nodes as animCurves.

This module turns the parser's Maya-free Animation/AnimChannel objects (see
_gltf.read_animations()) into keyframes on the transforms that _build created.
Imported separately from the parser so _gltf.py stays Maya-free for the smoke
tests; this module DOES import maya and is only ever called from inside Maya.

Conventions mirrored from _build.py:
  * translation samples are run through the same `coord_convert` the mesh used,
    so animated translations land at the unit scale of the geometry.
  * rotation samples are glTF quaternions (x, y, z, w) converted to euler
    degrees via om.MQuaternion(...).asEulerRotation(), exactly like _apply_trs.
  * scale samples are keyed verbatim (scale is unitless).

Known limitations (v1):
  * Euler conversion of per-key quaternions can introduce gimbal/axis flips
    between adjacent keys (no quaternion-curve unrolling). The rotateOrder of
    the target node is respected but euler discontinuities are not smoothed.
  * CUBICSPLINE tangents are NOT reconstructed: we key the value element of
    each (in-tangent, value, out-tangent) triple with spline/auto tangents.
    This applies to morph `weights` channels too — the value element is keyed.

Morph-target `weights` channels:
  A glTF `weights` channel animates ALL of a mesh's morph weights at once. Its
  output is a flat SCALAR array laid out keyframe-major: for keyframe k and
  target t (of N targets), weight = output[k*N + t]. We resolve the target
  node's blendShape (via the node_to_blendshape map built by _build, falling
  back to walking the mesh shape's history) and key each blendShape weight[t].
"""
from __future__ import annotations

from typing import Dict, List, Optional

import maya.api.OpenMaya as om
from maya import cmds


# glTF samples are in seconds; Maya keys are in frames. Map the scene's time
# unit name to its frames-per-second. Fallback is 24 (film) for anything we
# don't recognise.
_TIME_UNIT_FPS = {
    "game": 15.0,
    "film": 24.0,
    "pal": 25.0,
    "ntsc": 30.0,
    "show": 48.0,
    "palf": 50.0,
    "ntscf": 60.0,
    "2fps": 2.0,
    "3fps": 3.0,
    "4fps": 4.0,
    "5fps": 5.0,
    "6fps": 6.0,
    "8fps": 8.0,
    "10fps": 10.0,
    "12fps": 12.0,
    "16fps": 16.0,
    "20fps": 20.0,
    "40fps": 40.0,
    "75fps": 75.0,
    "80fps": 80.0,
    "100fps": 100.0,
    "120fps": 120.0,
}


def scene_fps():
    # type: () -> float
    """Frames-per-second of the current scene's time unit.

    Prefers the OpenMaya conversion (robust against exotic "<n>fps" unit names
    Maya supports), falling back to the name table, then to 24.
    """
    try:
        # MTime(1 second) expressed in the UI time unit == frames per second.
        fps = om.MTime(1.0, om.MTime.kSeconds).asUnits(om.MTime.uiUnit())
        if fps and fps > 0:
            return float(fps)
    except Exception:
        pass
    try:
        unit = cmds.currentUnit(query=True, time=True) or "film"
    except Exception:
        unit = "film"
    return _TIME_UNIT_FPS.get(unit, 24.0)


def apply_animations(gltf, node_to_maya, coord_convert, unit_scale, options=None,
                     node_to_blendshape=None):
    # type: (object, Dict[int, str], object, float, Optional[dict], Optional[Dict[int, list]]) -> int
    """Key every glTF animation channel onto its target Maya node.

    Args:
      gltf          : parsed Gltf — provides read_animations().
      node_to_maya  : dict glTF node index -> Maya node full path (built by
                      _build.build_scene). Targets missing from this map are
                      warned + skipped (e.g. collapsed pass-through nodes).
      coord_convert : callable(triplet) -> triplet, the SAME one the mesh used,
                      so animated translation matches geometry units.
      unit_scale    : numeric scale factor (kept for signature symmetry /
                      potential future use; translation already goes through
                      coord_convert which folds the scale in).
      options       : import options dict (currently unused but threaded in).
      node_to_blendshape : dict glTF mesh-node index -> [blendShape node names]
                      built by _build for morphed meshes. Used to resolve a
                      `weights` channel to its blendShape(s). May be None/empty
                      (then we fall back to the mesh shape's history); morph
                      meshes are the only case where this is populated.

    Returns the number of channels successfully keyed. A no-op (returns 0) when
    the document has no animations — safe for the Anno building pipeline.
    """
    try:
        animations = gltf.read_animations()
    except Exception as exc:
        om.MGlobal.displayWarning("[glb_io] failed to read animations: %s" % exc)
        return 0

    if not animations:
        return 0

    fps = scene_fps()
    keyed_channels = 0
    node_to_blendshape = node_to_blendshape or {}

    for anim in animations:
        for channel in anim.channels:
            try:
                if _apply_channel(channel, node_to_maya, coord_convert, fps,
                                  node_to_blendshape):
                    keyed_channels += 1
            except Exception as exc:
                # One bad channel must never abort the whole import.
                om.MGlobal.displayWarning(
                    "[glb_io] animation channel (node %s, %s) failed: %s"
                    % (channel.target_node, channel.path, exc)
                )

    if keyed_channels:
        om.MGlobal.displayInfo(
            "[glb_io] applied %d animation channel(s) at %g fps"
            % (keyed_channels, fps)
        )
    return keyed_channels


# Maya attribute names per glTF path + axis.
_AXIS_ATTRS = {
    "translation": ("translateX", "translateY", "translateZ"),
    "scale": ("scaleX", "scaleY", "scaleZ"),
    "rotation": ("rotateX", "rotateY", "rotateZ"),
}


def _apply_channel(channel, node_to_maya, coord_convert, fps,
                   node_to_blendshape=None):
    """Key a single channel. Returns True if at least one key was set."""
    node = node_to_maya.get(channel.target_node)
    if not node:
        om.MGlobal.displayWarning(
            "[glb_io] animation targets node %d which has no live Maya node "
            "(collapsed/removed?); skipping channel '%s'"
            % (channel.target_node, channel.path)
        )
        return False
    if not cmds.objExists(node):
        om.MGlobal.displayWarning(
            "[glb_io] animation target '%s' no longer exists; skipping" % node
        )
        return False

    path = channel.path

    if path == "weights":
        return _apply_weights_channel(
            channel, node, fps, node_to_blendshape or {}
        )

    times = channel.times
    values = channel.values
    interp = (channel.interpolation or "LINEAR").upper()
    cubic = interp == "CUBICSPLINE"

    # CUBICSPLINE stores 3 outputs per key (in-tangent, value, out-tangent).
    # For v1 we lift just the value (middle) element of each triple. LINEAR /
    # STEP store one output per key.
    n_keys = len(times)

    attrs = _AXIS_ATTRS.get(path)
    if attrs is None:
        return False

    set_any = False
    prev_euler = None  # rotation continuity (euler unrolling) — see below
    for ki in range(n_keys):
        seconds = times[ki]
        frame = seconds * fps

        if cubic:
            value = values[ki * 3 + 1]
        else:
            if ki >= len(values):
                break
            value = values[ki]

        if path == "translation":
            x, y, z = coord_convert((value[0], value[1], value[2]))
            axis_vals = (x, y, z)
        elif path == "scale":
            axis_vals = (value[0], value[1], value[2])
        else:  # rotation: quaternion (x, y, z, w) -> euler degrees
            q = om.MQuaternion(value[0], value[1], value[2], value[3])
            e = q.asEulerRotation()
            # Euler unrolling: a quaternion maps to many equivalent euler triples
            # (and ±360 wraps). Converting each key independently lets adjacent
            # frames land on different branches -> the joint flips / spins the
            # long way (the "junky limbs" on fast rigs like BrainStem). Snapping
            # each euler to the solution closest to the previous frame keeps the
            # animation curves continuous.
            if prev_euler is not None:
                try:
                    e.setToClosestSolution(prev_euler)
                except Exception:
                    pass
            prev_euler = om.MEulerRotation(e.x, e.y, e.z, e.order)
            axis_vals = (
                _deg(e.x),
                _deg(e.y),
                _deg(e.z),
            )

        for attr, val in zip(attrs, axis_vals):
            cmds.setKeyframe(node, attribute=attr, time=frame, value=float(val))
            set_any = True

    if not set_any:
        return False

    # Tangent handling per interpolation.
    if interp == "STEP":
        itt, ott = "linear", "step"
    elif cubic:
        # No exact tangent reconstruction in v1 — use spline tangents so the
        # curve is at least smooth through the keyed values.
        itt, ott = "spline", "spline"
    else:  # LINEAR (and unknown -> default linear)
        itt, ott = "linear", "linear"

    for attr in attrs:
        try:
            cmds.keyTangent(node, edit=True, attribute=attr, itt=itt, ott=ott)
        except Exception:
            # Tangent setting is cosmetic; never let it fail the channel.
            pass

    return True


def _apply_weights_channel(channel, node, fps, node_to_blendshape):
    """Key a glTF morph-target `weights` channel onto its blendShape weights.

    A `weights` channel animates ALL of the mesh's morph weights together. Its
    output is a flat SCALAR array laid out keyframe-major: for keyframe k and
    target t (of N targets), weight = output[k*N + t]. (CUBICSPLINE stores 3
    values per target per key — in/value/out — so the stride is 3*N and we take
    the middle value, consistent with the transform paths.)

    Returns True if at least one key was set.
    """
    blendshapes = _resolve_blendshapes(channel.target_node, node, node_to_blendshape)
    if not blendshapes:
        om.MGlobal.displayWarning(
            "[glb_io] weights animation targets node %d but no blendShape was "
            "found for it; skipping channel" % channel.target_node
        )
        return False

    times = channel.times
    values = channel.values
    n_keys = len(times)
    if n_keys == 0 or not values:
        return False

    interp = (channel.interpolation or "LINEAR").upper()
    cubic = interp == "CUBICSPLINE"
    stride_factor = 3 if cubic else 1

    set_any = False
    for bs in blendshapes:
        if not cmds.objExists(bs):
            continue
        # N = number of blendShape weight targets. weightCount is the multi
        # plug length; query the existing indices to be robust.
        try:
            n_targets = cmds.blendShape(bs, query=True, weightCount=True)
        except Exception:
            n_targets = 0
        if not n_targets:
            # Fall back to the output layout: total / keys / stride.
            if cubic:
                n_targets = len(values) // (n_keys * 3) if n_keys else 0
            else:
                n_targets = len(values) // n_keys if n_keys else 0
        if not n_targets:
            continue

        expected = n_keys * n_targets * stride_factor
        if len(values) < expected:
            om.MGlobal.displayWarning(
                "[glb_io] weights channel for '%s' has %d output values but "
                "%d keys x %d targets (x%d) expects %d; skipping"
                % (bs, len(values), n_keys, n_targets, stride_factor, expected)
            )
            continue

        for ki in range(n_keys):
            frame = times[ki] * fps
            for t in range(n_targets):
                if cubic:
                    # (in, value, out) triple per target per key; take value.
                    base = (ki * n_targets + t) * 3
                    w = values[base + 1]
                else:
                    w = values[ki * n_targets + t]
                try:
                    cmds.setKeyframe(
                        bs, attribute="weight[%d]" % t,
                        time=frame, value=float(w),
                    )
                    set_any = True
                except Exception:
                    pass

        # Tangent style mirrors the transform channels.
        if interp == "STEP":
            itt, ott = "linear", "step"
        elif cubic:
            itt, ott = "spline", "spline"
        else:
            itt, ott = "linear", "linear"
        for t in range(n_targets):
            try:
                cmds.keyTangent(
                    bs, edit=True, attribute="weight[%d]" % t, itt=itt, ott=ott
                )
            except Exception:
                pass

    return set_any


def _resolve_blendshapes(node_index, node_path, node_to_blendshape):
    """Resolve the blendShape node(s) for a glTF mesh node.

    Preference order:
      1. The node_to_blendshape map built by _build (authoritative — survives
         flatten since blendShape is a DG node, not path-based).
      2. Fall back to walking the mesh shape(s) under node_path and collecting
         any blendShape in their history (handles callers that don't thread the
         map, and shapes whose owning transform got remapped).

    Returns a de-duplicated list of blendShape node names (may be empty).
    """
    found = []

    mapped = node_to_blendshape.get(node_index) if node_to_blendshape else None
    if mapped:
        for bs in mapped:
            if bs and bs not in found and cmds.objExists(bs):
                found.append(bs)
    if found:
        return found

    # History fallback: the node's mesh shapes -> blendShapes upstream.
    try:
        shapes = cmds.listRelatives(
            node_path, shapes=True, fullPath=True, type="mesh",
            noIntermediate=True,
        ) or []
        # Also consider intermediate shapes (a blendShape's input is often an
        # intermediate orig shape), so don't filter them out for history.
        all_shapes = cmds.listRelatives(
            node_path, shapes=True, fullPath=True, type="mesh"
        ) or []
        for s in (shapes + all_shapes):
            hist = cmds.listHistory(s) or []
            for bs in cmds.ls(hist, type="blendShape") or []:
                if bs not in found:
                    found.append(bs)
    except Exception:
        pass

    return found


def _deg(radians):
    import math
    return math.degrees(radians)
