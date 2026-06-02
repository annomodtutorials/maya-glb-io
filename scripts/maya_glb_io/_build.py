"""Build Maya scene nodes from a parsed Gltf.

Thin orchestrator: extracts images, picks a material builder from
_materials/, then walks the glTF node hierarchy creating transforms and
meshes. All material wiring is delegated to _materials package modules.

_gltf.py is pure Python and never imports maya — keeps the parser
unit-testable outside Maya.

Skinning:
  glTF skin -> Maya joints (built IN the node hierarchy) + skinCluster.
  Before the build we compute the union of every skin's `joints`; any glTF
  node in that set is created by _build_node as a Maya `joint` (instead of a
  plain transform), in its exact place in the hierarchy. This means joints
  inherit the same ancestor transforms as everything else (correct space)
  and exist exactly once (no duplicate skeleton, no name clash).

  _build_skins then does NOT build a separate skeleton: it looks the already
  built joints up by node index and creates one skinCluster per mesh SHAPE
  (a single transform may carry many shapes, so we bind the shape, not the
  transform), applying the glTF JOINTS_n/WEIGHTS_n via the bulk
  MFnSkinCluster.setWeights API. Per the glTF spec, a node that has BOTH a
  `mesh` and a `skin` has its own transform IGNORED for the skinned geometry
  (the joints define world placement); we neutralise that transform so the
  mesh and skeleton share one space.

  Set option `skip_skinning=True` to suppress all of this (used by the Anno
  building pipeline). See _build_skins() below for the algorithm.
"""
from __future__ import annotations

import json
import math
import os
from typing import List, Optional

import maya.api.OpenMaya as om
import maya.api.OpenMayaAnim as oma
from maya import cmds

from maya_glb_io import _materials
from maya_glb_io._gltf import Gltf
from maya_glb_io._materials import _common as MC


# ---------------------------------------------------------------------------
# Camera / light import constants
# ---------------------------------------------------------------------------

# Maya's default camera vertical film aperture, in INCHES (0.945" ≈ 24 mm
# sensor height). glTF cameras only specify a vertical FOV (yfov), so we hold
# the aperture fixed at Maya's default and solve for focal length from yfov.
# focal_mm = (verticalAperture_inch * 25.4) / (2 * tan(yfov / 2))
_MAYA_DEFAULT_VERTICAL_APERTURE_IN = 0.945

# Light intensity mapping. glTF KHR_lights_punctual intensities are PHYSICAL
# and large: directional in lux (lm/m²), point/spot in candela (lm/sr) — values
# in the hundreds to hundreds-of-thousands. Maya's default light intensity is
# ~1, so passing the raw glTF number through would blow out the scene.
#
# We apply a pragmatic, NON-physical divisor per light type purely so imported
# lights land in a usable range, then clamp to a sane ceiling. These are
# APPROXIMATE and meant to be tweaked by the artist after import — exact
# photometric matching is explicitly out of scope (see module/REPORT notes).
#   directional: lux  -> /1000  (e.g. a 1000 lux sun -> Maya intensity 1.0)
#   point/spot : cd   -> /1000  (e.g. a 1000 cd lamp -> Maya intensity 1.0)
_LIGHT_INTENSITY_SCALE = {
    "directional": 1.0 / 1000.0,
    "point": 1.0 / 1000.0,
    "spot": 1.0 / 1000.0,
}
# Fallback intensity scale for any unrecognised light type.
_LIGHT_INTENSITY_SCALE_DEFAULT = 1.0 / 1000.0
# Don't let a converted intensity exceed this (keeps a 100000-lux sun usable).
_LIGHT_INTENSITY_MAX = 50.0


