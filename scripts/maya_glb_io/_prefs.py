"""Persistent preferences + options-string serialization for the GLB importer.

Two things in one module because they share the schema:

  DEFAULTS  : the canonical option set, types, and default values
  get/set   : per-key access via Maya's optionVar (survives restarts)
  serialize : dict -> "k=v;k=v" string that Maya stores between imports
  parse     : "k=v;k=v" string -> dict with proper Python types

Keep this module Maya-free at import time — the optionVar calls are
inside functions that lazy-import maya.cmds. The smoke tests can import
the serializer without Maya.
"""
from __future__ import annotations

from typing import Any, Dict


_OV_PREFIX = "glbIO_pref_"
_OV_EXPORT_PREFIX = "glbIO_exp_"


# Schema: key -> (default_value, type_string)
#   type_string is one of: "bool", "int", "float", "str"
DEFAULTS = {
    "target":           ("",       "str"),    # "" = "use default" / picks via Auto
    "scale":            ("auto",   "str"),    # "auto" or float-as-string e.g. "100.0"
    "useNamespace":     (False,    "bool"),
    "skipMaterials":    (False,    "bool"),
    "skipSkinning":     (False,    "bool"),   # skip glTF skins -> Maya joints/skinCluster
    "importAnimation":  (True,     "bool"),   # glTF animation samplers -> Maya animCurves
    "centerPivot":      (False,    "bool"),
    "centerOnOrigin":   (False,    "bool"),
    "dropToFloor":      (True,     "bool"),   # sit bbox bottom on the grid (Y=0)
    "bakeTransforms":   (True,     "bool"),   # freeze imported geo -> clean identity
    "convertSpecGloss": (True,     "bool"),
    "materialPrefix":   ("",       "str"),
    # openPBR emission luminance (nits) for emissive materials. glTF emissive is
    # relative [0,1]; openPBR wants nits, so 1.0 reads as black. 1000 = strong
    # glow (default). Lower it if it washes out, raise for hotter neon.
    "emissionLuminance": ("1000.0", "str"),
}


# Export-side schema (mirrors the importer schema split). See exporter.py for
# the meaning of each key.
EXPORT_DEFAULTS = {
    "format":          ("glb",        "str"),   # "glb" | "gltf"
    "scope":           ("selection",  "str"),   # "selection" | "scene"
    "embedTextures":   (False,        "bool"),  # embed images vs reference files
    "convertUnits":    (True,         "bool"),  # Maya units -> metres (glTF std)
    "yUp":             (False,        "bool"),  # no-op axis flag (both are Y-up)
    "applyTransforms": (False,        "bool"),  # bake world TRS into verts
    "copyTextures":    (True,         "bool"),  # in reference mode, copy next to output
    "exportSkin":      (True,         "bool"),  # write skinClusters as glTF skins
    "exportAnimation": (True,         "bool"),  # write keyframes as glTF animations
}


def _schema_for(key):
    """Return the (default, type) pair for a key from either schema."""
    if key in DEFAULTS:
        return DEFAULTS[key]
    if key in EXPORT_DEFAULTS:
        return EXPORT_DEFAULTS[key]
    return None


def default_value(key):
    pair = _schema_for(key)
    return pair[0] if pair else None


def type_of(key):
    pair = _schema_for(key)
    return pair[1] if pair else "str"


# ---------------------------------------------------------------------------
# Persistent storage (optionVar)
# ---------------------------------------------------------------------------

def _cmds():
    from maya import cmds
    return cmds


def get(key):
    """Get a single preference value. Returns the schema default if unset."""
    cmds = _cmds()
    ov = _OV_PREFIX + key
    default, kind = DEFAULTS.get(key, (None, "str"))
    if not cmds.optionVar(exists=ov):
        return default
    raw = cmds.optionVar(query=ov)
    return _coerce(raw, kind)


def set_(key, value):
    """Persist a single preference. The schema's type determines the optionVar flavor."""
    cmds = _cmds()
    ov = _OV_PREFIX + key
    _, kind = DEFAULTS.get(key, (None, "str"))
    if kind == "bool":
        cmds.optionVar(intValue=(ov, 1 if value else 0))
    elif kind == "int":
        cmds.optionVar(intValue=(ov, int(value)))
    elif kind == "float":
        cmds.optionVar(floatValue=(ov, float(value)))
    else:
        cmds.optionVar(stringValue=(ov, str(value)))


def get_all():
    """Return every preference as a dict, falling back to schema defaults."""
    return {k: get(k) for k in DEFAULTS}


def reset_all():
    """Remove every glbIO_pref_* optionVar."""
    cmds = _cmds()
    for k in DEFAULTS:
        ov = _OV_PREFIX + k
        if cmds.optionVar(exists=ov):
            cmds.optionVar(remove=ov)


# ---------------------------------------------------------------------------
# Export preferences (separate optionVar namespace from import)
# ---------------------------------------------------------------------------

def get_export(key):
    """Get a single export preference value (schema default if unset)."""
    cmds = _cmds()
    ov = _OV_EXPORT_PREFIX + key
    default, kind = EXPORT_DEFAULTS.get(key, (None, "str"))
    if not cmds.optionVar(exists=ov):
        return default
    raw = cmds.optionVar(query=ov)
    return _coerce(raw, kind)


def set_export(key, value):
    """Persist a single export preference."""
    cmds = _cmds()
    ov = _OV_EXPORT_PREFIX + key
    _, kind = EXPORT_DEFAULTS.get(key, (None, "str"))
    if kind == "bool":
        cmds.optionVar(intValue=(ov, 1 if value else 0))
    elif kind == "int":
        cmds.optionVar(intValue=(ov, int(value)))
    elif kind == "float":
        cmds.optionVar(floatValue=(ov, float(value)))
    else:
        cmds.optionVar(stringValue=(ov, str(value)))


def get_all_export():
    """Return every export preference as a dict, falling back to defaults."""
    return {k: get_export(k) for k in EXPORT_DEFAULTS}


def reset_all_export():
    """Remove every glbIO_exp_* optionVar."""
    cmds = _cmds()
    for k in EXPORT_DEFAULTS:
        ov = _OV_EXPORT_PREFIX + k
        if cmds.optionVar(exists=ov):
            cmds.optionVar(remove=ov)


# ---------------------------------------------------------------------------
# Serializer (Maya-free — used by smoke tests too)
# ---------------------------------------------------------------------------

def serialize(opts):
    """Dict -> "k=v;k=v" string suitable for an MPxFileTranslator options string."""
    parts = []
    for k, v in opts.items():
        if isinstance(v, bool):
            parts.append("{0}={1}".format(k, 1 if v else 0))
        else:
            parts.append("{0}={1}".format(k, v))
    return ";".join(parts)


def parse(text):
    """Parse "k=v;k=v" into a dict with proper Python types per the schema."""
    result = {}
    if not text:
        return result
    for chunk in str(text).split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        k = k.strip()
        v = v.strip()
        kind = type_of(k)
        result[k] = _coerce(v, kind)
    return result


def _coerce(raw, kind):
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    if kind == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return str(raw) if raw is not None else ""
