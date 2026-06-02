"""glTF 2.0 parser. Pure stdlib (json + struct + base64), no third-party deps.

Public entry: load(path) -> Gltf

Spec: https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html

v0.1 scope: meshes, materials (metal-rough PBR + normal + emissive), textures
(embedded or external, PNG / JPEG / WebP), node hierarchy, scenes. No animation
or morph targets yet — those land in subsequent iterations.

v0.2 adds: skinning (skins array + per-vertex JOINTS_n/WEIGHTS_n decode via
read_skin(); applied in Maya by _build._build_skins).

Morph targets (blendshapes): primitive_morph_targets() decodes a primitive's
per-vertex POSITION/NORMAL/TANGENT deltas; mesh_morph_weights() /
node_morph_weights() / mesh_target_names() expose the default weights + names.
Parsing only — Maya blendShape construction lives in _build, weight animation
(the `weights` channel path) in _anim.

KHR_materials_variants: variant_names() exposes the root variant list and
primitive_variant_material() resolves a primitive's material index per variant
id. Parsing only — Maya-side switching lives in _build / commands. Other KHR
extensions beyond the standard PBR core aren't decoded yet.

Cameras + lights: camera_def() / node_camera() expose core glTF cameras;
light_defs() / light_def() / node_light() expose KHR_lights_punctual lights.
Parsing only — Maya camera/light shape creation lives in _build._build_node.
Both return None / [] when absent, so glTFs (and every Anno glb) without them
are entirely unaffected.
"""
from __future__ import annotations

import base64
import json
import os
import struct
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# GLB binary container constants (spec §4.4)
# ---------------------------------------------------------------------------

_GLB_MAGIC = 0x46546C67       # b'glTF' little-endian
_GLB_VERSION = 2
_CHUNK_JSON = 0x4E4F534A      # b'JSON'
_CHUNK_BIN = 0x004E4942       # b'BIN\0'


# ---------------------------------------------------------------------------
# Accessor componentType and type tables (spec §5)
# ---------------------------------------------------------------------------

# componentType enum -> (struct format char, byte size)
COMPONENT_TYPES = {
    5120: ("b", 1),   # BYTE
    5121: ("B", 1),   # UNSIGNED_BYTE
    5122: ("h", 2),   # SHORT
    5123: ("H", 2),   # UNSIGNED_SHORT
    5125: ("I", 4),   # UNSIGNED_INT
    5126: ("f", 4),   # FLOAT
}

# accessor.type -> elements per item
ELEMENT_COUNTS = {
    "SCALAR": 1,
    "VEC2": 2, "VEC3": 3, "VEC4": 4,
    "MAT2": 4, "MAT3": 9, "MAT4": 16,
}

# Per-component-type divisor for `normalized` accessors (spec §3.6.2.1).
# Signed types use max(comp / div, -1); unsigned use comp / div.
_NORMALIZE_DIVISORS = {
    5120: 127.0,    # BYTE
    5121: 255.0,    # UNSIGNED_BYTE
    5122: 32767.0,  # SHORT
    5123: 65535.0,  # UNSIGNED_SHORT
}


def _apply_norm(v, divisor):
    """Apply glTF normalized-integer rescale, clamping signed to [-1, 1]."""
    f = v / divisor
    if v < 0 and f < -1.0:
        return -1.0
    return f


MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/ktx2": ".ktx2",
}

EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ktx2": "image/ktx2",
}


class GltfError(Exception):
    pass


class Skin(object):
    """Decoded glTF skin (parser output, Maya-free).

    See Gltf.read_skin() for field semantics. Plain class rather than
    @dataclass to keep this module 3.6-friendly for any older host Python.
    """

    __slots__ = ("joint_node_indices", "inverse_bind_matrices", "skeleton_node_index")

    def __init__(self, joint_node_indices, inverse_bind_matrices, skeleton_node_index):
        self.joint_node_indices = joint_node_indices
        self.inverse_bind_matrices = inverse_bind_matrices
        self.skeleton_node_index = skeleton_node_index

    def __repr__(self):
        return (
            "Skin(joints={0}, ibms={1}, skeleton={2})"
            .format(
                len(self.joint_node_indices),
                len(self.inverse_bind_matrices),
                self.skeleton_node_index,
            )
        )


