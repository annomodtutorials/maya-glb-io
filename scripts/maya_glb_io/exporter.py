"""Maya-facing glTF/GLB export orchestrator.

Public API:
    export_selection(path, options=None) -> dict
    export_all(path, options=None)       -> dict
    export_nodes(transforms, path, options=None) -> dict

Walks a set of Maya transforms, mirrors their parenting + local TRS into a glTF
node hierarchy, extracts each mesh's geometry (one primitive per shadingGroup),
resolves each SG's surface shader into a glTF PBR material (via
_materials_export), dedupes materials + images, and writes a .glb or .gltf via
_glb_export.

Mesh extraction is adapted from maya_anno_addon/_glb_writer.py but generalised
(no Anno meter-only assumption): geometry is unwelded per-face-vertex, normals
are per-vertex object-space, tangents are UV-derived vec4, TEXCOORD_0 has V
flipped to glTF top-left, COLOR_0 emitted as float vec4 when a colour set
exists.

Options (see _prefs.EXPORT_DEFAULTS):
    format        "glb" | "gltf"
    scope         "selection" | "scene"   (export_selection/all pick this)
    embedTextures bool   embed images (data-uri for .gltf, BIN bufferView for
                         .glb) instead of referencing external files
    convertUnits  bool   divide positions by the Maya-cm->metre scale so the
                         output is in metres (glTF standard). Default on.
    yUp           bool   reserved; glTF and Maya are both Y-up so no axis swap
                         is applied (kept as a no-op flag for symmetry / future
                         Z-up source handling). Default False (= no change).
    applyTransforms bool when True, bake each node's world transform into the
                         vertices and emit identity nodes. When False (default)
                         the node hierarchy carries local TRS and verts stay in
                         object space.
    copyTextures  bool   in REFERENCE mode, copy each source texture next to the
                         output file and point the uri at the copy. Default on.
"""
from __future__ import annotations

import math
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

import maya.api.OpenMaya as om
import maya.api.OpenMayaAnim as oma
from maya import cmds

from maya_glb_io import _glb_export as GX
from maya_glb_io import _materials_export as MX


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def export_selection(path, options=None):
    # type: (str, Optional[dict]) -> dict
    sel = cmds.ls(selection=True, long=True, type="transform") or []
    # Also accept selected shapes -> their transforms.
    if not sel:
        shapes = cmds.ls(selection=True, long=True, shapes=True) or []
        parents = []
        for s in shapes:
            p = cmds.listRelatives(s, parent=True, fullPath=True) or []
            parents.extend(p)
        sel = list(dict.fromkeys(parents))
    if not sel:
        om.MGlobal.displayError("[glb_io] export: nothing selected.")
        return {"path": None, "node_count": 0, "primitive_count": 0,
                "material_count": 0}
    return export_nodes(sel, path, options=options)


def export_all(path, options=None):
    # type: (str, Optional[dict]) -> dict
    # All top-level (world-parented) transforms, excluding default cameras.
    roots = cmds.ls(assemblies=True, long=True) or []
    default_cams = set(_default_camera_transforms())
    roots = [r for r in roots if r not in default_cams]
    if not roots:
        om.MGlobal.displayError("[glb_io] export: scene has no exportable transforms.")
        return {"path": None, "node_count": 0, "primitive_count": 0,
                "material_count": 0}
    return export_nodes(roots, path, options=options)


def export_nodes(transforms, path, options=None):
    # type: (List[str], str, Optional[dict]) -> dict
    """Export the given transform subtrees to `path`."""
    options = _normalize_options(options)
    fmt = options.get("format", "glb")
    embed = bool(options.get("embedTextures"))
    copy_tex = bool(options.get("copyTextures"))
    apply_xform = bool(options.get("applyTransforms"))
    export_skin = bool(options.get("exportSkin", True))
    export_anim = bool(options.get("exportAnimation", True))
    export_morph = bool(options.get("exportMorphTargets", True))
    unit_scale = _resolve_unit_scale(options)

    # Baking world transforms into vertices is incompatible with skinning +
    # animation (joints/animCurves drive nodes, which must keep their TRS), so
    # disable both when applyTransforms is on. Morph deltas are object-space
    # offsets independent of the node transform, so they survive baking.
    if apply_xform:
        export_skin = False
        export_anim = False

    om.MGlobal.displayInfo(
        "[glb_io] exporting %d root(s) -> %s (format=%s, unit_scale=%s)"
        % (len(transforms), path, fmt, unit_scale)
    )

    builder = _SceneBuilder(unit_scale=unit_scale, apply_transforms=apply_xform,
                            export_skin=export_skin, export_morph=export_morph)

    # De-dup top-level transforms (avoid exporting a node twice if both it and
    # an ancestor were selected). Keep only roots whose ancestors aren't also in
    # the set.
    roots = _filter_to_roots(transforms)

    for root in roots:
        builder.add_subtree(root, parent_node_index=None)

    if not builder.meshes_have_geometry():
        om.MGlobal.displayWarning(
            "[glb_io] export: no mesh geometry found under the given transforms."
        )

    # Resolve skinClusters into SkinDefs (may add joint nodes), THEN sample
    # animation across all nodes (including any joints the skin pass added).
    if export_skin:
        builder.finalize_skins()

    animations = []
    if export_anim:
        anim = builder.build_animation()
        if anim is not None and anim.channels:
            animations = [anim]

    nodes = builder.nodes
    meshes = builder.meshes
    materials = builder.materials
    root_indices = builder.root_node_indices
    skins = builder.skins

    # Texture handling: in REFERENCE mode, optionally copy sources next to the
    # output and set each image's uri to the (relative) copied name.
    if not embed:
        _finalize_reference_textures(builder.image_cache, path, copy_tex)

    # Build + write the document.
    out_path = GX.export_to_file(
        path, nodes, meshes, materials, root_indices,
        fmt=fmt, embed_images=embed, generator=GX.GENERATOR,
        skins=skins, animations=animations,
    )

    prim_count = sum(len(m.primitives) for m in meshes)
    om.MGlobal.displayInfo(
        "[glb_io] export complete: %d node(s), %d primitive(s), %d material(s), "
        "%d skin(s), %d animation(s) -> %s"
        % (len(nodes), prim_count, len(materials), len(skins),
           len(animations), out_path)
    )

    return {
        "path": out_path,
        "node_count": len(nodes),
        "primitive_count": prim_count,
        "material_count": len(materials),
        "skin_count": len(skins),
        "animation_count": len(animations),
    }


# ---------------------------------------------------------------------------
# Scene builder — transforms -> glTF node/mesh/material descriptors
# ---------------------------------------------------------------------------

