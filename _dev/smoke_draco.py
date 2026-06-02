"""Maya-free smoke test for KHR_mesh_quantization + KHR_draco_mesh_compression.

Run from system Python (any 3.7+), from the maya_glb_io dir:
    python _dev\smoke_draco.py

PART A (always runs, no dependency):
  KHR_mesh_quantization stores POSITION/NORMAL/TANGENT/TEXCOORD as small
  integer types (BYTE/UBYTE/SHORT/USHORT). The integer component types and the
  `normalized` flag are decoded by _gltf.read_accessor. This test hand-builds a
  normalized SHORT VEC3 accessor and asserts it dequantizes to the expected
  floats (signed SHORT -> [-1, 1] via /32767, clamped). Also checks a
  normalized SHORT VEC2 read through an interleaved byteStride to confirm
  stride + normalize still cooperate.

PART B (Draco):
  If DracoPy is importable, encode a tiny mesh, wrap it as a glTF
  KHR_draco_mesh_compression primitive, and assert decode_draco_primitive
  reconstructs POSITION / indices / TEXCOORD_0 / NORMAL. If DracoPy is NOT
  installed (and can't be auto-installed offline), we DON'T hard-fail — we only
  assert that primitive_has_draco detects the extension and that
  decode_draco_primitive raises a clean catchable DracoError, then print a
  skip note. This mirrors the optional-dependency contract in the importer.

Prints SUCCESS at the end; raises (non-zero exit) on any real mismatch.
"""
import base64
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, SCRIPTS)

from maya_glb_io import _gltf   # noqa: E402
from maya_glb_io import _draco  # noqa: E402


def _approx(a, b, eps=1e-4):
    return abs(a - b) <= eps


# ---------------------------------------------------------------------------
# PART A — KHR_mesh_quantization (normalized integer accessor dequantization)
# ---------------------------------------------------------------------------

def test_quantization_normalized_short_vec3():
    """A normalized SHORT VEC3 accessor must dequantize to the spec floats."""
    # Three vertices, each a VEC3 of signed SHORTs. Per spec normalized signed
    # SHORT -> f = max(c / 32767, -1).
    raw = [
        (32767, 0, -32767),       # -> (1.0, 0.0, -1.0)
        (16384, -16384, 32767),   # -> (~0.5, ~-0.5, 1.0)
        (-32768, 100, -100),      # -> (-1.0 clamped, ~0.00305, ~-0.00305)
    ]
    flat = [c for v in raw for c in v]
    data = struct.pack("<%dh" % len(flat), *flat)
    uri = "data:application/octet-stream;base64," + base64.b64encode(data).decode("ascii")

    doc = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(data), "uri": uri}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(data)}],
        "accessors": [
            {
                "bufferView": 0, "byteOffset": 0,
                "componentType": 5122,   # SHORT
                "normalized": True,
                "count": len(raw), "type": "VEC3",
            },
        ],
    }
    g = _gltf.Gltf(doc, None, "")
    decoded = g.read_accessor(0)

    expected = [
        (1.0, 0.0, -1.0),
        (16384 / 32767.0, -16384 / 32767.0, 1.0),
        (-1.0, 100 / 32767.0, -100 / 32767.0),
    ]
    assert len(decoded) == len(expected), "vec3 count mismatch: %d" % len(decoded)
    for got, want in zip(decoded, expected):
        assert len(got) == 3, "not a VEC3: %r" % (got,)
        for gc, wc in zip(got, want):
            assert _approx(gc, wc), "quant VEC3 mismatch: %s vs %s" % (gc, wc)
    print("PART A: normalized SHORT VEC3 dequantizes correctly")
    print("  decoded[0]:", tuple(round(c, 5) for c in decoded[0]))
    print("  decoded[1]:", tuple(round(c, 5) for c in decoded[1]))


def test_quantization_normalized_short_vec2_interleaved():
    """Normalized SHORT VEC2 read through a byteStride (interleaved) layout."""
    # Two attributes interleaved per vertex: a SHORT VEC2 (the one we read) plus
    # 4 padding bytes, giving an 8-byte stride. Read accessor must honor BOTH the
    # stride and the normalize.
    verts = [
        (32767, -32767),   # -> (1.0, -1.0)
        (0, 16384),        # -> (0.0, ~0.5)
    ]
    stride = 8  # 2 SHORTs (4 bytes) + 4 pad bytes
    buf = bytearray()
    for (a, b) in verts:
        buf += struct.pack("<hh", a, b)
        buf += b"\x00\x00\x00\x00"  # padding to the stride
    data = bytes(buf)
    uri = "data:application/octet-stream;base64," + base64.b64encode(data).decode("ascii")

    doc = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(data), "uri": uri}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(data), "byteStride": stride},
        ],
        "accessors": [
            {
                "bufferView": 0, "byteOffset": 0,
                "componentType": 5122, "normalized": True,
                "count": len(verts), "type": "VEC2",
            },
        ],
    }
    g = _gltf.Gltf(doc, None, "")
    decoded = g.read_accessor(0)
    expected = [(1.0, -1.0), (0.0, 16384 / 32767.0)]
    assert len(decoded) == len(expected), "vec2 count mismatch: %d" % len(decoded)
    for got, want in zip(decoded, expected):
        for gc, wc in zip(got, want):
            assert _approx(gc, wc), "quant VEC2 (strided) mismatch: %s vs %s" % (gc, wc)
    print("PART A: normalized SHORT VEC2 through byteStride dequantizes correctly")


# ---------------------------------------------------------------------------
# PART B — KHR_draco_mesh_compression
# ---------------------------------------------------------------------------

