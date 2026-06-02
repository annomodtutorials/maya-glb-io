"""Maya-free smoke test for glTF morph-target (blendshape) PARSING.

Run from system Python (any 3.7+):
    python _dev\smoke_morph_import.py

What it does (no Maya in the loop):
  1. Hand-builds a minimal glTF dict in memory: one mesh with one primitive that
     carries a base POSITION plus 2 morph TARGETS (each a POSITION delta, and
     target 1 also a NORMAL delta). The mesh has default `weights` and
     `extras.targetNames`. Accessor data is packed into a base64 data: URI
     buffer so read_accessor can decode it.
  2. Wraps the dict as a Gltf (no .glb container, no BIN chunk) and exercises
     the new parser helpers: primitive_morph_targets, primitive_target_count,
     mesh_morph_weights, node_morph_weights, mesh_target_names.
  3. Asserts the decoded shape: target count, decoded POSITION/NORMAL deltas,
     default + node-override weights, target names.
  4. Asserts a no-targets mesh returns empty lists everywhere.

The Maya BUILDER (_build blendShape construction) and APPLIER
(_anim weight keying) cannot be exercised without Maya, so this only covers the
parser layer — the same split as smoke_parse.py / smoke_anim_import.py.

Prints SUCCESS on full agreement; raises (non-zero exit) on any mismatch.
"""
import base64
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, SCRIPTS)

from maya_glb_io import _gltf  # noqa: E402


# Base geometry: a single triangle (3 verts).
BASE_POS = [
    (0.0, 0.0, 0.0),
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
]
# Target 0: shove every vertex +1 in X.
T0_POS = [
    (1.0, 0.0, 0.0),
    (1.0, 0.0, 0.0),
    (1.0, 0.0, 0.0),
]
# Target 1: lift every vertex +2 in Y, and a NORMAL delta too.
T1_POS = [
    (0.0, 2.0, 0.0),
    (0.0, 2.0, 0.0),
    (0.0, 2.0, 0.0),
]
T1_NRM = [
    (0.0, 0.0, 1.0),
    (0.0, 0.0, 1.0),
    (0.0, 0.0, 1.0),
]

DEFAULT_WEIGHTS = [0.25, 0.75]
NODE_WEIGHTS = [1.0, 0.0]
TARGET_NAMES = ["smile", "frown"]


def _approx(a, b, eps=1e-5):
    return abs(a - b) <= eps


def _pack_vec3(rows):
    flat = [c for r in rows for c in r]
    return struct.pack("<%df" % len(flat), *flat)


def _build_gltf():
    """Build a glTF doc with one morphed primitive + one plain primitive mesh."""
    # Buffer = base POS | T0 POS | T1 POS | T1 NRM, each a tight VEC3 block.
    blocks = [
        _pack_vec3(BASE_POS),
        _pack_vec3(T0_POS),
        _pack_vec3(T1_POS),
        _pack_vec3(T1_NRM),
    ]
    buf = b"".join(blocks)
    uri = "data:application/octet-stream;base64," + base64.b64encode(buf).decode("ascii")

    # bufferViews + accessors, one per block.
    buffer_views = []
    accessors = []
    offset = 0
    for blk in blocks:
        buffer_views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(blk)})
        accessors.append({
            "bufferView": len(buffer_views) - 1, "byteOffset": 0,
            "componentType": 5126, "count": 3, "type": "VEC3",
        })
        offset += len(blk)

    # accessor indices: 0=basePOS, 1=T0POS, 2=T1POS, 3=T1NRM
    doc = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0, 1]}],
        "nodes": [
            # Node 0 references the morphed mesh and OVERRIDES weights.
            {"name": "morph_node", "mesh": 0, "weights": list(NODE_WEIGHTS)},
            # Node 1 references the plain (no-targets) mesh.
            {"name": "plain_node", "mesh": 1},
        ],
        "buffers": [{"byteLength": len(buf), "uri": uri}],
        "bufferViews": buffer_views,
        "accessors": accessors,
        "meshes": [
            {
                "name": "morphed",
                "weights": list(DEFAULT_WEIGHTS),
                "extras": {"targetNames": list(TARGET_NAMES)},
                "primitives": [
                    {
                        "attributes": {"POSITION": 0},
                        "targets": [
                            {"POSITION": 1},
                            {"POSITION": 2, "NORMAL": 3},
                        ],
                    }
                ],
            },
            {
                "name": "plain",
                "primitives": [{"attributes": {"POSITION": 0}}],
            },
        ],
    }
    return doc


