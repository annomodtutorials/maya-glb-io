"""openPBR target — Maya 2025+ native modern PBR shader.

openPBRSurface follows the OpenPBR Surface spec (Autodesk + Adobe + others,
2024). Maps cleanly to glTF metal-rough PBR.

Attribute names follow the OpenPBR spec, camelCase:
    baseColor / baseWeight / baseMetalness / baseDiffuseRoughness
    specularColor / specularWeight / specularRoughness / specularIOR
    emissionColor / emissionLuminance
    geometryNormal / geometryCoatNormal / geometryOpacity

For tangent-space normals openPBR's `geometryNormal` is a vector input —
it expects a bump2d.outNormal connection, NOT a raw file.outColor.
Same workflow as the classic Maya bump2d → normalCamera pattern.
"""
from __future__ import annotations

from maya import cmds
import maya.api.OpenMaya as om

from maya_glb_io._materials import _common as C


# Try multiple attribute names per role (Maya version differences)
_ATTRS = {
    "base_color":  ("baseColor", "base_color"),
    "metalness":   ("baseMetalness", "metalness", "base_metalness"),
    "roughness":   ("specularRoughness", "specular_roughness"),
    "normal":      ("geometryNormal", "geometry_normal", "normalCamera"),
    "emission_color": ("emissionColor", "emission_color"),
    "emission":    ("emissionLuminance", "emission_luminance", "emission"),
}


def build_placeholder_material(index, name_prefix):
    mat_name = C.join_prefix(name_prefix, "material_{0}".format(index))
    shader = cmds.shadingNode("openPBRSurface", asShader=True, name=mat_name + "_SHD")
    sg = C.make_shading_group(shader, mat_name + "_SG")
    return shader, sg


def build_material(gltf, mat, index, image_paths, name_prefix):
    mat_name = C.sanitize(mat.get("name") or "material_%d" % index)
    mat_name = C.join_prefix(name_prefix, mat_name)

    shader = cmds.shadingNode("openPBRSurface", asShader=True, name=mat_name + "_SHD")
    sg = C.make_shading_group(shader, mat_name + "_SG")

    sg_ext = (mat.get("extensions") or {}).get("KHR_materials_pbrSpecularGlossiness")

    if sg_ext is not None:
        # Legacy spec-gloss material (e.g. older Sketchfab exports). openPBR has
        # a native specular workflow, so map spec-gloss straight onto it instead
        # of the lossy metal-rough conversion — keeps both textures and avoids
        # the spurious metalness that makes these go dark.
        base_file = _from_specgloss(gltf, mat, sg_ext, shader, image_paths, mat_name)
        opacity_pbr = {
            "baseColorFactor": sg_ext.get("diffuseFactor", [1.0, 1.0, 1.0, 1.0]),
            "baseColorTexture": sg_ext.get("diffuseTexture"),
        }
    else:
        pbr = C.get_pbr_metallic_roughness(mat)
        base_file = _base_color(gltf, pbr, shader, image_paths, mat_name)
        _metal_rough(gltf, pbr, shader, image_paths, mat_name)
        opacity_pbr = pbr

    _normal(gltf, mat, shader, image_paths, mat_name)
    _emissive(gltf, mat, shader, image_paths, mat_name)
    _opacity(gltf, mat, opacity_pbr, shader, base_file)
    _transmission(gltf, mat, shader, image_paths, mat_name)

    # Advanced glTF material extensions -> openPBR layers. Each is a no-op when
    # its extension dict is absent, so plain PBR / spec-gloss materials are
    # completely unaffected. They are independent layers on top of the base, so
    # they run for BOTH the metal-rough and the spec-gloss branch.
    _from_clearcoat(gltf, mat, shader, image_paths, mat_name)
    _from_sheen(gltf, mat, shader, image_paths, mat_name)
    _from_anisotropy(gltf, mat, shader, image_paths, mat_name)
    _from_iridescence(gltf, mat, shader, image_paths, mat_name)
    _from_specular(gltf, mat, shader, image_paths, mat_name)

    return shader, sg


def _first_existing_attr(shader, candidate_names):
    for n in candidate_names:
        if cmds.attributeQuery(n, node=shader, exists=True):
            return n
    return None