def _make_draco_glb_doc():
    """Return (doc, dracopy_or_None). If DracoPy is importable, builds a real
    Draco bitstream for a 4-vertex quad (2 triangles) with UVs + normals and a
    glTF primitive referencing it. Otherwise returns (doc_without_buffer, None)
    so the absent-dependency branch can still test detection + clean error."""
    try:
        import DracoPy
        import numpy as np
    except Exception:
        # No DracoPy — still produce a doc with the extension + a (dummy)
        # bufferView so primitive_has_draco / decode_draco_primitive can be
        # exercised on the missing-dependency path.
        dummy = b"\x00" * 32
        uri = "data:application/octet-stream;base64," + base64.b64encode(dummy).decode("ascii")
        doc = {
            "asset": {"version": "2.0"},
            "buffers": [{"byteLength": len(dummy), "uri": uri}],
            "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(dummy)}],
        }
        return doc, None

    points = np.array([[0, 0, 0], [2, 0, 0], [0, 3, 0], [2, 3, 0]], dtype=np.float64)
    faces = np.array([[0, 1, 2], [1, 3, 2]], dtype=np.uint32)
    tex = np.array([[0, 0], [1, 0], [0, 1], [1, 1]], dtype=np.float64)
    nrm = np.array([[0, 0, 1]] * 4, dtype=np.float64)
    # Lossless-ish: high quantization bits so the round-trip is near-exact.
    enc = DracoPy.encode(points, faces, tex_coord=tex, normals=nrm, quantization_bits=16)

    uri = "data:application/octet-stream;base64," + base64.b64encode(enc).decode("ascii")
    doc = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(enc), "uri": uri}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(enc)}],
    }
    return doc, DracoPy


def test_draco():
    doc, dracopy = _make_draco_glb_doc()

    prim = {
        "extensions": {
            "KHR_draco_mesh_compression": {
                "bufferView": 0,
                "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
            }
        },
        # Fallback accessor metadata that a real exporter would emit — we must
        # IGNORE these and use the Draco stream instead.
        "attributes": {"POSITION": 99, "NORMAL": 99, "TEXCOORD_0": 99},
        "indices": 99,
    }

    # Detection works regardless of whether DracoPy is present.
    assert _gltf.Gltf.primitive_has_draco(prim), "primitive_has_draco should be True"
    assert not _gltf.Gltf.primitive_has_draco({"attributes": {"POSITION": 0}}), \
        "non-Draco primitive falsely detected as Draco"
    print("PART B: primitive_has_draco detects the extension")

    g = _gltf.Gltf(doc, None, "")

    if dracopy is None:
        # Optional dependency absent — assert the clean, catchable error and skip
        # the real decode without failing the suite.
        try:
            g.decode_draco_primitive(prim)
        except _draco.DracoError as exc:
            print("PART B: DracoPy not installed -> clean DracoError raised:")
            print("        ", str(exc)[:80])
            print("PART B: SKIPPED real decode (install DracoPy to exercise it)")
            return
        raise AssertionError(
            "decode_draco_primitive should raise DracoError when DracoPy absent"
        )

    # DracoPy present — decode for real and verify the reconstruction.
    decoded = g.decode_draco_primitive(prim)
    pos = decoded["POSITION"]
    idx = decoded["indices"]
    uvs = decoded["TEXCOORD_0"]
    nrm = decoded["NORMAL"]

    print("PART B: decoded POSITION count:", len(pos))
    print("        decoded indices:", idx)
    print("        decoded TEXCOORD_0 count:", None if uvs is None else len(uvs))
    print("        decoded NORMAL   count:", None if nrm is None else len(nrm))

    assert len(pos) == 4, "expected 4 decoded vertices, got %d" % len(pos)
    assert len(idx) == 6, "expected 6 flat indices (2 tris), got %d" % len(idx)
    # Index set must reference all 4 vertices and stay in range.
    assert max(idx) == 3 and min(idx) == 0, "index range wrong: %r" % idx
    assert uvs is not None and len(uvs) == 4, "TEXCOORD_0 not reconstructed"
    assert nrm is not None and len(nrm) == 4, "NORMAL not reconstructed"

    # The decoded vertex SET must match the input set (Draco may reorder
    # vertices, so compare as rounded sets rather than positionally).
    want = {(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 3.0, 0.0), (2.0, 3.0, 0.0)}
    got = {tuple(round(c, 3) for c in p) for p in pos}
    assert got == want, "decoded positions mismatch:\n  got %s\n  want %s" % (got, want)

    # Each decoded vertex carries a +Z normal (within tolerance).
    for n in nrm:
        assert _approx(n[2], 1.0, eps=1e-2), "normal not ~+Z: %r" % (n,)

    # When the extension's attribute map omits a semantic, it must come back None
    # even though DracoPy might decode it.
    prim_nouv = {
        "extensions": {
            "KHR_draco_mesh_compression": {
                "bufferView": 0,
                "attributes": {"POSITION": 0},  # NORMAL/TEXCOORD_0 not declared
            }
        }
    }
    d2 = g.decode_draco_primitive(prim_nouv)
    assert d2["TEXCOORD_0"] is None, "undeclared TEXCOORD_0 should be None"
    assert d2["NORMAL"] is None, "undeclared NORMAL should be None"
    assert len(d2["POSITION"]) == 4, "POSITION still required when only it is declared"
    print("PART B: undeclared semantics correctly nulled per the attributes map")
    print("PART B: full Draco round-trip OK (DracoPy %s)"
          % getattr(dracopy, "__version__", "?"))


def main():
    test_quantization_normalized_short_vec3()
    test_quantization_normalized_short_vec2_interleaved()
    test_draco()
    print()
    print("SUCCESS")


if __name__ == "__main__":
    main()