def build_scene(gltf, source_path, options=None):
    # type: (Gltf, str, Optional[dict]) -> dict
    """Walk the glTF default scene and build Maya nodes.

    options keys:
      target          : shader-target name ('stingray' / 'openpbr' / 'phong' /
                       'arnold') or None to auto-pick by Maya version
      coord_convert   : callable(triplet) -> triplet for translation conversion
      skip_materials  : bool, when True don't build any materials and leave
                       meshes with no shading-group assignment
      scale           : numeric scale factor applied to all positions and
                       translations. None = auto (100 for cm scene, 1 for m).
                       Pass 1.0 to keep glTF's native meter scale unchanged.
      flatten         : bool, when True (default) collapse pass-through
                       transforms (no TRS, no shape, single child) after build.

    Returns a dict:
      {
        "top_transforms":     [str, ...],
        "primitives":         [(shape_full_path, gltf_material_index_or_None), ...],
        "node_to_maya":       {gltf_node_index: maya_full_path, ...},
        "node_to_blendshape": {gltf_node_index: [blendShape_name, ...], ...},
        "variant_names":      [str, ...],   # KHR_materials_variants, [] if none
      }

    "node_to_maya" maps EVERY built glTF node index (plain transforms AND
    skin joints) to the live Maya node full path after the flatten/collapse and
    pivot passes. Used by the animation applier to resolve channel targets.

    "node_to_blendshape" maps a glTF mesh-node index to the blendShape node(s)
    built for its primitives' morph targets (one per morphed primitive). Empty
    when no mesh in the document has morph targets, so non-morph imports are
    entirely unaffected. The animation applier uses it to resolve the
    blendShape(s) for a glTF `weights` channel.

    "variant_names" lists the document's KHR_materials_variants names (empty
    when the extension is absent). Each variant-carrying mesh shape also gets a
    `glbVariantMap` string attribute (JSON {variantName: shadingGroupName}) so
    the switcher command can reassign shading groups post-import.
    """
    options = options or {}
    base_name = MC.sanitize(os.path.splitext(os.path.basename(source_path))[0])

    # Node/material name prefix. Empty by default so Maya node names stay clean
    # (users disliked the source file name being auto-prepended, e.g.
    # "stonePyramid_stonePyramid_SHD"). Only applied when the user explicitly
    # sets a Material Name Prefix in the import options.
    node_prefix = options.get("materialPrefix") or ""

    # On-disk image files DO keep the file basename as a prefix so two different
    # GLBs writing into the shared sourceimages/ folder don't collide / overwrite
    # each other. This prefix never reaches the Maya node names.
    image_paths = _extract_images(gltf, base_name)

    # Emission luminance (nits) for openPBR emissive materials — tunable so the
    # user can dial glow strength. Pushed onto the shared _common module that the
    # material builders read (avoids threading options through every builder).
    try:
        MC.EMISSION_LUMINANCE = float(options.get("emissionLuminance", 1000.0))
    except (TypeError, ValueError):
        MC.EMISSION_LUMINANCE = 1000.0

    skip_mats = options.get("skip_materials") or options.get("skipMaterials")
    if skip_mats:
        materials = []
    else:
        target = options.get("_target_resolved", options.get("target"))
        materials = _materials.build_materials(
            gltf, image_paths, node_prefix, target=target
        )

    doc = gltf.doc
    scene_index = doc.get("scene", 0)
    scene = doc.get("scenes", [{"nodes": []}])[scene_index]
    root_nodes = scene.get("nodes", [])

    # Unit scale: glTF spec defines units as METERS. Maya default scene is in
    # centimeters -> factor of 100. Detect the scene unit and scale positions
    # so the imported model matches the artist's working scale.
    scale = options.get("_scale_resolved")
    if scale is None:
        scale = options.get("scale")
    if scale is None or (isinstance(scale, str) and scale.lower() == "auto"):
        scale = _auto_unit_scale()
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        scale = _auto_unit_scale()

    # Compose the coord_convert: user-supplied + scale
    user_convert = options.get("coord_convert") or (lambda v: v)
    if scale != 1.0:
        def coord_convert(v, _f=user_convert, _s=scale):
            x, y, z = _f(v)
            return (x * _s, y * _s, z * _s)
    else:
        coord_convert = user_convert

    # Tracker for shape -> glTF material index, populated as primitives are built
    primitive_metadata = []  # list of (shape_full_path, mat_index)

    # Tracker for skinned shapes. Each entry is a dict:
    #   {"shape": shape_full_path, "skin_index": int,
    #    "joints": joints_accessor_list, "weights": weights_accessor_list}
    # Populated by _build_primitive when the owning node has a skin AND we
    # aren't in skip_skinning mode. Drained later by _build_skins().
    skin_jobs = []
    skip_skinning = bool(options.get("skip_skinning") or options.get("skipSkinning"))

    # Union of every skin's joint node indices. Any glTF node in this set is
    # built as a Maya `joint` (in-hierarchy) rather than a plain transform, so
    # the skeleton lives in the exact same space as everything else and is
    # created exactly once (no duplicate skeleton -> no name clash, no
    # mesh/skeleton space mismatch). Empty when skip_skinning or no skins.
    joint_node_indices = set()
    if not skip_skinning:
        for skin in (doc.get("skins") or []):
            for j in skin.get("joints", []):
                joint_node_indices.add(j)

    # Maps glTF node index -> Maya node full path. Filled as transforms and
    # joints are created; patched in lockstep with the flatten pass so it always
    # points at live nodes. Returned to the caller and used by the animation
    # applier to resolve channel targets. EVERY node (plain transforms AND
    # joints) is recorded so animation can key the joints.
    node_to_maya = {}

    # Maps glTF mesh-node index -> list of blendShape node names built for that
    # node's morphed primitives. Filled by _build_primitive when a primitive has
    # morph targets. Threaded into the animation applier so a `weights` channel
    # can resolve the right blendShape(s). Empty for non-morph imports -> the
    # whole morph path is a no-op and existing behaviour is unchanged.
    node_to_blendshape = {}

    # KHR_materials_variants: the document's variant names (empty list when the
    # extension is absent -> all variant code below is a no-op, so non-variant
    # glTFs are entirely unaffected). Threaded down to _build_primitive, which
    # stamps a per-shape variant->shading-group map so the switcher command can
    # reassign shading groups later. Skipped entirely if materials weren't built.
    variant_names = gltf.variant_names() if materials else []

    # Build glTF root nodes directly at world (no wrapper group).
    # Maya's File > Import dialog has its own "Group" checkbox that wraps
    # imports — that's outside our control. Recommend users uncheck it.
    top_transforms = []
    for node_index in root_nodes:
        top = _build_node(
            gltf, node_index, materials, base_name, None,
            coord_convert, primitive_metadata,
            skin_jobs=skin_jobs, skip_skinning=skip_skinning,
            node_to_maya=node_to_maya, joint_node_indices=joint_node_indices,
            variant_names=variant_names, node_to_blendshape=node_to_blendshape,
            unit_scale=scale,
        )
        if top:
            top_transforms.append(top)

    # Hierarchy cleanup — per root subtree, including absorbing the root
    # itself if it's a pass-through (single child, identity TRS, no shape).
    # The flatten can rewrite mesh shape DAG paths, so we patch both
    # primitive_metadata and skin_jobs in lockstep via _patch_paths_after_reparent.
    if options.get("flatten", True):
        cleaned = []
        for t in top_transforms:
            survivor = _collapse_root_subtree(
                t, primitive_metadata, skin_jobs, node_to_maya,
            )
            if survivor:
                cleaned.append(survivor)
        top_transforms = cleaned

    # Pivot / centering options — applied per top-level root.
    # Done BEFORE skinCluster creation: once joints are bound, moving the
    # mesh transform would also drag the bind pose. Centering pre-bind keeps
    # the bind pose aligned to the new origin.
    for t in top_transforms:
        _apply_pivot_options(
            t,
            center_pivot=bool(options.get("centerPivot")),
            center_on_origin=bool(options.get("centerOnOrigin")),
        )

    # Skin pass — create the skinClusters. Joints were ALREADY built in
    # _build_node (in-hierarchy); _build_skins just looks them up by node index
    # via node_to_maya and binds each mesh shape with its glTF weights. It does
    # reparent each skinned-mesh transform to world (spec: ignore the mesh node
    # transform) and returns those new world-level transforms so drop-to-floor
    # picks them up alongside the existing roots.
    if skin_jobs and not skip_skinning:
        new_skin_tops = _build_skins(
            gltf, skin_jobs, base_name, coord_convert,
            node_to_maya=node_to_maya,
        )
        for t in new_skin_tops:
            if t and t not in top_transforms:
                top_transforms.append(t)
        # Some prior top_transforms may have been emptied (their only content
        # was a skinned mesh that we reparented out). Drop dead/childless ones.
        top_transforms = [
            t for t in top_transforms
            if t and cmds.objExists(t)
        ]

    # Animation pass — key the imported transforms/joints from the glTF
    # samplers. No-op when the document has no animations (the Anno building
    # pipeline). Disabled via the importAnimation option. Wrapped defensively so
    # an animation failure can never break the geometry import.
    import_anim = options.get("importAnimation", True)
    has_anim = bool(gltf.doc.get("animations"))
    if import_anim and has_anim:
        try:
            from maya_glb_io import _anim
            _anim.apply_animations(
                gltf, node_to_maya, coord_convert, scale, options,
                node_to_blendshape=node_to_blendshape,
            )
        except Exception as exc:
            om.MGlobal.displayWarning(
                "[glb_io] animation import failed (geometry imported OK): %s" % exc
            )

    # Drop-to-floor — sit the whole import on the grid (bbox bottom at world
    # Y=0) instead of floating at the origin. Runs last so it reflects the final
    # placement; shifts mesh roots AND joint roots by the same delta so a
    # skinned rig moves as one piece and its bind pose stays valid. Skipped when
    # the user explicitly centred geometry on the origin.
    if options.get("dropToFloor", True) and not options.get("centerOnOrigin"):
        _drop_to_floor(top_transforms)

    return {
        "top_transforms": top_transforms,
        "primitives": primitive_metadata,
        "node_to_maya": node_to_maya,
        "node_to_blendshape": node_to_blendshape,
        "variant_names": variant_names,
    }


def _collapse_root_subtree(root, primitive_metadata, skin_jobs=None, node_to_maya=None):
    """Run the descendant collapse, then check whether the root itself is a
    pass-through that should be absorbed too. Returns the (possibly new) root,
    or None if the entire subtree was empty and got deleted.
    """
    if not cmds.objExists(root):
        return None

    _collapse_passthrough_transforms(root, primitive_metadata, skin_jobs, node_to_maya)

    if not cmds.objExists(root):
        return None

    # Never collapse a joint root, nor a transform whose subtree contains
    # joints — that would destroy/relocate the skeleton.
    if _protect_from_collapse(root):
        return root

    # Root absorbable? same rules as descendants
    if cmds.listRelatives(root, shapes=True, fullPath=True):
        return root
    if _has_nontrivial_trs(root):
        return root
    children = cmds.listRelatives(root, children=True, fullPath=True) or []
    if len(children) == 0:
        _drop_collapsed_node(node_to_maya, root)
        cmds.delete(root)
        return None
    if len(children) == 1:
        new_child = cmds.parent(children[0], world=True)[0]
        _drop_collapsed_node(node_to_maya, root)
        cmds.delete(root)
        _patch_paths_after_reparent(
            primitive_metadata, skin_jobs, children[0], new_child, node_to_maya,
        )
        return new_child
    return root  # multiple children — keep as a real container


def _has_nontrivial_trs(node):
    t = cmds.getAttr(node + ".translate")[0]
    r = cmds.getAttr(node + ".rotate")[0]
    s = cmds.getAttr(node + ".scale")[0]
    if any(abs(v) > 1e-6 for v in t) or any(abs(v) > 1e-6 for v in r):
        return True
    if any(abs(v - 1.0) > 1e-6 for v in s):
        return True
    return False


def _is_joint(node):
    """True if the Maya node is a `joint` (skeleton node)."""
    try:
        return cmds.nodeType(node) == "joint"
    except Exception:
        return False


def _protect_from_collapse(node):
    """Joints must never be collapsed/deleted by the flatten pass (it would
    destroy the skeleton). Also protect any plain transform that has a joint
    somewhere in its subtree: collapsing it would reparent/move the joints and
    break the bind-pose space they share with the mesh.
    """
    if _is_joint(node):
        return True
    # cmds.listRelatives(type="joint") only returns joints in the subtree.
    if cmds.listRelatives(node, allDescendents=True, type="joint", fullPath=True):
        return True
    return False


def _drop_to_floor(top_transforms):
    """Translate every top-level transform so the combined mesh bounding box
    sits on the grid (minimum Y at world 0) instead of floating at the origin.

    All tops (mesh roots AND joint roots) are shifted by the same world delta,
    so a skinned rig moves as one piece and its bind pose stays valid.
    """
    live = [t for t in top_transforms if t and cmds.objExists(t)]
    if not live:
        return

    # Combined bbox min-Y across every top that actually has mesh geometry.
    min_y = None
    for t in live:
        if not (cmds.listRelatives(t, allDescendents=True, type="mesh") or []):
            continue
        bb = cmds.exactWorldBoundingBox(t)
        if min_y is None or bb[1] < min_y:
            min_y = bb[1]

    if min_y is None or abs(min_y) < 1e-6:
        return

    for t in live:
        try:
            cmds.xform(t, translation=(0.0, -min_y, 0.0),
                       worldSpace=True, relative=True)
        except Exception:
            pass