def _connect_with_fallback(src, shader, candidates, label):
    attr = _first_existing_attr(shader, candidates)
    if attr is None:
        om.MGlobal.displayWarning(
            "[glb_io] openPBRSurface has no attribute matching '{0}' (tried {1})".format(
                label, " / ".join(candidates)
            )
        )
        return False
    try:
        cmds.connectAttr(src, shader + "." + attr, force=True)
        return True
    except RuntimeError as exc:
        om.MGlobal.displayWarning(
            "[glb_io] openPBR connect failed: {0} -> {1}.{2}: {3}".format(
                src, shader, attr, exc
            )
        )
        return False


def _set_with_fallback(shader, candidates, value):
    attr = _first_existing_attr(shader, candidates)
    if attr is None:
        return False
    try:
        if isinstance(value, (list, tuple)):
            cmds.setAttr(shader + "." + attr, *value, type="double3")
        else:
            cmds.setAttr(shader + "." + attr, value)
        return True
    except (RuntimeError, ValueError):
        return False


def _base_color(gltf, pbr, shader, image_paths, name):
    """Wire base color. Returns the baseColor file node (or None) so the caller
    can pull its alpha channel for transparency."""
    factor = pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0])
    _set_with_fallback(shader, _ATTRS["base_color"], [factor[0], factor[1], factor[2]])

    tex_ref = pbr.get("baseColorTexture")
    if tex_ref is None:
        return None

    img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
    if img_idx is None or img_idx >= len(image_paths):
        return None

    f = C.make_file_node(image_paths[img_idx], "sRGB", name + "_baseColor")
    _connect_with_fallback(f + ".outColor", shader, _ATTRS["base_color"], "baseColor")
    return f


# Opacity input candidates on openPBRSurface (scalar, 1 = opaque, matching
# glTF's alpha convention exactly).
_OPACITY_ATTRS = ("geometryOpacity", "geometry_opacity")


def _opacity(gltf, mat, pbr, shader, base_file):
    """Import glTF transparency into openPBRSurface.geometryOpacity.

    glTF alpha is 1 = opaque, 0 = transparent — the same convention as
    geometryOpacity, so it maps directly with no inversion. Only BLEND / MASK
    materials get wired; OPAQUE (the glTF default, e.g. DamagedHelmet) is left
    fully opaque because there is no transparency data to import.

    If the base-color texture carries an alpha channel we drive opacity from it;
    otherwise we fall back to the constant baseColorFactor alpha. (glTF would
    multiply the two; v1 uses whichever is present, texture taking priority.)
    """
    alpha_mode = (mat.get("alphaMode") or "OPAQUE").upper()
    if alpha_mode == "OPAQUE":
        return

    factor = pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0])
    alpha = factor[3] if len(factor) > 3 else 1.0

    if base_file is not None and pbr.get("baseColorTexture") is not None:
        _connect_with_fallback(
            base_file + ".outAlpha", shader, _OPACITY_ATTRS, "geometryOpacity"
        )
    else:
        _set_with_fallback(shader, _OPACITY_ATTRS, alpha)


# Transmission (refractive glass) attribute candidates on openPBRSurface.
_TR_WEIGHT_ATTRS = ("transmissionWeight", "transmission_weight")
_TR_COLOR_ATTRS = ("transmissionColor", "transmission_color")
_TR_DEPTH_ATTRS = ("transmissionDepth", "transmission_depth")
_IOR_ATTRS = ("specularIOR", "specular_IOR", "specular_ior")