class AnimChannel(object):
    """One decoded glTF animation channel (parser output, Maya-free).

    A channel targets a single node + property path and carries the fully
    decoded sampler input (times) and output (values).

    Fields:
      target_node   : int — index into doc["nodes"]. Always present (channels
                      with no target are dropped at decode time).
      path          : str — "translation" | "rotation" | "scale" | "weights".
      interpolation : str — "LINEAR" | "STEP" | "CUBICSPLINE" (default LINEAR).
      times         : list[float] — SCALAR input accessor, seconds.
      values        : decoded output accessor:
                        translation/scale -> list[(x, y, z)]
                        rotation          -> list[(x, y, z, w)]  (quaternion)
                        weights           -> flat list[float]
                      For CUBICSPLINE the output holds 3 entries per keyframe
                      (in-tangent, value, out-tangent) laid out consecutively;
                      the applier picks the middle (value) element per key for
                      v1. `interpolation` lets the applier tell the layouts
                      apart.
    """

    __slots__ = ("target_node", "path", "interpolation", "times", "values")

    def __init__(self, target_node, path, interpolation, times, values):
        self.target_node = target_node
        self.path = path
        self.interpolation = interpolation
        self.times = times
        self.values = values

    def __repr__(self):
        return (
            "AnimChannel(node={0}, path={1}, interp={2}, keys={3})"
            .format(self.target_node, self.path, self.interpolation, len(self.times))
        )


class Animation(object):
    """Decoded glTF animation (parser output, Maya-free).

    Fields:
      name     : str — animation name, or "anim_<i>" if the glTF omits one.
      channels : list[AnimChannel] — only channels with a resolvable target
                 node and decodable sampler survive here.
    """

    __slots__ = ("name", "channels")

    def __init__(self, name, channels):
        self.name = name
        self.channels = channels

    def __repr__(self):
        return "Animation(name={0!r}, channels={1})".format(self.name, len(self.channels))


# ---------------------------------------------------------------------------
# Gltf container
# ---------------------------------------------------------------------------