def _apply_pivot_options(top_transform, center_pivot, center_on_origin):
    """Either move the geometry so its bbox center sits at origin, or just
    re-set the pivot point to the bbox center without moving the geometry.

    center_on_origin implies a centered pivot too (the new origin and the
    pivot coincide).
    """
    if not (center_pivot or center_on_origin):
        return

    # exactWorldBoundingBox only works if there's geometry under the transform
    children = cmds.listRelatives(top_transform, allDescendents=True, type="mesh") or []
    if not children:
        return

    bbox = cmds.exactWorldBoundingBox(top_transform)
    cx = (bbox[0] + bbox[3]) / 2.0
    cy = (bbox[1] + bbox[4]) / 2.0
    cz = (bbox[2] + bbox[5]) / 2.0

    if center_on_origin:
        # Translate the container by -bbox_center (relative) so the bbox
        # center moves to world origin. Pivot ends up at origin too.
        cmds.xform(top_transform, translation=(-cx, -cy, -cz),
                   worldSpace=True, relative=True)
    elif center_pivot:
        # Move pivot to bbox center without moving geometry
        cmds.xform(top_transform, pivots=(cx, cy, cz), worldSpace=True)


def _auto_unit_scale():
    """Return the multiplier needed to convert glTF meters to Maya's scene unit."""
    linear = cmds.currentUnit(query=True, linear=True) or "cm"
    # Maya unit → number of centimeters per unit
    unit_to_cm = {
        "mm": 0.1, "cm": 1.0, "m": 100.0,
        "in": 2.54, "ft": 30.48, "yd": 91.44,
    }
    cm_per_unit = unit_to_cm.get(linear, 1.0)
    # glTF is in meters = 100 cm. To get scene-unit count: 100 / cm_per_unit
    return 100.0 / cm_per_unit


def _collapse_passthrough_transforms(top_transform, primitive_metadata,
                                     skin_jobs=None, node_to_maya=None):
    """Delete descendant transforms that exist just to nest geometry — no TRS,
    no shape, only a single child. Repeats until stable.

    Does NOT process `top_transform` itself; that's _collapse_root_subtree's job
    (which calls this then checks the root separately).

    `primitive_metadata`, `skin_jobs` and `node_to_maya` are updated in-place:
    shape/node full-paths are rewritten after reparenting (and dropped when a
    node is deleted) so downstream code (material assignment, skinCluster
    creation, animation targeting) still finds the right live Maya node.
    """
    changed = True
    pass_count = 0
    while changed and pass_count < 10:
        changed = False
        pass_count += 1
        descendants = cmds.listRelatives(
            top_transform, allDescendents=True, type="transform", fullPath=True
        ) or []
        # Process deepest-first
        for node in descendants:
            if not cmds.objExists(node):
                continue
            # listRelatives(type="transform") also returns joints (joint is a
            # transform subtype). Never collapse a joint, nor a transform that
            # has joints in its subtree — protects the skeleton + its space.
            if _protect_from_collapse(node):
                continue
            if cmds.listRelatives(node, shapes=True, fullPath=True):
                continue
            if _has_nontrivial_trs(node):
                continue
            children = cmds.listRelatives(node, children=True, fullPath=True) or []
            parent = cmds.listRelatives(node, parent=True, fullPath=True)
            parent = parent[0] if parent else None

            if not children:
                _drop_collapsed_node(node_to_maya, node)
                cmds.delete(node)
                changed = True
            elif len(children) == 1 and parent:
                new_child = cmds.parent(children[0], parent)[0]
                _drop_collapsed_node(node_to_maya, node)
                cmds.delete(node)
                _patch_paths_after_reparent(
                    primitive_metadata, skin_jobs, children[0], new_child,
                    node_to_maya,
                )
                changed = True


def _patch_paths_after_reparent(prim_metadata, skin_jobs, old_parent_path,
                                new_parent_path, node_to_maya=None):
    """After a cmds.parent(), DAG paths under it change. Rewrite metadata,
    pending skin-job shape paths, and the node->maya map so all stay valid.
    """
    def remap(old):
        if old == old_parent_path:
            return new_parent_path
        if old.startswith(old_parent_path + "|"):
            return new_parent_path + old[len(old_parent_path):]
        return old

    for i, (shape_path, mat_idx) in enumerate(prim_metadata):
        new_path = remap(shape_path)
        if new_path != shape_path:
            prim_metadata[i] = (new_path, mat_idx)

    if skin_jobs:
        for job in skin_jobs:
            new_path = remap(job["shape"])
            if new_path != job["shape"]:
                job["shape"] = new_path

    if node_to_maya:
        for node_index, path in list(node_to_maya.items()):
            new_path = remap(path)
            if new_path != path:
                node_to_maya[node_index] = new_path


def _drop_collapsed_node(node_to_maya, deleted_path):
    """A pass-through transform at `deleted_path` is about to be deleted. Remove
    any node_to_maya entry that points exactly at it so the animation applier
    won't try to key a dead node. (Its single child, if any, is reparented and
    remapped separately by _patch_paths_after_reparent.)
    """
    if not node_to_maya:
        return
    for node_index, path in list(node_to_maya.items()):
        if path == deleted_path:
            del node_to_maya[node_index]


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def _extract_images(gltf, name_prefix):
    """Write embedded images to <project>/sourceimages/ and return their paths."""
    images = gltf.doc.get("images") or []
    if not images:
        return []

    workspace_dir = cmds.workspace(query=True, rd=True)
    sourceimages_dir = os.path.join(workspace_dir, "sourceimages")
    if not os.path.isdir(sourceimages_dir):
        os.makedirs(sourceimages_dir)

    paths = []
    for i, img in enumerate(images):
        data, _mime, ext = gltf.get_image_bytes(i)

        uri = img.get("uri") or ""
        if uri and not uri.startswith("data:"):
            out_name = os.path.basename(uri)
        else:
            img_name = img.get("name") or ("img%d" % i)
            out_name = "{0}_{1}{2}".format(name_prefix, MC.sanitize(img_name), ext)

        out_path = os.path.join(sourceimages_dir, out_name)
        if not os.path.isfile(out_path):
            with open(out_path, "wb") as f:
                f.write(data)
        paths.append(out_path)

    return paths


# ---------------------------------------------------------------------------
# Node hierarchy
# ---------------------------------------------------------------------------