def main():
    doc = _build_gltf()
    g = _gltf.Gltf(doc, None, "")

    morphed = doc["meshes"][0]
    plain = doc["meshes"][1]
    prim = morphed["primitives"][0]
    plain_prim = plain["primitives"][0]

    # --- target count ------------------------------------------------------
    assert g.primitive_target_count(prim) == 2, \
        "expected 2 targets, got %d" % g.primitive_target_count(prim)
    assert g.primitive_target_count(plain_prim) == 0, \
        "plain primitive should have 0 targets"
    print("target counts OK (morphed=2, plain=0)")

    # --- decoded deltas ----------------------------------------------------
    targets = g.primitive_morph_targets(prim)
    assert len(targets) == 2, "expected 2 decoded targets, got %d" % len(targets)

    t0, t1 = targets
    assert "POSITION" in t0, "target 0 missing POSITION deltas"
    assert "NORMAL" not in t0, "target 0 should have no NORMAL deltas"
    assert "POSITION" in t1 and "NORMAL" in t1, "target 1 missing POSITION/NORMAL"

    for got, want in zip(t0["POSITION"], T0_POS):
        for gc, wc in zip(got, want):
            assert _approx(gc, wc), "T0 POSITION mismatch: %s vs %s" % (gc, wc)
    for got, want in zip(t1["POSITION"], T1_POS):
        for gc, wc in zip(got, want):
            assert _approx(gc, wc), "T1 POSITION mismatch: %s vs %s" % (gc, wc)
    for got, want in zip(t1["NORMAL"], T1_NRM):
        for gc, wc in zip(got, want):
            assert _approx(gc, wc), "T1 NORMAL mismatch: %s vs %s" % (gc, wc)
    print("decoded POSITION/NORMAL deltas OK")

    # plain primitive -> empty target list
    assert g.primitive_morph_targets(plain_prim) == [], \
        "plain primitive should decode to []"
    print("plain primitive decodes to [] OK")

    # --- weights -----------------------------------------------------------
    mw = g.mesh_morph_weights(morphed)
    assert len(mw) == 2, "expected 2 mesh weights, got %d" % len(mw)
    for got, want in zip(mw, DEFAULT_WEIGHTS):
        assert _approx(got, want), "mesh weight mismatch: %s vs %s" % (got, want)

    nw = g.node_morph_weights(doc["nodes"][0])
    assert len(nw) == 2, "expected 2 node weights, got %d" % len(nw)
    for got, want in zip(nw, NODE_WEIGHTS):
        assert _approx(got, want), "node weight mismatch: %s vs %s" % (got, want)

    assert g.mesh_morph_weights(plain) == [], "plain mesh should have no weights"
    assert g.node_morph_weights(doc["nodes"][1]) == [], \
        "plain node should have no weight override"
    print("default + node-override weights OK")

    # --- target names ------------------------------------------------------
    names = g.mesh_target_names(morphed)
    assert names == TARGET_NAMES, "target names mismatch: %r" % names
    assert g.mesh_target_names(plain) == [], "plain mesh should have no targetNames"
    print("target names OK:", names)

    # --- EXPORT round-trip (bonus): writer emits targets, reader decodes them
    _check_export_roundtrip()

    print()
    print("SUCCESS")


def _check_export_roundtrip():
    """Maya-free check of the morph EXPORT path: build a Primitive with morph
    deltas + a MeshDef with weights/targetNames, write a .glb via the plug-in's
    own writer, re-read it, and assert the parser helpers recover everything.

    Also writes a `weights` animation channel (flat keyframe-major SCALAR) and
    confirms it decodes back to the same flat values.
    """
    from maya_glb_io import _glb_export as GX  # noqa: E402

    # One-triangle base; two morph targets with POSITION deltas.
    base_pos = [c for v in BASE_POS for c in v]
    t0_delta = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0]   # +X everywhere
    t1_delta = [0.0, 2.0, 0.0, 0.0, 2.0, 0.0, 0.0, 2.0, 0.0]   # +2Y everywhere

    prim = GX.Primitive(
        positions=list(base_pos),
        indices=[0, 1, 2],
        morph_targets=[{"POSITION": t0_delta}, {"POSITION": t1_delta}],
    )
    mesh = GX.MeshDef(
        name="exp_morph",
        primitives=[prim],
        weights=list(DEFAULT_WEIGHTS),
        target_names=list(TARGET_NAMES),
    )
    node = GX.Node(name="exp_node", mesh_index=0)

    # A weights animation channel: 2 keyframes x 2 targets, keyframe-major.
    # k0 -> (0.1, 0.2), k1 -> (0.9, 0.8)
    anim = GX.AnimationDef(name="Morph", channels=[
        GX.AnimChannelDef(0, "weights", [0.0, 1.0],
                          [0.1, 0.2, 0.9, 0.8], "LINEAR"),
    ])

    doc, bin_bytes = GX.build_gltf_doc(
        [node], [mesh], [], [0],
        embed_images=False, gltf_external_bin=False, animations=[anim],
    )
    glb = GX.pack_glb(doc, bin_bytes)
    g = _gltf.loads(glb, base_dir="")

    rt_mesh = g.doc["meshes"][0]
    rt_prim = rt_mesh["primitives"][0]

    assert g.primitive_target_count(rt_prim) == 2, "exported target count != 2"
    rt_targets = g.primitive_morph_targets(rt_prim)
    for got, want in zip(rt_targets[0]["POSITION"],
                         [(1.0, 0.0, 0.0)] * 3):
        for gc, wc in zip(got, want):
            assert _approx(gc, wc), "exp T0 delta mismatch"
    for got, want in zip(rt_targets[1]["POSITION"],
                         [(0.0, 2.0, 0.0)] * 3):
        for gc, wc in zip(got, want):
            assert _approx(gc, wc), "exp T1 delta mismatch"

    rt_w = g.mesh_morph_weights(rt_mesh)
    for got, want in zip(rt_w, DEFAULT_WEIGHTS):
        assert _approx(got, want), "exp mesh weight mismatch"
    assert g.mesh_target_names(rt_mesh) == TARGET_NAMES, "exp target names mismatch"

    # weights animation: SCALAR flat output, keyframe-major.
    rt_anim = g.read_animations()[0]
    wch = [c for c in rt_anim.channels if c.path == "weights"]
    assert len(wch) == 1, "expected 1 weights channel, got %d" % len(wch)
    ch = wch[0]
    assert list(ch.times) == [0.0, 1.0], "weights times mismatch: %r" % ch.times
    for got, want in zip(ch.values, [0.1, 0.2, 0.9, 0.8]):
        assert _approx(got, want), "weights value mismatch: %s vs %s" % (got, want)

    print("EXPORT round-trip OK (targets, weights, targetNames, weights anim)")


if __name__ == "__main__":
    main()
