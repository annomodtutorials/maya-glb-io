"""Maya-free smoke test for the glTF skin parser.

Run from system Python (any 3.7+):
    python _dev\smoke_skin_import.py

Builds a tiny 2-joint, 4-vertex skinned glb entirely in memory and verifies
that:
  * gltf.read_skin(0) returns the right joints + IBMs
  * normalized WEIGHTS_0 (UNSIGNED_BYTE) decodes to the correct floats
  * JOINTS_0 (UNSIGNED_BYTE) decodes to the right per-vertex joint indices

The actual Maya skinCluster / joint creation is covered in _build.py and
requires a Maya session — there's no headless harness for that yet.
"""
import json
import os
import struct
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, SCRIPTS)

from maya_glb_io import _gltf  # noqa: E402


# ---------------------------------------------------------------------------
# Build a minimal skinned glb in memory
# ---------------------------------------------------------------------------

def _identity_mat4():
    # column-major (same as both glTF and Maya conventions)
    return [
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def _translate_mat4(tx, ty, tz):
    return [
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        tx,  ty,  tz,  1.0,
    ]


def build_test_glb():
    # --- Geometry: 4 verts at (0,0,0), (1,0,0), (0,1,0), (1,1,0); 2 tris ----
    positions = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                 (0.0, 1.0, 0.0), (1.0, 1.0, 0.0)]
    indices = [0, 1, 2, 1, 3, 2]

    # --- Skin: 2 joints. Joint0 at origin, joint1 at +X 1m -------------------
    # IBMs are the WORLD-TO-BIND matrices, i.e. inverse of each joint's bind world.
    # joint0 bind world = identity -> IBM = identity
    # joint1 bind world = translate(+1, 0, 0) -> IBM = translate(-1, 0, 0)
    ibm0 = _identity_mat4()
    ibm1 = _translate_mat4(-1.0, 0.0, 0.0)
    ibm_flat = ibm0 + ibm1  # 32 floats

    # --- Weights: vert0,1 fully on joint0; vert2 split 50/50; vert3 fully on joint1
    # JOINTS_0 vec4 ubyte per vert (we use slots 0 and 1; 2 and 3 = 0)
    joints_per_vert = [
        (0, 0, 0, 0),  # v0 -> joint0
        (0, 0, 0, 0),  # v1 -> joint0
        (0, 1, 0, 0),  # v2 -> joint0 + joint1
        (1, 0, 0, 0),  # v3 -> joint1
    ]
    # WEIGHTS_0 vec4 ubyte normalized
    # Choose 255-encodings that round-trip to nice floats
    # v0 weights = (1, 0, 0, 0) -> bytes (255, 0, 0, 0)
    # v2 weights = (0.5, 0.5, 0, 0) -> bytes (128, 127, 0, 0) which sums to 1 after norm
    # Use exact splits to keep the test deterministic
    weights_per_vert_bytes = [
        (255,   0, 0, 0),
        (255,   0, 0, 0),
        (128, 127, 0, 0),
        (255,   0, 0, 0),
    ]
    expected_weights_floats = [
        (255 / 255.0,   0.0, 0.0, 0.0),
        (255 / 255.0,   0.0, 0.0, 0.0),
        (128 / 255.0, 127 / 255.0, 0.0, 0.0),
        (255 / 255.0,   0.0, 0.0, 0.0),
    ]

    # --- Pack binary chunk in order: positions, indices, IBMs, joints, weights
    # Each section is 4-byte aligned for glTF compliance.
    def align4(blob):
        pad = (-len(blob)) % 4
        return blob + (b"\x00" * pad)

    pos_blob = b"".join(struct.pack("<3f", *p) for p in positions)
    idx_blob = struct.pack("<%dH" % len(indices), *indices)
    ibm_blob = struct.pack("<%df" % len(ibm_flat), *ibm_flat)
    joints_blob = b"".join(struct.pack("<4B", *j) for j in joints_per_vert)
    weights_blob = b"".join(struct.pack("<4B", *w) for w in weights_per_vert_bytes)

    sections = [
        ("pos", pos_blob),
        ("idx", idx_blob),
        ("ibm", ibm_blob),
        ("jts", joints_blob),
        ("wts", weights_blob),
    ]

    bin_chunk = b""
    offsets = {}
    for name, blob in sections:
        offsets[name] = (len(bin_chunk), len(blob))
        bin_chunk += align4(blob)

    # --- JSON document ------------------------------------------------------
    doc = {
        "asset": {"version": "2.0", "generator": "smoke_skin_import.py"},
        "scene": 0,
        "scenes": [{"nodes": [0, 1, 2]}],   # mesh node + 2 joints at root
        "nodes": [
            # Node 0: the skinned mesh instance
            {"name": "TestMesh", "mesh": 0, "skin": 0},
            # Nodes 1, 2: joints. joint1 is a child of joint0 (translated +X 1m).
            {"name": "joint0", "children": [2]},
            {"name": "joint1", "translation": [1.0, 0.0, 0.0]},
        ],
        "meshes": [{
            "name": "TestMesh",
            "primitives": [{
                "attributes": {
                    "POSITION": 0,
                    "JOINTS_0": 3,
                    "WEIGHTS_0": 4,
                },
                "indices": 1,
                "mode": 4,  # TRIANGLES
            }],
        }],
        "skins": [{
            "name": "TestSkin",
            "joints": [1, 2],            # glTF node indices
            "skeleton": 1,
            "inverseBindMatrices": 2,    # accessor index
        }],
        "buffers": [{"byteLength": len(bin_chunk)}],
        "bufferViews": [
            # 0: positions
            {"buffer": 0, "byteOffset": offsets["pos"][0], "byteLength": offsets["pos"][1], "target": 34962},
            # 1: indices
            {"buffer": 0, "byteOffset": offsets["idx"][0], "byteLength": offsets["idx"][1], "target": 34963},
            # 2: IBMs
            {"buffer": 0, "byteOffset": offsets["ibm"][0], "byteLength": offsets["ibm"][1]},
            # 3: JOINTS_0
            {"buffer": 0, "byteOffset": offsets["jts"][0], "byteLength": offsets["jts"][1], "target": 34962},
            # 4: WEIGHTS_0
            {"buffer": 0, "byteOffset": offsets["wts"][0], "byteLength": offsets["wts"][1], "target": 34962},
        ],
        "accessors": [
            # 0: POSITION vec3 float
            {"bufferView": 0, "componentType": 5126, "count": 4, "type": "VEC3",
             "min": [0, 0, 0], "max": [1, 1, 0]},
            # 1: indices ushort
            {"bufferView": 1, "componentType": 5123, "count": 6, "type": "SCALAR"},
            # 2: IBM mat4 float
            {"bufferView": 2, "componentType": 5126, "count": 2, "type": "MAT4"},
            # 3: JOINTS_0 vec4 ubyte (NOT normalized)
            {"bufferView": 3, "componentType": 5121, "count": 4, "type": "VEC4"},
            # 4: WEIGHTS_0 vec4 ubyte normalized
            {"bufferView": 4, "componentType": 5121, "count": 4, "type": "VEC4", "normalized": True},
        ],
    }

    # --- GLB container ------------------------------------------------------
    json_text = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    # JSON chunk padded with spaces (0x20) to 4-byte boundary
    pad = (-len(json_text)) % 4
    json_text += b"\x20" * pad
    # BIN chunk padded with zeros
    pad = (-len(bin_chunk)) % 4
    bin_chunk_padded = bin_chunk + (b"\x00" * pad)

    total_len = 12 + 8 + len(json_text) + 8 + len(bin_chunk_padded)
    glb = b""
    glb += struct.pack("<III", 0x46546C67, 2, total_len)
    glb += struct.pack("<II", len(json_text), 0x4E4F534A) + json_text
    glb += struct.pack("<II", len(bin_chunk_padded), 0x004E4942) + bin_chunk_padded

    return glb, expected_weights_floats, joints_per_vert


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

