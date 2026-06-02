"""Pure-ish glTF 2.0 / GLB *writer*. The inverse of _gltf.py's reader.

This module is the WRITE counterpart to _gltf.py. It builds a complete glTF
2.0 document (asset / scenes / nodes / meshes / primitives / accessors /
bufferViews / one buffer / materials / images / textures / samplers) from
plain Python primitive arrays, then serialises it either as:

  * .glb  -> 12-byte header + JSON chunk + BIN chunk, every chunk padded to a
             4-byte boundary (spec §4.4). JSON chunk padded with 0x20, BIN
             chunk padded with 0x00.
  * .gltf -> a JSON file + an external .bin file referenced by buffers[0].uri.

Design goal: the geometry/material *assembly* layer is Maya-free so the smoke
test (`_dev/smoke_export.py`) can hand-build a primitive + material, write a
.glb, and round-trip it through this plug-in's own `_gltf.loads()` with no Maya
in the loop.

The Maya-facing orchestration (walking selected transforms, extracting mesh
attributes, resolving shaders) lives in `exporter.py` and `_materials_export.py`
and feeds this module the plain arrays it needs.

Conventions mirrored from _gltf.py / _glb_writer.py:
  componentType 5126 = FLOAT, 5125 = UNSIGNED_INT, 5123 = UNSIGNED_SHORT.
  accessor types SCALAR / VEC2 / VEC3 / VEC4.
  POSITION accessors carry min/max (spec requirement for POSITION).
  TANGENT is VEC4 (xyz = direction, w = handedness sign).
  TEXCOORD_0 is stored with V already flipped to glTF's top-left origin by the
  caller (matching how the importer does `v = 1 - V`).
  COLOR_0 is emitted as float VEC4 (keeps it simple — no normalized-ubyte path).
"""
from __future__ import annotations

import base64
import json
import os
import struct
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# GLB binary container constants (spec §4.4) — identical to _gltf.py
# ---------------------------------------------------------------------------

_GLB_MAGIC = 0x46546C67       # b'glTF'
_GLB_VERSION = 2
_CHUNK_JSON = 0x4E4F534A      # b'JSON'
_CHUNK_BIN = 0x004E4942       # b'BIN\0'

# Accessor componentType enums
COMP_FLOAT = 5126
COMP_UINT = 5125
COMP_USHORT = 5123
COMP_UBYTE = 5121

# bufferView.target enums
TARGET_ARRAY_BUFFER = 34962          # vertex attributes
TARGET_ELEMENT_ARRAY_BUFFER = 34963  # indices

# primitive.mode
MODE_TRIANGLES = 4

GENERATOR = "maya_glb_io exporter"


class ExportError(Exception):
    pass


# ---------------------------------------------------------------------------
# Primitive container (Maya-free input to the doc builder)
# ---------------------------------------------------------------------------

class Primitive(object):
    """A single drawable primitive: flat attribute arrays + an index list.

    Flat arrays mean POSITION is [x0,y0,z0, x1,y1,z1, ...] etc. Optional
    attributes may be empty lists / None to omit them. `material_index` is the
    index into the export's materials list, or None for no material.

    All vertex attributes must describe the SAME vertex count (i.e. unwelded
    per-face-vertex geometry, like _glb_writer.py produces). `indices` then
    references those vertices (often just 0..N-1 for fully unwelded meshes).
    """

    __slots__ = (
        "positions", "normals", "tangents", "texcoords", "colors",
        "indices", "material_index", "joints", "weights", "morph_targets",
    )

    def __init__(self, positions, indices, normals=None, tangents=None,
                 texcoords=None, colors=None, material_index=None,
                 joints=None, weights=None, morph_targets=None):
        self.positions = positions or []
        self.indices = indices or []
        self.normals = normals or []
        self.tangents = tangents or []
        self.texcoords = texcoords or []
        self.colors = colors or []
        self.material_index = material_index
        # Skinning attributes (optional). `joints` is a flat list of 4 ints per
        # vertex (influence indices into the skin's joints list), `weights` a
        # flat list of 4 floats per vertex. Both must be parallel to POSITION.
        self.joints = joints or []
        self.weights = weights or []
        # Morph targets (optional). A list of dicts, each holding flat per-vertex
        # DELTA arrays parallel to POSITION, e.g.
        #   {"POSITION": [dx,dy,dz, ...], "NORMAL": [...]}
        # One entry per blendShape target, in target/weight order.
        self.morph_targets = morph_targets or []

    def vertex_count(self):
        return len(self.positions) // 3