def _transmission(gltf, mat, shader, image_paths, name):
    """Import glTF KHR_materials_transmission (+ ior / volume) into
    openPBRSurface's transmission layer for true refractive glass.

    transmissionFactor -> transmissionWeight (texture R channel if present),
    KHR_materials_ior  -> specularIOR (default 1.5),
    KHR_materials_volume.attenuationColor    -> transmissionColor,
    KHR_materials_volume.attenuationDistance -> transmissionDepth.

    Note: refraction renders in Arnold/a renderer; Maya's default Viewport 2.0
    shows it only approximately.
    """
    info = C.transmission_info(mat)
    if not info:
        return
    if info["factor"] <= 0.0 and info["texture"] is None:
        return

    # Transmission weight (scalar). Drive from the texture R channel if present,
    # else the constant factor.
    tex_ref = info["texture"]
    wired = False
    if tex_ref is not None:
        img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            f = C.make_file_node(image_paths[img_idx], "Raw", name + "_transmission")
            wired = _connect_with_fallback(
                f + ".outColorR", shader, _TR_WEIGHT_ATTRS, "transmissionWeight"
            )
    if not wired:
        _set_with_fallback(shader, _TR_WEIGHT_ATTRS, info["factor"] or 1.0)

    # Index of refraction.
    _set_with_fallback(shader, _IOR_ATTRS, info["ior"])

    # Volume absorption tint + depth (solid glass body).
    if info["attenuation_color"] is not None:
        ac = info["attenuation_color"]
        _set_with_fallback(shader, _TR_COLOR_ATTRS, [ac[0], ac[1], ac[2]])
    if info["attenuation_distance"] is not None:
        _set_with_fallback(shader, _TR_DEPTH_ATTRS, info["attenuation_distance"])


def _metal_rough(gltf, pbr, shader, image_paths, name):
    metal_f = pbr.get("metallicFactor", 1.0)
    rough_f = pbr.get("roughnessFactor", 1.0)

    _set_with_fallback(shader, _ATTRS["metalness"], metal_f)
    _set_with_fallback(shader, _ATTRS["roughness"], rough_f)

    tex_ref = pbr.get("metallicRoughnessTexture")
    if tex_ref is None:
        return

    img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
    if img_idx is None or img_idx >= len(image_paths):
        return

    f = C.make_file_node(image_paths[img_idx], "Raw", name + "_metalRough")

    # glTF spec: metallicRoughnessTexture.B → metalness, .G → roughness (both scalar)
    _connect_with_fallback(f + ".outColorB", shader, _ATTRS["metalness"], "baseMetalness")
    _connect_with_fallback(f + ".outColorG", shader, _ATTRS["roughness"], "specularRoughness")


# openPBR specular-colour input candidates (the dielectric specular tint used by
# the legacy spec-gloss path).
_SPEC_COLOR_ATTRS = ("specularColor", "specular_color")


