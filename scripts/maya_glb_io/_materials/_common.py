"""Helpers shared across all shader-target material builders.

Keep target-specific node names OUT of here — this module only knows
about Maya stock nodes (file, place2dTexture, multiplyDivide, reverse,
expression, plusMinusAverage).
"""
from __future__ import annotations

from typing import Optional

import maya.api.OpenMaya as om
from maya import cmds

from maya_glb_io._gltf import Gltf
from maya_glb_io._materials import _specgloss


def sanitize(name):
    """Make `name` a valid Maya node name (alnum + underscore, no leading digit)."""
    if not name:
        return "_unnamed"
    out = []
    for ch in name:
        # Only ASCII alphanumerics are legal Maya node-name characters. Python's
        # str.isalnum() returns True for CJK/accented letters too, but Maya
        # rejects those ("New name has no legal characters") — so restrict to
        # ord < 128. Everything else (incl. unicode letters, spaces, symbols)
        # becomes an underscore.
        if ch == "_" or (ch.isalnum() and ord(ch) < 128):
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out)
    # A name with no ASCII alphanumeric (e.g. a node named "ルートパート" or "...")
    # is rejected by Maya — fall back to a safe, legal name.
    if not any(c.isalnum() and ord(c) < 128 for c in s):
        return "_unnamed"
    if s[0].isdigit():
        s = "_" + s
    return s


def join_prefix(prefix, base):
    """Join an optional name prefix with a base name.

    Returns "<prefix>_<base>" when `prefix` is a non-empty string, else just
    `base`. Keeps node names clean when the user hasn't asked for a prefix
    (the importer defaults the prefix to empty so node names aren't littered
    with the source file name, e.g. avoids "stonePyramid_stonePyramid_SHD").
    """
    if prefix:
        return "{0}_{1}".format(prefix, base)
    return base


# openPBRSurface emission is physically in NITS (cd/m2): the luminance that a
# white emission_color maps to. glTF emissive is a relative [0,1] value, so a
# luminance of 1.0 nit reads as essentially black after the viewport tone-map.
# This module-level default is what the openPBR builder uses for emissive
# materials; the importer overwrites it per-import from the `emissionLuminance`
# preference (see _build.build_scene), so the user can tune the glow strength.
EMISSION_LUMINANCE = 1000.0


def emissive_strength(mat):
    """Return the KHR_materials_emissive_strength multiplier (default 1.0).

    This glTF extension scales emissive brightness beyond the [0,1] factor —
    used by HDR/neon materials. Multiplies the emission luminance / weight.
    """
    ext = (mat.get("extensions") or {}).get("KHR_materials_emissive_strength")
    if ext and "emissiveStrength" in ext:
        try:
            return float(ext["emissiveStrength"])
        except (TypeError, ValueError):
            return 1.0
    return 1.0


def transmission_info(mat):
    """Decode glTF transmission/glass extensions into a flat dict, or None.

    Reads:
      * KHR_materials_transmission -> transmissionFactor / transmissionTexture
        (the fraction of light passing straight through, R channel of texture)
      * KHR_materials_ior          -> index of refraction (default 1.5)
      * KHR_materials_volume       -> thicknessFactor, attenuationColor,
                                      attenuationDistance (absorption/tint of a
                                      solid glass body, vs. a thin pane)

    Returns None when the material has no KHR_materials_transmission (so callers
    cheaply skip non-glass materials). Otherwise:
      {
        "factor": float,                 # 0..1
        "texture": {...} or None,        # glTF textureInfo (R channel)
        "ior": float,                    # default 1.5
        "attenuation_color": [r,g,b] or None,
        "attenuation_distance": float or None,
        "thickness": float,              # 0 = thin-walled
      }
    """
    exts = mat.get("extensions") or {}
    tr = exts.get("KHR_materials_transmission")
    if not tr:
        return None

    info = {
        "factor": 0.0,
        "texture": tr.get("transmissionTexture"),
        "ior": 1.5,
        "attenuation_color": None,
        "attenuation_distance": None,
        "thickness": 0.0,
    }
    try:
        info["factor"] = float(tr.get("transmissionFactor", 0.0))
    except (TypeError, ValueError):
        info["factor"] = 0.0

    ior_ext = exts.get("KHR_materials_ior")
    if ior_ext and "ior" in ior_ext:
        try:
            info["ior"] = float(ior_ext["ior"])
        except (TypeError, ValueError):
            pass

    vol = exts.get("KHR_materials_volume")
    if vol:
        try:
            info["thickness"] = float(vol.get("thicknessFactor", 0.0))
        except (TypeError, ValueError):
            info["thickness"] = 0.0
        if "attenuationColor" in vol:
            info["attenuation_color"] = vol["attenuationColor"]
        if "attenuationDistance" in vol:
            try:
                info["attenuation_distance"] = float(vol["attenuationDistance"])
            except (TypeError, ValueError):
                info["attenuation_distance"] = None

    return info


