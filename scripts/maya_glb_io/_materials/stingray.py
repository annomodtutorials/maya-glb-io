"""StingrayPBS target — Maya-native, viewport-renderable PBR shader.

Bundled with Maya 2017+ via `shaderFXPlugin`. Best default for users who
want PBR-looking results in the viewport without installing a renderer.

StingrayPBS attribute names vary slightly between Maya versions:
    - Texture INPUTS are `TEX_color_map`, `TEX_normal_map`, etc.
      (with the `TEX_` prefix — visible as "TEX Color Map" in the AE)
    - Boolean toggles are `use_color_map`, `use_normal_map`, etc.
    - Scalar values are `base_color`, `metallic`, `roughness`, `emissive`.

We try the TEX_-prefixed name first, fall back to the bare name, and
warn loudly if neither works so the user can paste back the warning.
"""
from __future__ import annotations

from maya import cmds
import maya.api.OpenMaya as om

from maya_glb_io._materials import _common as C


# Map role -> list of candidate StingrayPBS texture-input attribute names,
# tried in order. First name that exists on the shader gets the connection.
_TEX_CANDIDATES = {
    "color":     ("TEX_color_map", "color_map"),
    "normal":    ("TEX_normal_map", "normal_map"),
    "metallic":  ("TEX_metallic_map", "metallic_map"),
    "roughness": ("TEX_roughness_map", "roughness_map"),
    "emissive":  ("TEX_emissive_map", "emissive_map"),
    "ao":        ("TEX_ao_map", "ao_map"),
}


def build_placeholder_material(index, name_prefix):
    mat_name = C.join_prefix(name_prefix, "material_{0}".format(index))
    shader = cmds.shadingNode("StingrayPBS", asShader=True, name=mat_name + "_SHD")
    sg = C.make_shading_group(shader, mat_name + "_SG")
    return shader, sg


def build_material(gltf, mat, index, image_paths, name_prefix):
    mat_name = C.sanitize(mat.get("name") or "material_%d" % index)
    mat_name = C.join_prefix(name_prefix, mat_name)

    shader = cmds.shadingNode("StingrayPBS", asShader=True, name=mat_name + "_SHD")
    sg = C.make_shading_group(shader, mat_name + "_SG")

    pbr = C.get_pbr_metallic_roughness(mat)

    _base_color(gltf, pbr, shader, image_paths, mat_name)
    _metal_rough(gltf, pbr, shader, image_paths, mat_name)
    _normal(gltf, mat, shader, image_paths, mat_name)
    _occlusion(gltf, mat, shader, image_paths, mat_name)
    _emissive(gltf, mat, shader, image_paths, mat_name)

    return shader, sg


# --- helpers -----------------------------------------------------------------

def _connect_map(shader, role, file_out):
    """Connect a file node's output to StingrayPBS's map input for `role`.

    Tries each candidate attribute name in order; returns the name that worked
    or None and logs a visible warning. Also flips the matching use_*_map flag.
    """
    for attr_name in _TEX_CANDIDATES[role]:
        full_attr = shader + "." + attr_name
        if not cmds.attributeQuery(attr_name, node=shader, exists=True):
            continue
        try:
            cmds.connectAttr(file_out, full_attr, force=True)
            # Flip the matching use_*_map boolean
            use_attr_name = "use_" + role + "_map"
            if cmds.attributeQuery(use_attr_name, node=shader, exists=True):
                cmds.setAttr(shader + "." + use_attr_name, 1)
            return attr_name
        except RuntimeError as exc:
            om.MGlobal.displayWarning(
                "[glb_io] StingrayPBS connect failed: {0} -> {1}: {2}".format(
                    file_out, full_attr, exc
                )
            )

    om.MGlobal.displayWarning(
        "[glb_io] StingrayPBS shader {0} has no attribute matching role '{1}' "
        "(tried {2}). Texture will be created but not connected.".format(
            shader, role, " / ".join(_TEX_CANDIDATES[role])
        )
    )
    return None


# --- channel wirings ---------------------------------------------------------

