"""Maya target — `phong` classic shader. Always-available fallback.

Loses metallic-roughness information (Phong predates PBR). Color, normal
bump, transparency, emissive are wired up. Specularity is fixed.

Use this when StingrayPBS / openPBR aren't available, or when the user
explicitly wants the legacy Maya look.
"""
from __future__ import annotations

from maya import cmds
import maya.api.OpenMaya as om

from maya_glb_io._materials import _common as C


def build_placeholder_material(index, name_prefix):
    mat_name = C.join_prefix(name_prefix, "material_{0}".format(index))
    shader = cmds.shadingNode("phong", asShader=True, name=mat_name + "_SHD")
    sg = C.make_shading_group(shader, mat_name + "_SG")
    return shader, sg


def build_material(gltf, mat, index, image_paths, name_prefix):
    mat_name = C.sanitize(mat.get("name") or "material_%d" % index)
    mat_name = C.join_prefix(name_prefix, mat_name)

    shader = cmds.shadingNode("phong", asShader=True, name=mat_name + "_SHD")
    sg = C.make_shading_group(shader, mat_name + "_SG")

    pbr = C.get_pbr_metallic_roughness(mat)

    _base_color(gltf, pbr, shader, image_paths, mat_name)
    _normal(gltf, mat, shader, image_paths, mat_name)
    _emissive(gltf, mat, shader, image_paths, mat_name)
    _transparency(gltf, pbr, mat, shader, image_paths, mat_name)
    _set_specularity_from_roughness(pbr, shader)

    return shader, sg


def _base_color(gltf, pbr, shader, image_paths, name):
    factor = pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0])
    cmds.setAttr(shader + ".color", factor[0], factor[1], factor[2], type="double3")

    tex_ref = pbr.get("baseColorTexture")
    if tex_ref is None:
        return

    img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
    if img_idx is None or img_idx >= len(image_paths):
        return

    f = C.make_file_node(image_paths[img_idx], "sRGB", name + "_color")
    C.try_connect(f + ".outColor", shader + ".color")


def _normal(gltf, mat, shader, image_paths, name):
    tex_ref = mat.get("normalTexture")
    if tex_ref is None:
        return

    img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
    if img_idx is None or img_idx >= len(image_paths):
        return

    f = C.make_file_node(image_paths[img_idx], "Raw", name + "_normal")
    bump = cmds.shadingNode("bump2d", asUtility=True, name=name + "_bump")
    cmds.setAttr(bump + ".bumpInterp", 1)  # Tangent Space Normals
    cmds.connectAttr(f + ".outAlpha", bump + ".bumpValue", force=True)
    cmds.connectAttr(bump + ".outNormal", shader + ".normalCamera", force=True)


def _emissive(gltf, mat, shader, image_paths, name):
    factor = mat.get("emissiveFactor", [0.0, 0.0, 0.0])
    tex_ref = mat.get("emissiveTexture")

    if tex_ref is not None:
        img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            f = C.make_file_node(image_paths[img_idx], "sRGB", name + "_incandescence")
            C.try_connect(f + ".outColor", shader + ".incandescence")
            return

    if factor != [0.0, 0.0, 0.0]:
        cmds.setAttr(shader + ".incandescence", factor[0], factor[1], factor[2], type="double3")


def _transparency(gltf, pbr, mat, shader, image_paths, name):
    """glTF baseColor.A → phong.transparency (inverted: phong uses opacity-as-darkness)."""
    if mat.get("alphaMode", "OPAQUE") == "OPAQUE":
        return

    tex_ref = pbr.get("baseColorTexture")
    if tex_ref is None:
        return

    img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
    if img_idx is None or img_idx >= len(image_paths):
        return

    # The file node we created in _base_color is already in the graph but
    # we don't have a handle. Easiest path: make a second file node for the
    # alpha pull. Slightly wasteful but keeps the wirings independent.
    f = C.make_file_node(image_paths[img_idx], "sRGB", name + "_opacity")
    # Phong.transparency takes color where (1,1,1) = fully transparent.
    # Use a reverse to invert outAlpha and broadcast.
    rev = cmds.shadingNode("reverse", asUtility=True, name=name + "_opacity_rev")
    cmds.connectAttr(f + ".outAlpha", rev + ".inputX", force=True)
    for ch in ("R", "G", "B"):
        cmds.connectAttr(rev + ".outputX", shader + ".transparency" + ch, force=True)


def _set_specularity_from_roughness(pbr, shader):
    """Map roughness to phong's cosinePower (Blinn/Phong exponent).
    Roughness 0 → very sharp specular (cosinePower ~100); roughness 1 → broad."""
    rough = pbr.get("roughnessFactor", 1.0)
    # Approximate: cosinePower = 2 + (1 - roughness)^2 * 100
    cosine_power = 2.0 + (1.0 - rough) * (1.0 - rough) * 100.0
    cmds.setAttr(shader + ".cosinePower", cosine_power)