def clearcoat_info(mat):
    """Decode glTF KHR_materials_clearcoat into a flat dict, or None.

    Reads (https://github.com/KhronosGroup/glTF/tree/main/extensions/2.0/Khronos/KHR_materials_clearcoat):
      * clearcoatFactor          (0..1, default 0)  -> coat weight
      * clearcoatTexture         (R channel)        -> coat weight texture
      * clearcoatRoughnessFactor (0..1, default 0)  -> coat roughness
      * clearcoatRoughnessTexture(G channel)        -> coat roughness texture
      * clearcoatNormalTexture   (tangent-space)    -> coat normal map

    Returns None when the material has no KHR_materials_clearcoat. Otherwise:
      {
        "factor": float,                # clearcoatFactor 0..1
        "texture": {...} or None,       # clearcoatTexture (R = weight)
        "roughness": float,             # clearcoatRoughnessFactor 0..1
        "roughness_texture": {...} or None,  # clearcoatRoughnessTexture (G)
        "normal_texture": {...} or None,     # clearcoatNormalTexture
      }
    """
    cc = (mat.get("extensions") or {}).get("KHR_materials_clearcoat")
    if not cc:
        return None
    info = {
        "factor": 0.0,
        "texture": cc.get("clearcoatTexture"),
        "roughness": 0.0,
        "roughness_texture": cc.get("clearcoatRoughnessTexture"),
        "normal_texture": cc.get("clearcoatNormalTexture"),
    }
    try:
        info["factor"] = float(cc.get("clearcoatFactor", 0.0))
    except (TypeError, ValueError):
        info["factor"] = 0.0
    try:
        info["roughness"] = float(cc.get("clearcoatRoughnessFactor", 0.0))
    except (TypeError, ValueError):
        info["roughness"] = 0.0
    return info


def sheen_info(mat):
    """Decode glTF KHR_materials_sheen into a flat dict, or None.

    Reads (KHR_materials_sheen):
      * sheenColorFactor      ([r,g,b], default [0,0,0]) -> fuzz colour
      * sheenColorTexture     (RGB, sRGB)                -> fuzz colour texture
      * sheenRoughnessFactor  (0..1, default 0)          -> fuzz roughness
      * sheenRoughnessTexture (A channel)                -> fuzz roughness texture

    Returns None when the material has no KHR_materials_sheen. Otherwise:
      {
        "color": [r,g,b],               # sheenColorFactor
        "color_texture": {...} or None, # sheenColorTexture (RGB)
        "roughness": float,             # sheenRoughnessFactor 0..1
        "roughness_texture": {...} or None,  # sheenRoughnessTexture (A)
      }
    """
    sh = (mat.get("extensions") or {}).get("KHR_materials_sheen")
    if not sh:
        return None
    color = sh.get("sheenColorFactor", [0.0, 0.0, 0.0])
    try:
        color = [float(color[0]), float(color[1]), float(color[2])]
    except (TypeError, ValueError, IndexError):
        color = [0.0, 0.0, 0.0]
    info = {
        "color": color,
        "color_texture": sh.get("sheenColorTexture"),
        "roughness": 0.0,
        "roughness_texture": sh.get("sheenRoughnessTexture"),
    }
    try:
        info["roughness"] = float(sh.get("sheenRoughnessFactor", 0.0))
    except (TypeError, ValueError):
        info["roughness"] = 0.0
    return info