class Gltf(object):
    """Parsed glTF document + the raw binary chunk (if .glb).

    The JSON document is kept as a plain dict tree so callers can read any
    field directly. Accessor decoding + image extraction go through helper
    methods that cache reads.
    """

    def __init__(self, doc, bin_chunk, base_dir):
        self.doc = doc                # type: Dict[str, Any]
        self.bin = bin_chunk          # type: Optional[bytes]
        self.base_dir = base_dir      # for resolving external uris
        self._buffer_cache = {}       # type: Dict[int, bytes]

    # --- buffers / accessors -------------------------------------------------

    def get_buffer(self, buffer_index):
        # type: (int) -> bytes
        if buffer_index in self._buffer_cache:
            return self._buffer_cache[buffer_index]
        buf = self.doc["buffers"][buffer_index]
        uri = buf.get("uri")

        if uri is None:
            # Buffer 0 in a .glb references the embedded BIN chunk (spec §4.4.3)
            if buffer_index == 0 and self.bin is not None:
                data = self.bin
            else:
                raise GltfError(
                    "buffer {0} has no uri and no embedded BIN chunk".format(buffer_index)
                )
        elif uri.startswith("data:"):
            _, b64 = uri.split(",", 1)
            data = base64.b64decode(b64)
        else:
            path = os.path.join(self.base_dir, uri)
            with open(path, "rb") as f:
                data = f.read()

        self._buffer_cache[buffer_index] = data
        return data

    def get_bufferview_bytes(self, bv_index):
        # type: (int) -> bytes
        """Return the raw bytes a bufferView spans (honoring byteOffset/Length).

        Used by the Draco decoder (the compressed mesh lives in a bufferView,
        not an accessor) and any other consumer needing the raw slice.
        """
        bv = self.doc["bufferViews"][bv_index]
        buf = self.get_buffer(bv["buffer"])
        off = bv.get("byteOffset", 0)
        ln = bv.get("byteLength")
        if ln is None:
            return buf[off:]
        return buf[off:off + ln]

    def read_accessor(self, accessor_index):
        # type: (int) -> List
        """Decode an accessor into a Python list.

        Scalar accessors return a flat [int|float] list.
        Vector accessors return [(x, y[, z[, w]]), ...].

        Honors the accessor's `normalized` flag: integer components are
        rescaled to floats per the glTF spec (signed -> [-1,1], unsigned ->
        [0,1]). Needed for skin WEIGHTS_n stored as UNSIGNED_BYTE/SHORT, and
        for KHR_mesh_quantization POSITION/NORMAL/TANGENT/TEXCOORD accessors
        that pack their components as BYTE/UBYTE/SHORT/USHORT (the integer
        component types + the `normalized` flag are handled here; a quantized
        but NON-normalized POSITION returns raw ints that the node's TRS scales
        back, applied by _build._apply_trs).

        Sparse accessors and zero-buffer accessors aren't handled in v0.1 —
        they return empty lists with a warning printed by the caller.
        """
        acc = self.doc["accessors"][accessor_index]
        comp_type = acc["componentType"]
        elem_type = acc["type"]
        count = acc["count"]
        bv_index = acc.get("bufferView")
        normalized = bool(acc.get("normalized", False))

        ctype_char, ctype_size = COMPONENT_TYPES[comp_type]
        elem_count = ELEMENT_COUNTS[elem_type]
        item_size = ctype_size * elem_count

        if bv_index is None:
            # No bufferView — sparse-only or all-default. v0.1: empty.
            return []

        bv = self.doc["bufferViews"][bv_index]
        buf_data = self.get_buffer(bv["buffer"])
        start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        byte_stride = bv.get("byteStride", 0)

        norm_scale = _NORMALIZE_DIVISORS.get(comp_type) if normalized else None

        if byte_stride and byte_stride != item_size:
            # Interleaved — read item-by-item
            results = []
            fmt = "<" + ctype_char * elem_count
            for i in range(count):
                off = start + i * byte_stride
                if elem_count == 1:
                    val = struct.unpack_from(fmt, buf_data, off)[0]
                    if norm_scale is not None:
                        val = max(val / norm_scale, -1.0) if val < 0 else val / norm_scale
                    results.append(val)
                else:
                    tup = struct.unpack_from(fmt, buf_data, off)
                    if norm_scale is not None:
                        tup = tuple(_apply_norm(v, norm_scale) for v in tup)
                    results.append(tup)
            return results

        # Tightly packed — one struct.unpack call
        fmt = "<" + ctype_char * (elem_count * count)
        flat = struct.unpack_from(fmt, buf_data, start)
        if norm_scale is not None:
            flat = tuple(_apply_norm(v, norm_scale) for v in flat)
        if elem_count == 1:
            return list(flat)
        return [flat[i * elem_count:(i + 1) * elem_count] for i in range(count)]

    # --- KHR_draco_mesh_compression ------------------------------------------

    @staticmethod
    def primitive_has_draco(prim):
        # type: (dict) -> bool
        """True if a primitive carries KHR_draco_mesh_compression.

        When present, the primitive's plain `attributes`/`indices` accessors are
        FALLBACK metadata (bounds/counts) and the real vertex data lives in the
        Draco bitstream; the builder must decode that instead. Returns False for
        every ordinary primitive (the common case), so callers cheaply gate.
        """
        if not isinstance(prim, dict):
            return False
        ext = (prim.get("extensions") or {}).get("KHR_draco_mesh_compression")
        return bool(ext) and "bufferView" in ext

    def decode_draco_primitive(self, prim):
        # type: (dict) -> dict
        """Decode a primitive's Draco geometry into glTF-semantic arrays.

        Reads the compressed bufferView referenced by
        prim.extensions.KHR_draco_mesh_compression.bufferView, runs it through
        the DracoPy-backed decoder (_draco.decode), and returns:

          {
            "POSITION":   [(x, y, z), ...],
            "NORMAL":     [(x, y, z), ...] | None,
            "TEXCOORD_0": [(u, v), ...]   | None,
            "indices":    [i, i, i, ...],          # flat triangle list
          }

        The decoder maps Draco's typed attributes (.points/.normals/.tex_coord)
        onto the matching glTF semantics — equivalently to the extension's
        `attributes` table (glTF semantic -> Draco attribute id). The
        primitive's fallback accessors are intentionally NOT read.

        Raises _draco.DracoError if DracoPy isn't available or the buffer fails
        to decode; the caller (_build._build_primitive) catches that, warns, and
        skips the primitive rather than aborting the whole import.

        _draco is imported lazily HERE so this module stays importable without
        DracoPy (and without maya) — the smoke tests rely on that.
        """
        from maya_glb_io import _draco

        ext = (prim.get("extensions") or {}).get("KHR_draco_mesh_compression")
        if not ext or "bufferView" not in ext:
            raise _draco.DracoError(
                "primitive has no KHR_draco_mesh_compression bufferView"
            )

        try:
            data = self.get_bufferview_bytes(ext["bufferView"])
        except Exception as exc:
            raise _draco.DracoError(
                "could not read Draco bufferView %r: %s"
                % (ext.get("bufferView"), exc)
            )

        decoded = _draco.decode(data)

        # The extension's attributes map declares which glTF semantics the Draco
        # stream carries. DracoPy resolves POSITION/NORMAL/TEXCOORD_0 by type
        # for us, so we only need to NULL out any optional semantic the
        # primitive doesn't actually declare (defensive — keeps us in lockstep
        # with the extension table rather than trusting the decoder alone).
        attr_map = ext.get("attributes") or {}
        if "NORMAL" not in attr_map:
            decoded["NORMAL"] = None
        if "TEXCOORD_0" not in attr_map:
            decoded["TEXCOORD_0"] = None
        return decoded

    # --- skins ---------------------------------------------------------------

    def read_skin(self, skin_index):
        # type: (int) -> Skin
        """Decode glTF skin definition (joints, IBMs, optional skeleton root).

        Returns a Skin dataclass-like object with:
          joint_node_indices    : list[int] — indices into doc["nodes"]
          inverse_bind_matrices : list[tuple[float * 16]] — one per joint,
                                  in glTF's column-major mat4 layout.
                                  Empty list if the skin omits the accessor
                                  (spec allows it -> identity IBMs).
          skeleton_node_index   : int or None — common ancestor joint root
        """
        skin_doc = self.doc["skins"][skin_index]
        joints = list(skin_doc.get("joints", []))
        skeleton = skin_doc.get("skeleton")

        ibm_accessor = skin_doc.get("inverseBindMatrices")
        if ibm_accessor is None:
            ibms = []
        else:
            raw = self.read_accessor(ibm_accessor)
            # MAT4 accessors come back as tuples of 16 floats
            ibms = [tuple(row) for row in raw]

        return Skin(
            joint_node_indices=joints,
            inverse_bind_matrices=ibms,
            skeleton_node_index=skeleton,
        )

    # --- animations ----------------------------------------------------------

    def read_animations(self):
        # type: () -> List[Animation]
        """Decode every glTF animation into Animation/AnimChannel objects.

        Returns an empty list if the document has no `animations` array (the
        common case for Anno glbs). Channels whose target node is absent, or
        whose sampler can't be decoded, are skipped (with the bad channel
        dropped rather than aborting the whole animation).

        Each surviving channel has its sampler input (times, seconds) and
        output (values) fully decoded via read_accessor. Rotation outputs come
        back as VEC4 (x, y, z, w) quaternion tuples; translation/scale as VEC3;
        weights as a flat scalar list. CUBICSPLINE outputs keep their raw
        3-per-key layout — see AnimChannel for the contract.
        """
        anims_doc = self.doc.get("animations") or []
        animations = []  # type: List[Animation]

        for ai, anim in enumerate(anims_doc):
            name = anim.get("name") or "anim_%d" % ai
            samplers = anim.get("samplers", [])
            channels = []  # type: List[AnimChannel]

            for ch in anim.get("channels", []):
                target = ch.get("target") or {}
                node_idx = target.get("node")
                if node_idx is None:
                    # Channel with no target node (e.g. targeting an extension);
                    # nothing for us to drive.
                    continue
                path = target.get("path")
                if path not in ("translation", "rotation", "scale", "weights"):
                    continue

                sampler_idx = ch.get("sampler")
                if sampler_idx is None or sampler_idx >= len(samplers):
                    continue
                sampler = samplers[sampler_idx]

                input_acc = sampler.get("input")
                output_acc = sampler.get("output")
                if input_acc is None or output_acc is None:
                    continue

                interp = sampler.get("interpolation", "LINEAR")

                try:
                    times = self.read_accessor(input_acc)
                    values = self.read_accessor(output_acc)
                except Exception:
                    # Undecodable sampler (e.g. sparse / zero-buffer) — drop it
                    # rather than killing the whole import.
                    continue

                if not times or not values:
                    continue

                channels.append(AnimChannel(
                    target_node=node_idx,
                    path=path,
                    interpolation=interp,
                    times=times,
                    values=values,
                ))

            animations.append(Animation(name=name, channels=channels))

        return animations

    # --- morph targets (blendshapes) -----------------------------------------

    def primitive_morph_targets(self, prim):
        # type: (dict) -> List[dict]
        """Decode a primitive's glTF morph targets into per-target delta dicts.

        glTF stores morph targets as prim["targets"] = a list of dicts, each
        like {"POSITION": accessorIdx, "NORMAL": accessorIdx, "TANGENT":
        accessorIdx} giving per-vertex DELTAS added to the base attribute.

        Returns a list parallel to prim["targets"]; each entry is a dict with
        whichever of "POSITION"/"NORMAL"/"TANGENT" the target declared, decoded
        via read_accessor into the usual [(x, y, z), ...] tuples (POSITION /
        NORMAL are VEC3; TANGENT deltas are VEC3 per the spec — the base TANGENT
        is VEC4 but its w handedness is not morphed).

        Returns an empty list when the primitive has no targets (the common
        case), so callers can cheaply gate on it. Targets that fail to decode
        contribute an empty dict rather than aborting.
        """
        targets = prim.get("targets")
        if not targets:
            return []
        out = []  # type: List[dict]
        for tgt in targets:
            decoded = {}
            if not isinstance(tgt, dict):
                out.append(decoded)
                continue
            for attr in ("POSITION", "NORMAL", "TANGENT"):
                acc = tgt.get(attr)
                if acc is None:
                    continue
                try:
                    decoded[attr] = self.read_accessor(acc)
                except Exception:
                    # Undecodable target accessor — skip just this attribute.
                    pass
            out.append(decoded)
        return out

    @staticmethod
    def primitive_target_count(prim):
        # type: (dict) -> int
        """Number of morph targets a primitive declares (0 when none)."""
        targets = prim.get("targets")
        return len(targets) if targets else 0

    @staticmethod
    def mesh_morph_weights(mesh):
        # type: (dict) -> List[float]
        """Return a mesh's default morph weights (mesh["weights"]) or []."""
        weights = mesh.get("weights")
        if not weights:
            return []
        return [float(w) for w in weights]

    @staticmethod
    def node_morph_weights(node):
        # type: (dict) -> List[float]
        """Return a node's morph-weight override (node["weights"]) or [].

        A node referencing a mesh may override the mesh's default weights.
        """
        weights = node.get("weights")
        if not weights:
            return []
        return [float(w) for w in weights]

    @staticmethod
    def mesh_target_names(mesh):
        # type: (dict) -> List[str]
        """Return morph-target names from mesh.extras.targetNames, or [].

        These name the blendShape weights when present; callers fall back to
        target0, target1, ... when this is empty or too short.
        """
        extras = mesh.get("extras") or {}
        names = extras.get("targetNames")
        if not names:
            return []
        return [str(n) for n in names]

    # --- material variants (KHR_materials_variants) --------------------------

    def variant_names(self):
        # type: () -> List[str]
        """Return the document's KHR_materials_variants variant names, in order.

        The index into this list is the variant id referenced by each
        primitive's mapping. Returns an empty list when the extension is
        absent (the common case), so callers can cheaply gate on it.

        Spec: KHR_materials_variants
        https://github.com/KhronosGroup/glTF/tree/main/extensions/2.0/Khronos/KHR_materials_variants
        """
        ext = (self.doc.get("extensions") or {}).get("KHR_materials_variants")
        if not ext:
            return []
        names = []
        for v in ext.get("variants") or []:
            # Each entry is {"name": "..."}; tolerate a missing name.
            names.append(v.get("name") if isinstance(v, dict) else None)
        # Replace any missing name with a stable placeholder so indices line up.
        return [n if n is not None else ("variant_%d" % i)
                for i, n in enumerate(names)]

    def has_variants(self):
        # type: () -> bool
        """True if the document declares any KHR_materials_variants variants."""
        return bool(self.variant_names())

    @staticmethod
    def primitive_variant_mappings(prim):
        # type: (dict) -> List[dict]
        """Return a primitive's raw KHR_materials_variants mappings list.

        Each mapping is {"material": <materialIndex>, "variants": [<id>, ...]}.
        Empty list when the primitive carries no variant mappings.
        """
        ext = (prim.get("extensions") or {}).get("KHR_materials_variants")
        if not ext:
            return []
        return ext.get("mappings") or []

    @classmethod
    def primitive_variant_material(cls, prim, variant_id):
        # type: (dict, int) -> Optional[int]
        """Resolve the material index a primitive uses for `variant_id`.

        Returns the material index of the first mapping whose `variants` list
        contains `variant_id`, or None if no mapping matches (the caller should
        then fall back to the primitive's base `material`).
        """
        for mapping in cls.primitive_variant_mappings(prim):
            if variant_id in (mapping.get("variants") or []):
                return mapping.get("material")
        return None

    # --- cameras (core glTF) -------------------------------------------------

    def camera_def(self, index):
        # type: (int) -> Optional[dict]
        """Return a glTF camera definition by index, or None.

        The returned dict is a shallow normalisation of doc["cameras"][index]:
          {"type": "perspective"|"orthographic", "perspective": {...}} or
          {"type": ..., "orthographic": {...}}
        i.e. the camera's `type` plus the matching projection sub-dict
        (yfov/aspectRatio/znear/zfar for perspective; xmag/ymag/znear/zfar for
        orthographic). Returns None if the index is out of range or the camera
        is malformed (missing/unknown type), so the Maya builder can cheaply
        gate on it.

        Spec §3.10.1 (cameras): https://registry.khronos.org/glTF/specs/2.0/
        """
        cameras = self.doc.get("cameras") or []
        if index is None or index < 0 or index >= len(cameras):
            return None
        cam = cameras[index]
        if not isinstance(cam, dict):
            return None
        ctype = cam.get("type")
        if ctype not in ("perspective", "orthographic"):
            return None
        out = {"type": ctype}
        if "name" in cam:
            out["name"] = cam.get("name")
        # Carry through whichever projection sub-dict matches the type. Default
        # to an empty dict so callers can use .get() with their own fallbacks.
        out[ctype] = cam.get(ctype) or {}
        return out

    @staticmethod
    def node_camera(node):
        # type: (dict) -> Optional[int]
        """Return the camera index a node references (node["camera"]), or None.

        None when the node references no camera (the common case), so the Maya
        builder can cheaply gate on it.
        """
        if not isinstance(node, dict):
            return None
        return node.get("camera")

    # --- lights (KHR_lights_punctual) ----------------------------------------

    def light_defs(self):
        # type: () -> List[dict]
        """Return the document's KHR_lights_punctual light definitions, in order.

        The index into this list is what a node references via
        node.extensions.KHR_lights_punctual.light. Each entry is the raw light
        dict, e.g.
          {"type": "directional"|"point"|"spot", "color": [r,g,b],
           "intensity": float, "range": float,
           "spot": {"innerConeAngle": rad, "outerConeAngle": rad},
           "name": str}
        with most fields optional per spec. Returns an empty list when the
        extension is absent (the common case — every Anno glb), so callers can
        cheaply gate on it.

        Spec: KHR_lights_punctual
        https://github.com/KhronosGroup/glTF/tree/main/extensions/2.0/Khronos/KHR_lights_punctual
        """
        ext = (self.doc.get("extensions") or {}).get("KHR_lights_punctual")
        if not ext:
            return []
        lights = ext.get("lights")
        if not lights:
            return []
        return [l for l in lights if isinstance(l, dict)]

    def light_def(self, index):
        # type: (int) -> Optional[dict]
        """Return one KHR_lights_punctual light by index, or None if invalid."""
        lights = self.light_defs()
        if index is None or index < 0 or index >= len(lights):
            return None
        return lights[index]

    @staticmethod
    def node_light(node):
        # type: (dict) -> Optional[int]
        """Return the light index a node references, or None.

        Reads node.extensions.KHR_lights_punctual.light. None when the node has
        no light (the common case), so the Maya builder can cheaply gate on it.
        """
        if not isinstance(node, dict):
            return None
        ext = (node.get("extensions") or {}).get("KHR_lights_punctual")
        if not ext:
            return None
        return ext.get("light")

    # --- images --------------------------------------------------------------

    def get_image_bytes(self, image_index):
        # type: (int) -> Tuple[bytes, str, str]
        """Return (raw_bytes, mime_type, file_extension) for a glTF image."""
        img = self.doc["images"][image_index]
        uri = img.get("uri")
        mime = img.get("mimeType")

        if uri is not None:
            if uri.startswith("data:"):
                head, b64 = uri.split(",", 1)
                # head looks like "data:image/png;base64"
                if mime is None and head.startswith("data:"):
                    mime = head[5:].split(";")[0]
                data = base64.b64decode(b64)
            else:
                path = os.path.join(self.base_dir, uri)
                with open(path, "rb") as f:
                    data = f.read()
                if mime is None:
                    ext = os.path.splitext(uri)[1].lower()
                    mime = EXT_TO_MIME.get(ext, "application/octet-stream")
        else:
            # bufferView reference (only valid in .glb)
            bv = self.doc["bufferViews"][img["bufferView"]]
            buf = self.get_buffer(bv["buffer"])
            off = bv.get("byteOffset", 0)
            ln = bv["byteLength"]
            data = buf[off:off + ln]

        ext = MIME_TO_EXT.get(mime or "", ".bin")
        return data, (mime or "application/octet-stream"), ext


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load(path):
    # type: (str) -> Gltf
    """Load a .glb or .gltf file. Returns a Gltf."""
    base_dir = os.path.dirname(os.path.abspath(path))

    # Decide format by sniffing the first 4 bytes — extension is advisory only
    with open(path, "rb") as f:
        head = f.read(4)

    if head == b"glTF":
        with open(path, "rb") as f:
            return _parse_glb(f.read(), base_dir)

    # Treat as JSON .gltf
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    return Gltf(doc, None, base_dir)