# ---------------------------------------------------------------------------
# Material / texture / image descriptors (Maya-free)
# ---------------------------------------------------------------------------

class ImageRef(object):
    """A glTF image source. Either an external file path (REFERENCE mode) or
    raw bytes to embed (EMBED mode). The doc builder decides how to encode it
    based on the target format + embed flag.

    file_path : absolute path to the source texture on disk (may be None if
                only `data` is supplied).
    data      : raw image bytes (read lazily by the builder if None and a
                file_path is set and embedding is requested).
    mime      : "image/png" / "image/jpeg" / etc. Inferred from extension if
                None.
    name      : optional friendly name stored in the glTF image entry.
    """

    __slots__ = ("file_path", "data", "mime", "name")

    def __init__(self, file_path=None, data=None, mime=None, name=None):
        self.file_path = file_path
        self.data = data
        self.mime = mime
        self.name = name


class TextureRef(object):
    """A reference from a material slot to an image, used inside MaterialDef.

    image : an ImageRef (the builder dedupes identical ImageRefs by identity
            *and* by file_path so callers can pass fresh objects safely).
    """

    __slots__ = ("image",)

    def __init__(self, image):
        self.image = image


class MaterialDef(object):
    """A renderer-neutral PBR material description that maps 1:1 onto glTF's
    metallic-roughness model. `exporter.py` / `_materials_export.py` fill this
    in from whatever Maya shader they found.

    All texture slots are optional TextureRef instances; all factor fields fall
    back to glTF spec defaults when left at their defaults here.
    """

    __slots__ = (
        "name",
        "base_color_factor", "base_color_texture",
        "metallic_factor", "roughness_factor", "metallic_roughness_texture",
        "normal_texture", "normal_scale",
        "occlusion_texture", "occlusion_strength",
        "emissive_factor", "emissive_texture",
        "alpha_mode", "alpha_cutoff", "double_sided",
    )

    def __init__(self, name="material"):
        self.name = name
        self.base_color_factor = [1.0, 1.0, 1.0, 1.0]
        self.base_color_texture = None        # TextureRef or None
        self.metallic_factor = 1.0
        self.roughness_factor = 1.0
        self.metallic_roughness_texture = None
        self.normal_texture = None
        self.normal_scale = 1.0
        self.occlusion_texture = None
        self.occlusion_strength = 1.0
        self.emissive_factor = [0.0, 0.0, 0.0]
        self.emissive_texture = None
        self.alpha_mode = "OPAQUE"            # OPAQUE / MASK / BLEND
        self.alpha_cutoff = 0.5
        self.double_sided = False


# ---------------------------------------------------------------------------
# Node container (Maya-free)
# ---------------------------------------------------------------------------

class Node(object):
    """A glTF node: optional name, optional local transform, optional mesh, and
    child node indices.

    Provide EITHER a 16-float column-major `matrix` OR a TRS triple
    (translation xyz, rotation quaternion xyzw, scale xyz). If both are None the
    node is identity. `mesh_index` references the export's meshes list.
    """

    __slots__ = ("name", "matrix", "translation", "rotation", "scale",
                 "mesh_index", "children", "skin_index")

    def __init__(self, name=None, matrix=None, translation=None, rotation=None,
                 scale=None, mesh_index=None, children=None, skin_index=None):
        self.name = name
        self.matrix = matrix              # list[16] column-major, or None
        self.translation = translation    # (x,y,z) or None
        self.rotation = rotation          # (x,y,z,w) quaternion or None
        self.scale = scale                # (x,y,z) or None
        self.mesh_index = mesh_index
        self.children = children or []    # list of node indices
        self.skin_index = skin_index      # index into doc["skins"], or None


class MeshDef(object):
    """A glTF mesh = a named bag of primitives.

    Optional morph-target metadata mirrors glTF's mesh-level fields:
      weights      : default per-target weights -> mesh["weights"].
      target_names : per-target names -> mesh["extras"]["targetNames"].
    Both default to empty and are only emitted when a primitive actually carries
    morph targets.
    """

    __slots__ = ("name", "primitives", "weights", "target_names")

    def __init__(self, name=None, primitives=None, weights=None,
                 target_names=None):
        self.name = name
        self.primitives = primitives or []   # list of Primitive
        self.weights = weights or []         # list[float]
        self.target_names = target_names or []  # list[str]


# ---------------------------------------------------------------------------
# Skin + animation descriptors (Maya-free)
# ---------------------------------------------------------------------------