class _SceneBuilder(object):

    def __init__(self, unit_scale=1.0, apply_transforms=False,
                 export_skin=True, export_morph=True):
        self.unit_scale = unit_scale
        self.apply_transforms = apply_transforms
        self.export_skin = export_skin
        self.export_morph = export_morph

        self.nodes = []                 # list[GX.Node]
        self.meshes = []                # list[GX.MeshDef]
        self.materials = []             # list[GX.MaterialDef]
        self.root_node_indices = []     # list[int]
        self.skins = []                 # list[GX.SkinDef]

        self.image_cache = {}           # normcase-abspath -> GX.ImageRef
        self._material_index_of = {}    # shader node name -> material index

        # Maya transform full-path -> glTF node index. Lets skins/animation
        # resolve joint + target node indices across the whole export.
        self.node_index_of = {}
        # Pending skin-cluster jobs discovered while building meshes. Drained by
        # finalize_skins() once every joint subtree has been added to the node
        # list (joints referenced by a skin MUST exist as glTF nodes).
        self._skin_jobs = []            # list of dict (see _build_mesh)

    def meshes_have_geometry(self):
        return any(m.primitives for m in self.meshes)

    def add_subtree(self, transform, parent_node_index):
        """Create a glTF node for `transform`, attach a mesh if it has one, and
        recurse into child transforms."""
        # Idempotent: a transform already turned into a node (e.g. a joint that
        # also lives under an exported root) is reused, not duplicated. Re-parent
        # it under the requested parent if it was previously a root with no
        # parent recorded.
        full = _full_path(transform)
        if full in self.node_index_of:
            existing = self.node_index_of[full]
            if parent_node_index is not None and existing in self.root_node_indices:
                # Promote from standalone root to a child of parent_node_index.
                self.root_node_indices.remove(existing)
                if existing not in self.nodes[parent_node_index].children:
                    self.nodes[parent_node_index].children.append(existing)
            return existing

        node_index = len(self.nodes)
        node = GX.Node(name=_short_name(transform))
        self.nodes.append(node)
        self.node_index_of[full] = node_index

        if parent_node_index is None:
            self.root_node_indices.append(node_index)
        else:
            self.nodes[parent_node_index].children.append(node_index)

        # Transform. When apply_transforms is on we bake world transforms into
        # verts instead, so nodes stay identity. Otherwise a glTF ROOT node
        # (no parent in the exported file) must carry its full WORLD transform,
        # while a nested child carries its transform relative to its parent.
        if not self.apply_transforms:
            self._set_node_local_trs(
                transform, node, is_root=(parent_node_index is None)
            )

        # Mesh shape under this transform?
        shape = _first_mesh_shape(transform)
        if shape is not None:
            mesh_def = self._build_mesh(transform, shape, node_index)
            if mesh_def is not None and mesh_def.primitives:
                node.mesh_index = len(self.meshes)
                self.meshes.append(mesh_def)

        # Recurse into child transforms (skip shapes).
        children = cmds.listRelatives(transform, children=True, fullPath=True,
                                      type="transform") or []
        for child in children:
            self.add_subtree(child, node_index)

        return node_index

    def _set_node_local_trs(self, transform, node, is_root=False):
        """Read the transform's placement and store it as a glTF TRS triple.

        We decompose a MATRIX rather than reading the translate / rotate / scale
        channels directly. This matters because Maya lets you offset the
        rotate/scale *pivots* independently of the translate channel, and glTF
        has no concept of a pivot. Decomposing the matrix (which already has all
        pivot offsets composited in) bakes the pivot into a plain TRS that
        reproduces the exact same placement — so "move pivot to base, then
        reposition" round-trips correctly.

        For a glTF ROOT node we use the transform's WORLD matrix, because the
        exported root has no parent in the file and must therefore carry the
        full placement (otherwise any Maya parent's transform is lost and the
        object re-imports offset). For a nested child we use the matrix relative
        to its (also-exported) parent.

        Translations are divided by unit_scale to convert Maya units -> metres.
        """
        try:
            sel = om.MSelectionList()
            sel.add(transform)
            dag = sel.getDagPath(0)
            if is_root:
                # Full world transform.
                m = dag.inclusiveMatrix()
            else:
                # local = world(self) * inverse(world(parent))
                m = dag.inclusiveMatrix() * dag.exclusiveMatrixInverse()
            tm = om.MTransformationMatrix(m)
            t = tm.translation(om.MSpace.kTransform)
            q = tm.rotation(asQuaternion=True)
            s = tm.scale(om.MSpace.kTransform)
            node.translation = (t.x / self.unit_scale,
                                t.y / self.unit_scale,
                                t.z / self.unit_scale)
            node.rotation = (q.x, q.y, q.z, q.w)
            node.scale = (s[0], s[1], s[2])
        except Exception as exc:
            om.MGlobal.displayWarning(
                "[glb_io] export: could not read transform '%s' (%s); "
                "writing identity" % (transform, exc)
            )
            node.translation = (0.0, 0.0, 0.0)
            node.rotation = (0.0, 0.0, 0.0, 1.0)
            node.scale = (1.0, 1.0, 1.0)

    def _build_mesh(self, transform, shape, mesh_node_index):
        # type: (str, str, int) -> Optional[GX.MeshDef]
        """Extract `shape` into a GX.MeshDef (one primitive per shadingGroup)."""
        try:
            sel = om.MSelectionList()
            sel.add(shape)
            dag = sel.getDagPath(0)
        except Exception as exc:
            om.MGlobal.displayWarning(
                "[glb_io] export: can't resolve shape '%s': %s" % (shape, exc)
            )
            return None

        mfn = om.MFnMesh(dag)
        shape_full = dag.fullPathName()
        num_polys = mfn.numPolygons

        face_to_sg = _get_face_to_sg_map(transform, shape_full, num_polys)
        if not face_to_sg:
            face_to_sg = {None: list(range(num_polys))}

        sg_order = sorted(face_to_sg.keys(), key=lambda k: (k is None, k or ""))

        # World matrix for baking when apply_transforms is on.
        world_mat = None
        if self.apply_transforms:
            world_mat = dag.inclusiveMatrix()

        # Skin detection. Per-vertex top-4 (joint, weight) for the ORIGINAL
        # (welded) mesh vertices; reused across every primitive of this mesh.
        skin_info = None
        if self.export_skin:
            skin_info = self._extract_skin_weights(dag, shape_full)

        # Morph (blendShape) detection. Per-(original)-vertex POSITION deltas for
        # each blendShape target; reused across every primitive of this mesh.
        morph_info = None
        if self.export_morph:
            morph_info = self._extract_morph_targets(dag, shape_full)

        mesh_def = GX.MeshDef(name=_short_name(transform))
        # Carry the mesh-level morph metadata (default weights + target names).
        if morph_info:
            mesh_def.weights = morph_info["weights"]
            mesh_def.target_names = morph_info["target_names"]

        # We need the original mesh vertex id per unwelded face-vertex whenever
        # skin OR morph export is active (both map per-original-vertex data onto
        # the unwelded POSITION stream).
        need_orig_vids = bool(skin_info or morph_info)

        for sg_name in sg_order:
            faces = face_to_sg[sg_name]
            if not faces:
                continue
            orig_vids = [] if need_orig_vids else None
            prim = self._extract_primitive(
                dag, mfn, faces, world_mat, orig_vids_out=orig_vids
            )
            if prim is None or prim.vertex_count() == 0:
                continue

            # Skin attributes for this primitive's unwelded vertices.
            if skin_info and orig_vids:
                self._attach_skin_attrs(prim, orig_vids, skin_info)

            # Morph-target deltas for this primitive's unwelded vertices.
            if morph_info and orig_vids:
                self._attach_morph_targets(prim, orig_vids, morph_info)

            # Resolve material for this SG.
            if sg_name:
                shader = _shader_for_sg(sg_name)
                prim.material_index = self._intern_material(shader, sg_name)
            mesh_def.primitives.append(prim)

        # Queue a skin job for this mesh node once it has skinned primitives.
        if skin_info and any(p.joints for p in mesh_def.primitives):
            self._skin_jobs.append({
                "mesh_node_index": mesh_node_index,
                "influences": skin_info["influences"],   # joint full paths
                "bind_pre_matrices": skin_info["bind_pre_matrices"],
                "name": _short_name(transform) + "_skin",
            })

        return mesh_def

    def _intern_material(self, shader, sg_name):
        """Resolve+dedupe a Maya shader into the export's materials list.
        Returns the material index, or None if there's no shader."""
        if not shader:
            return None
        if shader in self._material_index_of:
            return self._material_index_of[shader]
        mat_name = _clean_material_name(shader)
        mdef = MX.shader_to_material_def(shader, mat_name, self.image_cache)
        idx = len(self.materials)
        self.materials.append(mdef)
        self._material_index_of[shader] = idx
        return idx

    def _extract_primitive(self, dag, mfn, face_indices, world_mat,
                           orig_vids_out=None):
        # type: (Any, Any, List[int], Any, Optional[list]) -> Optional[GX.Primitive]
        """Unweld the given faces into flat glTF arrays. Adapted + generalised
        from maya_anno_addon/_glb_writer._extract_unwelded_filtered.

        When `orig_vids_out` is a list, the ORIGINAL mesh vertex id of each
        emitted (unwelded) face-vertex is appended to it, parallel 1:1 with the
        POSITION stream. Skin export uses this to look up per-vertex weights for
        the unwelded vertices."""
        pos_array = mfn.getPoints(om.MSpace.kObject)
        has_uvs = mfn.numUVs() > 0
        u_arr, v_arr = (mfn.getUVs() if has_uvs else (None, None))

        # Vertex colour set (if any).
        color_set_name = None
        try:
            color_sets = cmds.polyColorSet(
                dag.fullPathName(), query=True, allColorSets=True
            ) or []
            if color_sets:
                color_set_name = color_sets[0]
        except (RuntimeError, ImportError):
            color_set_name = None

        # Normal matrix for baking (inverse-transpose of world for directions).
        normal_mat = None
        if world_mat is not None:
            normal_mat = world_mat.inverse().transpose()

        positions = []
        normals = []
        tangents = []
        uvs = []
        colors = []
        indices = []

        face_set = set(face_indices)
        poly_iter = om.MItMeshPolygon(dag)
        next_vid = 0

        while not poly_iter.isDone():
            face_idx = poly_iter.index()
            if face_idx not in face_set:
                poly_iter.next()
                continue

            _tri_points, triangle_verts = poly_iter.getTriangles()
            triangle_count = len(triangle_verts) // 3
            for tri_i in range(triangle_count):
                base = tri_i * 3
                tri_globals = [triangle_verts[base + j] for j in range(3)]

                tri_uvs = (_tri_uvs(poly_iter, tri_globals, u_arr, v_arr)
                           if has_uvs else None)
                tri_tangent = _compute_tri_tangent(pos_array, tri_globals, tri_uvs)

                for j in range(3):
                    vg = tri_globals[j]

                    # POSITION
                    p = pos_array[vg]
                    if world_mat is not None:
                        wp = om.MPoint(p) * world_mat
                        positions.extend([wp.x / self.unit_scale,
                                          wp.y / self.unit_scale,
                                          wp.z / self.unit_scale])
                    else:
                        positions.extend([p.x / self.unit_scale,
                                          p.y / self.unit_scale,
                                          p.z / self.unit_scale])

                    # NORMAL
                    try:
                        n = mfn.getVertexNormal(vg, True, om.MSpace.kObject)
                        if normal_mat is not None:
                            nv = om.MVector(n.x, n.y, n.z) * normal_mat
                            nv.normalize()
                            normals.extend([nv.x, nv.y, nv.z])
                        else:
                            normals.extend([n.x, n.y, n.z])
                    except RuntimeError:
                        normals.extend([0.0, 1.0, 0.0])

                    # TANGENT (vec4, w=1 right-handed)
                    tangents.extend([tri_tangent[0], tri_tangent[1],
                                     tri_tangent[2], 1.0])

                    # TEXCOORD_0 (flip V to glTF top-left)
                    if has_uvs:
                        try:
                            local_fv = _find_local_face_vertex(poly_iter, vg)
                            uv_idx = poly_iter.getUVIndex(local_fv)
                            uvs.extend([float(u_arr[uv_idx]),
                                        1.0 - float(v_arr[uv_idx])])
                        except (RuntimeError, IndexError):
                            uvs.extend([0.0, 0.0])

                    # COLOR_0
                    if color_set_name:
                        colors.extend(_face_vertex_color(
                            mfn, face_idx, vg, color_set_name
                        ))

                    # Track the original mesh vertex id for skin-weight lookup.
                    if orig_vids_out is not None:
                        orig_vids_out.append(vg)

                    indices.append(next_vid)
                    next_vid += 1

            poly_iter.next()

        if not positions:
            return None

        return GX.Primitive(
            positions=positions,
            indices=indices,
            normals=normals,
            tangents=tangents,
            texcoords=uvs,
            colors=colors,
        )

    # -----------------------------------------------------------------------
    # Skin extraction
    # -----------------------------------------------------------------------

    def _extract_skin_weights(self, dag, shape_full):
        # type: (Any, str) -> Optional[dict]
        """Detect a skinCluster on `shape_full` and pre-compute per-(original)
        -vertex top-4 (influence-index, weight) pairs.

        Returns a dict or None (no skinCluster / failure):
          {
            "influences"        : [joint full path, ...],   # influence order
            "bind_pre_matrices" : [(16 floats Maya row-major), ...],
            "per_vertex"        : [ [(infl_idx, w), ...up to 4], ... ],
          }
        per_vertex is indexed by ORIGINAL mesh vertex id.
        """
        try:
            history = cmds.listHistory(shape_full) or []
            clusters = cmds.ls(history, type="skinCluster") or []
        except Exception as exc:
            om.MGlobal.displayWarning(
                "[glb_io] export: skinCluster lookup failed on '%s': %s"
                % (shape_full, exc)
            )
            return None
        if not clusters:
            return None
        sc = clusters[0]

        # Resolve the cluster + its influences via the API so the weight layout
        # order (getWeights), the influence-name order, and the bindPreMatrix
        # logical index all stay consistent. influenceObjects() defines the
        # physical order getWeights uses; indexForInfluenceObject() gives the
        # logical index that bindPreMatrix[i] is keyed on.
        try:
            sel = om.MSelectionList()
            sel.add(sc)
            sc_obj = sel.getDependNode(0)
            fn_skin = oma.MFnSkinCluster(sc_obj)
            infl_dags = fn_skin.influenceObjects()
        except Exception as exc:
            om.MGlobal.displayWarning(
                "[glb_io] export: could not query influences of '%s': %s; "
                "skipping skin" % (sc, exc)
            )
            return None
        if not infl_dags:
            return None

        influences = [d.fullPathName() for d in infl_dags]
        n_infl = len(influences)

        # bindPreMatrix[<logical>] IS the inverse bind (world) matrix Maya stores,
        # in Maya row-major order (16 floats). Index it by the logical index of
        # each physical influence so it aligns with `influences`.
        bind_pre = []
        for d in infl_dags:
            try:
                logical = fn_skin.indexForInfluenceObject(d)
                m = cmds.getAttr("%s.bindPreMatrix[%d]" % (sc, logical))
                bind_pre.append(tuple(float(v) for v in m))
            except Exception:
                # Identity fallback (still a valid IBM — glTF treats it as no
                # pre-transform).
                bind_pre.append((1.0, 0.0, 0.0, 0.0,
                                 0.0, 1.0, 0.0, 0.0,
                                 0.0, 0.0, 1.0, 0.0,
                                 0.0, 0.0, 0.0, 1.0))

        # Bulk-read every weight via the OpenMayaAnim API. getWeights with a
        # null/empty component returns the full flat [vert0_infl0, vert0_infl1,
        # ... vertN_inflM] array plus the number of influences, in
        # influenceObjects() order.
        try:
            comp = om.MObject.kNullObj
            raw_weights, infl_count = fn_skin.getWeights(dag, comp)
        except Exception as exc:
            om.MGlobal.displayWarning(
                "[glb_io] export: MFnSkinCluster.getWeights failed on '%s': %s; "
                "skipping skin" % (sc, exc)
            )
            return None

        if infl_count <= 0:
            return None

        # raw_weights is laid out in influenceObjects() order, so column i maps
        # to influences[i] directly. JOINTS_0 values are exactly these column
        # indices (= position in the skin's joints list). Clamp defensively.
        num_verts = len(raw_weights) // infl_count
        per_vertex = []
        for v in range(num_verts):
            base = v * infl_count
            pairs = []
            for i in range(infl_count):
                if i >= n_infl:
                    break
                w = raw_weights[base + i]
                if w > 0.0:
                    pairs.append((i, w))
            # Top-4 by weight, then normalize.
            pairs.sort(key=lambda kv: -kv[1])
            pairs = pairs[:4]
            total = sum(w for _, w in pairs)
            if total > 0.0:
                pairs = [(i, w / total) for i, w in pairs]
            per_vertex.append(pairs)

        return {
            "influences": influences,
            "bind_pre_matrices": bind_pre,
            "per_vertex": per_vertex,
        }

    def _attach_skin_attrs(self, prim, orig_vids, skin_info):
        """Fill prim.joints / prim.weights (4 per vertex) from the precomputed
        per-original-vertex weights, aligned to the unwelded POSITION stream."""
        per_vertex = skin_info["per_vertex"]
        joints = []
        weights = []
        for vid in orig_vids:
            if 0 <= vid < len(per_vertex):
                pairs = per_vertex[vid]
            else:
                pairs = []
            j4 = [0, 0, 0, 0]
            w4 = [0.0, 0.0, 0.0, 0.0]
            for slot, (infl_idx, w) in enumerate(pairs[:4]):
                j4[slot] = infl_idx
                w4[slot] = w
            joints.extend(j4)
            weights.extend(w4)
        prim.joints = joints
        prim.weights = weights

    # -----------------------------------------------------------------------
    # Morph (blendShape) extraction
    # -----------------------------------------------------------------------

    def _extract_morph_targets(self, dag, shape_full):
        # type: (Any, str) -> Optional[dict]
        """Detect a blendShape on `shape_full` and read each target's per-
        (original)-vertex POSITION deltas from the stored target geometry.

        We read the blendShape's stored deltas directly (no scene mutation) via
        the inputTargetItem plugs:
          <bs>.inputTarget[0].inputTargetGroup[g].inputTargetItem[6000]
              .inputPointsTarget     (sparse delta vectors, object space)
              .inputComponentsTarget (the vertex ids those deltas apply to)
        Index 6000 is the full-weight (1.0) target item — the standard slot for
        a simple morph. Deltas are SPARSE (only changed verts), so we scatter
        them into a dense per-original-vertex array.

        Returns None when there's no blendShape, else:
          {
            "target_names" : [str, ...],            # one per target group
            "weights"      : [float, ...],          # current weight per group
            "deltas"       : [ {vid: (dx,dy,dz)}, ... ],  # per group, in metres
          }
        Deltas are divided by unit_scale so they match the exported (metre)
        positions.
        """
        try:
            history = cmds.listHistory(shape_full) or []
            blendshapes = cmds.ls(history, type="blendShape") or []
        except Exception as exc:
            om.MGlobal.displayWarning(
                "[glb_io] export: blendShape lookup failed on '%s': %s"
                % (shape_full, exc)
            )
            return None
        if not blendshapes:
            return None
        bs = blendshapes[0]

        try:
            weight_indices = cmds.getAttr(bs + ".weight", multiIndices=True) or []
        except Exception:
            weight_indices = []
        if not weight_indices:
            return None

        # Alias map: weight[i] -> friendly target name. aliasAttr (list form)
        # returns [alias, plug, alias, plug, ...].
        alias_of = {}
        try:
            raw = cmds.aliasAttr(bs, query=True) or []
            for k in range(0, len(raw) - 1, 2):
                alias, plug = raw[k], raw[k + 1]
                # plug looks like "weight[3]"
                if plug.startswith("weight["):
                    try:
                        gi = int(plug[len("weight["):-1])
                        alias_of[gi] = alias
                    except ValueError:
                        pass
        except Exception:
            pass

        target_names = []
        weights = []
        deltas = []
        any_delta = False
        for gi in weight_indices:
            target_names.append(alias_of.get(gi, "target%d" % gi))
            try:
                weights.append(float(cmds.getAttr("%s.weight[%d]" % (bs, gi))))
            except Exception:
                weights.append(0.0)

            delta_map = self._read_target_item_deltas(bs, gi)
            if delta_map:
                any_delta = True
            deltas.append(delta_map)

        if not any_delta:
            # blendShape exists but no readable stored deltas (e.g. live-target
            # mesh connections only). Skip morph export for this mesh.
            return None

        return {
            "target_names": target_names,
            "weights": weights,
            "deltas": deltas,
        }

    def _read_target_item_deltas(self, bs, group_index):
        """Read the sparse {vertex_id: (dx,dy,dz)} delta map for one blendShape
        target group at the full-weight item (index 6000). Deltas are converted
        to metres (divided by unit_scale)."""
        base = "%s.inputTarget[0].inputTargetGroup[%d].inputTargetItem[6000]" % (
            bs, group_index
        )
        try:
            pts = cmds.getAttr(base + ".inputPointsTarget") or []
        except Exception:
            return {}
        try:
            comps = cmds.getAttr(base + ".inputComponentsTarget") or []
        except Exception:
            comps = []
        if not pts:
            return {}

        # Expand component specs ("vtx[3]", "vtx[5:8]") into a flat id list, in
        # the same order as the point deltas.
        vids = []
        for c in comps:
            if not isinstance(c, str) or "[" not in c:
                continue
            inner = c[c.index("[") + 1:c.rindex("]")]
            if ":" in inner:
                try:
                    lo, hi = inner.split(":", 1)
                    vids.extend(range(int(lo), int(hi) + 1))
                except ValueError:
                    pass
            else:
                try:
                    vids.append(int(inner))
                except ValueError:
                    pass

        out = {}
        us = self.unit_scale or 1.0
        for i, p in enumerate(pts):
            if i >= len(vids):
                break
            # inputPointsTarget yields (x, y, z, w) tuples; w is unused.
            dx, dy, dz = float(p[0]), float(p[1]), float(p[2])
            out[vids[i]] = (dx / us, dy / us, dz / us)
        return out

    def _attach_morph_targets(self, prim, orig_vids, morph_info):
        """Build prim.morph_targets: one {"POSITION": [dx,dy,dz, ...]} per target
        group, with deltas scattered onto the unwelded POSITION stream via the
        original vertex ids."""
        groups = morph_info["deltas"]
        targets = []
        for delta_map in groups:
            flat = []
            for vid in orig_vids:
                d = delta_map.get(vid)
                if d is None:
                    flat.extend((0.0, 0.0, 0.0))
                else:
                    flat.extend(d)
            targets.append({"POSITION": flat})
        prim.morph_targets = targets

    def finalize_skins(self):
        """Resolve queued skin jobs into GX.SkinDef objects.

        Must run AFTER every root subtree has been added (so joint transforms
        exist as glTF nodes) but the joints might NOT be under the exported
        roots. For any influence not yet exported as a node, we export its
        skeleton-root subtree so the joint gets a node entry, then resolve the
        influence -> glTF node index. Finally sets each mesh node's skin_index.
        """
        for job in self._skin_jobs:
            influences = job["influences"]

            # Ensure every influence has a glTF node. If missing, export the
            # top-most ancestor of the joint (its skeleton root) as a new root
            # subtree so the whole chain — including this joint — gets nodes.
            for joint in influences:
                if joint not in self.node_index_of:
                    root = _top_ancestor(joint)
                    self.add_subtree(root, parent_node_index=None)

            joint_node_indices = []
            ok = True
            for joint in influences:
                idx = self.node_index_of.get(joint)
                if idx is None:
                    om.MGlobal.displayWarning(
                        "[glb_io] export: joint '%s' has no exported node; "
                        "skipping this skin" % joint
                    )
                    ok = False
                    break
                joint_node_indices.append(idx)
            if not ok:
                continue

            # IBMs: transpose Maya row-major -> glTF column-major and convert
            # the translation row to metres (positions were exported in metres).
            ibms = [
                _maya_rowmajor_to_gltf_ibm(m, self.unit_scale)
                for m in job["bind_pre_matrices"]
            ]

            # `skeleton` (optional) should be a common root of the joints. Use
            # the influence that is an ancestor (DAG-path prefix) of every other
            # influence; if there isn't one, omit it (the importer derives the
            # hierarchy from node `children`, so this is purely advisory).
            skel = self._common_skeleton_root(influences)
            skin_def = GX.SkinDef(
                joint_node_indices=joint_node_indices,
                inverse_bind_matrices=ibms,
                skeleton_node_index=skel,
                name=job["name"],
            )
            skin_index = len(self.skins)
            self.skins.append(skin_def)
            self.nodes[job["mesh_node_index"]].skin_index = skin_index

    def _common_skeleton_root(self, influences):
        """Return the glTF node index of the influence that is a DAG ancestor of
        every other influence, or None if no single common root exists."""
        for cand in influences:
            is_ancestor_of_all = True
            for other in influences:
                if other == cand:
                    continue
                if not other.startswith(cand + "|"):
                    is_ancestor_of_all = False
                    break
            if is_ancestor_of_all:
                return self.node_index_of.get(cand)
        return None

    # -----------------------------------------------------------------------
    # Animation sampling
    # -----------------------------------------------------------------------

    def build_animation(self):
        # type: () -> Optional[GX.AnimationDef]
        """Sample TRS (and morph-weight) animation for every exported node that
        has animCurves on its translate/rotate/scale channels, or whose mesh
        carries an animated blendShape.

        Channels are sampled at integer frames over the scene's active playback
        range. Only animated properties get channels (keeps files small). Times
        are converted to seconds via the scene fps. Translation/scale -> VEC3
        (translation divided by unit_scale -> metres); rotation -> VEC4
        quaternion (x,y,z,w); morph weights -> a flat keyframe-major SCALAR
        stream (N weights per keyframe), read after setting current time.

        Mutates current time while sampling; the original time is restored and
        viewport refresh suspended/resumed in a finally block.
        """
        if not self.node_index_of:
            return None

        # Which nodes are animated, and on which TRS groups?
        animated = []   # list of (transform_full_path, node_index, want_t, want_r, want_s)
        for full, node_index in self.node_index_of.items():
            want_t = _has_anim(full, ("translateX", "translateY", "translateZ"))
            want_r = _has_anim(full, ("rotateX", "rotateY", "rotateZ"))
            want_s = _has_anim(full, ("scaleX", "scaleY", "scaleZ"))
            if want_t or want_r or want_s:
                animated.append((full, node_index, want_t, want_r, want_s))

        # Which exported MESH nodes have an animated blendShape? Map node_index
        # -> (blendShape, [weight group indices]). Only when morph export is on.
        morphed = {}  # node_index -> (bs_name, [group_indices])
        if self.export_morph:
            for full, node_index in self.node_index_of.items():
                info = self._animated_blendshape_for_node(full)
                if info:
                    morphed[node_index] = info

        if not animated and not morphed:
            return None

        fmin, fmax = _playback_range()
        if fmax < fmin:
            return None
        frames = list(range(int(round(fmin)), int(round(fmax)) + 1))
        if not frames:
            return None
        fps = _scene_fps()
        times = [f / fps for f in frames]

        anim_name = _scene_name() or "take001"
        anim = GX.AnimationDef(name=anim_name, channels=[])

        original_time = cmds.currentTime(query=True)
        try:
            try:
                cmds.refresh(suspend=True)
            except Exception:
                pass

            # Per-node accumulators across frames.
            samples = {}  # node_index -> {"t":[...], "r":[...], "s":[...]}
            for _full, node_index, want_t, want_r, want_s in animated:
                samples[node_index] = {"t": [], "r": [], "s": []}
            # Morph weight accumulator: node_index -> flat keyframe-major list.
            morph_samples = {ni: [] for ni in morphed}

            for f in frames:
                cmds.currentTime(f, edit=True)
                for full, node_index, want_t, want_r, want_s in animated:
                    rec = samples[node_index]
                    if want_t:
                        t = cmds.getAttr(full + ".translate")[0]
                        rec["t"].append((t[0] / self.unit_scale,
                                         t[1] / self.unit_scale,
                                         t[2] / self.unit_scale))
                    if want_r:
                        q = _node_local_quat(full)
                        rec["r"].append(q)
                    if want_s:
                        s = cmds.getAttr(full + ".scale")[0]
                        rec["s"].append((s[0], s[1], s[2]))
                # Morph weights — append this keyframe's N weights in order.
                for node_index, (bs, groups) in morphed.items():
                    row = morph_samples[node_index]
                    for gi in groups:
                        try:
                            row.append(float(
                                cmds.getAttr("%s.weight[%d]" % (bs, gi))
                            ))
                        except Exception:
                            row.append(0.0)
        finally:
            try:
                cmds.currentTime(original_time, edit=True)
            except Exception:
                pass
            try:
                cmds.refresh(suspend=False)
            except Exception:
                pass

        # Build channels from the samples.
        for _full, node_index, want_t, want_r, want_s in animated:
            rec = samples[node_index]
            if want_t and rec["t"]:
                anim.channels.append(GX.AnimChannelDef(
                    node_index, "translation", list(times), rec["t"]
                ))
            if want_r and rec["r"]:
                anim.channels.append(GX.AnimChannelDef(
                    node_index, "rotation", list(times), rec["r"]
                ))
            if want_s and rec["s"]:
                anim.channels.append(GX.AnimChannelDef(
                    node_index, "scale", list(times), rec["s"]
                ))

        # Morph-weight channels: a flat keyframe-major SCALAR stream per node.
        for node_index, flat in morph_samples.items():
            if flat:
                anim.channels.append(GX.AnimChannelDef(
                    node_index, "weights", list(times), flat
                ))

        if not anim.channels:
            return None
        return anim

    def _animated_blendshape_for_node(self, transform_full):
        """If `transform_full` owns a mesh with an animated blendShape, return
        (blendShape_name, [weight group indices]); else None.

        A blendShape counts as animated if ANY of its weight[i] plugs is driven
        by an animCurve. The group-index list is the weight multiIndices order
        (= glTF morph-target order, matching how the mesh's targets are
        exported)."""
        shape = _first_mesh_shape(transform_full)
        if shape is None:
            return None
        try:
            history = cmds.listHistory(shape) or []
            blendshapes = cmds.ls(history, type="blendShape") or []
        except Exception:
            return None
        if not blendshapes:
            return None
        bs = blendshapes[0]
        try:
            groups = cmds.getAttr(bs + ".weight", multiIndices=True) or []
        except Exception:
            groups = []
        if not groups:
            return None
        # Animated if any weight[i] has an incoming animCurve.
        any_anim = False
        for gi in groups:
            plug = "%s.weight[%d]" % (bs, gi)
            try:
                conns = cmds.listConnections(
                    plug, source=True, destination=False, type="animCurve"
                ) or []
            except Exception:
                conns = []
            if conns:
                any_anim = True
                break
        if not any_anim:
            return None
        return (bs, list(groups))