def _build_node(gltf, node_index, materials, name_prefix, parent,
                coord_convert, prim_metadata,
                skin_jobs=None, skip_skinning=False, node_to_maya=None,
                joint_node_indices=None, variant_names=None,
                node_to_blendshape=None, unit_scale=1.0):
    node = gltf.doc["nodes"][node_index]
    raw_name = node.get("name") or "node_%d" % node_index

    is_joint = bool(joint_node_indices) and node_index in joint_node_indices
    node_skin = node.get("skin") if (not skip_skinning and "mesh" in node) else None
    is_skinned_mesh = node_skin is not None

    if is_joint:
        # Build a Maya `joint` IN-PLACE in the hierarchy (same parent as a plain
        # transform would get). cmds.joint parents to the current selection, so
        # select the parent (or clear for a world-level joint) first. The joint
        # inherits the exact ancestor transforms of every other node -> the
        # skeleton ends up in the same space as the mesh.
        if parent:
            cmds.select(parent, replace=True)
        else:
            cmds.select(clear=True)
        # position=(0,0,0) relative=True gives a zero local offset; _apply_trs
        # then overlays the glTF node's local TRS. We zero jointOrient and drive
        # the joint purely with rotate so its local TRS matches the glTF node
        # exactly (and so the animation applier, which keys rotateXYZ, lines up).
        xform = cmds.joint(
            name=MC.sanitize(raw_name), position=(0.0, 0.0, 0.0),
            relative=True, radius=0.5,
        )
        # cmds.joint returns the leaf name only, which can be ambiguous if a
        # node elsewhere shares it. Resolve to the unique full DAG path now (the
        # new joint is the active selection right after creation).
        sel = cmds.ls(selection=True, long=True)
        if sel:
            xform = sel[0]
        try:
            cmds.setAttr(xform + ".jointOrientX", 0.0)
            cmds.setAttr(xform + ".jointOrientY", 0.0)
            cmds.setAttr(xform + ".jointOrientZ", 0.0)
        except Exception:
            pass
    else:
        # cmds.createNode rejects parent=None; only pass it when set
        kwargs = {"name": MC.sanitize(raw_name)}
        if parent:
            kwargs["parent"] = parent
        xform = cmds.createNode("transform", **kwargs)

    # Record the glTF node -> Maya node mapping (full path). Resolve to a full
    # path so it survives the later flatten patching, which keys off full paths.
    if node_to_maya is not None:
        node_to_maya[node_index] = _full_path(xform)

    # Apply the node's local TRS for EVERY node, including skinned-mesh nodes.
    # The mesh and the skeleton live in the SAME hierarchy, so any shared
    # ancestor transform (e.g. CesiumMan's Z_UP / Armature matrices) is carried
    # by both the mesh and the joints and cancels naturally inside Maya's
    # skinCluster — exactly mirroring glTF's `inverse(meshGlobal) * jointGlobal`.
    # (Forcing the skinned mesh to identity would strip that shared transform
    # from the mesh while the joints kept it -> gross distortion.)
    _apply_trs(node, xform, coord_convert)

    # Cameras / lights — create the Maya shape UNDER this node's transform so
    # the glTF node TRS (already applied above) orients it. glTF cameras look
    # down -Z with +Y up (same as Maya), and directional/spot lights emit down
    # -Z (same as Maya), so the transform alone gives correct aim — no extra
    # rotation. Each is wrapped so a malformed camera/light can never abort the
    # geometry import. Both are no-ops for nodes without a camera/light (every
    # Anno glb), so existing behaviour is unchanged. Skipped on joint nodes (a
    # skin joint never also carries a camera/light).
    if not is_joint:
        if gltf.node_camera(node) is not None:
            try:
                _build_camera(gltf, node, xform, unit_scale)
            except Exception as exc:
                om.MGlobal.displayWarning(
                    "[glb_io] camera import failed on node %d (geometry OK): %s"
                    % (node_index, exc)
                )
        if gltf.node_light(node) is not None:
            try:
                _build_light(gltf, node, xform)
            except Exception as exc:
                om.MGlobal.displayWarning(
                    "[glb_io] light import failed on node %d (geometry OK): %s"
                    % (node_index, exc)
                )

    if "mesh" in node:
        # A node with both `mesh` and `skin` is a skinned-mesh instance.
        # Pass the skin index down so each primitive can be queued for
        # post-build skinCluster wiring.
        _build_mesh(
            gltf, node["mesh"], materials, name_prefix, xform,
            coord_convert, prim_metadata,
            skin_index=node_skin, skin_jobs=skin_jobs,
            variant_names=variant_names,
            node_index=node_index, node=node,
            node_to_blendshape=node_to_blendshape,
        )

    for child_index in node.get("children", []):
        _build_node(
            gltf, child_index, materials, name_prefix, xform,
            coord_convert, prim_metadata,
            skin_jobs=skin_jobs, skip_skinning=skip_skinning,
            node_to_maya=node_to_maya, joint_node_indices=joint_node_indices,
            variant_names=variant_names, node_to_blendshape=node_to_blendshape,
            unit_scale=unit_scale,
        )

    return xform


def _full_path(node):
    """Return the unique full DAG path for a transform name (or the name itself
    if it isn't a DAG node / can't be resolved)."""
    try:
        paths = cmds.ls(node, long=True)
        if paths:
            return paths[0]
    except Exception:
        pass
    return node


def _apply_trs(node, xform, coord_convert):
    if "matrix" in node:
        m = node["matrix"]
        mm = om.MMatrix(m)
        tm = om.MTransformationMatrix(mm)
        t = tm.translation(om.MSpace.kWorld)
        e = tm.rotation(asQuaternion=False)
        s = tm.scale(om.MSpace.kWorld)
        t_out = coord_convert((t.x, t.y, t.z))
        cmds.xform(xform, translation=t_out)
        cmds.xform(xform, rotation=(math.degrees(e.x), math.degrees(e.y), math.degrees(e.z)))
        cmds.xform(xform, scale=(s[0], s[1], s[2]))
        return

    if "translation" in node:
        t = node["translation"]
        cmds.xform(xform, translation=coord_convert((t[0], t[1], t[2])))
    if "rotation" in node:
        r = node["rotation"]
        q = om.MQuaternion(r[0], r[1], r[2], r[3])
        e = q.asEulerRotation()
        cmds.xform(xform, rotation=(math.degrees(e.x), math.degrees(e.y), math.degrees(e.z)))
    if "scale" in node:
        s = node["scale"]
        cmds.xform(xform, scale=(s[0], s[1], s[2]))


# ---------------------------------------------------------------------------
# Cameras
# ---------------------------------------------------------------------------

def _build_camera(gltf, node, xform, unit_scale):
    """Create a Maya camera SHAPE under `xform` from the node's glTF camera.

    The camera shape is parented to the EXISTING transform `xform` (which
    already carries the node's TRS), so the transform orients the camera —
    glTF and Maya cameras both look down local -Z with +Y up, so no extra
    rotation is needed.

    perspective: glTF gives a vertical FOV (yfov, radians). Maya cameras are
      driven by focal length against a film aperture; we hold the vertical
      aperture at Maya's default (0.945 in) and solve
        focal_mm = (verticalAperture_in * 25.4) / (2 * tan(yfov / 2))
      then set .focalLength (Maya focal length is in millimetres).

    orthographic: set .orthographic 1 and .orthographicWidth = xmag * 2 (the
      full width; glTF xmag is the half-width), scaled to scene units.

    Clip planes (znear/zfar) are DISTANCES in scene units, so they're scaled by
      `unit_scale` (glTF metres -> Maya scene unit). zfar is optional in glTF
      (an infinite-projection camera omits it); we leave Maya's default far
      clip in that case.
    """
    cam_index = gltf.node_camera(node)
    cam = gltf.camera_def(cam_index)
    if cam is None:
        return

    # Create the camera shape directly under the node's transform. createNode
    # makes a bare cameraShape parented to xform; we then set its attributes.
    shape = cmds.createNode("camera", parent=xform, skipSelect=True)

    ctype = cam["type"]
    if ctype == "perspective":
        persp = cam.get("perspective") or {}
        yfov = persp.get("yfov")
        cmds.setAttr(shape + ".orthographic", 0)
        if yfov:
            try:
                tan_half = math.tan(float(yfov) / 2.0)
                if tan_half > 1e-9:
                    v_ap_in = _MAYA_DEFAULT_VERTICAL_APERTURE_IN
                    focal_mm = (v_ap_in * 25.4) / (2.0 * tan_half)
                    # Keep Maya's default vertical aperture so the FOV we solved
                    # for is the FOV Maya renders.
                    cmds.setAttr(shape + ".verticalFilmAperture", v_ap_in)
                    cmds.setAttr(shape + ".focalLength", focal_mm)
            except (TypeError, ValueError):
                pass
        _set_clip_planes(shape, persp, unit_scale)
    else:  # orthographic
        ortho = cam.get("orthographic") or {}
        cmds.setAttr(shape + ".orthographic", 1)
        xmag = ortho.get("xmag")
        if xmag:
            try:
                width = abs(float(xmag)) * 2.0 * unit_scale
                if width > 0.0:
                    cmds.setAttr(shape + ".orthographicWidth", width)
            except (TypeError, ValueError):
                pass
        _set_clip_planes(shape, ortho, unit_scale)

    return shape


def _set_clip_planes(shape, proj, unit_scale):
    """Set a camera shape's near/far clip from a glTF projection sub-dict.

    znear/zfar are distances in glTF metres -> scaled to scene units. zfar is
    optional (infinite projection); when absent Maya's default far clip stays.
    """
    znear = proj.get("znear")
    zfar = proj.get("zfar")
    if znear is not None:
        try:
            n = float(znear) * unit_scale
            if n > 0.0:
                cmds.setAttr(shape + ".nearClipPlane", n)
        except (TypeError, ValueError):
            pass
    if zfar is not None:
        try:
            f = float(zfar) * unit_scale
            if f > 0.0:
                cmds.setAttr(shape + ".farClipPlane", f)
        except (TypeError, ValueError):
            pass


# ---------------------------------------------------------------------------
# Lights (KHR_lights_punctual)
# ---------------------------------------------------------------------------