class SkinDef(object):
    """A glTF skin description.

    joint_node_indices    : list[int] — node indices (into the export's node
                            list) that act as joints. Their order IS the
                            influence order referenced by Primitive.joints.
    inverse_bind_matrices : list of 16-float tuples, ALREADY in glTF
                            column-major layout (one MAT4 per joint). Emitted
                            into a single FLOAT MAT4 accessor. May be empty
                            (glTF then treats every IBM as identity).
    skeleton_node_index   : optional int — the common-root node of the joints.
    name                  : optional friendly name.
    """

    __slots__ = ("name", "joint_node_indices", "inverse_bind_matrices",
                 "skeleton_node_index")

    def __init__(self, joint_node_indices, inverse_bind_matrices=None,
                 skeleton_node_index=None, name=None):
        self.name = name
        self.joint_node_indices = joint_node_indices or []
        self.inverse_bind_matrices = inverse_bind_matrices or []
        self.skeleton_node_index = skeleton_node_index


class AnimChannelDef(object):
    """One animation channel: a sampled curve driving a single node property.

    node_index    : int — the target node (index into the export's node list).
    path          : "translation" | "rotation" | "scale" | "weights".
    times         : list[float] — keyframe times in SECONDS (sampler input).
    values        : list of tuples — VEC3 (x,y,z) for translation/scale, or
                    VEC4 quaternion (x,y,z,w) for rotation. For "weights" this
                    is instead a flat list[float] laid out keyframe-major:
                    [k0_t0, k0_t1, ..., k1_t0, ...] (N weights per keyframe).
    interpolation : "LINEAR" (default) | "STEP" | "CUBICSPLINE".
    """

    __slots__ = ("node_index", "path", "times", "values", "interpolation")

    def __init__(self, node_index, path, times, values, interpolation="LINEAR"):
        self.node_index = node_index
        self.path = path
        self.times = times or []
        self.values = values or []
        self.interpolation = interpolation or "LINEAR"


class AnimationDef(object):
    """A glTF animation = a named bag of AnimChannelDef channels."""

    __slots__ = ("name", "channels")

    def __init__(self, name=None, channels=None):
        self.name = name
        self.channels = channels or []   # list of AnimChannelDef


# ---------------------------------------------------------------------------
# Buffer packer — accumulates bufferViews + accessors into one BIN blob
# ---------------------------------------------------------------------------

class _BufferPacker(object):
    """Builds the single glTF buffer, padding each appended view to a 4-byte
    boundary (spec requires accessor alignment to its component size; 4-byte
    alignment satisfies float/uint/ushort/ubyte all at once)."""

    def __init__(self):
        self._chunks = []          # list[bytes]
        self._len = 0
        self.buffer_views = []     # list[dict]
        self.accessors = []        # list[dict]

    def _append_bytes(self, raw):
        pad = (4 - (self._len % 4)) % 4
        if pad:
            self._chunks.append(b"\x00" * pad)
            self._len += pad
        offset = self._len
        self._chunks.append(raw)
        self._len += len(raw)
        return offset

    def add_float_accessor(self, flat_values, elem_type, target,
                           include_minmax=False):
        """Pack a list of floats as a FLOAT accessor of `elem_type`
        (VEC2/VEC3/VEC4/SCALAR). Returns the new accessor index."""
        elem_count = _ELEMENT_COUNTS[elem_type]
        count = len(flat_values) // elem_count
        raw = struct.pack("<" + "f" * len(flat_values), *flat_values)
        offset = self._append_bytes(raw)

        bv_index = len(self.buffer_views)
        bv = {
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(raw),
        }
        # `target` is omitted for accessors that aren't vertex attributes or
        # indices (e.g. IBM MAT4 and animation input/output accessors). A
        # bufferView with target=null would be invalid, so only set it when
        # a real target enum is supplied.
        if target is not None:
            bv["target"] = target
        self.buffer_views.append(bv)

        accessor = {
            "bufferView": bv_index,
            "componentType": COMP_FLOAT,
            "count": count,
            "type": elem_type,
        }
        if include_minmax:
            accessor["min"], accessor["max"] = _minmax_per_component(
                flat_values, elem_count
            )
        acc_index = len(self.accessors)
        self.accessors.append(accessor)
        return acc_index

    def add_index_accessor(self, indices):
        """Pack a list of ints as a SCALAR index accessor. Picks the narrowest
        legal componentType (ushort if max < 65536, else uint). Returns the
        accessor index."""
        if indices:
            max_idx = max(indices)
        else:
            max_idx = 0
        if max_idx < 65536:
            comp = COMP_USHORT
            fmt = "H"
        else:
            comp = COMP_UINT
            fmt = "I"
        raw = struct.pack("<" + fmt * len(indices), *indices)
        offset = self._append_bytes(raw)

        bv_index = len(self.buffer_views)
        self.buffer_views.append({
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(raw),
            "target": TARGET_ELEMENT_ARRAY_BUFFER,
        })

        acc_index = len(self.accessors)
        self.accessors.append({
            "bufferView": bv_index,
            "componentType": comp,
            "count": len(indices),
            "type": "SCALAR",
        })
        return acc_index

    def add_ushort_vec4_accessor(self, flat_values, target=TARGET_ARRAY_BUFFER):
        """Pack a list of ints as an UNSIGNED_SHORT VEC4 accessor (used for
        JOINTS_0). `flat_values` is [j0,j1,j2,j3, j0,j1,j2,j3, ...]. Returns the
        new accessor index."""
        count = len(flat_values) // 4
        ints = [int(v) & 0xFFFF for v in flat_values]
        raw = struct.pack("<" + "H" * len(ints), *ints)
        offset = self._append_bytes(raw)

        bv_index = len(self.buffer_views)
        self.buffer_views.append({
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(raw),
            "target": target,
        })

        acc_index = len(self.accessors)
        self.accessors.append({
            "bufferView": bv_index,
            "componentType": COMP_USHORT,
            "count": count,
            "type": "VEC4",
        })
        return acc_index

    def add_image_bufferview(self, raw):
        """Append raw image bytes as a (target-less) bufferView. Returns the
        bufferView index (used for embedded glb images)."""
        offset = self._append_bytes(raw)
        bv_index = len(self.buffer_views)
        self.buffer_views.append({
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(raw),
        })
        return bv_index

    def get_bin(self):
        """Return the assembled BIN blob, padded to a 4-byte boundary."""
        blob = b"".join(self._chunks)
        pad = (4 - (len(blob) % 4)) % 4
        if pad:
            blob += b"\x00" * pad
        return blob