# ---------------------------------------------------------------------------
# Transform helpers
# ---------------------------------------------------------------------------

def _local_rotation_quat(transform):
    """Return the transform's LOCAL rotation as a glTF quaternion (x,y,z,w),
    honoring its rotateOrder."""
    try:
        sel = om.MSelectionList()
        sel.add(transform)
        dag = sel.getDagPath(0)
        fn = om.MFnTransform(dag)
        q = fn.rotation(om.MSpace.kTransform, asQuaternion=True)
        return (q.x, q.y, q.z, q.w)
    except Exception:
        return (0.0, 0.0, 0.0, 1.0)


def _filter_to_roots(transforms):
    """Drop any transform that is a descendant of another in the list, so we
    don't export a subtree twice."""
    full = []
    for t in transforms:
        # Normalise to a full path.
        try:
            f = cmds.ls(t, long=True)
            if f:
                full.append(f[0])
        except Exception:
            full.append(t)
    full = list(dict.fromkeys(full))

    roots = []
    for t in full:
        is_descendant = False
        for other in full:
            if other != t and t.startswith(other + "|"):
                is_descendant = True
                break
        if not is_descendant:
            roots.append(t)
    return roots


def _default_camera_transforms():
    cams = cmds.ls(cameras=True, long=True) or []
    out = []
    for c in cams:
        try:
            if cmds.camera(c, query=True, startupCamera=True):
                p = cmds.listRelatives(c, parent=True, fullPath=True) or []
                out.extend(p)
        except Exception:
            pass
    return out


