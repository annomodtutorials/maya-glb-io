"""Maya surface shader -> glTF PBR MaterialDef (the inverse of the importer's
_materials/* builders).

Public entry:
    shader_to_material_def(shader_node, name, image_cache, options) -> MaterialDef

Supported source shaders (best-effort — unknown types fall back to a sensible
default + a warning, never a crash):

  openPBRSurface (Maya 2025+)
      baseColor / baseMetalness / specularRoughness / geometryNormal /
      emissionColor (+ emissionLuminance gate) / geometryOpacity
  StingrayPBS (shaderFXPlugin)
      TEX_color_map / TEX_normal_map / TEX_metallic_map / TEX_roughness_map /
      TEX_emissive_map / TEX_ao_map, gated by use_*_map booleans; scalar
      base_color / metallic / roughness / emissive factors.
  aiStandardSurface (Arnold / mtoa)
      baseColor / metalness / specularRoughness / normalCamera (via
      aiNormalMap / bump2d) / emissionColor (+ emission gate) / opacity
  lambert / blinn / phong (Maya legacy)
      color / normalCamera / transparency -> baseColor + normal + alpha.
      Blinn/Phong specular roughness is approximated from cosinePower /
      eccentricity. Metalness defaults to 0 (these are dielectric models).

Texture resolution: each material slot follows the Maya `file` node feeding it
(via its `fileTextureName`). We don't bake or re-channel-pack — a single Maya
`file` driving baseColor becomes one glTF baseColorTexture pointing at that same
image file. glTF's combined metallic-roughness (B=metal, G=rough) is emitted as
ONE texture only if BOTH metal and rough trace back to the *same* file node
(common for already-packed ORM maps); otherwise whichever single channel has a
texture wins and the other falls back to its factor.

The caller supplies an `image_cache` dict so identical file paths dedupe to one
ImageRef across all materials in the export.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from maya import cmds

from maya_glb_io import _glb_export as GX


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def shader_to_material_def(shader, name, image_cache, options=None):
    # type: (str, str, dict, Optional[dict]) -> GX.MaterialDef
    """Read a Maya surface shader node and produce a glTF MaterialDef.

    `image_cache` maps normalized-abspath -> GX.ImageRef so textures dedupe.
    `options` may carry export flags (currently unused here but threaded for
    future use, e.g. forcing alpha modes).
    """
    options = options or {}
    mdef = GX.MaterialDef(name=name)

    if not shader or not cmds.objExists(shader):
        _warn("material '%s' has no source shader; emitting default" % name)
        return mdef

    node_type = cmds.nodeType(shader)

    try:
        if node_type == "openPBRSurface":
            _from_openpbr(shader, mdef, image_cache)
        elif node_type == "StingrayPBS":
            _from_stingray(shader, mdef, image_cache)
        elif node_type == "aiStandardSurface":
            _from_arnold(shader, mdef, image_cache)
        elif node_type in ("lambert", "blinn", "phong", "phongE", "anisotropic"):
            _from_legacy(shader, node_type, mdef, image_cache)
        else:
            _warn(
                "unsupported shader type '%s' on '%s'; exporting a best-effort "
                "default material" % (node_type, shader)
            )
            _from_generic(shader, mdef, image_cache)
    except Exception as exc:  # never let one bad shader abort the export
        _warn("failed reading shader '%s' (%s): %s — using defaults"
              % (shader, node_type, exc))

    mdef.double_sided = bool(options.get("doubleSided", mdef.double_sided))
    return mdef


# ---------------------------------------------------------------------------
# openPBRSurface
# ---------------------------------------------------------------------------

def _from_openpbr(shader, mdef, image_cache):
    # baseColor
    tex = _texture_for(shader, ("baseColor", "base_color"), image_cache)
    if tex:
        mdef.base_color_texture = tex
    rgb = _color_value(shader, ("baseColor", "base_color"))
    alpha = _scalar_value(shader, ("geometryOpacity", "geometry_opacity", "opacity"))
    if rgb is not None:
        mdef.base_color_factor = [rgb[0], rgb[1], rgb[2],
                                  alpha if alpha is not None else 1.0]

    # metalness
    metal_tex = _texture_for(
        shader, ("baseMetalness", "metalness", "base_metalness"), image_cache
    )
    metal = _scalar_value(shader, ("baseMetalness", "metalness", "base_metalness"))
    if metal is not None:
        mdef.metallic_factor = metal

    # roughness
    rough_tex = _texture_for(
        shader, ("specularRoughness", "specular_roughness"), image_cache
    )
    rough = _scalar_value(shader, ("specularRoughness", "specular_roughness"))
    if rough is not None:
        mdef.roughness_factor = rough

    _assign_metal_rough_textures(mdef, metal_tex, rough_tex)

    # normal (via bump2d -> geometryNormal)
    ntex, nscale = _normal_texture_for(
        shader, ("geometryNormal", "geometry_normal", "normalCamera"), image_cache
    )
    if ntex:
        mdef.normal_texture = ntex
        mdef.normal_scale = nscale

    # emission
    etex = _texture_for(shader, ("emissionColor", "emission_color"), image_cache)
    ecol = _color_value(shader, ("emissionColor", "emission_color"))
    elum = _scalar_value(shader, ("emissionLuminance", "emission_luminance",
                                  "emission"))
    gate = 1.0 if (elum is None or elum > 0.0) else 0.0
    if etex:
        mdef.emissive_texture = etex
        mdef.emissive_factor = [1.0, 1.0, 1.0]
    elif ecol is not None and gate > 0.0:
        mdef.emissive_factor = [ecol[0], ecol[1], ecol[2]]

    _infer_alpha_mode(mdef)


# ---------------------------------------------------------------------------
# StingrayPBS
# ---------------------------------------------------------------------------

def _from_stingray(shader, mdef, image_cache):
    # base color
    if _bool_attr(shader, "use_color_map"):
        tex = _texture_for(shader, ("TEX_color_map", "color_map"), image_cache)
        if tex:
            mdef.base_color_texture = tex
    rgb = _color_value(shader, ("base_color",))
    if rgb is not None:
        mdef.base_color_factor = [rgb[0], rgb[1], rgb[2], 1.0]

    # metallic / roughness factors
    metal = _scalar_value(shader, ("metallic",))
    if metal is not None:
        mdef.metallic_factor = metal
    rough = _scalar_value(shader, ("roughness",))
    if rough is not None:
        mdef.roughness_factor = rough

    metal_tex = rough_tex = None
    if _bool_attr(shader, "use_metallic_map"):
        metal_tex = _texture_for(shader, ("TEX_metallic_map", "metallic_map"),
                                 image_cache)
    if _bool_attr(shader, "use_roughness_map"):
        rough_tex = _texture_for(shader, ("TEX_roughness_map", "roughness_map"),
                                 image_cache)
    _assign_metal_rough_textures(mdef, metal_tex, rough_tex)

    # normal (StingrayPBS takes a raw tangent-space normal file)
    if _bool_attr(shader, "use_normal_map"):
        ntex = _file_image_through(
            _connected_file(shader, ("TEX_normal_map", "normal_map")), image_cache
        )
        if ntex:
            mdef.normal_texture = GX.TextureRef(ntex)
            ns = _scalar_value(shader, ("normal_strength",))
            if ns is not None:
                mdef.normal_scale = ns

    # occlusion
    if _bool_attr(shader, "use_ao_map"):
        aoimg = _file_image_through(
            _connected_file(shader, ("TEX_ao_map", "ao_map")), image_cache
        )
        if aoimg:
            mdef.occlusion_texture = GX.TextureRef(aoimg)

    # emissive
    if _bool_attr(shader, "use_emissive_map"):
        etex = _texture_for(shader, ("TEX_emissive_map", "emissive_map"),
                            image_cache)
        if etex:
            mdef.emissive_texture = etex
            mdef.emissive_factor = [1.0, 1.0, 1.0]
    else:
        ecol = _color_value(shader, ("emissive",))
        if ecol is not None and any(c > 0.0 for c in ecol):
            mdef.emissive_factor = [ecol[0], ecol[1], ecol[2]]

    _infer_alpha_mode(mdef)


# ---------------------------------------------------------------------------
# aiStandardSurface (Arnold)
# ---------------------------------------------------------------------------

def _from_arnold(shader, mdef, image_cache):
    tex = _texture_for(shader, ("baseColor",), image_cache)
    if tex:
        mdef.base_color_texture = tex
    rgb = _color_value(shader, ("baseColor",))
    opacity = _color_value(shader, ("opacity",))
    a = opacity[0] if opacity is not None else 1.0
    if rgb is not None:
        mdef.base_color_factor = [rgb[0], rgb[1], rgb[2], a]

    metal = _scalar_value(shader, ("metalness",))
    if metal is not None:
        mdef.metallic_factor = metal
    rough = _scalar_value(shader, ("specularRoughness",))
    if rough is not None:
        mdef.roughness_factor = rough

    metal_tex = _texture_for(shader, ("metalness",), image_cache)
    rough_tex = _texture_for(shader, ("specularRoughness",), image_cache)
    _assign_metal_rough_textures(mdef, metal_tex, rough_tex)

    # normalCamera fed by aiNormalMap (.input) or bump2d (.bumpValue)
    ntex, nscale = _normal_texture_for(shader, ("normalCamera",), image_cache)
    if ntex:
        mdef.normal_texture = ntex
        mdef.normal_scale = nscale

    etex = _texture_for(shader, ("emissionColor",), image_cache)
    ecol = _color_value(shader, ("emissionColor",))
    egate = _scalar_value(shader, ("emission",))
    if etex:
        mdef.emissive_texture = etex
        mdef.emissive_factor = [1.0, 1.0, 1.0]
    elif ecol is not None and (egate is None or egate > 0.0) and any(c > 0 for c in ecol):
        mdef.emissive_factor = [ecol[0], ecol[1], ecol[2]]

    _infer_alpha_mode(mdef)


# ---------------------------------------------------------------------------
# lambert / blinn / phong (legacy)
# ---------------------------------------------------------------------------

def _from_legacy(shader, node_type, mdef, image_cache):
    tex = _texture_for(shader, ("color",), image_cache)
    if tex:
        mdef.base_color_texture = tex
    rgb = _color_value(shader, ("color",))

    # transparency: Maya uses color where (1,1,1)=fully transparent ->
    # glTF opacity = 1 - transparency (use the red channel as scalar).
    transp = _color_value(shader, ("transparency",))
    alpha = 1.0
    if transp is not None:
        alpha = max(0.0, min(1.0, 1.0 - transp[0]))
    if rgb is not None:
        mdef.base_color_factor = [rgb[0], rgb[1], rgb[2], alpha]
    elif alpha < 1.0:
        mdef.base_color_factor = [1.0, 1.0, 1.0, alpha]

    # Legacy shaders are dielectric — metalness 0.
    mdef.metallic_factor = 0.0

    # Roughness approximated from highlight tightness.
    rough = _legacy_roughness(shader, node_type)
    if rough is not None:
        mdef.roughness_factor = rough

    ntex, nscale = _normal_texture_for(shader, ("normalCamera",), image_cache)
    if ntex:
        mdef.normal_texture = ntex
        mdef.normal_scale = nscale

    inc = _color_value(shader, ("incandescence",))
    inctex = _texture_for(shader, ("incandescence",), image_cache)
    if inctex:
        mdef.emissive_texture = inctex
        mdef.emissive_factor = [1.0, 1.0, 1.0]
    elif inc is not None and any(c > 0.0 for c in inc):
        mdef.emissive_factor = [inc[0], inc[1], inc[2]]

    _infer_alpha_mode(mdef)


def _legacy_roughness(shader, node_type):
    """Approximate glTF roughness from a legacy specular model.

    blinn.eccentricity ~ roughness (0=sharp..1=broad) — use directly.
    phong.cosinePower: high=sharp. roughness ~ sqrt(2/(cosinePower+2)) clamped.
    lambert has no spec model -> rough 1.0 (fully matte).
    """
    import math
    if node_type == "lambert":
        return 1.0
    if node_type in ("blinn",):
        ecc = _scalar_value(shader, ("eccentricity",))
        if ecc is not None:
            return max(0.0, min(1.0, ecc))
        return 0.5
    if node_type in ("phong", "phongE"):
        cp = _scalar_value(shader, ("cosinePower",))
        if cp is not None and cp > 0:
            r = math.sqrt(2.0 / (cp + 2.0))
            return max(0.0, min(1.0, r))
        return 0.5
    return None


# ---------------------------------------------------------------------------
# Generic fallback — try common attribute names heuristically
# ---------------------------------------------------------------------------

def _from_generic(shader, mdef, image_cache):
    """Last-ditch: probe a few near-universal attribute names so even an
    unknown shader contributes its base color instead of a flat default."""
    for cand in (("baseColor",), ("base_color",), ("color",), ("diffuse",),
                 ("diffuseColor",)):
        rgb = _color_value(shader, cand)
        if rgb is not None:
            mdef.base_color_factor = [rgb[0], rgb[1], rgb[2], 1.0]
            tex = _texture_for(shader, cand, image_cache)
            if tex:
                mdef.base_color_texture = tex
            break
    mdef.metallic_factor = 0.0
    mdef.roughness_factor = 1.0


# ---------------------------------------------------------------------------
# Attribute readers
# ---------------------------------------------------------------------------

def _first_attr(shader, candidates):
    for n in candidates:
        if cmds.attributeQuery(n, node=shader, exists=True):
            return n
    return None


def _scalar_value(shader, candidates):
    attr = _first_attr(shader, candidates)
    if attr is None:
        return None
    try:
        v = cmds.getAttr(shader + "." + attr)
    except (RuntimeError, ValueError):
        return None
    if isinstance(v, (list, tuple)):
        if v and isinstance(v[0], (list, tuple)):
            return float(v[0][0])
        if v:
            return float(v[0])
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _color_value(shader, candidates):
    attr = _first_attr(shader, candidates)
    if attr is None:
        return None
    try:
        v = cmds.getAttr(shader + "." + attr)
    except (RuntimeError, ValueError):
        return None
    if isinstance(v, (list, tuple)) and v:
        first = v[0]
        if isinstance(first, (list, tuple)) and len(first) >= 3:
            return [float(first[0]), float(first[1]), float(first[2])]
        if len(v) >= 3 and not isinstance(first, (list, tuple)):
            return [float(v[0]), float(v[1]), float(v[2])]
    return None


def _bool_attr(shader, name):
    if not cmds.attributeQuery(name, node=shader, exists=True):
        # If a StingrayPBS lacks the use_* flag, default to "consider it on"
        # so a connected map still exports. Returning True is the safer choice.
        return True
    try:
        return bool(cmds.getAttr(shader + "." + name))
    except (RuntimeError, ValueError):
        return True


# ---------------------------------------------------------------------------
# Texture / file-node tracing
# ---------------------------------------------------------------------------

def _connected_file(shader, candidates):
    """Walk upstream from shader.<attr> to find a Maya `file` node. Returns the
    file node name or None. Traverses through common intermediary nodes
    (bump2d, aiNormalMap, aiImage, reverse, multiplyDivide, gammaCorrect)."""
    attr = _first_attr(shader, candidates)
    if attr is None:
        return None
    return _trace_to_file(shader + "." + attr, depth=0)


def _trace_to_file(plug, depth):
    if depth > 8:
        return None
    srcs = cmds.listConnections(plug, source=True, destination=False,
                                plugs=False) or []
    for src in srcs:
        if not cmds.objExists(src):
            continue
        ntype = cmds.nodeType(src)
        if ntype == "file":
            return src
        if ntype == "aiImage":
            return src  # treated like a file (has .filename)
        # Walk through known pass-through utility nodes.
        for in_attr in ("bumpValue", "input", "inputX", "input1", "value",
                         "color", "inColor", "outValue", "normalCamera"):
            if cmds.attributeQuery(in_attr, node=src, exists=True):
                found = _trace_to_file(src + "." + in_attr, depth + 1)
                if found:
                    return found
        # Generic: probe every incoming connection one level deeper.
        for up in cmds.listConnections(src, source=True, destination=False) or []:
            if cmds.objExists(up) and cmds.nodeType(up) in ("file", "aiImage"):
                return up
    return None


def _file_texture_path(file_node):
    """Return the resolved fileTextureName (or aiImage.filename) for a node."""
    if not file_node or not cmds.objExists(file_node):
        return None
    ntype = cmds.nodeType(file_node)
    attr = "fileTextureName" if ntype == "file" else "filename"
    if not cmds.attributeQuery(attr, node=file_node, exists=True):
        return None
    try:
        path = cmds.getAttr(file_node + "." + attr)
    except (RuntimeError, ValueError):
        return None
    return path or None


def _intern_image(path, image_cache, name=None):
    import os
    key = os.path.normcase(os.path.abspath(path))
    if key in image_cache:
        return image_cache[key]
    img = GX.ImageRef(file_path=path, name=name)
    image_cache[key] = img
    return img


def _file_image_through(file_node, image_cache):
    """file node -> deduped ImageRef (or None)."""
    path = _file_texture_path(file_node)
    if not path:
        return None
    return _intern_image(path, image_cache)


def _texture_for(shader, candidates, image_cache):
    """Return a GX.TextureRef if shader.<attr> is driven by a file, else None."""
    fnode = _connected_file(shader, candidates)
    img = _file_image_through(fnode, image_cache)
    if img is None:
        return None
    return GX.TextureRef(img)


def _normal_texture_for(shader, candidates, image_cache):
    """Resolve a normal map. Returns (TextureRef or None, scale_float).

    Handles the bump2d / aiNormalMap indirection used on import: the shader's
    normal input connects to a utility whose own input is the file. We also
    pull a scale from bump2d.bumpDepth / aiNormalMap.strength when present.
    """
    attr = _first_attr(shader, candidates)
    if attr is None:
        return None, 1.0

    scale = 1.0
    srcs = cmds.listConnections(shader + "." + attr, source=True,
                                destination=False) or []
    for src in srcs:
        if not cmds.objExists(src):
            continue
        ntype = cmds.nodeType(src)
        if ntype == "bump2d":
            s = _scalar_value(src, ("bumpDepth",))
            if s is not None:
                scale = s
        elif ntype == "aiNormalMap":
            s = _scalar_value(src, ("strength",))
            if s is not None:
                scale = s

    fnode = _connected_file(shader, candidates)
    img = _file_image_through(fnode, image_cache)
    if img is None:
        return None, scale
    return GX.TextureRef(img), scale


# ---------------------------------------------------------------------------
# Shared post-processing
# ---------------------------------------------------------------------------

def _assign_metal_rough_textures(mdef, metal_tex, rough_tex):
    """glTF packs metal in B and rough in G of ONE texture. We only emit a
    combined metallicRoughnessTexture when both channels resolve to the SAME
    image file (already-packed ORM workflow). Otherwise we keep factors and
    skip the texture, since splitting one Maya file across two glTF channels
    would require image processing we don't do here."""
    if metal_tex and rough_tex:
        same = (metal_tex.image.file_path and rough_tex.image.file_path and
                _samefile(metal_tex.image.file_path, rough_tex.image.file_path))
        if same:
            mdef.metallic_roughness_texture = metal_tex
            return
        # Different files — prefer roughness texture (more visually impactful),
        # keep metal as a factor. Warn so the user knows a channel was dropped.
        _warn("metallic and roughness use different texture files; glTF needs "
              "them packed in one image. Emitting the roughness texture only; "
              "metalness exported as a factor.")
        mdef.metallic_roughness_texture = rough_tex
    elif metal_tex:
        mdef.metallic_roughness_texture = metal_tex
    elif rough_tex:
        mdef.metallic_roughness_texture = rough_tex


def _samefile(a, b):
    import os
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _infer_alpha_mode(mdef):
    """If baseColor alpha < 1 or a baseColor texture likely carries alpha, set
    BLEND. Conservative: only flips to BLEND when the factor alpha < 1.0. We
    don't sniff texture alpha channels (would require decoding the image)."""
    if mdef.alpha_mode != "OPAQUE":
        return
    if len(mdef.base_color_factor) >= 4 and mdef.base_color_factor[3] < 1.0:
        mdef.alpha_mode = "BLEND"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _warn(msg):
    try:
        import maya.api.OpenMaya as om
        om.MGlobal.displayWarning("[glb_io] export: " + msg)
    except Exception:
        pass