def anisotropy_info(mat):
    """Decode glTF KHR_materials_anisotropy into a flat dict, or None.

    Reads (KHR_materials_anisotropy):
      * anisotropyStrength (0..1, default 0)     -> specular roughness anisotropy
      * anisotropyRotation (radians, default 0)  -> anisotropy direction
      * anisotropyTexture  (RG = direction, B = strength)

    Returns None when the material has no KHR_materials_anisotropy. Otherwise:
      {
        "strength": float,        # anisotropyStrength 0..1
        "rotation": float,        # anisotropyRotation, radians
        "texture": {...} or None, # anisotropyTexture
      }
    """
    an = (mat.get("extensions") or {}).get("KHR_materials_anisotropy")
    if not an:
        return None
    info = {
        "strength": 0.0,
        "rotation": 0.0,
        "texture": an.get("anisotropyTexture"),
    }
    try:
        info["strength"] = float(an.get("anisotropyStrength", 0.0))
    except (TypeError, ValueError):
        info["strength"] = 0.0
    try:
        info["rotation"] = float(an.get("anisotropyRotation", 0.0))
    except (TypeError, ValueError):
        info["rotation"] = 0.0
    return info


def iridescence_info(mat):
    """Decode glTF KHR_materials_iridescence into a flat dict, or None.

    Reads (KHR_materials_iridescence):
      * iridescenceFactor           (0..1, default 0)  -> on/off gate
      * iridescenceIor              (default 1.3)      -> thin-film IOR
      * iridescenceThicknessMinimum (nm, default 100)  -> thin-film min thickness
      * iridescenceThicknessMaximum (nm, default 400)  -> thin-film max thickness
      * iridescenceThicknessTexture (G channel)        -> thickness map

    Returns None when the material has no KHR_materials_iridescence. Otherwise:
      {
        "factor": float,           # iridescenceFactor 0..1
        "ior": float,              # iridescenceIor
        "thickness_min": float,    # nm
        "thickness_max": float,    # nm
        "thickness_texture": {...} or None,
      }
    """
    ir = (mat.get("extensions") or {}).get("KHR_materials_iridescence")
    if not ir:
        return None
    info = {
        "factor": 0.0,
        "ior": 1.3,
        "thickness_min": 100.0,
        "thickness_max": 400.0,
        "texture": ir.get("iridescenceTexture"),          # R = per-pixel factor
        "thickness_texture": ir.get("iridescenceThicknessTexture"),  # G = thickness
    }
    try:
        info["factor"] = float(ir.get("iridescenceFactor", 0.0))
    except (TypeError, ValueError):
        info["factor"] = 0.0
    try:
        info["ior"] = float(ir.get("iridescenceIor", 1.3))
    except (TypeError, ValueError):
        info["ior"] = 1.3
    try:
        info["thickness_min"] = float(ir.get("iridescenceThicknessMinimum", 100.0))
    except (TypeError, ValueError):
        info["thickness_min"] = 100.0
    try:
        info["thickness_max"] = float(ir.get("iridescenceThicknessMaximum", 400.0))
    except (TypeError, ValueError):
        info["thickness_max"] = 400.0
    return info


def specular_info(mat):
    """Decode glTF KHR_materials_specular into a flat dict, or None.

    Reads (KHR_materials_specular):
      * specularFactor       (0..1, default 1)        -> specular weight
      * specularTexture      (A channel)              -> specular weight texture
      * specularColorFactor  ([r,g,b], default [1,1,1]) -> specular colour
      * specularColorTexture (RGB, sRGB)              -> specular colour texture

    Returns None when the material has no KHR_materials_specular. Otherwise:
      {
        "factor": float,                # specularFactor 0..1
        "texture": {...} or None,       # specularTexture (A = weight)
        "color": [r,g,b],               # specularColorFactor
        "color_texture": {...} or None, # specularColorTexture (RGB)
      }
    """
    sp = (mat.get("extensions") or {}).get("KHR_materials_specular")
    if not sp:
        return None
    color = sp.get("specularColorFactor", [1.0, 1.0, 1.0])
    try:
        color = [float(color[0]), float(color[1]), float(color[2])]
    except (TypeError, ValueError, IndexError):
        color = [1.0, 1.0, 1.0]
    info = {
        "factor": 1.0,
        "texture": sp.get("specularTexture"),
        "color": color,
        "color_texture": sp.get("specularColorTexture"),
    }
    try:
        info["factor"] = float(sp.get("specularFactor", 1.0))
    except (TypeError, ValueError):
        info["factor"] = 1.0
    return info