def _short_name(full_path):
    name = full_path.split("|")[-1]
    # Strip namespace for a cleaner glTF name.
    if ":" in name:
        name = name.split(":")[-1]
    return name


def _full_path(node):
    """Normalise a Maya node name to its unambiguous full DAG path so it can be
    used as a stable key in the node-index map."""
    try:
        resolved = cmds.ls(node, long=True)
        if resolved:
            return resolved[0]
    except Exception:
        pass
    return node


def _top_ancestor(node):
    """Return the world-level (assembly) ancestor of `node`, or `node` itself if
    it is already top-level. Used so a skin's joint subtree gets fully exported
    even when the joints live outside the user's selected roots."""
    full = _full_path(node)
    # A full DAG path is "|root|a|b|c"; the top assembly is "|root".
    parts = [p for p in full.split("|") if p]
    if not parts:
        return full
    return "|" + parts[0]


def _maya_rowmajor_to_gltf_ibm(maya_flat16, unit_scale):
    """Convert a Maya bindPreMatrix (16 floats, row-major) into a glTF
    inverseBindMatrix (16 floats, column-major), with the translation row scaled
    from Maya units to metres (divided by unit_scale) so the IBM operates in the
    same metre space as the exported vertices.

    Maya MMatrix is row-major: translation at flat indices 12,13,14. Flattening a
    row-major 4x4 and a column-major 4x4 are transposes of each other, so the
    transpose below yields glTF's column-major layout. After transposing, the
    translation sits in the last COLUMN -> flat indices 12,13,14.
    """
    m = list(maya_flat16)
    # Transpose 4x4: out[col*4 + row] = in[row*4 + col].
    t = [0.0] * 16
    for row in range(4):
        for col in range(4):
            t[col * 4 + row] = float(m[row * 4 + col])
    # Translation column (indices 12,13,14) -> metres.
    if unit_scale and unit_scale != 1.0:
        t[12] /= unit_scale
        t[13] /= unit_scale
        t[14] /= unit_scale
    return tuple(t)