def loads(data, base_dir=""):
    # type: (bytes, str) -> Gltf
    """Load a .glb from a bytes object.

    Used by the Anno addon pipeline: rdm4-bin.exe writes glb bytes to
    stdout, we capture them and pass straight here without ever touching
    the disk. base_dir is the directory to resolve any external resource
    URIs against — for an Anno-derived glb there typically aren't any.
    """
    if len(data) < 4:
        raise GltfError("glb data too short to identify")
    head = data[:4]
    if head == b"glTF":
        return _parse_glb(bytes(data), base_dir)
    # Try as JSON
    try:
        doc = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise GltfError("bytes are neither valid glb nor JSON glTF: {0}".format(exc))
    return Gltf(doc, None, base_dir)


def _parse_glb(data, base_dir):
    # type: (bytes, str) -> Gltf
    if len(data) < 12:
        raise GltfError("glb too short to contain a header")

    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != _GLB_MAGIC:
        raise GltfError("not a glb (magic was 0x{0:08X})".format(magic))
    if version != _GLB_VERSION:
        raise GltfError("unsupported glb version {0}".format(version))

    json_doc = None
    bin_chunk = None
    offset = 12

    while offset < length:
        if offset + 8 > length:
            break
        chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        chunk_data = data[offset:offset + chunk_len]
        offset += chunk_len

        if chunk_type == _CHUNK_JSON:
            # JSON chunk is padded with 0x20 spaces to a 4-byte boundary
            json_text = chunk_data.rstrip(b"\x00").rstrip(b"\x20").decode("utf-8")
            json_doc = json.loads(json_text)
        elif chunk_type == _CHUNK_BIN:
            # BIN chunk is padded with 0x00 (spec §4.4.3)
            bin_chunk = bytes(chunk_data)
        # else: per spec we ignore unrecognised chunks

    if json_doc is None:
        raise GltfError("glb has no JSON chunk")

    return Gltf(json_doc, bin_chunk, base_dir)