_ELEMENT_COUNTS = {
    "SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
    "MAT2": 4, "MAT3": 9, "MAT4": 16,
}

_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ktx2": "image/ktx2",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def _minmax_per_component(flat_values, elem_count):
    if not flat_values:
        zeros = [0.0] * elem_count
        return zeros, list(zeros)
    mins = [flat_values[i] for i in range(elem_count)]
    maxs = list(mins)
    n = len(flat_values) // elem_count
    for i in range(n):
        base = i * elem_count
        for c in range(elem_count):
            v = flat_values[base + c]
            if v < mins[c]:
                mins[c] = v
            if v > maxs[c]:
                maxs[c] = v
    return mins, maxs


def _mime_for_path(path, fallback="image/png"):
    ext = os.path.splitext(path or "")[1].lower()
    return _EXT_TO_MIME.get(ext, fallback)


# ---------------------------------------------------------------------------
# Document builder
# ---------------------------------------------------------------------------

def build_gltf_doc(nodes, meshes, materials, scene_root_indices,
                   embed_images=False, gltf_external_bin=False,
                   bin_uri=None, generator=GENERATOR,
                   skins=None, animations=None):
    """Assemble a complete glTF doc + the matching BIN blob from descriptors.

    Args:
        nodes:   list[Node].
        meshes:  list[MeshDef] (each holds Primitive objects).
        materials: list[MaterialDef].
        scene_root_indices: list[int] of root node indices for scenes[0].
        skins:   optional list[SkinDef] -> emitted as the doc's "skins" array.
        animations: optional list[AnimationDef] -> emitted as "animations".
        embed_images: when True, image bytes are embedded — as data URIs for a
            .gltf, or as BIN-chunk bufferViews for a .glb (controlled by
            gltf_external_bin: False => glb/embedded-binary path).
        gltf_external_bin: True when writing a .gltf with an external .bin (so
            buffers[0].uri = bin_uri). When False the buffer is a glb BIN chunk
            (no uri).
        bin_uri: the relative uri for the external .bin (only when
            gltf_external_bin is True).
        generator: asset.generator string.

    Returns (doc_dict, bin_bytes).

    Image handling is the trickiest part:
      * REFERENCE mode (embed_images False): each image -> {"uri": <relative or
        basename>, "mimeType": ...}. The CALLER is responsible for placing /
        copying the file so the uri resolves. We store the ImageRef.file_path's
        basename as the uri by default; exporter.py overrides with a relative
        path it computed.
      * EMBED + .gltf: data URI (base64).
      * EMBED + .glb : image bytes appended to BIN as a bufferView, image entry
        references {"bufferView": n, "mimeType": ...}.
    """
    packer = _BufferPacker()

    # --- collect + dedupe images referenced by materials --------------------
    image_index_of = {}          # key -> image array index
    images_json = []             # list of dict (built after we know encoding)
    image_refs_in_order = []     # list[ImageRef] parallel to images_json

    def _image_key(img_ref):
        # Dedupe by file path when available, else object identity.
        if img_ref.file_path:
            return ("path", os.path.normcase(os.path.abspath(img_ref.file_path)))
        return ("id", id(img_ref))

    def _intern_image(img_ref):
        key = _image_key(img_ref)
        if key in image_index_of:
            return image_index_of[key]
        idx = len(image_refs_in_order)
        image_index_of[key] = idx
        image_refs_in_order.append(img_ref)
        return idx

    # --- collect + dedupe textures (sampler+source pairs) -------------------
    # We use a single default sampler (repeat wrap, linear filter) for all.
    texture_index_of = {}        # image_index -> texture array index
    textures_json = []

    def _intern_texture(tex_ref):
        img_idx = _intern_image(tex_ref.image)
        if img_idx in texture_index_of:
            return texture_index_of[img_idx]
        t_idx = len(textures_json)
        texture_index_of[img_idx] = t_idx
        textures_json.append({"sampler": 0, "source": img_idx})
        return t_idx

    # --- materials ----------------------------------------------------------
    materials_json = []
    for mat in materials:
        materials_json.append(_material_to_json(mat, _intern_texture))

    # --- meshes / primitives / accessors ------------------------------------
    meshes_json = []
    for mesh in meshes:
        prims_json = []
        for prim in mesh.primitives:
            prims_json.append(_primitive_to_json(prim, packer))
        entry = {"primitives": prims_json}
        if mesh.name:
            entry["name"] = mesh.name
        # Morph metadata: default weights + target names. Only emitted when a
        # primitive actually declared targets (keeps non-morph meshes clean).
        has_targets = any(p.get("targets") for p in prims_json)
        if has_targets:
            if mesh.weights:
                entry["weights"] = [float(w) for w in mesh.weights]
            if mesh.target_names:
                extras = entry.setdefault("extras", {})
                extras["targetNames"] = [str(n) for n in mesh.target_names]
        meshes_json.append(entry)

    # --- nodes --------------------------------------------------------------
    nodes_json = []
    for node in nodes:
        nodes_json.append(_node_to_json(node))

    # --- skins --------------------------------------------------------------
    skins_json = []
    for skin in (skins or []):
        skins_json.append(_skin_to_json(skin, packer))

    # --- animations ---------------------------------------------------------
    animations_json = []
    for anim in (animations or []):
        anim_entry = _animation_to_json(anim, packer)
        if anim_entry is not None:
            animations_json.append(anim_entry)

    # --- images: encode now that the BIN has all geometry already packed ----
    #     (embedded-glb image bufferViews must come AFTER geometry so geometry
    #     offsets don't shift — but order within BIN doesn't actually matter as
    #     long as offsets are correct; we append images last for tidiness.)
    samplers_json = []
    if textures_json:
        samplers_json = [{
            "magFilter": 9729,   # LINEAR
            "minFilter": 9987,   # LINEAR_MIPMAP_LINEAR
            "wrapS": 10497,      # REPEAT
            "wrapT": 10497,      # REPEAT
        }]

    for img_ref in image_refs_in_order:
        images_json.append(
            _image_to_json(img_ref, embed_images, gltf_external_bin, packer)
        )

    bin_bytes = packer.get_bin()

    # --- buffer -------------------------------------------------------------
    buffer_entry = {"byteLength": len(bin_bytes)}
    if gltf_external_bin and bin_uri is not None:
        buffer_entry["uri"] = bin_uri

    doc = {
        "asset": {"version": "2.0", "generator": generator},
        "scene": 0,
        "scenes": [{"nodes": list(scene_root_indices)}],
        "nodes": nodes_json,
        "meshes": meshes_json,
        "accessors": packer.accessors,
        "bufferViews": packer.buffer_views,
        "buffers": [buffer_entry],
    }
    if materials_json:
        doc["materials"] = materials_json
    if textures_json:
        doc["textures"] = textures_json
    if images_json:
        doc["images"] = images_json
    if samplers_json:
        doc["samplers"] = samplers_json
    if skins_json:
        doc["skins"] = skins_json
    if animations_json:
        doc["animations"] = animations_json

    return doc, bin_bytes