# ---------------------------------------------------------------------------
# Animation helpers
# ---------------------------------------------------------------------------

def _has_anim(node, attrs):
    """True if any of `attrs` on `node` is driven by an animCurve."""
    for attr in attrs:
        plug = node + "." + attr
        try:
            conns = cmds.listConnections(plug, source=True, destination=False,
                                         type="animCurve") or []
        except Exception:
            conns = []
        if conns:
            return True
    return False


def _playback_range():
    """Return (min_frame, max_frame) of the active playback range."""
    try:
        fmin = cmds.playbackOptions(query=True, min=True)
        fmax = cmds.playbackOptions(query=True, max=True)
        return float(fmin), float(fmax)
    except Exception:
        return 0.0, 0.0


_TIME_UNIT_FPS = {
    "game": 15.0, "film": 24.0, "pal": 25.0, "ntsc": 30.0,
    "show": 48.0, "palf": 50.0, "ntscf": 60.0,
    # numeric-style names Maya also accepts
    "23.976fps": 23.976, "24fps": 24.0, "25fps": 25.0, "30fps": 30.0,
    "48fps": 48.0, "50fps": 50.0, "60fps": 60.0,
}


def _scene_fps():
    """Derive frames-per-second from Maya's current time unit (fallback 24)."""
    try:
        unit = cmds.currentUnit(query=True, time=True) or "film"
    except Exception:
        return 24.0
    if unit in _TIME_UNIT_FPS:
        return _TIME_UNIT_FPS[unit]
    # Names like "100fps" -> parse the leading number.
    if unit.endswith("fps"):
        try:
            return float(unit[:-3])
        except ValueError:
            pass
    return 24.0


