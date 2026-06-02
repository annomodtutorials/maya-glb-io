"""Per-shader-target material builders for the glTF importer.

Public entry: build_materials(gltf, image_paths, name_prefix, target=None)

`target` is one of: 'stingray', 'openpbr', 'phong', 'arnold'.
Future: 'redshift', 'vray', 'renderman'.

If `target` is None, an auto pick happens based on Maya version + plug-in
availability — openPBR for 2025+ (Maya-native, modern), StingrayPBS
otherwise. Arnold/Redshift/V-Ray/RenderMan must be requested explicitly
because users may have those renderers installed but prefer Maya-native
output.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import maya.api.OpenMaya as om

from maya_glb_io import _compat
from maya_glb_io._gltf import Gltf


# Map shader-target name -> module path. Modules are lazy-imported so a
# missing optional dependency (e.g. unhandled Renderman) doesn't break
# the importer for users targeting StingrayPBS.
_TARGETS = {
    "stingray":  "maya_glb_io._materials.stingray",
    "openpbr":   "maya_glb_io._materials.openpbr",
    "phong":     "maya_glb_io._materials.maya_phong",
    "arnold":    "maya_glb_io._materials.arnold",
}


def list_targets():
    """Return the canonical target names the importer knows about."""
    return list(_TARGETS.keys())


def resolve_target(target):
    """Resolve `target` (or None) to a concrete target name.

    None → auto-pick by Maya version: openPBR if available else StingrayPBS.
    """
    if target is not None:
        target = target.lower().strip()
        if target not in _TARGETS:
            raise ValueError(
                "unknown shader target {0!r}; valid: {1}".format(target, list(_TARGETS))
            )
        return target

    if _compat.openpbr_available():
        return "openpbr"
    if _compat.stingray_pbs_available():
        return "stingray"
    # Always-available fallback
    return "phong"


def build_materials(gltf, image_paths, name_prefix, target=None):
    # type: (Gltf, List[str], str, Optional[str]) -> List[Tuple[str, str]]
    """Create Maya shaders for every glTF material using the chosen target.

    Returns [(surface_shader_name, shading_group_name), ...] indexed by glTF
    material index.
    """
    materials = gltf.doc.get("materials") or []
    if not materials:
        return []

    target = resolve_target(target)

    import importlib
    mod = importlib.import_module(_TARGETS[target])

    om.MGlobal.displayInfo(
        "[glb_io] building materials with target='{0}'".format(target)
    )

    results = []
    for i, mat in enumerate(materials):
        try:
            shader_sg = mod.build_material(gltf, mat, i, image_paths, name_prefix)
        except Exception as exc:
            om.MGlobal.displayError(
                "[glb_io] material #{0} ({1!r}) build failed: {2}".format(
                    i, mat.get("name"), exc
                )
            )
            import traceback
            om.MGlobal.displayError(traceback.format_exc())
            # Create a placeholder so primitive material lookups don't crash
            shader_sg = mod.build_placeholder_material(i, name_prefix) \
                if hasattr(mod, "build_placeholder_material") else (None, None)
        results.append(shader_sg)

    return results