def _build_light(gltf, node, xform):
    """Create a Maya light SHAPE under `xform` from the node's glTF light.

    glTF KHR_lights_punctual -> Maya light type mapping:
      directional -> directionalLight  (emits down -Z; Maya directional too)
      point       -> pointLight        (omnidirectional)
      spot        -> spotLight         (emits down -Z; Maya spot too)

    The light shape is parented to the EXISTING node transform `xform`, so the
    node TRS aims it — no extra rotation needed for directional/spot.

    color    -> .color (RGB, passed through; glTF color is linear, as is Maya's)
    intensity-> .intensity, scaled by a pragmatic per-type divisor and clamped
                (see _LIGHT_INTENSITY_SCALE — physical lux/candela are huge, so
                this is APPROXIMATE and tweakable, not photometrically exact).
    spot cone: Maya .coneAngle is the FULL cone in DEGREES; glTF outerConeAngle
                is the HALF-angle in radians -> coneAngle = degrees(outer) * 2.
                .penumbraAngle is set from the (outer - inner) falloff band.

    NOTE: glTF `range` (attenuation cutoff distance) has no direct Maya
    equivalent and is intentionally not applied (documented limitation).
    """
    light_index = gltf.node_light(node)
    light = gltf.light_def(light_index)
    if light is None:
        return

    ltype = light.get("type")
    name = MC.sanitize(light.get("name") or "light")

    # Create the light shape, then parent it under the node transform. The
    # *Light commands return a transform name; we create then move the shape
    # under our existing xform so the node TRS aims the light.
    if ltype == "directional":
        light_tf = cmds.directionalLight(name=name)
    elif ltype == "point":
        light_tf = cmds.pointLight(name=name)
    elif ltype == "spot":
        light_tf = cmds.spotLight(name=name)
    else:
        om.MGlobal.displayWarning(
            "[glb_io] unsupported light type %r; skipping" % (ltype,)
        )
        return

    # cmds.*Light returns the light SHAPE leaf name (it creates a new transform
    # at world with the shape under it). Resolve the shape's full path first (the
    # leaf name can be ambiguous if another light shares it), reparent the SHAPE
    # under our node transform, then delete the now-empty temp transform.
    shape_full = _full_path(light_tf)
    temp_transform = cmds.listRelatives(shape_full, parent=True, fullPath=True)
    cmds.parent(shape_full, xform, shape=True, relative=True)
    if temp_transform:
        try:
            cmds.delete(temp_transform[0])
        except Exception:
            pass

    # Resolve the shape's new full path under xform for attribute edits.
    shapes = cmds.listRelatives(xform, shapes=True, type="light", fullPath=True) or []
    shape_path = shapes[-1] if shapes else (xform + "|" + light_tf)

    # Color (RGB). glTF default is white when omitted.
    color = light.get("color")
    if color and len(color) >= 3:
        try:
            cmds.setAttr(
                shape_path + ".color",
                float(color[0]), float(color[1]), float(color[2]),
                type="double3",
            )
        except Exception:
            pass

    # Intensity — scaled + clamped (approximate, tweakable).
    intensity = light.get("intensity")
    if intensity is None:
        intensity = 1.0  # glTF default
    scale = _LIGHT_INTENSITY_SCALE.get(ltype, _LIGHT_INTENSITY_SCALE_DEFAULT)
    try:
        maya_intensity = float(intensity) * scale
        if maya_intensity > _LIGHT_INTENSITY_MAX:
            maya_intensity = _LIGHT_INTENSITY_MAX
        cmds.setAttr(shape_path + ".intensity", maya_intensity)
    except (TypeError, ValueError):
        pass

    # Spot cone + penumbra. glTF outerConeAngle is the HALF-angle in radians;
    # Maya coneAngle is the FULL cone in degrees -> degrees(outer) * 2. The
    # penumbra softens the edge over the (outer - inner) band; Maya expects it
    # in degrees and a positive value spreads the falloff outward.
    if ltype == "spot":
        spot = light.get("spot") or {}
        outer = spot.get("outerConeAngle")
        inner = spot.get("innerConeAngle")
        if outer is None:
            outer = math.radians(45.0)  # glTF default outer ≈ PI/4
        if inner is None:
            inner = 0.0
        try:
            cone_deg = math.degrees(float(outer)) * 2.0
            # Clamp to Maya's valid coneAngle range (0.006 .. 179.994).
            cone_deg = max(0.006, min(179.994, cone_deg))
            cmds.setAttr(shape_path + ".coneAngle", cone_deg)
            penumbra_deg = math.degrees(float(outer) - float(inner))
            if penumbra_deg < 0.0:
                penumbra_deg = 0.0
            cmds.setAttr(shape_path + ".penumbraAngle", penumbra_deg)
        except (TypeError, ValueError):
            pass

    return shape_path


# ---------------------------------------------------------------------------
# Meshes
# ---------------------------------------------------------------------------

def _build_mesh(gltf, mesh_index, materials, name_prefix, parent,
                coord_convert, prim_metadata,
                skin_index=None, skin_jobs=None, variant_names=None,
                node_index=None, node=None, node_to_blendshape=None):
    mesh = gltf.doc["meshes"][mesh_index]
    mesh_name = MC.sanitize(mesh.get("name") or "mesh_%d" % mesh_index)

    # Morph-target defaults are mesh-level (mesh["weights"], extras.targetNames),
    # optionally overridden by the owning node's weights. Resolve once and pass
    # down to each primitive. Empty lists for non-morph meshes -> no-op.
    mesh_weights = gltf.mesh_morph_weights(mesh)
    node_weights = gltf.node_morph_weights(node) if node else []
    default_weights = node_weights or mesh_weights
    target_names = gltf.mesh_target_names(mesh)

    primitives = mesh.get("primitives", [])
    multi = len(primitives) > 1
    for i, prim in enumerate(primitives):
        prim_name = mesh_name + ("_p%d" % i if multi else "")
        _build_primitive(
            gltf, prim, prim_name, materials, parent,
            coord_convert, prim_metadata,
            skin_index=skin_index, skin_jobs=skin_jobs,
            variant_names=variant_names,
            node_index=node_index, default_weights=default_weights,
            target_names=target_names, node_to_blendshape=node_to_blendshape,
        )


