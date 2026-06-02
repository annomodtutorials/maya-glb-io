"""Maya-free smoke test for SKINNED + ANIMATED glTF/GLB export (_glb_export.py).

Run from system Python (any 3.7+):
    python _dev\smoke_skin_anim_export.py

What it does (no Maya in the loop):
  1. Hand-builds a 3-vertex primitive carrying JOINTS_0 (4 ints/vertex) and
     WEIGHTS_0 (4 floats/vertex) skin attributes, wrapped in a MeshDef.
  2. Builds a 3-node hierarchy: a skinned-mesh node plus two joint nodes, and a
     SkinDef referencing the two joints with two inverse-bind matrices (16
     column-major floats each).
  3. Builds an AnimationDef with one translation channel on a joint (3 keys).
  4. Writes a real .glb via the writer, re-parses it with the plug-in's OWN
     reader (_gltf.loads), and asserts the round-trip:
       * node.skin index wired,
       * skins[] present: joint count, IBM accessor decodes to 16 floats/joint,
       * JOINTS_0 decodes as UNSIGNED_SHORT VEC4, WEIGHTS_0 as FLOAT VEC4,
       * animations[]: one channel whose sampler input/output decode to the
         right counts, input accessor carries min/max.
  5. Also round-trips a .gltf + .bin to exercise the JSON-with-sidecar path.

Prints SUCCESS on full agreement; raises (non-zero exit) on any mismatch.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, SCRIPTS)

from maya_glb_io import _glb_export as GX   # noqa: E402
from maya_glb_io import _gltf               # noqa: E402


# Hand-built triangle geometry (flat arrays, unwelded — 3 verts).
TRI_POSITIONS = [
    0.0, 0.0, 0.0,
    1.0, 0.0, 0.0,
    0.0, 1.0, 0.0,
]
# 4 joint indices per vertex (into the skin's joints list: 0 or 1 here).
TRI_JOINTS = [
    0, 1, 0, 0,
    1, 0, 0, 0,
    0, 1, 0, 0,
]
# 4 weights per vertex (normalized).
TRI_WEIGHTS = [
    0.75, 0.25, 0.0, 0.0,
    1.0, 0.0, 0.0, 0.0,
    0.5, 0.5, 0.0, 0.0,
]
TRI_INDICES = [0, 1, 2]

# Two IBMs (identity, then a -1 X translation), already column-major.
IBM_0 = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
)
IBM_1 = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    -1.0, 0.0, 0.0, 1.0,
)

# One translation channel on joint node (3 keyframes).
ANIM_TIMES = [0.0, 0.5, 1.0]
ANIM_VALUES = [
    (0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 2.0, 0.0),
]


def _approx(a, b, eps=1e-5):
    return abs(a - b) <= eps


def _build_descriptors():
    prim = GX.Primitive(
        positions=list(TRI_POSITIONS),
        indices=list(TRI_INDICES),
        joints=list(TRI_JOINTS),
        weights=list(TRI_WEIGHTS),
    )
    mesh = GX.MeshDef(name="skinned_tri", primitives=[prim])

    # Nodes: 0 = skinned mesh, 1 = joint A (also the skeleton root), 2 = joint B.
    mesh_node = GX.Node(name="skinned_tri", mesh_index=0, skin_index=0)
    joint_a = GX.Node(name="jointA", children=[2])
    joint_b = GX.Node(name="jointB", translation=(1.0, 0.0, 0.0))
    nodes = [mesh_node, joint_a, joint_b]

    skin = GX.SkinDef(
        joint_node_indices=[1, 2],
        inverse_bind_matrices=[IBM_0, IBM_1],
        skeleton_node_index=1,
        name="tri_skin",
    )

    # Animate jointB's translation.
    chan = GX.AnimChannelDef(
        node_index=2, path="translation",
        times=list(ANIM_TIMES), values=list(ANIM_VALUES),
    )
    anim = GX.AnimationDef(name="take001", channels=[chan])

    # Scene roots: the mesh node and the joint root.
    roots = [0, 1]
    return nodes, [mesh], [], roots, [skin], [anim]


def _check_roundtrip(g, label):
    doc = g.doc
    print("  [%s] parsing..." % label)

    assert doc["asset"]["version"] == "2.0", "asset.version != 2.0"

    # --- node.skin wiring --------------------------------------------------
    mesh_node = doc["nodes"][0]
    assert mesh_node.get("mesh") == 0, "mesh node missing mesh"
    assert mesh_node.get("skin") == 0, \
        "mesh node skin index wrong: %r" % mesh_node.get("skin")

    # --- skins -------------------------------------------------------------
    assert "skins" in doc, "doc has no skins array"
    assert len(doc["skins"]) == 1, "expected 1 skin, got %d" % len(doc["skins"])
    skin = doc["skins"][0]
    assert skin.get("joints") == [1, 2], \
        "skin joints mismatch: %r" % skin.get("joints")
    assert skin.get("skeleton") == 1, "skin skeleton mismatch"
    assert skin.get("name") == "tri_skin", "skin name mismatch"

    # IBM accessor: FLOAT MAT4, 2 entries, decodes to 16 floats each.
    ibm_acc_idx = skin["inverseBindMatrices"]
    ibm_acc = doc["accessors"][ibm_acc_idx]
    assert ibm_acc["type"] == "MAT4", "IBM accessor not MAT4"
    assert ibm_acc["componentType"] == 5126, "IBM accessor not FLOAT"
    assert ibm_acc["count"] == 2, "IBM accessor count != 2"
    ibms = g.read_accessor(ibm_acc_idx)
    assert len(ibms) == 2, "decoded IBM count != 2"
    assert len(ibms[0]) == 16, "IBM[0] not 16 floats (got %d)" % len(ibms[0])
    for got, want in zip(ibms[0], IBM_0):
        assert _approx(got, want), "IBM[0] mismatch: %s vs %s" % (got, want)
    for got, want in zip(ibms[1], IBM_1):
        assert _approx(got, want), "IBM[1] mismatch: %s vs %s" % (got, want)

    # --- JOINTS_0 / WEIGHTS_0 ---------------------------------------------
    prim = doc["meshes"][0]["primitives"][0]
    attrs = prim["attributes"]
    assert "JOINTS_0" in attrs, "primitive missing JOINTS_0"
    assert "WEIGHTS_0" in attrs, "primitive missing WEIGHTS_0"

    j_acc = doc["accessors"][attrs["JOINTS_0"]]
    assert j_acc["componentType"] == 5123, "JOINTS_0 not UNSIGNED_SHORT"
    assert j_acc["type"] == "VEC4", "JOINTS_0 not VEC4"
    assert j_acc["count"] == 3, "JOINTS_0 count != 3"
    joints = g.read_accessor(attrs["JOINTS_0"])
    flat_j = [int(c) for v in joints for c in v]
    assert flat_j == TRI_JOINTS, "JOINTS_0 values mismatch: %r" % flat_j

    w_acc = doc["accessors"][attrs["WEIGHTS_0"]]
    assert w_acc["componentType"] == 5126, "WEIGHTS_0 not FLOAT"
    assert w_acc["type"] == "VEC4", "WEIGHTS_0 not VEC4"
    weights = g.read_accessor(attrs["WEIGHTS_0"])
    flat_w = [c for v in weights for c in v]
    for got, want in zip(flat_w, TRI_WEIGHTS):
        assert _approx(got, want), "WEIGHTS_0 mismatch: %s vs %s" % (got, want)

    # --- animations --------------------------------------------------------
    assert "animations" in doc, "doc has no animations array"
    assert len(doc["animations"]) == 1, "expected 1 animation"
    a = doc["animations"][0]
    assert a.get("name") == "take001", "animation name mismatch"
    assert len(a["channels"]) == 1, "expected 1 channel"
    assert len(a["samplers"]) == 1, "expected 1 sampler"

    ch = a["channels"][0]
    assert ch["target"]["node"] == 2, "channel target node mismatch"
    assert ch["target"]["path"] == "translation", "channel path mismatch"
    samp = a["samplers"][ch["sampler"]]
    assert samp.get("interpolation", "LINEAR") == "LINEAR", "interp mismatch"

    in_acc = doc["accessors"][samp["input"]]
    assert in_acc["type"] == "SCALAR", "anim input not SCALAR"
    assert in_acc["componentType"] == 5126, "anim input not FLOAT"
    assert in_acc["count"] == 3, "anim input count != 3"
    # glTF requires min/max on animation input accessors.
    assert "min" in in_acc and "max" in in_acc, "anim input lacks min/max"
    assert _approx(in_acc["min"][0], 0.0), "anim input min wrong"
    assert _approx(in_acc["max"][0], 1.0), "anim input max wrong"
    times = g.read_accessor(samp["input"])
    for got, want in zip(times, ANIM_TIMES):
        assert _approx(got, want), "anim time mismatch: %s vs %s" % (got, want)

    out_acc = doc["accessors"][samp["output"]]
    assert out_acc["type"] == "VEC3", "anim output not VEC3"
    assert out_acc["count"] == 3, "anim output count != 3"
    values = g.read_accessor(samp["output"])
    assert len(values) == 3, "anim output decoded count != 3"
    for got_v, want_v in zip(values, ANIM_VALUES):
        for got, want in zip(got_v, want_v):
            assert _approx(got, want), "anim value mismatch: %s vs %s" % (got, want)

    # --- exercise the reader's higher-level skin/animation decoders --------
    decoded_skin = g.read_skin(0)
    assert decoded_skin.joint_node_indices == [1, 2], "read_skin joints wrong"
    assert len(decoded_skin.inverse_bind_matrices) == 2, "read_skin IBM count"
    decoded_anims = g.read_animations()
    assert len(decoded_anims) == 1, "read_animations count != 1"
    assert len(decoded_anims[0].channels) == 1, "read_animations channels != 1"
    rch = decoded_anims[0].channels[0]
    assert rch.target_node == 2 and rch.path == "translation", \
        "read_animations channel target wrong"

    print("  [%s] round-trip OK (1 skin/2 joints, JOINTS/WEIGHTS, 1 anim)" % label)


def main():
    nodes, meshes, materials, roots, skins, animations = _build_descriptors()

    out_dir = os.path.join(HERE, "_smoke_out")
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    # --- .glb path ---------------------------------------------------------
    glb_path = os.path.join(out_dir, "smoke_skin_anim.glb")
    doc, bin_bytes = GX.build_gltf_doc(
        nodes, meshes, materials, roots,
        embed_images=False, gltf_external_bin=False,
        skins=skins, animations=animations,
    )
    GX.write_glb(glb_path, doc, bin_bytes)
    print("wrote", glb_path, "(%d bytes)" % os.path.getsize(glb_path))

    with open(glb_path, "rb") as f:
        g_glb = _gltf.loads(f.read(), base_dir=out_dir)
    _check_roundtrip(g_glb, "glb")

    # Header sanity.
    with open(glb_path, "rb") as f:
        head = f.read(12)
    import struct
    magic, version, length = struct.unpack("<III", head)
    assert magic == 0x46546C67, "glb magic wrong"
    assert version == 2, "glb version wrong"
    assert length == os.path.getsize(glb_path), \
        "glb header length %d != file size %d" % (length, os.path.getsize(glb_path))
    print("  [glb] header check OK (magic/version/length)")

    # --- .gltf + .bin path -------------------------------------------------
    gltf_path = os.path.join(out_dir, "smoke_skin_anim.gltf")
    written = GX.export_to_file(
        gltf_path, nodes, meshes, materials, roots,
        fmt="gltf", embed_images=False,
        skins=skins, animations=animations,
    )
    print("wrote", written)
    bin_sidecar = os.path.join(out_dir, "smoke_skin_anim.bin")
    assert os.path.isfile(bin_sidecar), "external .bin sidecar not written"

    g_gltf = _gltf.load(gltf_path)
    _check_roundtrip(g_gltf, "gltf")

    print()
    print("SUCCESS")


if __name__ == "__main__":
    main()