def _node_to_json(node):
    entry = {}
    if node.name:
        entry["name"] = node.name
    if node.matrix is not None:
        entry["matrix"] = [float(v) for v in node.matrix]
    else:
        if node.translation is not None and any(v != 0.0 for v in node.translation):
            entry["translation"] = [float(v) for v in node.translation]
        if node.rotation is not None and tuple(node.rotation) != (0.0, 0.0, 0.0, 1.0):
            entry["rotation"] = [float(v) for v in node.rotation]
        if node.scale is not None and any(v != 1.0 for v in node.scale):
            entry["scale"] = [float(v) for v in node.scale]
    if node.mesh_index is not None:
        entry["mesh"] = node.mesh_index
    if node.skin_index is not None:
        entry["skin"] = node.skin_index
    if node.children:
        entry["children"] = list(node.children)
    return entry


def _skin_to_json(skin, packer):
    """Encode one SkinDef into a glTF skin entry, packing its IBMs into a single
    FLOAT MAT4 accessor (16 floats per joint, glTF column-major)."""
    entry = {"joints": [int(j) for j in skin.joint_node_indices]}
    if skin.name:
        entry["name"] = skin.name
    if skin.skeleton_node_index is not None:
        entry["skeleton"] = int(skin.skeleton_node_index)
    if skin.inverse_bind_matrices:
        flat = []
        for mat in skin.inverse_bind_matrices:
            flat.extend(float(v) for v in mat)
        # IBMs are not vertex attributes -> no bufferView target.
        entry["inverseBindMatrices"] = packer.add_float_accessor(
            flat, "MAT4", None
        )
    return entry