def _build_primitive(gltf, prim, name, materials, parent,
                     coord_convert, prim_metadata,
                     skin_index=None, skin_jobs=None, variant_names=None,
                     node_index=None, default_weights=None, target_names=None,
                     node_to_blendshape=None):
    attrs = prim.get("attributes", {})

    mode = prim.get("mode", 4)  # 4 = TRIANGLES
    if mode != 4:
        om.MGlobal.displayWarning(
            "[glb_io] primitive '%s' uses non-triangle mode %d; v0.1 skips it" % (name, mode)
        )
        return

    # KHR_draco_mesh_compression: the real vertex data lives in a Draco
    # bitstream, NOT the plain accessors (those are fallback bounds/counts). When
    # present we decode the stream and use the decoded POSITION/indices/NORMAL/
    # TEXCOORD_0 in place of the accessor reads below. Everything downstream
    # (UVs, material, skin, morph, metadata) is unchanged — it just consumes the
    # decoded arrays. Non-Draco primitives skip this entirely and behave exactly
    # as before.
    draco_uvs = None  # decoded [(u, v), ...] when Draco carries TEXCOORD_0
    if gltf.primitive_has_draco(prim):
        # Lazily ensure the DracoPy decoder is available — this is the only
        # Maya-side trigger, and only fires when a Draco primitive is actually
        # encountered (non-Draco imports never pay for it). Auto-installs once.
        from maya_glb_io import _draco
        if not _draco.ensure_dracopy():
            om.MGlobal.displayWarning(
                "[glb_io] Draco-compressed primitive '%s' skipped — could not "
                "decode; install DracoPy (mayapy -m pip install DracoPy)" % name
            )
            return
        try:
            decoded = gltf.decode_draco_primitive(prim)
        except Exception as exc:
            om.MGlobal.displayWarning(
                "[glb_io] Draco-compressed primitive '%s' skipped — could not "
                "decode (%s); install DracoPy if missing" % (name, exc)
            )
            return
        positions = decoded.get("POSITION") or []
        if not positions:
            om.MGlobal.displayWarning(
                "[glb_io] Draco primitive '%s' decoded to no positions; skipping"
                % name
            )
            return
        indices = decoded.get("indices")
        if not indices:
            indices = list(range(len(positions)))
        draco_uvs = decoded.get("TEXCOORD_0")
    else:
        if "POSITION" not in attrs:
            om.MGlobal.displayWarning("[glb_io] primitive '%s' missing POSITION; skipping" % name)
            return
        positions = gltf.read_accessor(attrs["POSITION"])
        if not positions:
            return

        if "indices" in prim:
            indices = gltf.read_accessor(prim["indices"])
        else:
            indices = list(range(len(positions)))

    num_tris = len(indices) // 3

    points = om.MPointArray()
    for p in positions:
        x, y, z = coord_convert((p[0], p[1], p[2]))
        points.append(om.MPoint(x, y, z))

    poly_counts = om.MIntArray()
    for _ in range(num_tris):
        poly_counts.append(3)

    poly_connects = om.MIntArray()
    for idx in indices:
        poly_connects.append(idx)

    transform_obj = _node_to_mobject(parent)
    mfn = om.MFnMesh()
    mfn.create(points, poly_counts, poly_connects, parent=transform_obj)

    # UVs (channel 0). For Draco primitives the UVs come from the decoded
    # stream (draco_uvs); otherwise from the TEXCOORD_0 accessor. Both feed the
    # identical assign path below (per-vertex UVs indexed like positions, with
    # glTF's top-origin V flipped to Maya's bottom-origin).
    if draco_uvs is not None or "TEXCOORD_0" in attrs:
        if draco_uvs is not None:
            uvs = draco_uvs
        else:
            uvs = gltf.read_accessor(attrs["TEXCOORD_0"])
        if uvs:
            u_array = om.MFloatArray()
            v_array = om.MFloatArray()
            for uv in uvs:
                u_array.append(uv[0])
                v_array.append(1.0 - uv[1])  # glTF V origin is top, Maya is bottom

            mfn.setUVs(u_array, v_array)
            uv_counts = om.MIntArray()
            for _ in range(num_tris):
                uv_counts.append(3)
            uv_ids = om.MIntArray()
            for idx in indices:
                uv_ids.append(idx)
            try:
                mfn.assignUVs(uv_counts, uv_ids)
            except RuntimeError as e:
                om.MGlobal.displayWarning("[glb_io] UV assign failed for '%s': %s" % (name, e))

    # Material assignment (skipped if materials list is empty)
    mat_idx = prim.get("material")
    shape_path = mfn.fullPathName()
    if materials and mat_idx is not None and mat_idx < len(materials):
        entry = materials[mat_idx]
        if entry and entry[1]:
            cmds.sets(shape_path, edit=True, forceElement=entry[1])

    # KHR_materials_variants: stamp a per-shape variant -> shading-group map so
    # the switcher can reassign this shape later, and apply the default variant
    # if the primitive has no base material to fall back on. Entirely gated on
    # the extension being present (variant_names empty -> nothing happens), so
    # non-variant glTFs are unaffected. See _stamp_variant_map.
    if variant_names and materials and gltf.primitive_variant_mappings(prim):
        _stamp_variant_map(
            gltf, prim, shape_path, materials, variant_names, mat_idx,
        )

    # Always record the metadata regardless of material assignment
    prim_metadata.append((shape_path, mat_idx))

    # Morph targets (blendShape). Entirely gated on the primitive declaring
    # `targets`; non-morph primitives skip all of this and behave exactly as
    # before. Built BEFORE the skin job is queued so the blendShape lands
    # frontOfChain (morph deforms the base geometry, then the skinCluster
    # deforms the morphed result — the usual rig order).
    if gltf.primitive_target_count(prim) > 0:
        try:
            _build_morph_targets(
                gltf, prim, name, parent, shape_path, points,
                poly_counts, poly_connects, coord_convert,
                node_index, default_weights, target_names, node_to_blendshape,
            )
        except Exception as e:
            om.MGlobal.displayWarning(
                "[glb_io] morph-target build failed for '%s': %s" % (name, e)
            )

    # Queue a skin job if this primitive is part of a skinned mesh instance.
    # We can't build the skinCluster yet — joints don't exist, and shape DAG
    # paths may still change during the flatten pass. _build_skins() drains
    # this queue after flatten/centering.
    if skin_index is not None and skin_jobs is not None:
        joint_indices = []
        weight_values = []
        # JOINTS_0/WEIGHTS_0 are required for skinned prims; _1 are optional
        # (8-influence rigs). glTF technically allows _2, _3 etc but they're
        # vanishingly rare; we read up to _3 to be safe.
        for set_n in range(4):
            jname = "JOINTS_%d" % set_n
            wname = "WEIGHTS_%d" % set_n
            if jname not in attrs or wname not in attrs:
                break
            try:
                joint_indices.append(gltf.read_accessor(attrs[jname]))
                weight_values.append(gltf.read_accessor(attrs[wname]))
            except Exception as e:
                om.MGlobal.displayWarning(
                    "[glb_io] failed to decode %s/%s on '%s': %s"
                    % (jname, wname, name, e)
                )
                break
        if joint_indices:
            skin_jobs.append({
                "shape": shape_path,
                "skin_index": skin_index,
                "joints_sets": joint_indices,    # list of [(j0,j1,j2,j3), ...]
                "weights_sets": weight_values,   # list of [(w0,w1,w2,w3), ...]
                "vertex_count": len(positions),
            })


# ---------------------------------------------------------------------------
# Morph targets (blendShapes)
# ---------------------------------------------------------------------------

def _build_morph_targets(gltf, prim, name, parent, shape_path, base_points,
                         poly_counts, poly_connects, coord_convert, node_index,
                         default_weights, target_names, node_to_blendshape):
    """Build a Maya blendShape from a primitive's glTF morph targets.

    glTF morph targets store per-vertex POSITION deltas (added to the base
    positions). We materialise each target as a temporary standalone mesh
    (SAME topology as the base, but vertices = base + delta), build ONE
    blendShape from all the target meshes onto the base SHAPE, then delete the
    temporaries (the blendShape stores their geometry internally).

    Building the target meshes fresh from topology — rather than duplicating the
    base transform — keeps multi-primitive meshes (many shapes under one
    transform) and meshes with child nodes correct: each target is a clean
    single-shape mesh holding exactly this primitive's geometry.

    The delta is run through the SAME coord_convert as the base positions so
    units match (coord_convert is linear here — scale + optional axis swap — so
    applying it to a delta vector is correct).

    The blendShape is created frontOfChain so morphs deform the base geometry
    before any later skinCluster — the conventional rig deformer order.

    Args:
      base_points  : the base mesh's om.MPointArray (already coord-converted),
                     the reference for base + delta target points.
      poly_counts  : om.MIntArray of per-face vertex counts (the base topology).
      poly_connects: om.MIntArray of flattened face-vertex indices.
      default_weights : per-target default weights (node override or mesh
                     weights). May be shorter/empty -> missing entries -> 0.
      target_names : per-target names (extras.targetNames). Used to alias the
                     blendShape weights; missing entries fall back to target<i>.
      node_to_blendshape : dict glTF node index -> [blendShape names]; the new
                     blendShape is appended for `node_index` (when both given).

    NORMAL / TANGENT deltas are decoded by the parser but NOT applied here:
    Maya's blendShape interpolates normals from the deformed geometry, so the
    POSITION delta drives the visible morph. (Documented limitation.)
    """
    targets = gltf.primitive_morph_targets(prim)
    if not targets:
        return

    base_count = len(base_points)
    if base_count == 0:
        return

    # Bind to the base SHAPE (not its transform): a transform may carry several
    # primitive shapes, so the blendShape must target this exact shape.
    if not cmds.objExists(shape_path):
        return

    default_weights = default_weights or []
    target_names = target_names or []

    target_transforms = []  # temp world-level transforms holding target meshes
    alias_names = []        # the desired weight alias per target, in order

    for ti, target in enumerate(targets):
        deltas = target.get("POSITION")
        # Desired weight alias for this target.
        if ti < len(target_names) and target_names[ti]:
            alias = MC.sanitize(str(target_names[ti]))
        else:
            alias = "target%d" % ti
        alias_names.append(alias)

        # base + delta points for this target.
        pts = om.MPointArray()
        for vi in range(base_count):
            bp = base_points[vi]
            if deltas and vi < len(deltas):
                d = deltas[vi]
                dx, dy, dz = coord_convert((d[0], d[1], d[2]))
                pts.append(om.MPoint(bp.x + dx, bp.y + dy, bp.z + dz))
            else:
                pts.append(om.MPoint(bp.x, bp.y, bp.z))

        # Fresh standalone mesh (its own world-level transform) with the SAME
        # topology as the base. MFnMesh.create with parent omitted makes a new
        # transform + shape at the world root.
        try:
            tgt_mfn = om.MFnMesh()
            tgt_mfn.create(pts, poly_counts, poly_connects)
            tgt_shape = tgt_mfn.fullPathName()
            tgt_transform = cmds.listRelatives(
                tgt_shape, parent=True, fullPath=True
            )[0]
            tgt_transform = cmds.rename(
                tgt_transform, MC.sanitize(name + "_morph_%s" % alias)
            )
            target_transforms.append(_full_path(tgt_transform))
        except Exception as e:
            om.MGlobal.displayWarning(
                "[glb_io] could not build morph target %d ('%s') for '%s': %s"
                % (ti, alias, name, e)
            )

    if not target_transforms:
        return

    # Create ONE blendShape: all target meshes driving the base SHAPE.
    bs_name = MC.sanitize(name + "_blendShape")
    try:
        bs = cmds.blendShape(
            *(target_transforms + [shape_path]),
            frontOfChain=True, name=bs_name
        )
        bs_node = bs[0]
    except Exception as e:
        om.MGlobal.displayWarning(
            "[glb_io] blendShape creation failed for '%s': %s" % (name, e)
        )
        for d in target_transforms:
            if cmds.objExists(d):
                cmds.delete(d)
        return

    # Delete the temporary target transforms — the blendShape captured their
    # geometry as targets.
    for d in target_transforms:
        if cmds.objExists(d):
            cmds.delete(d)

    # Rename the blendShape weight aliases to the target names (best-effort).
    # Maya derives the default alias from the duplicate transform name; renaming
    # to a clean, stable alias keeps the weight-animation applier and the user
    # UI tidy. weight[i] is the i-th target in the order we passed them.
    _rename_blendshape_aliases(bs_node, alias_names)

    # Apply default weights (node override or mesh weights). Missing entries
    # stay at Maya's default of 0.
    for ti in range(len(target_transforms)):
        w = default_weights[ti] if ti < len(default_weights) else 0.0
        try:
            cmds.setAttr("%s.weight[%d]" % (bs_node, ti), float(w))
        except Exception:
            pass

    # Record the blendShape for the owning glTF mesh node so the animation
    # applier can resolve a `weights` channel back to it.
    if node_to_blendshape is not None and node_index is not None:
        node_to_blendshape.setdefault(node_index, []).append(bs_node)