def _approx_equal(a, b, eps=1e-5):
    return abs(a - b) <= eps


def run():
    print("Building synthetic skinned glb...")
    glb_bytes, expected_weights, expected_joints = build_test_glb()
    print("  glb size: %d bytes" % len(glb_bytes))

    print("Parsing with _gltf.loads()...")
    g = _gltf.loads(glb_bytes, base_dir="")
    assert g.bin is not None, "BIN chunk missing"
    assert len(g.doc.get("skins", [])) == 1, "expected one skin in parsed doc"

    print("Reading skin 0...")
    skin = g.read_skin(0)
    print("  skin:", skin)

    assert skin.joint_node_indices == [1, 2], (
        "joint_node_indices: expected [1, 2], got %r" % (skin.joint_node_indices,)
    )
    assert skin.skeleton_node_index == 1, (
        "skeleton: expected 1, got %r" % (skin.skeleton_node_index,)
    )
    assert len(skin.inverse_bind_matrices) == 2, (
        "expected 2 IBMs, got %d" % len(skin.inverse_bind_matrices)
    )

    ibm0 = skin.inverse_bind_matrices[0]
    ibm1 = skin.inverse_bind_matrices[1]
    # IBM 0 should be identity
    assert _approx_equal(ibm0[0], 1.0) and _approx_equal(ibm0[5], 1.0)
    assert _approx_equal(ibm0[12], 0.0) and _approx_equal(ibm0[13], 0.0)
    # IBM 1 should be translate(-1, 0, 0): column-major, translation at [12..14]
    assert _approx_equal(ibm1[12], -1.0), (
        "IBM1 translation X: expected -1, got %r" % ibm1[12]
    )

    print("Reading JOINTS_0 + WEIGHTS_0 accessors...")
    mesh0 = g.doc["meshes"][0]
    attrs = mesh0["primitives"][0]["attributes"]

    joints = g.read_accessor(attrs["JOINTS_0"])
    weights = g.read_accessor(attrs["WEIGHTS_0"])

    print("  joints:", joints)
    print("  weights:", weights)

    assert len(joints) == 4
    assert len(weights) == 4
    for v, (got_j, exp_j) in enumerate(zip(joints, expected_joints)):
        assert tuple(int(x) for x in got_j) == exp_j, (
            "joints vtx[%d]: expected %r, got %r" % (v, exp_j, got_j)
        )
    for v, (got_w, exp_w) in enumerate(zip(weights, expected_weights)):
        for slot in range(4):
            assert _approx_equal(got_w[slot], exp_w[slot]), (
                "weights vtx[%d] slot[%d]: expected %r, got %r"
                % (v, slot, exp_w[slot], got_w[slot])
            )

    # Sanity: positions decode correctly (the existing path)
    positions = g.read_accessor(attrs["POSITION"])
    assert len(positions) == 4
    assert tuple(positions[1]) == (1.0, 0.0, 0.0)

    indices = g.read_accessor(mesh0["primitives"][0]["indices"])
    assert indices == [0, 1, 2, 1, 3, 2]

    print("")
    print("PASS: skin parser produces correct joints, IBMs, and normalized weights")


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("FAIL:", e)
        sys.exit(1)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("ERROR:", e)
        sys.exit(2)