def _base_color(gltf, pbr, shader, image_paths, name):
    factor = pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0])
    try:
        cmds.setAttr(shader + ".base_color", factor[0], factor[1], factor[2], type="double3")
    except RuntimeError:
        pass

    tex_ref = pbr.get("baseColorTexture")
    if tex_ref is None:
        return

    img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
    if img_idx is None or img_idx >= len(image_paths):
        return

    f = C.make_file_node(image_paths[img_idx], "sRGB", name + "_baseColor")
    _connect_map(shader, "color", f + ".outColor")


def _metal_rough(gltf, pbr, shader, image_paths, name):
    """glTF packs metalness=B, roughness=G in metallicRoughnessTexture."""
    C.try_set(shader + ".metallic", pbr.get("metallicFactor", 1.0))
    C.try_set(shader + ".roughness", pbr.get("roughnessFactor", 1.0))

    tex_ref = pbr.get("metallicRoughnessTexture")
    if tex_ref is None:
        return

    img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
    if img_idx is None or img_idx >= len(image_paths):
        return

    f = C.make_file_node(image_paths[img_idx], "Raw", name + "_metalRough")

    # StingrayPBS expects color (vec3) for metallic/roughness map inputs even
    # though only one channel is read. Broadcast the appropriate channel to
    # R/G/B so any channel sampled internally gets the right value.
    metal_attr = _find_attr(shader, _TEX_CANDIDATES["metallic"])
    if metal_attr:
        C.broadcast_scalar_to_color(f + ".outColorB", shader + "." + metal_attr)
        C.try_set(shader + ".use_metallic_map", 1)

    rough_attr = _find_attr(shader, _TEX_CANDIDATES["roughness"])
    if rough_attr:
        C.broadcast_scalar_to_color(f + ".outColorG", shader + "." + rough_attr)
        C.try_set(shader + ".use_roughness_map", 1)


def _normal(gltf, mat, shader, image_paths, name):
    """glTF normals are OpenGL convention (Y-up). StingrayPBS handles tangent
    normals internally — connect file.outColor straight to TEX_normal_map.
    """
    tex_ref = mat.get("normalTexture")
    if tex_ref is None:
        return

    img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
    if img_idx is None or img_idx >= len(image_paths):
        return

    f = C.make_file_node(image_paths[img_idx], "Raw", name + "_normal")
    _connect_map(shader, "normal", f + ".outColor")

    scale = tex_ref.get("scale", 1.0)
    if scale != 1.0:
        C.try_set(shader + ".normal_strength", scale)


def _occlusion(gltf, mat, shader, image_paths, name):
    tex_ref = mat.get("occlusionTexture")
    if tex_ref is None:
        return

    img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
    if img_idx is None or img_idx >= len(image_paths):
        return

    f = C.make_file_node(image_paths[img_idx], "Raw", name + "_ao")
    ao_attr = _find_attr(shader, _TEX_CANDIDATES["ao"])
    if ao_attr:
        C.broadcast_scalar_to_color(f + ".outColorR", shader + "." + ao_attr)
        C.try_set(shader + ".use_ao_map", 1)


def _emissive(gltf, mat, shader, image_paths, name):
    factor = mat.get("emissiveFactor", [0.0, 0.0, 0.0])
    tex_ref = mat.get("emissiveTexture")

    if tex_ref is not None:
        img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            f = C.make_file_node(image_paths[img_idx], "sRGB", name + "_emissive")
            _connect_map(shader, "emissive", f + ".outColor")
            C.try_set(shader + ".emissive_intensity", 1.0)
            return

    if factor != [0.0, 0.0, 0.0]:
        try:
            cmds.setAttr(shader + ".emissive", factor[0], factor[1], factor[2], type="double3")
            C.try_set(shader + ".emissive_intensity", 1.0)
        except RuntimeError:
            pass


def _find_attr(shader, candidate_names):
    """Return the first attribute name from `candidate_names` that exists on `shader`."""
    for n in candidate_names:
        if cmds.attributeQuery(n, node=shader, exists=True):
            return n
    return None
