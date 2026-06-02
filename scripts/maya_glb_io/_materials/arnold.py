"""Arnold target — aiStandardSurface + aiImage + aiNormalMap.

Only used when the user explicitly picks 'arnold' as the import target.
Never the auto-selected default — that's a project-level rule (Anno never
uses Arnold; GLB defaults to a Maya-native target). See
project_glb_importer_shader_targets memory.

Requires the `mtoa` plug-in loaded.
"""
from __future__ import annotations

from maya import cmds
import maya.api.OpenMaya as om

from maya_glb_io._materials import _common as C


def build_placeholder_material(index, name_prefix):
    mat_name = C.join_prefix(name_prefix, "material_{0}".format(index))
    shader = cmds.shadingNode("aiStandardSurface", asShader=True, name=mat_name + "_SHD")
    sg = C.make_shading_group(shader, mat_name + "_SG")
    return shader, sg


def build_material(gltf, mat, index, image_paths, name_prefix):
    mat_name = C.sanitize(mat.get("name") or "material_%d" % index)
    mat_name = C.join_prefix(name_prefix, mat_name)

    shader = cmds.shadingNode("aiStandardSurface", asShader=True, name=mat_name + "_SHD")
    sg = C.make_shading_group(shader, mat_name + "_SG")

    pbr = C.get_pbr_metallic_roughness(mat)

    base_node = _base_color(gltf, pbr, shader, image_paths, mat_name)
    _metal_rough(gltf, pbr, shader, image_paths, mat_name)
    _normal(gltf, mat, shader, image_paths, mat_name)
    _emissive(gltf, mat, shader, image_paths, mat_name)
    _opacity(gltf, mat, pbr, shader, base_node)
    _transmission(gltf, mat, shader, image_paths, mat_name)

    return shader, sg


def _ai_image(file_path, colorspace, name):
    node = cmds.shadingNode("aiImage", asTexture=True, isColorManaged=True, name=name)
    cmds.setAttr(node + ".filename", file_path, type="string")
    try:
        cmds.setAttr(node + ".colorSpace", colorspace, type="string")
    except RuntimeError:
        pass
    return node


def _base_color(gltf, pbr, shader, image_paths, name):
    """Wire base color. Returns the baseColor aiImage node (or None) so the
    caller can pull its alpha channel for transparency."""
    factor = pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0])
    cmds.setAttr(shader + ".baseColor", factor[0], factor[1], factor[2], type="double3")

    tex_ref = pbr.get("baseColorTexture")
    if tex_ref is None:
        return None

    img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
    if img_idx is None or img_idx >= len(image_paths):
        return None

    node = _ai_image(image_paths[img_idx], "sRGB", name + "_baseColor")
    cmds.connectAttr(node + ".outColor", shader + ".baseColor", force=True)
    return node


def _opacity(gltf, mat, pbr, shader, base_node):
    """Import glTF transparency into aiStandardSurface.opacity (a colour where
    (1,1,1) = fully opaque). Only BLEND / MASK materials are wired; OPAQUE is
    left fully opaque (no transparency data to import)."""
    alpha_mode = (mat.get("alphaMode") or "OPAQUE").upper()
    if alpha_mode == "OPAQUE":
        return

    factor = pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0])
    alpha = factor[3] if len(factor) > 3 else 1.0

    if base_node is not None and pbr.get("baseColorTexture") is not None:
        # aiImage.outAlpha (scalar) broadcast to the opacity RGB sub-attrs.
        for ch in ("R", "G", "B"):
            C.try_connect(base_node + ".outAlpha", shader + ".opacity" + ch)
    else:
        try:
            cmds.setAttr(shader + ".opacity", alpha, alpha, alpha, type="double3")
        except RuntimeError:
            pass


def _metal_rough(gltf, pbr, shader, image_paths, name):
    cmds.setAttr(shader + ".metalness", pbr.get("metallicFactor", 1.0))
    cmds.setAttr(shader + ".specularRoughness", pbr.get("roughnessFactor", 1.0))

    tex_ref = pbr.get("metallicRoughnessTexture")
    if tex_ref is None:
        return

    img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
    if img_idx is None or img_idx >= len(image_paths):
        return

    node = _ai_image(image_paths[img_idx], "Raw", name + "_metalRough")
    cmds.connectAttr(node + ".outColorB", shader + ".metalness", force=True)
    cmds.connectAttr(node + ".outColorG", shader + ".specularRoughness", force=True)


def _normal(gltf, mat, shader, image_paths, name):
    tex_ref = mat.get("normalTexture")
    if tex_ref is None:
        return

    img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
    if img_idx is None or img_idx >= len(image_paths):
        return

    node = _ai_image(image_paths[img_idx], "Raw", name + "_normal")
    nmap = cmds.shadingNode("aiNormalMap", asUtility=True, name=name + "_normalMap")
    cmds.connectAttr(node + ".outColor", nmap + ".input", force=True)
    scale = tex_ref.get("scale", 1.0)
    if scale != 1.0:
        cmds.setAttr(nmap + ".strength", scale)
    cmds.connectAttr(nmap + ".outValue", shader + ".normalCamera", force=True)


def _emissive(gltf, mat, shader, image_paths, name):
    factor = mat.get("emissiveFactor", [0.0, 0.0, 0.0])
    tex_ref = mat.get("emissiveTexture")
    # Arnold's emission is a 0-1 weight (1.0 already glows), so we keep the base
    # weight at 1.0 and only scale up for HDR emissives via emissive_strength.
    weight = 1.0 * C.emissive_strength(mat)

    if tex_ref is not None:
        img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            node = _ai_image(image_paths[img_idx], "sRGB", name + "_emissive")
            cmds.connectAttr(node + ".outColor", shader + ".emissionColor", force=True)
            cmds.setAttr(shader + ".emission", weight)
            return

    if factor != [0.0, 0.0, 0.0]:
        cmds.setAttr(shader + ".emissionColor", factor[0], factor[1], factor[2], type="double3")
        cmds.setAttr(shader + ".emission", weight)


def _transmission(gltf, mat, shader, image_paths, name):
    """Import glTF KHR_materials_transmission (+ ior / volume) into
    aiStandardSurface's transmission for refractive glass.

    transmissionFactor -> transmission (texture R channel if present),
    KHR_materials_ior  -> specularIOR,
    KHR_materials_volume.attenuationColor    -> transmissionColor,
    KHR_materials_volume.attenuationDistance -> transmissionDepth.
    """
    info = C.transmission_info(mat)
    if not info:
        return
    if info["factor"] <= 0.0 and info["texture"] is None:
        return

    tex_ref = info["texture"]
    wired = False
    if tex_ref is not None:
        img_idx = C.texture_image_index(gltf, tex_ref.get("index"))
        if img_idx is not None and img_idx < len(image_paths):
            node = _ai_image(image_paths[img_idx], "Raw", name + "_transmission")
            wired = C.try_connect(node + ".outColorR", shader + ".transmission")
    if not wired:
        C.try_set(shader + ".transmission", info["factor"] or 1.0)

    C.try_set(shader + ".specularIOR", info["ior"])

    if info["attenuation_color"] is not None:
        ac = info["attenuation_color"]
        C.try_set(shader + ".transmissionColor", ac[0], ac[1], ac[2], type="double3")
    if info["attenuation_distance"] is not None:
        C.try_set(shader + ".transmissionDepth", info["attenuation_distance"])