def texture_image_index(gltf, tex_index):
    """Resolve a glTF texture index -> source image index."""
    if tex_index is None:
        return None
    try:
        tex = gltf.doc["textures"][tex_index]
    except (KeyError, IndexError):
        return None
    return tex.get("source")


def make_file_node(file_path, colorspace, name):
    """Create a Maya `file` node + `place2dTexture` with the colorspace set."""
    node = cmds.shadingNode("file", asTexture=True, isColorManaged=True, name=name)
    cmds.setAttr(node + ".fileTextureName", file_path, type="string")
    try:
        cmds.setAttr(node + ".colorSpace", colorspace, type="string")
    except RuntimeError:
        pass  # older Maya may not expose .colorSpace

    p2d = cmds.shadingNode("place2dTexture", asUtility=True, name=name + "_p2d")
    _connect_place2d(p2d, node)
    return node


def _connect_place2d(p2d, file_node):
    pairs = [
        ("outUV", "uvCoord"),
        ("outUvFilterSize", "uvFilterSize"),
        ("coverage", "coverage"),
        ("translateFrame", "translateFrame"),
        ("rotateFrame", "rotateFrame"),
        ("mirrorU", "mirrorU"),
        ("mirrorV", "mirrorV"),
        ("stagger", "stagger"),
        ("wrapU", "wrapU"),
        ("wrapV", "wrapV"),
        ("repeatUV", "repeatUV"),
        ("offset", "offset"),
        ("rotateUV", "rotateUV"),
        ("noiseUV", "noiseUV"),
        ("vertexUvOne", "vertexUvOne"),
        ("vertexUvTwo", "vertexUvTwo"),
        ("vertexUvThree", "vertexUvThree"),
        ("vertexCameraOne", "vertexCameraOne"),
    ]
    for src, dst in pairs:
        try:
            cmds.connectAttr(p2d + "." + src, file_node + "." + dst, force=True)
        except RuntimeError:
            pass


def broadcast_scalar_to_color(scalar_plug, color_attr):
    """Connect a single scalar plug (e.g. 'file.outColorR') to all 3 RGB
    sub-attrs of a color attribute (e.g. 'StingrayPBS_SHD.metallic_map').

    Many PBR shader inputs are color (vec3) even when only one channel is
    semantically scalar (metalness, roughness, AO). Broadcasting a single
    scalar to R/G/B presents the shader with a uniform value regardless of
    which channel it samples internally.
    """
    for ch in ("R", "G", "B"):
        cmds.connectAttr(scalar_plug, color_attr + ch, force=True)


def try_set(attr, *value, **kwargs):
    """setAttr that silently no-ops if the attribute doesn't exist."""
    try:
        if kwargs.get("type"):
            cmds.setAttr(attr, *value, type=kwargs["type"])
        elif len(value) == 1:
            cmds.setAttr(attr, value[0])
        else:
            cmds.setAttr(attr, *value)
    except (RuntimeError, ValueError):
        pass


def try_connect(src, dst):
    """connectAttr that silently no-ops if either attribute doesn't exist."""
    try:
        cmds.connectAttr(src, dst, force=True)
        return True
    except RuntimeError:
        return False


def make_shading_group(shader, name):
    sg = cmds.sets(
        renderable=True,
        noSurfaceShader=True,
        empty=True,
        name=name,
    )
    cmds.connectAttr(shader + ".outColor", sg + ".surfaceShader")
    return sg


def get_pbr_metallic_roughness(mat):
    """Extract glTF's metallic-roughness payload, including legacy spec-gloss
    conversion if the material declares KHR_materials_pbrSpecularGlossiness
    instead of (or in addition to) the standard pbrMetallicRoughness block.
    """
    pbr = dict(mat.get("pbrMetallicRoughness") or {})
    ext = (mat.get("extensions") or {}).get("KHR_materials_pbrSpecularGlossiness")
    if ext and not pbr:
        # Legacy-only material — convert
        pbr = _specgloss.specgloss_to_metalrough(ext)
    elif ext and pbr:
        # Both present — modern blocks usually win, but if metalrough is empty
        # and specgloss has data, prefer the conversion
        if not pbr.get("baseColorTexture") and ext.get("diffuseTexture"):
            pbr = _specgloss.specgloss_to_metalrough(ext)
    return pbr