def _animation_to_json(anim, packer):
    """Encode one AnimationDef into a glTF animation entry. Each channel gets a
    SCALAR FLOAT input accessor (times, with min/max) and a VEC3/VEC4 FLOAT
    output accessor (values). Returns None if the animation has no usable
    channels."""
    samplers = []
    channels = []
    for ch in anim.channels:
        if not ch.times or not ch.values:
            continue
        # Input: keyframe times (SCALAR). glTF requires min/max on animation
        # input accessors.
        input_acc = packer.add_float_accessor(
            list(ch.times), "SCALAR", None, include_minmax=True
        )
        # Output accessor type per path:
        #   rotation        -> VEC4 (quaternion xyzw)
        #   translation/scale -> VEC3
        #   weights         -> SCALAR, flat keyframe-major (N weights per key)
        if ch.path == "weights":
            elem_type = "SCALAR"
            flat = [float(c) for c in ch.values]
        else:
            elem_type = "VEC4" if ch.path == "rotation" else "VEC3"
            flat = []
            for v in ch.values:
                flat.extend(float(c) for c in v)
        output_acc = packer.add_float_accessor(flat, elem_type, None)

        sampler_index = len(samplers)
        samplers.append({
            "input": input_acc,
            "output": output_acc,
            "interpolation": ch.interpolation or "LINEAR",
        })
        channels.append({
            "sampler": sampler_index,
            "target": {"node": int(ch.node_index), "path": ch.path},
        })

    if not channels:
        return None

    entry = {"samplers": samplers, "channels": channels}
    if anim.name:
        entry["name"] = anim.name
    return entry