def _from_specgloss(gltf, mat, ext, shader, image_paths, name):
    """Map a legacy KHR_materials_pbrSpecularGlossiness material onto
    openPBRSurface's NATIVE specular workflow — the lossless target for
    spec-gloss (no spurious metalness, both textures used):

        diffuseFactor / diffuseTexture        -> baseColor
        metalness                             -> 0 (spec-gloss is dielectric)
        specularFactor / sgTexture.rgb        -> specularColor
        glossinessFactor / sgTexture.alpha    -> specularRoughness (1 - gloss)

    Returns the baseColor file node (or None) so the caller can pull the diffuse
    alpha for transparency.
    """
    diffuse = ext.get("diffuseFactor", [1.0, 1.0, 1.0, 1.0])
    specular = ext.get("specularFactor", [1.0, 1.0, 1.0])
    gloss = ext.get("glossinessFactor", 1.0)

    # Base colour (diffuse).
    _set_with_fallback(shader, _ATTRS["base_color"], [diffuse[0], diffuse[1], diffuse[2]])
    base_file = None
    dt = ext.get("diffuseTexture")
    if dt is not None:
        img_idx = C.texture_image_index(gltf, dt.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            base_file = C.make_file_node(image_paths[img_idx], "sRGB", name + "_baseColor")
            _connect_with_fallback(
                base_file + ".outColor", shader, _ATTRS["base_color"], "baseColor"
            )

    # Dielectric — no metalness (this is the key fix vs the old metal-rough
    # conversion, which over-estimated metalness and made these go dark).
    _set_with_fallback(shader, _ATTRS["metalness"], 0.0)

    # Specular colour + roughness factors.
    _set_with_fallback(shader, _SPEC_COLOR_ATTRS, [specular[0], specular[1], specular[2]])
    _set_with_fallback(shader, _ATTRS["roughness"], 1.0 - gloss)

    # Per-pixel spec-gloss texture: RGB = specular colour, alpha = glossiness.
    sgt = ext.get("specularGlossinessTexture")
    if sgt is not None:
        img_idx = C.texture_image_index(gltf, sgt.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            f = C.make_file_node(image_paths[img_idx], "sRGB", name + "_specGloss")
            _connect_with_fallback(
                f + ".outColor", shader, _SPEC_COLOR_ATTRS, "specularColor"
            )
            # glossiness (alpha) -> invert -> roughness
            rev = cmds.shadingNode("reverse", asUtility=True, name=name + "_gloss_rev")
            try:
                cmds.connectAttr(f + ".outAlpha", rev + ".inputX", force=True)
                _connect_with_fallback(
                    rev + ".outputX", shader, _ATTRS["roughness"], "specularRoughness"
                )
            except RuntimeError:
                pass

    return base_file


def _normal(gltf, mat, shader, image_paths, name):
    """openPBR's geometryNormal expects a vector — wire through bump2d.

    Pattern:
        file (Raw)  .outAlpha  ->  bump2d.bumpValue
        bump2d.bumpInterp = 1 (Tangent Space Normals)
        bump2d.outNormal  ->  openPBRSurface.geometryNormal
    """
    tex_ref = mat.get("normalTexture")
    if tex_ref is None:
        return

    img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
    if img_idx is None or img_idx >= len(image_paths):
        return

    f = C.make_file_node(image_paths[img_idx], "Raw", name + "_normal")
    bump = cmds.shadingNode("bump2d", asUtility=True, name=name + "_bump")
    cmds.setAttr(bump + ".bumpInterp", 1)
    cmds.connectAttr(f + ".outAlpha", bump + ".bumpValue", force=True)

    scale = tex_ref.get("scale", 1.0)
    if scale != 1.0:
        cmds.setAttr(bump + ".bumpDepth", scale)

    _connect_with_fallback(bump + ".outNormal", shader, _ATTRS["normal"], "geometryNormal")


def _emissive(gltf, mat, shader, image_paths, name):
    """Wire emission. openPBR emission is luminance in NITS, so we drive
    emissionLuminance from the tunable C.EMISSION_LUMINANCE default (set per
    import from the emissionLuminance preference) times any
    KHR_materials_emissive_strength. emissionColor carries the colour/texture.
    A luminance of 1.0 (the old default) is essentially black after tone-map —
    hence the much higher default so glTF emissives actually glow."""
    factor = mat.get("emissiveFactor", [0.0, 0.0, 0.0])
    tex_ref = mat.get("emissiveTexture")
    luminance = C.EMISSION_LUMINANCE * C.emissive_strength(mat)

    if tex_ref is not None:
        img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            f = C.make_file_node(image_paths[img_idx], "sRGB", name + "_emissive")
            _connect_with_fallback(
                f + ".outColor", shader, _ATTRS["emission_color"], "emissionColor"
            )
            _set_with_fallback(shader, _ATTRS["emission"], luminance)
            return

    if factor != [0.0, 0.0, 0.0]:
        _set_with_fallback(
            shader, _ATTRS["emission_color"], [factor[0], factor[1], factor[2]]
        )
        _set_with_fallback(shader, _ATTRS["emission"], luminance)


# ---------------------------------------------------------------------------
# Advanced material extensions -> openPBR coat / fuzz / anisotropy / thin-film
# / specular layers. Each helper is gated on its extension dict being present
# (the C.*_info readers return None otherwise) so a plain PBR material is never
# touched. All attribute writes go through _set_with_fallback /
# _connect_with_fallback, so a Maya build whose openPBRSurface lacks one of
# these inputs degrades with a warning instead of crashing.
# ---------------------------------------------------------------------------

# Coat (clearcoat) layer attribute candidates. (glTF clearcoat supplies no coat
# colour or coat IOR, so only weight / roughness / normal are mapped.)
_COAT_WEIGHT_ATTRS = ("coatWeight", "coat_weight")
_COAT_ROUGH_ATTRS = ("coatRoughness", "coat_roughness")
_COAT_NORMAL_ATTRS = ("geometryCoatNormal", "geometry_coat_normal", "coatNormal")


def _from_clearcoat(gltf, mat, shader, image_paths, name):
    """KHR_materials_clearcoat -> openPBR coat layer.

        clearcoatFactor          -> coatWeight     (texture R channel if present)
        clearcoatRoughnessFactor -> coatRoughness  (texture G channel if present)
        clearcoatNormalTexture   -> geometryCoatNormal (via bump2d, tangent space)

    Skipped entirely when clearcoatFactor == 0 and there is no clearcoat texture.
    """
    info = C.clearcoat_info(mat)
    if not info:
        return
    if info["factor"] <= 0.0 and info["texture"] is None:
        return

    # Coat weight: texture R channel if present, else the constant factor.
    weight_wired = False
    tex_ref = info["texture"]
    if tex_ref is not None:
        img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            f = C.make_file_node(image_paths[img_idx], "Raw", name + "_clearcoat")
            weight_wired = _connect_with_fallback(
                f + ".outColorR", shader, _COAT_WEIGHT_ATTRS, "coatWeight"
            )
    if not weight_wired:
        _set_with_fallback(shader, _COAT_WEIGHT_ATTRS, info["factor"] or 1.0)

    # Coat roughness: texture G channel if present, else the constant factor.
    rough_wired = False
    rtex_ref = info["roughness_texture"]
    if rtex_ref is not None:
        img_idx = C.texture_image_index(gltf, rtex_ref.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            f = C.make_file_node(image_paths[img_idx], "Raw", name + "_clearcoatRough")
            rough_wired = _connect_with_fallback(
                f + ".outColorG", shader, _COAT_ROUGH_ATTRS, "coatRoughness"
            )
    if not rough_wired:
        _set_with_fallback(shader, _COAT_ROUGH_ATTRS, info["roughness"])

    # Coat normal map (tangent space) -> geometryCoatNormal via bump2d, mirroring
    # the base-layer _normal() workflow.
    ntex_ref = info["normal_texture"]
    if ntex_ref is not None:
        img_idx = C.texture_image_index(gltf, ntex_ref.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            nf = C.make_file_node(image_paths[img_idx], "Raw", name + "_clearcoatNormal")
            bump = cmds.shadingNode("bump2d", asUtility=True, name=name + "_coatBump")
            cmds.setAttr(bump + ".bumpInterp", 1)  # tangent-space normals
            cmds.connectAttr(nf + ".outAlpha", bump + ".bumpValue", force=True)
            scale = ntex_ref.get("scale", 1.0)
            if scale != 1.0:
                cmds.setAttr(bump + ".bumpDepth", scale)
            _connect_with_fallback(
                bump + ".outNormal", shader, _COAT_NORMAL_ATTRS, "geometryCoatNormal"
            )


# Fuzz (sheen) layer attribute candidates.
_FUZZ_WEIGHT_ATTRS = ("fuzzWeight", "fuzz_weight")
_FUZZ_COLOR_ATTRS = ("fuzzColor", "fuzz_color")
_FUZZ_ROUGH_ATTRS = ("fuzzRoughness", "fuzz_roughness")


def _from_sheen(gltf, mat, shader, image_paths, name):
    """KHR_materials_sheen -> openPBR fuzz layer.

        sheenColorFactor     -> fuzzColor     (+ RGB texture)
        (sheen present)       -> fuzzWeight = 1.0  (openPBR fuzz needs a weight;
                                 sheenColor itself is just the tint)
        sheenRoughnessFactor -> fuzzRoughness (+ texture A channel)

    Skipped when sheenColor is [0,0,0] and there is no sheen colour texture.
    """
    info = C.sheen_info(mat)
    if not info:
        return
    color = info["color"]
    if color == [0.0, 0.0, 0.0] and info["color_texture"] is None:
        return

    # Fuzz colour (the sheen tint).
    _set_with_fallback(shader, _FUZZ_COLOR_ATTRS, [color[0], color[1], color[2]])
    ctex_ref = info["color_texture"]
    if ctex_ref is not None:
        img_idx = C.texture_image_index(gltf, ctex_ref.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            f = C.make_file_node(image_paths[img_idx], "sRGB", name + "_sheenColor")
            _connect_with_fallback(
                f + ".outColor", shader, _FUZZ_COLOR_ATTRS, "fuzzColor"
            )

    # openPBR fuzz needs a non-zero weight to render; glTF sheen has no separate
    # weight (the colour is the strength), so enable the layer fully.
    _set_with_fallback(shader, _FUZZ_WEIGHT_ATTRS, 1.0)

    # Fuzz roughness: texture A channel if present, else the constant factor.
    rough_wired = False
    rtex_ref = info["roughness_texture"]
    if rtex_ref is not None:
        img_idx = C.texture_image_index(gltf, rtex_ref.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            f = C.make_file_node(image_paths[img_idx], "Raw", name + "_sheenRough")
            rough_wired = _connect_with_fallback(
                f + ".outAlpha", shader, _FUZZ_ROUGH_ATTRS, "fuzzRoughness"
            )
    if not rough_wired:
        _set_with_fallback(shader, _FUZZ_ROUGH_ATTRS, info["roughness"])


# Specular anisotropy attribute candidates.
_ANISO_ATTRS = ("specularRoughnessAnisotropy", "specular_roughness_anisotropy")
_ANISO_ROT_ATTRS = (
    "specularRoughnessAnisotropyRotation",
    "specular_roughness_anisotropy_rotation",
)


def _from_anisotropy(gltf, mat, shader, image_paths, name):
    """KHR_materials_anisotropy -> openPBR specular anisotropy.

        anisotropyStrength -> specularRoughnessAnisotropy (0..1)
        anisotropyRotation -> the anisotropy rotation attr if present

    glTF's anisotropyRotation is in radians; openPBR's rotation attr is expressed
    in turns (0..1 = a full rotation). We convert radians -> turns
    (rad / 2*pi). If openPBRSurface has no rotation attr we set strength only and
    warn. Skipped when anisotropyStrength == 0.
    """
    info = C.anisotropy_info(mat)
    if not info:
        return
    if info["strength"] <= 0.0:
        return

    # Per-pixel strength: glTF's anisotropyTexture BLUE channel holds strength
    # (RG hold the direction, which openPBR has no per-pixel input for — see the
    # rotation note below). Wire B -> specularRoughnessAnisotropy if present,
    # else use the constant strength factor.
    strength_wired = False
    tex_ref = info.get("texture")
    if tex_ref is not None:
        img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            f = C.make_file_node(image_paths[img_idx], "Raw", name + "_anisotropy")
            strength_wired = _connect_with_fallback(
                f + ".outColorB", shader, _ANISO_ATTRS, "specularRoughnessAnisotropy"
            )
    if not strength_wired:
        _set_with_fallback(shader, _ANISO_ATTRS, info["strength"])

    rotation = info["rotation"]
    if rotation:
        # radians -> turns (0..1). openPBRSurface's anisotropy rotation is a
        # normalised 0..1 turn value.
        import math
        turns = (rotation / (2.0 * math.pi)) % 1.0
        if not _set_with_fallback(shader, _ANISO_ROT_ATTRS, turns):
            om.MGlobal.displayWarning(
                "[glb_io] openPBRSurface has no anisotropy-rotation attribute "
                "(tried {0}); set anisotropy strength only".format(
                    " / ".join(_ANISO_ROT_ATTRS)
                )
            )


# Thin-film (iridescence) attribute candidates.
_THINFILM_WEIGHT_ATTRS = ("thinFilmWeight", "thin_film_weight")
_THINFILM_THICK_ATTRS = ("thinFilmThickness", "thin_film_thickness")
_THINFILM_IOR_ATTRS = ("thinFilmIOR", "thin_film_IOR", "thin_film_ior")


def _from_iridescence(gltf, mat, shader, image_paths, name):
    """KHR_materials_iridescence -> openPBR thin-film layer.

        iridescenceFactor           -> thinFilmWeight   (ENABLES the layer!)
        iridescenceIor              -> thinFilmIOR
        iridescenceThicknessMaximum -> thinFilmThickness (nanometres)

    CRITICAL: openPBR's thin film has its OWN weight (default 0 = off). Setting
    only thickness + IOR leaves the effect invisible — the weight must be set to
    turn the layer on. openPBR's thin film is a SINGLE thickness, whereas glTF
    supplies a min/max range (optionally textured); we approximate with the
    maximum thickness (strongest spread). Skipped when iridescenceFactor == 0.
    """
    info = C.iridescence_info(mat)
    if not info:
        return
    if info["factor"] <= 0.0:
        return

    # Turn the thin-film layer ON (this was the missing piece). Weight from the
    # iridescenceTexture R channel if present, else the constant factor.
    weight_wired = False
    wtex = info.get("texture")
    if wtex is not None:
        img_idx = C.texture_image_index(gltf, wtex.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            f = C.make_file_node(image_paths[img_idx], "Raw", name + "_iridescence")
            weight_wired = _connect_with_fallback(
                f + ".outColorR", shader, _THINFILM_WEIGHT_ATTRS, "thinFilmWeight"
            )
    if not weight_wired:
        _set_with_fallback(shader, _THINFILM_WEIGHT_ATTRS, info["factor"])

    _set_with_fallback(shader, _THINFILM_IOR_ATTRS, info["ior"])

    # Thickness: the iridescenceThicknessTexture G channel is remapped from
    # [0,1] to [thicknessMin, thicknessMax] nm via a remapValue node and driven
    # into thinFilmThickness. THIS is what the "iridescence map" plugs into.
    # Without a texture we use the constant maximum thickness.
    thick_wired = False
    ttex = info.get("thickness_texture")
    if ttex is not None:
        img_idx = C.texture_image_index(gltf, ttex.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            tf = C.make_file_node(image_paths[img_idx], "Raw", name + "_iridThick")
            remap = cmds.shadingNode("remapValue", asUtility=True,
                                     name=name + "_iridThickRemap")
            try:
                cmds.connectAttr(tf + ".outColorG", remap + ".inputValue", force=True)
                cmds.setAttr(remap + ".outputMin", info["thickness_min"])
                cmds.setAttr(remap + ".outputMax", info["thickness_max"])
                thick_wired = _connect_with_fallback(
                    remap + ".outValue", shader, _THINFILM_THICK_ATTRS,
                    "thinFilmThickness"
                )
            except RuntimeError:
                pass
    if not thick_wired:
        _set_with_fallback(shader, _THINFILM_THICK_ATTRS, info["thickness_max"])


# Specular (dielectric) layer attribute candidates.
_SPEC_WEIGHT_ATTRS = ("specularWeight", "specular_weight")
# _SPEC_COLOR_ATTRS already defined above (shared with the spec-gloss path).


def _from_specular(gltf, mat, shader, image_paths, name):
    """KHR_materials_specular -> openPBR specular weight / colour.

        specularFactor      -> specularWeight (texture A channel if present)
        specularColorFactor -> specularColor  (+ RGB texture, sRGB)

    Only applied when the extension is present, so it never clobbers the
    spec-gloss path's specularColor (that path has no KHR_materials_specular).
    Skipping it entirely when absent keeps metal-rough materials untouched.
    """
    info = C.specular_info(mat)
    if not info:
        return

    # Specular weight: texture A channel if present, else the constant factor.
    weight_wired = False
    tex_ref = info["texture"]
    if tex_ref is not None:
        img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            f = C.make_file_node(image_paths[img_idx], "Raw", name + "_specular")
            weight_wired = _connect_with_fallback(
                f + ".outAlpha", shader, _SPEC_WEIGHT_ATTRS, "specularWeight"
            )
    if not weight_wired:
        _set_with_fallback(shader, _SPEC_WEIGHT_ATTRS, info["factor"])

    # Specular colour: RGB texture if present, else the constant factor.
    color = info["color"]
    _set_with_fallback(shader, _SPEC_COLOR_ATTRS, [color[0], color[1], color[2]])
    ctex_ref = info["color_texture"]
    if ctex_ref is not None:
        img_idx = C.texture_image_index(gltf, ctex_ref.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            f = C.make_file_node(image_paths[img_idx], "sRGB", name + "_specularColor")
            _connect_with_fallback(
                f + ".outColor", shader, _SPEC_COLOR_ATTRS, "specularColor"
            )