def _rename_blendshape_aliases(bs_node, alias_names):
    """Rename a blendShape's weight[i] aliases to alias_names[i] (best-effort).

    Maya exposes each blendShape target as an aliased attribute on
    weight[i]. We rename via cmds.aliasAttr so the weights carry the glTF
    target names. Failures are non-fatal (the weight[i] index still works for
    animation, which keys weight[i] directly).
    """
    for i, alias in enumerate(alias_names):
        if not alias:
            continue
        plug = "%s.weight[%d]" % (bs_node, i)
        try:
            current = cmds.aliasAttr(plug, query=True)
        except Exception:
            current = None
        if current == alias:
            continue
        try:
            cmds.aliasAttr(alias, plug)
        except Exception:
            # Name clash or other issue — keep Maya's default alias; harmless.
            pass


def _stamp_variant_map(gltf, prim, shape_path, materials, variant_names, base_mat_idx):
    """Store this primitive's KHR_materials_variants switching table on its shape.

    For each variant name we resolve the shading group it should use:
      * the material from the matching mapping, if any;
      * otherwise the primitive's base `material` (so a variant with no mapping
        for this primitive keeps showing the default look);
      * skipped entirely if neither resolves to a real shading group.

    The resulting {variantName: shadingGroupName} dict is JSON-serialised onto a
    string attribute named `glbVariantMap` on the SHAPE. A shape attribute
    survives scene save/reload, so the switcher (commands.set_active_material_
    variant) keeps working in a later session. The default look already assigned
    in _build_primitive (the base material, or variant 0 when there's no base)
    is left untouched here.
    """
    def sg_for_material(idx):
        if idx is None or idx < 0 or idx >= len(materials):
            return None
        entry = materials[idx]
        if entry and entry[1]:
            return entry[1]
        return None

    variant_map = {}  # variantName -> shadingGroupName
    base_sg = sg_for_material(base_mat_idx)
    any_mapped = False

    for vid, vname in enumerate(variant_names):
        mapped_idx = gltf.primitive_variant_material(prim, vid)
        if mapped_idx is not None:
            sg = sg_for_material(mapped_idx)
            any_mapped = True
        else:
            sg = base_sg  # fall back to the primitive's default material
        if sg:
            variant_map[vname] = sg

    if not variant_map:
        return

    # If the primitive had NO base material, nothing was assigned in
    # _build_primitive — give the shape a sensible default look now using the
    # first variant that actually maps a material (variant 0's SG, by order).
    if base_sg is None and any_mapped:
        for vname in variant_names:
            sg = variant_map.get(vname)
            if sg:
                try:
                    cmds.sets(shape_path, edit=True, forceElement=sg)
                except Exception:
                    pass
                break

    try:
        if not cmds.attributeQuery("glbVariantMap", node=shape_path, exists=True):
            cmds.addAttr(shape_path, longName="glbVariantMap", dataType="string")
        cmds.setAttr(
            shape_path + ".glbVariantMap",
            json.dumps(variant_map),
            type="string",
        )
    except Exception as exc:
        om.MGlobal.displayWarning(
            "[glb_io] could not stamp variant map on '%s': %s" % (shape_path, exc)
        )


def _node_to_mobject(name):
    sel = om.MSelectionList()
    sel.add(name)
    return sel.getDependNode(0)


# ---------------------------------------------------------------------------
# Skinning (joints + skinCluster)
# ---------------------------------------------------------------------------

def _is_world_identity(transform):
    """True if `transform` has an identity WORLD matrix (within tolerance)."""
    try:
        wm = cmds.xform(transform, query=True, worldSpace=True, matrix=True)
    except Exception:
        return False
    ident = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    return all(abs(a - b) < 1e-5 for a, b in zip(wm, ident))


def _neutralize_skinned_mesh_transforms(skin_jobs, node_to_maya=None):
    """Strip inherited transforms from every distinct skinned-mesh transform.

    Per the glTF spec a skinned mesh node's transform (and, by extension, its
    ancestors') is ignored: the verts in object space + the joints reproduce the
    bind pose. We enforce that by reparenting each skinned-mesh transform to
    world with an identity local TRS, so no ancestor transform leaks into the
    bind. Many shapes may share one transform (BrainStem), so we operate on the
    set of distinct transforms and patch ALL affected job["shape"] paths.

    Returns the list of resulting world-level skinned-mesh transforms (so the
    caller can fold them into top_transforms for the drop-to-floor pass).
    """
    # Map current transform full-path -> list of jobs whose shape sits under it.
    transforms = {}
    for job in skin_jobs:
        shape = job["shape"]
        if not cmds.objExists(shape):
            continue
        parent = cmds.listRelatives(shape, parent=True, fullPath=True)
        if not parent:
            continue
        transforms.setdefault(parent[0], []).append(job)

    new_tops = []
    for transform, jobs in transforms.items():
        if not cmds.objExists(transform):
            continue
        # Already at world with identity? nothing to do (the common simple case,
        # e.g. RiggedSimple's mesh sits at the root already).
        is_root = not (cmds.listRelatives(transform, parent=True, fullPath=True) or [])
        if is_root and _is_world_identity(transform):
            new_tops.append(_full_path(transform))
            continue

        old_path = transform
        try:
            if not is_root:
                new_path = cmds.parent(transform, world=True)[0]
            else:
                new_path = transform
            # Zero local TRS so the shape's world transform is identity.
            cmds.setAttr(new_path + ".translate", 0, 0, 0, type="double3")
            cmds.setAttr(new_path + ".rotate", 0, 0, 0, type="double3")
            cmds.setAttr(new_path + ".scale", 1, 1, 1, type="double3")
        except Exception as e:
            om.MGlobal.displayWarning(
                "[glb_io] could not neutralize skinned-mesh transform '%s': %s"
                % (transform, e)
            )
            continue

        # Patch every job shape path that lived under the old transform path.
        new_full = _full_path(new_path)
        new_tops.append(new_full)
        for job in jobs:
            old_shape = job["shape"]
            # old_shape == old_path + "|" + shapeShort
            suffix = old_shape[len(old_path):]
            job["shape"] = new_full + suffix

        # Patch node_to_maya so the skinned-mesh node (and anything that was
        # under the old transform path) keeps pointing at the live node. Avoids
        # a stale-path warning if an animation channel targets the mesh node
        # (whose transform the spec ignores for skinned geometry anyway).
        if node_to_maya:
            for nidx, path in list(node_to_maya.items()):
                if path == old_path:
                    node_to_maya[nidx] = new_full
                elif path.startswith(old_path + "|"):
                    node_to_maya[nidx] = new_full + path[len(old_path):]

    return new_tops