def _primitive_to_json(prim, packer):
    attributes = {}

    # POSITION (with min/max — required by spec)
    attributes["POSITION"] = packer.add_float_accessor(
        prim.positions, "VEC3", TARGET_ARRAY_BUFFER, include_minmax=True
    )

    if prim.normals:
        attributes["NORMAL"] = packer.add_float_accessor(
            prim.normals, "VEC3", TARGET_ARRAY_BUFFER
        )
    if prim.tangents:
        attributes["TANGENT"] = packer.add_float_accessor(
            prim.tangents, "VEC4", TARGET_ARRAY_BUFFER
        )
    if prim.texcoords:
        attributes["TEXCOORD_0"] = packer.add_float_accessor(
            prim.texcoords, "VEC2", TARGET_ARRAY_BUFFER
        )
    if prim.colors:
        attributes["COLOR_0"] = packer.add_float_accessor(
            prim.colors, "VEC4", TARGET_ARRAY_BUFFER
        )

    # Skinning attributes: JOINTS_0 (UNSIGNED_SHORT VEC4) + WEIGHTS_0 (FLOAT
    # VEC4). Both must describe the same vertex count as POSITION.
    if prim.joints:
        attributes["JOINTS_0"] = packer.add_ushort_vec4_accessor(
            prim.joints, TARGET_ARRAY_BUFFER
        )
    if prim.weights:
        attributes["WEIGHTS_0"] = packer.add_float_accessor(
            prim.weights, "VEC4", TARGET_ARRAY_BUFFER
        )

    entry = {
        "attributes": attributes,
        "mode": MODE_TRIANGLES,
    }

    # Morph targets: one dict of delta accessors per target. POSITION deltas are
    # VEC3 (with min/max, like the base POSITION). NORMAL deltas are VEC3.
    if prim.morph_targets:
        targets_json = []
        for tgt in prim.morph_targets:
            tj = {}
            pos = tgt.get("POSITION")
            if pos:
                tj["POSITION"] = packer.add_float_accessor(
                    pos, "VEC3", None, include_minmax=True
                )
            nrm = tgt.get("NORMAL")
            if nrm:
                tj["NORMAL"] = packer.add_float_accessor(nrm, "VEC3", None)
            if tj:
                targets_json.append(tj)
        if targets_json:
            entry["targets"] = targets_json

    if prim.indices:
        entry["indices"] = packer.add_index_accessor(prim.indices)
    if prim.material_index is not None:
        entry["material"] = prim.material_index
    return entry


def _material_to_json(mat, intern_texture):
    pbr = {}

    if list(mat.base_color_factor) != [1.0, 1.0, 1.0, 1.0]:
        pbr["baseColorFactor"] = [float(v) for v in mat.base_color_factor]
    if mat.base_color_texture is not None:
        pbr["baseColorTexture"] = {"index": intern_texture(mat.base_color_texture)}

    if mat.metallic_factor != 1.0:
        pbr["metallicFactor"] = float(mat.metallic_factor)
    if mat.roughness_factor != 1.0:
        pbr["roughnessFactor"] = float(mat.roughness_factor)
    if mat.metallic_roughness_texture is not None:
        pbr["metallicRoughnessTexture"] = {
            "index": intern_texture(mat.metallic_roughness_texture)
        }

    entry = {}
    if mat.name:
        entry["name"] = mat.name
    if pbr:
        entry["pbrMetallicRoughness"] = pbr

    if mat.normal_texture is not None:
        nt = {"index": intern_texture(mat.normal_texture)}
        if mat.normal_scale != 1.0:
            nt["scale"] = float(mat.normal_scale)
        entry["normalTexture"] = nt

    if mat.occlusion_texture is not None:
        ot = {"index": intern_texture(mat.occlusion_texture)}
        if mat.occlusion_strength != 1.0:
            ot["strength"] = float(mat.occlusion_strength)
        entry["occlusionTexture"] = ot

    if list(mat.emissive_factor) != [0.0, 0.0, 0.0]:
        entry["emissiveFactor"] = [float(v) for v in mat.emissive_factor]
    if mat.emissive_texture is not None:
        entry["emissiveTexture"] = {"index": intern_texture(mat.emissive_texture)}

    if mat.alpha_mode and mat.alpha_mode != "OPAQUE":
        entry["alphaMode"] = mat.alpha_mode
        if mat.alpha_mode == "MASK" and mat.alpha_cutoff != 0.5:
            entry["alphaCutoff"] = float(mat.alpha_cutoff)
    if mat.double_sided:
        entry["doubleSided"] = True

    # An empty material is still valid; ensure it at least has a name slot.
    if not entry:
        entry = {"name": mat.name or "material"}
    return entry


def _image_to_json(img_ref, embed_images, gltf_external_bin, packer):
    """Encode one ImageRef into a glTF image entry, appending to BIN if needed."""
    mime = img_ref.mime or _mime_for_path(img_ref.file_path or "")
    name = img_ref.name

    if not embed_images:
        # REFERENCE mode: external uri. Default to basename of the source file;
        # exporter.py may have already set img_ref.name to a relative uri.
        uri = name or (os.path.basename(img_ref.file_path) if img_ref.file_path
                       else "texture")
        entry = {"uri": uri}
        # Don't store mimeType for external uris (extension implies it) but it's
        # harmless and helps tools — include it.
        if mime:
            entry["mimeType"] = mime
        return entry

    # EMBED mode — load the bytes if not already present.
    data = img_ref.data
    if data is None and img_ref.file_path:
        with open(img_ref.file_path, "rb") as f:
            data = f.read()
    if data is None:
        # Nothing to embed — degrade to a 1x1 transparent uri-less placeholder
        # is overkill; just emit a name-only entry (spec-valid? no — needs uri
        # or bufferView). Embed a tiny PNG-less marker via empty bufferView.
        data = b""

    if gltf_external_bin:
        # EMBED into a .gltf -> data URI (base64).
        b64 = base64.b64encode(data).decode("ascii")
        entry = {"uri": "data:%s;base64,%s" % (mime, b64), "mimeType": mime}
    else:
        # EMBED into a .glb -> bufferView in the BIN chunk.
        bv_index = packer.add_image_bufferview(data)
        entry = {"bufferView": bv_index, "mimeType": mime}
    if name and "uri" not in entry:
        entry["name"] = name
    return entry


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------