def _scene_name():
    """Short scene file name (no extension), or '' for an unsaved scene."""
    try:
        full = cmds.file(query=True, sceneName=True) or ""
    except Exception:
        return ""
    if not full:
        return ""
    base = os.path.basename(full)
    return os.path.splitext(base)[0]


def _node_local_quat(node):
    """Read a node's LOCAL rotation as a glTF quaternion (x,y,z,w), honoring its
    rotateOrder. Local space matches how the importer applies rotation channels
    (per-node TRS), so the round-trip stays consistent."""
    try:
        sel = om.MSelectionList()
        sel.add(node)
        dag = sel.getDagPath(0)
        fn = om.MFnTransform(dag)
        q = fn.rotation(om.MSpace.kTransform, asQuaternion=True)
        return (q.x, q.y, q.z, q.w)
    except Exception:
        return (0.0, 0.0, 0.0, 1.0)


def _first_mesh_shape(transform):
    shapes = cmds.listRelatives(transform, shapes=True, fullPath=True,
                                type="mesh", noIntermediate=True) or []
    return shapes[0] if shapes else None


# ---------------------------------------------------------------------------
# ShadingGroup / shader resolution (adapted from _glb_writer.py)
# ---------------------------------------------------------------------------

def _get_face_to_sg_map(mesh_transform, shape_full_path, num_polygons):
    if num_polygons <= 0:
        return {}

    sgs = cmds.listConnections(shape_full_path, type="shadingEngine") or []
    seen = set()
    unique_sgs = []
    for sg in sgs:
        if sg not in seen:
            seen.add(sg)
            unique_sgs.append(sg)
    if not unique_sgs:
        return {}

    face_to_sg = {}
    short_shape = shape_full_path.split("|")[-1]

    for sg in unique_sgs:
        members = cmds.sets(sg, query=True) or []
        for member in members:
            base = member.split(".")[0]
            base_short = base.split("|")[-1]
            if base_short != short_shape and base != shape_full_path:
                continue

            if "." not in member:
                for fi in range(num_polygons):
                    face_to_sg.setdefault(fi, sg)
            else:
                comp = member.split(".", 1)[1]
                if not comp.startswith("f["):
                    continue
                inner = comp[2:-1] if comp.endswith("]") else comp[2:]
                if ":" in inner:
                    try:
                        lo, hi = inner.split(":", 1)
                        lo_i, hi_i = int(lo), int(hi)
                    except ValueError:
                        continue
                    for fi in range(lo_i, hi_i + 1):
                        if 0 <= fi < num_polygons:
                            face_to_sg[fi] = sg
                else:
                    try:
                        fi = int(inner)
                    except ValueError:
                        continue
                    if 0 <= fi < num_polygons:
                        face_to_sg[fi] = sg

    if not face_to_sg:
        return {}

    orphans = [fi for fi in range(num_polygons) if fi not in face_to_sg]
    if orphans:
        fallback = unique_sgs[0]
        for fi in orphans:
            face_to_sg[fi] = fallback

    result = {}
    for fi, sg in face_to_sg.items():
        result.setdefault(sg, []).append(fi)
    for sg in result:
        result[sg].sort()
    return result


