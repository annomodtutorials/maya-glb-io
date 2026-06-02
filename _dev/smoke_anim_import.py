"""Maya-free smoke test for glTF animation PARSING (_gltf.read_animations()).

Run from system Python (any 3.7+):
    python _dev\smoke_anim_import.py

What it does (no Maya in the loop):
  1. Hand-builds a minimal glTF dict in memory: one node, one animation with a
     single translation channel (LINEAR) holding 2 keyframes. Accessor data is
     packed into a base64 data: URI buffer so read_accessor can decode it.
  2. Wraps the dict as a Gltf (no .glb container, no BIN chunk) and calls
     read_animations().
  3. Asserts the decoded shape: animation count, name, channel count, target
     node, path, interpolation, decoded times, and decoded VEC3 values.

The Maya APPLIER (_anim.apply_animations) cannot be exercised without Maya, so
this only covers the parser layer — the same split as smoke_parse.py.

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


# Two keyframes: t=0s -> (0,0,0), t=1s -> (10,20,30)
TIMES = [0.0, 1.0]
VALUES = [
    (0.0, 0.0, 0.0),
    (10.0, 20.0, 30.0),
]


def _approx(a, b, eps=1e-5):
    return abs(a - b) <= eps


def _build_gltf():
    """Build a minimal glTF doc with one animated translation node.

    Buffer layout:
      [0]  TIMES   : 2 floats  (8 bytes)  -> SCALAR input accessor
      [8]  VALUES  : 6 floats  (24 bytes) -> VEC3 output accessor
    """
    times_bytes = struct.pack("<%df" % len(TIMES), *TIMES)
    values_flat = [c for v in VALUES for c in v]
    values_bytes = struct.pack("<%df" % len(values_flat), *values_flat)
    buf = times_bytes + values_bytes
    uri = "data:application/octet-stream;base64," + base64.b64encode(buf).decode("ascii")

    doc = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "anim_node"}],
        "buffers": [{"byteLength": len(buf), "uri": uri}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(times_bytes)},
            {"buffer": 0, "byteOffset": len(times_bytes), "byteLength": len(values_bytes)},
        ],
        "accessors": [
            {
                "bufferView": 0, "byteOffset": 0,
                "componentType": 5126, "count": len(TIMES), "type": "SCALAR",
            },
            {
                "bufferView": 1, "byteOffset": 0,
                "componentType": 5126, "count": len(VALUES), "type": "VEC3",
            },
        ],
        "animations": [
            {
                "name": "Move",
                "samplers": [
                    {"input": 0, "output": 1, "interpolation": "LINEAR"},
                ],
                "channels": [
                    {"sampler": 0, "target": {"node": 0, "path": "translation"}},
                ],
            }
        ],
    }
    return doc


def main():
    doc = _build_gltf()
    g = _gltf.Gltf(doc, None, "")

    anims = g.read_animations()
    print("animations decoded:", len(anims))
    assert len(anims) == 1, "expected 1 animation, got %d" % len(anims)

    anim = anims[0]
    assert anim.name == "Move", "animation name mismatch: %r" % anim.name
    assert len(anim.channels) == 1, "expected 1 channel, got %d" % len(anim.channels)

    ch = anim.channels[0]
    print("channel:", ch)
    assert ch.target_node == 0, "target_node mismatch: %r" % ch.target_node
    assert ch.path == "translation", "path mismatch: %r" % ch.path
    assert ch.interpolation == "LINEAR", "interpolation mismatch: %r" % ch.interpolation

    # times: flat SCALAR list
    assert len(ch.times) == len(TIMES), "times count mismatch: %d" % len(ch.times)
    for got, want in zip(ch.times, TIMES):
        assert _approx(got, want), "time mismatch: %s vs %s" % (got, want)

    # values: list of VEC3 tuples
    assert len(ch.values) == len(VALUES), "values count mismatch: %d" % len(ch.values)
    for got_v, want_v in zip(ch.values, VALUES):
        assert len(got_v) == 3, "value not VEC3: %r" % (got_v,)
        for gc, wc in zip(got_v, want_v):
            assert _approx(gc, wc), "value comp mismatch: %s vs %s" % (gc, wc)

    print("  times :", list(ch.times))
    print("  values:", [tuple(v) for v in ch.values])

    # --- extra coverage: channel with no target node is dropped -------------
    doc2 = _build_gltf()
    doc2["animations"][0]["channels"].append(
        {"sampler": 0, "target": {"path": "translation"}}  # no "node"
    )
    g2 = _gltf.Gltf(doc2, None, "")
    anim2 = g2.read_animations()[0]
    assert len(anim2.channels) == 1, \
        "channel with no target node should be dropped (got %d)" % len(anim2.channels)
    print("targetless channel correctly dropped")

    # --- extra coverage: empty doc -> empty list (Anno no-op path) ----------
    g3 = _gltf.Gltf({"asset": {"version": "2.0"}}, None, "")
    assert g3.read_animations() == [], "no-animations doc should return []"
    print("no-animations doc returns [] (Anno no-op path OK)")

    print()
    print("SUCCESS")


if __name__ == "__main__":
    main()