def _build_skins(gltf, skin_jobs, name_prefix, coord_convert, node_to_maya=None):
    """Create skinClusters for every queued skin job.

    Joints are NOT created here — they were already built in-hierarchy by
    _build_node (any glTF node in a skin's `joints` list becomes a Maya
    `joint` in its exact place in the node tree). This function only:

      1. Looks the joints up by glTF node index via node_to_maya, in the order
         skin.joints lists them (= the skinCluster influence order, which is
         what the per-vertex JOINTS_n indices reference).
      2. Saves the bind pose (dagPose) so users can return to it.
      3. Creates ONE skinCluster per mesh SHAPE (not per transform — a single
         transform can hold many shapes, e.g. BrainStem's ~59 primitives, and
         cmds.skinCluster on such a transform fails) and applies the glTF
         JOINTS_n/WEIGHTS_n via the bulk MFnSkinCluster.setWeights API.

    Returns the list of new/affected world-level transforms (the reparented
    skinned-mesh transforms) so the caller can fold them into top_transforms for
    the drop-to-floor pass. The joints themselves stay wherever the glTF
    hierarchy placed them (already inside top_transforms' subtrees).
    """
    # We deliberately do NOT neutralize / reparent the skinned mesh. The mesh and
    # the skeleton share the same ancestor transforms in the node hierarchy
    # (e.g. CesiumMan's Z_UP/Armature), and Maya's skinCluster cancels that
    # shared transform automatically (its bindPreMatrix = inverse(jointWorld) is
    # relative to the mesh's world). Reparenting the mesh to world-identity would
    # remove that shared transform from the mesh ONLY, leaving the joints with an
    # uncancelled Z-up rotation -> the gross distortion seen on CesiumMan. The
    # mesh therefore stays exactly where _build_node placed it in the hierarchy.
    new_tops = []

    # Group skin jobs by skin index so we resolve each influence list once.
    jobs_by_skin = {}
    for job in skin_jobs:
        jobs_by_skin.setdefault(job["skin_index"], []).append(job)

    node_to_maya = node_to_maya or {}
    skins_doc = gltf.doc.get("skins") or []
    for skin_index, jobs in jobs_by_skin.items():
        if skin_index < 0 or skin_index >= len(skins_doc):
            om.MGlobal.displayWarning(
                "[glb_io] skin index %d out of range; skipping" % skin_index
            )
            continue
        try:
            skin = gltf.read_skin(skin_index)
        except Exception as e:
            om.MGlobal.displayWarning(
                "[glb_io] failed to decode skin %d: %s" % (skin_index, e)
            )
            continue

        if not skin.joint_node_indices:
            om.MGlobal.displayWarning(
                "[glb_io] skin %d has no joints; skipping" % skin_index
            )
            continue

        # Resolve the already-built joints by glTF node index, IN skin.joints
        # order. The position in this list is the influence index that the
        # per-vertex JOINTS_n values reference. We keep the full ordered list
        # (with None for any unresolved joint) so JOINTS_n indices stay aligned;
        # missing joints just get zero weight in the weight array.
        joint_names_in_order = [
            node_to_maya.get(idx) for idx in skin.joint_node_indices
        ]
        # Drop entries that vanished (e.g. node not built / collapsed). Keep the
        # remaining ones in order; build an index remap so the weight array uses
        # the surviving influences. (In the normal case nothing is dropped.)
        resolved = []
        old_to_new = {}
        for old_i, jname in enumerate(joint_names_in_order):
            if jname and cmds.objExists(jname):
                old_to_new[old_i] = len(resolved)
                resolved.append(jname)
        if not resolved:
            om.MGlobal.displayWarning(
                "[glb_io] skin %d: none of its joints resolved to live Maya "
                "joints; skipping cluster" % skin_index
            )
            continue

        # Save bind pose so users can return to it after posing.
        try:
            cmds.select(resolved, replace=True)
            cmds.dagPose(*resolved, save=True, bindPose=True)
        except Exception as e:
            om.MGlobal.displayWarning(
                "[glb_io] dagPose save failed for skin %d: %s" % (skin_index, e)
            )

        # Create one skinCluster per mesh shape bound to this skin
        for job in jobs:
            shape = job["shape"]
            if not cmds.objExists(shape):
                om.MGlobal.displayWarning(
                    "[glb_io] skin job shape '%s' missing post-flatten; skipping" % shape
                )
                continue
            try:
                _apply_skin_to_mesh(shape, resolved, old_to_new, skin, job)
            except Exception as e:
                om.MGlobal.displayError(
                    "[glb_io] skinCluster on '%s' failed: %s" % (shape, e)
                )

    return new_tops


def _apply_skin_to_mesh(shape, joint_names_in_order, old_to_new, skin, job):
    """Bind the mesh SHAPE `shape` to `joint_names_in_order` and apply the glTF
    per-vertex weights with the bulk MFnSkinCluster.setWeights API.

    `joint_names_in_order` is the list of SURVIVING Maya joint names (influence
    order). `old_to_new` maps the original skin.joints position (what JOINTS_n
    references) -> index into joint_names_in_order. This keeps weights aligned
    even if a joint failed to resolve.

    We bind the SHAPE (`shape`) rather than its parent transform: BrainStem-style
    meshes put dozens of shapes under one transform, and cmds.skinCluster on such
    a transform raises "more than one object selected". Each shape carries its
    own JOINTS_n/WEIGHTS_n (in `job`), so each shape is bound independently.
    """
    n_resolved = len(joint_names_in_order)

    # toSelectedBones=True binds only the joints we pass in. bindMethod=0
    # (closest-distance) is irrelevant — we overwrite every weight immediately.
    # We name the cluster after the shape so per-shape clusters stay distinct.
    shape_short = MC.sanitize(shape.rsplit("|", 1)[-1])
    sc_nodes = cmds.skinCluster(
        joint_names_in_order, shape,
        toSelectedBones=True,
        maximumInfluences=4,
        normalizeWeights=1,
        bindMethod=0,
        skinMethod=0,
        obeyMaxInfluences=False,
        name=shape_short + "_skinCluster",
    )
    skin_cluster = sc_nodes[0]

    joints_sets = job["joints_sets"]      # list of vec4 lists (uint per joint slot)
    weights_sets = job["weights_sets"]    # list of vec4 lists (float per slot)
    vertex_count = job["vertex_count"]

    # ----- Build the flat, vertex-major weight array -------------------------
    # MFnSkinCluster.setWeights wants:
    #   * an MIntArray of influence LOGICAL indices (matrix[] plug indices)
    #   * an MDoubleArray of weights laid out vertex-major: for each vertex,
    #     one value per influence (in the same order as the influence index
    #     array). Most are 0; the vertex's (up to) 4 influences carry weight.
    # We pass ALL resolved influences and a dense per-vertex row. For dense
    # meshes this is n_verts * n_influences doubles — large but built in C-speed
    # list ops and consumed in a single API call (vs O(verts) skinPercent).
    flat = [0.0] * (vertex_count * n_resolved)
    for v in range(vertex_count):
        # Aggregate (new influence index -> total weight) across all _n sets.
        agg = {}
        for jset, wset in zip(joints_sets, weights_sets):
            if v >= len(jset) or v >= len(wset):
                continue
            j4 = jset[v]
            w4 = wset[v]
            for slot in range(4):
                w = float(w4[slot])
                if w <= 0.0:
                    continue
                old_idx = int(j4[slot])
                new_idx = old_to_new.get(old_idx)
                if new_idx is None:
                    continue
                agg[new_idx] = agg.get(new_idx, 0.0) + w

        if not agg:
            continue

        # Keep the top-4 influences and normalize (matches the old behaviour).
        items = sorted(agg.items(), key=lambda kv: -kv[1])[:4]
        total = sum(w for _, w in items)
        if total <= 0.0:
            continue
        base = v * n_resolved
        for new_idx, w in items:
            flat[base + new_idx] = w / total

    # ----- Influence logical indices ----------------------------------------
    # MFnSkinCluster.indexForInfluenceObject gives the logical matrix[] index
    # for each joint dag path. These are what setWeights expects (NOT 0..n-1
    # positional — they can differ, though for a fresh bind they usually match).
    sel = om.MSelectionList()
    sel.add(skin_cluster)
    sc_obj = sel.getDependNode(0)
    mfn_skin = oma.MFnSkinCluster(sc_obj)

    influence_indices = om.MIntArray()
    for jname in joint_names_in_order:
        jsel = om.MSelectionList()
        jsel.add(jname)
        jdag = jsel.getDagPath(0)
        influence_indices.append(mfn_skin.indexForInfluenceObject(jdag))

    # ----- Mesh dag path + all-vertex component -----------------------------
    msel = om.MSelectionList()
    msel.add(shape)
    mesh_dag = msel.getDagPath(0)

    comp_fn = om.MFnSingleIndexedComponent()
    comp = comp_fn.create(om.MFn.kMeshVertComponent)
    comp_fn.setCompleteData(vertex_count)

    weights = om.MDoubleArray(flat)

    # Single bulk call — orders of magnitude faster than per-vertex skinPercent.
    # normalize=False because we already normalized the top-4 above.
    mfn_skin.setWeights(mesh_dag, comp, influence_indices, weights, False)