def _shader_for_sg(sg_name):
    if not sg_name:
        return ""
    try:
        srcs = cmds.listConnections(sg_name + ".surfaceShader",
                                    source=True, destination=False) or []
        if srcs:
            return srcs[0]
    except RuntimeError:
        pass
    return ""


# ---------------------------------------------------------------------------
# Mesh math helpers (adapted from _glb_writer.py)
# ---------------------------------------------------------------------------

def _face_vertex_color(mfn, face_idx, vertex_idx, color_set_name):
    if not color_set_name:
        return [1.0, 1.0, 1.0, 1.0]
    try:
        c = mfn.getFaceVertexColor(face_idx, vertex_idx, color_set_name)
        return [c.r, c.g, c.b, c.a]
    except (RuntimeError, IndexError):
        return [1.0, 1.0, 1.0, 1.0]


def _find_local_face_vertex(poly_iter, global_vertex_idx):
    verts = poly_iter.getVertices()
    for local_i, gv in enumerate(verts):
        if gv == global_vertex_idx:
            return local_i
    return 0


def _tri_uvs(poly_iter, tri_globals, u_arr, v_arr):
    out = []
    for g in tri_globals:
        try:
            local_fv = _find_local_face_vertex(poly_iter, g)
            uv_idx = poly_iter.getUVIndex(local_fv)
            out.append((float(u_arr[uv_idx]), 1.0 - float(v_arr[uv_idx])))
        except (RuntimeError, IndexError):
            out.append((0.0, 0.0))
    return out