def pack_glb(doc, bin_bytes):
    """Pack a glTF doc dict + BIN blob into a complete .glb byte string.

    JSON chunk is padded with 0x20 (space) to a 4-byte boundary; BIN chunk with
    0x00 (spec §4.4.3). The header's total length covers everything.
    """
    json_bytes = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    json_pad = (4 - (len(json_bytes) % 4)) % 4
    if json_pad:
        json_bytes += b"\x20" * json_pad

    bin_pad = (4 - (len(bin_bytes) % 4)) % 4
    if bin_pad:
        bin_bytes = bin_bytes + b"\x00" * bin_pad

    total = 12 + 8 + len(json_bytes)
    if bin_bytes:
        total += 8 + len(bin_bytes)

    out = struct.pack("<III", _GLB_MAGIC, _GLB_VERSION, total)
    out += struct.pack("<II", len(json_bytes), _CHUNK_JSON) + json_bytes
    if bin_bytes:
        out += struct.pack("<II", len(bin_bytes), _CHUNK_BIN) + bin_bytes
    return out


def write_glb(path, doc, bin_bytes):
    """Write a .glb to disk. Creates the parent directory if needed."""
    path = os.path.abspath(path)
    out_dir = os.path.dirname(path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    blob = pack_glb(doc, bin_bytes)
    with open(path, "wb") as f:
        f.write(blob)
    return path


def write_gltf(path, doc, bin_bytes, bin_name=None):
    """Write a .gltf JSON file + its sidecar .bin. The doc must already
    reference the .bin via buffers[0].uri (set bin_uri in build_gltf_doc).

    If the doc embeds everything as data URIs (no external buffer), bin_bytes
    may be empty and no .bin is written.
    """
    path = os.path.abspath(path)
    out_dir = os.path.dirname(path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    # Write sidecar .bin only if the buffer actually references one.
    buffers = doc.get("buffers", [])
    needs_bin = bool(bin_bytes) and buffers and buffers[0].get("uri")
    if needs_bin:
        if bin_name is None:
            bin_name = buffers[0]["uri"]
        bin_path = os.path.join(out_dir, os.path.basename(bin_name))
        with open(bin_path, "wb") as f:
            f.write(bin_bytes)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    return path


# ---------------------------------------------------------------------------
# High-level convenience: assemble + write in one call
# ---------------------------------------------------------------------------

def export_to_file(path, nodes, meshes, materials, scene_root_indices,
                   fmt=None, embed_images=False, generator=GENERATOR,
                   skins=None, animations=None):
    """Build a doc from descriptors and write it as .glb or .gltf.

    fmt: "glb" or "gltf". If None, inferred from the path extension (defaults
         to glb).

    skins / animations: optional lists of SkinDef / AnimationDef. Included in
    the doc only when non-empty.

    Returns the absolute path written.
    """
    if fmt is None:
        ext = os.path.splitext(path)[1].lower()
        fmt = "gltf" if ext == ".gltf" else "glb"
    fmt = fmt.lower()

    if fmt == "gltf":
        # External .bin (unless embedding images as data URIs — geometry still
        # goes to the external .bin either way).
        bin_uri = os.path.splitext(os.path.basename(path))[0] + ".bin"
        doc, bin_bytes = build_gltf_doc(
            nodes, meshes, materials, scene_root_indices,
            embed_images=embed_images, gltf_external_bin=True,
            bin_uri=bin_uri, generator=generator,
            skins=skins, animations=animations,
        )
        return write_gltf(path, doc, bin_bytes, bin_name=bin_uri)

    # glb
    doc, bin_bytes = build_gltf_doc(
        nodes, meshes, materials, scene_root_indices,
        embed_images=embed_images, gltf_external_bin=False,
        generator=generator,
        skins=skins, animations=animations,
    )
    return write_glb(path, doc, bin_bytes)