def _compute_tri_tangent(pos_array, tri_globals, tri_uvs):
    if not tri_uvs:
        return (1.0, 0.0, 0.0)
    p1 = pos_array[tri_globals[0]]
    p2 = pos_array[tri_globals[1]]
    p3 = pos_array[tri_globals[2]]
    e1 = (p2.x - p1.x, p2.y - p1.y, p2.z - p1.z)
    e2 = (p3.x - p1.x, p3.y - p1.y, p3.z - p1.z)
    duv1 = (tri_uvs[1][0] - tri_uvs[0][0], tri_uvs[1][1] - tri_uvs[0][1])
    duv2 = (tri_uvs[2][0] - tri_uvs[0][0], tri_uvs[2][1] - tri_uvs[0][1])
    denom = duv1[0] * duv2[1] - duv2[0] * duv1[1]
    if abs(denom) < 1e-8:
        return (1.0, 0.0, 0.0)
    f = 1.0 / denom
    tx = f * (duv2[1] * e1[0] - duv1[1] * e2[0])
    ty = f * (duv2[1] * e1[1] - duv1[1] * e2[1])
    tz = f * (duv2[1] * e1[2] - duv1[1] * e2[2])
    length = (tx * tx + ty * ty + tz * tz) ** 0.5
    if length < 1e-8:
        return (1.0, 0.0, 0.0)
    return (tx / length, ty / length, tz / length)


# ---------------------------------------------------------------------------
# Unit scale + options
# ---------------------------------------------------------------------------

def _auto_unit_scale():
    """Divisor to convert Maya scene-unit positions -> metres (glTF standard).

    Mirrors maya_anno_addon/_coord.auto_unit_scale + _build._auto_unit_scale:
    a cm scene returns 100.0 (divide positions by 100 to get metres).
    """
    linear = cmds.currentUnit(query=True, linear=True) or "cm"
    unit_to_cm = {
        "mm": 0.1, "cm": 1.0, "m": 100.0,
        "in": 2.54, "ft": 30.48, "yd": 91.44,
    }
    cm_per_unit = unit_to_cm.get(linear, 1.0)
    # metres = cm / 100. To convert N scene-units (= N*cm_per_unit cm) to metres
    # we divide by (100 / cm_per_unit).
    return 100.0 / cm_per_unit


def _resolve_unit_scale(options):
    """Return the POSITION divisor. 1.0 when unit conversion is off."""
    if not options.get("convertUnits", True):
        return 1.0
    return _auto_unit_scale()


def _normalize_options(options):
    from maya_glb_io import _prefs
    base = _prefs.get_all_export()
    if options:
        for k, v in options.items():
            base[k] = v
    return base


# ---------------------------------------------------------------------------
# Reference-mode texture finalisation
# ---------------------------------------------------------------------------

def _finalize_reference_textures(image_cache, out_path, copy_tex):
    """In REFERENCE mode, set each ImageRef.name to the uri the glTF should use.

    If copy_tex is True, copy each source texture into the output directory and
    use its basename as a relative uri. Otherwise, compute a relative path from
    the output directory to the original texture (so the uri still resolves).
    """
    out_dir = os.path.dirname(os.path.abspath(out_path))
    for img in image_cache.values():
        src = img.file_path
        if not src:
            continue
        if copy_tex:
            base = os.path.basename(src)
            dst = os.path.join(out_dir, base)
            try:
                if os.path.abspath(src) != os.path.abspath(dst) and os.path.isfile(src):
                    shutil.copy2(src, dst)
            except Exception as exc:
                om.MGlobal.displayWarning(
                    "[glb_io] export: could not copy texture '%s': %s" % (src, exc)
                )
            img.name = base
        else:
            try:
                rel = os.path.relpath(src, out_dir)
                img.name = rel.replace("\\", "/")
            except Exception:
                img.name = os.path.basename(src)


# Small shim so _SceneBuilder can sanitise material names without importing the
# importer's _materials package (which pulls in Maya material builders).
def MX_sanitize(name):
    out = []
    for ch in (name or "material"):
        out.append(ch if (ch.isalnum() or ch == "_") else "_")
    s = "".join(out)
    if not s or s[0].isdigit():
        s = "_" + s
    return s


# Suffixes the importer appends to Maya nodes when it builds a material:
#   <material>_SHD  (the surface shader)   <material>_SG (the shading group)
# We strip these when naming the glTF material so a re-import doesn't keep
# stacking them (stonePyramid_SHD -> stonePyramid_SHD_SHD -> ...). The glTF
# material name should be the clean base, which the importer re-decorates.
_IMPORTER_SHADER_SUFFIXES = ("_SHD", "_SHG", "_SG")


def _clean_material_name(shader):
    name = MX_sanitize(shader)
    for suffix in _IMPORTER_SHADER_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    return name
