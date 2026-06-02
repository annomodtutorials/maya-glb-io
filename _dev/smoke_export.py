"""Maya-free smoke test for the glTF/GLB *exporter* (_glb_export.py).

Run from system Python (any 3.7+):
    python _dev\smoke_export.py

What it does (no Maya in the loop):
  1. Hand-builds ONE triangle primitive (POSITION/NORMAL/TANGENT/TEXCOORD_0/
     COLOR_0 + indices) and a MaterialDef with a base-color factor + an
     external-reference texture.
  2. Builds a glTF doc + BIN via the pure helpers and writes a real .glb.
  3. Re-parses that .glb with the plug-in's OWN reader (_gltf.loads) and
     asserts the round-trip: positions, accessor counts, material name/factor,
     primitive material binding, texture/image wiring.
  4. Also round-trips a .gltf + .bin to exercise the JSON-with-sidecar path.

Prints SUCCESS on full agreement; raises (non-zero exit) on any mismatch.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, SCRIPTS)

from maya_glb_io import _glb_export as GX   # noqa: E402
from maya_glb_io import _gltf               # noqa: E402


# Hand-built triangle geometry (flat arrays, unwelded — 3 verts, indices 0,1,2)
TRI_POSITIONS = [
    0.0, 0.0, 0.0,
    1.0, 0.0, 0.0,
    0.0, 1.0, 0.0,
]
TRI_NORMALS = [
    0.0, 0.0, 1.0,
    0.0, 0.0, 1.0,
    0.0, 0.0, 1.0,
]
TRI_TANGENTS = [
    1.0, 0.0, 0.0, 1.0,
    1.0, 0.0, 0.0, 1.0,
    1.0, 0.0, 0.0, 1.0,
]
TRI_UVS = [
    0.0, 1.0,
    1.0, 1.0,
    0.0, 0.0,
]
TRI_COLORS = [
    1.0, 0.0, 0.0, 1.0,
    0.0, 1.0, 0.0, 1.0,
    0.0, 0.0, 1.0, 1.0,
]
TRI_INDICES = [0, 1, 2]


def _approx(a, b, eps=1e-5):
    return abs(a - b) <= eps


def _build_descriptors():
    prim = GX.Primitive(
        positions=list(TRI_POSITIONS),
        indices=list(TRI_INDICES),
        normals=list(TRI_NORMALS),
        tangents=list(TRI_TANGENTS),
        texcoords=list(TRI_UVS),
        colors=list(TRI_COLORS),
        material_index=0,
    )
    mesh = GX.MeshDef(name="tri_mesh", primitives=[prim])

    mat = GX.MaterialDef(name="smoke_mat")
    mat.base_color_factor = [0.25, 0.5, 0.75, 1.0]
    mat.metallic_factor = 0.0
    mat.roughness_factor = 0.8
    # Reference-mode texture (external uri set via ImageRef.name)
    img = GX.ImageRef(file_path=None, mime="image/png", name="smoke_basecolor.png")
    mat.base_color_texture = GX.TextureRef(img)

    node = GX.Node(name="tri_node", mesh_index=0, translation=(1.0, 2.0, 3.0))

    return [node], [mesh], [mat], [0]


def _check_roundtrip(g, label):
    doc = g.doc
    print("  [%s] parsing..." % label)

    # asset
    assert doc["asset"]["version"] == "2.0", "asset.version != 2.0"

    # scene + node
    assert doc["scene"] == 0, "scene index != 0"
    assert doc["scenes"][0]["nodes"] == [0], "scene root nodes mismatch"
    node = doc["nodes"][0]
    assert node.get("name") == "tri_node", "node name mismatch"
    assert node.get("mesh") == 0, "node.mesh != 0"
    assert node.get("translation") == [1.0, 2.0, 3.0], \
        "node translation mismatch: %r" % node.get("translation")

    # mesh / primitive
    mesh = doc["meshes"][0]
    assert mesh.get("name") == "tri_mesh", "mesh name mismatch"
    prim = mesh["primitives"][0]
    attrs = prim["attributes"]
    for needed in ("POSITION", "NORMAL", "TANGENT", "TEXCOORD_0", "COLOR_0"):
        assert needed in attrs, "primitive missing attribute %s" % needed
    assert prim.get("mode", 4) == 4, "primitive mode != TRIANGLES"
    assert prim.get("material") == 0, "primitive.material != 0"
    assert "indices" in prim, "primitive has no indices"

    # POSITION accessor: count, values, min/max
    pos = g.read_accessor(attrs["POSITION"])
    assert len(pos) == 3, "POSITION count != 3 (got %d)" % len(pos)
    flat = [c for v in pos for c in v]
    for got, want in zip(flat, TRI_POSITIONS):
        assert _approx(got, want), "POSITION value mismatch: %s vs %s" % (got, want)

    pos_acc = doc["accessors"][attrs["POSITION"]]
    assert "min" in pos_acc and "max" in pos_acc, "POSITION accessor lacks min/max"
    assert pos_acc["min"] == [0.0, 0.0, 0.0], "POSITION min mismatch: %r" % pos_acc["min"]
    assert pos_acc["max"] == [1.0, 1.0, 0.0], "POSITION max mismatch: %r" % pos_acc["max"]

    # NORMAL / TANGENT / TEXCOORD_0 / COLOR_0 counts + tangent vec4
    nrm = g.read_accessor(attrs["NORMAL"])
    assert len(nrm) == 3, "NORMAL count != 3"
    tan = g.read_accessor(attrs["TANGENT"])
    assert len(tan) == 3 and len(tan[0]) == 4, "TANGENT not vec4 x3"
    uv = g.read_accessor(attrs["TEXCOORD_0"])
    assert len(uv) == 3 and len(uv[0]) == 2, "TEXCOORD_0 not vec2 x3"
    col = g.read_accessor(attrs["COLOR_0"])
    assert len(col) == 3 and len(col[0]) == 4, "COLOR_0 not vec4 x3"

    # indices
    idx = g.read_accessor(prim["indices"])
    assert list(idx) == TRI_INDICES, "indices mismatch: %r" % list(idx)

    # material
    mat = doc["materials"][0]
    assert mat.get("name") == "smoke_mat", "material name mismatch"
    pbr = mat["pbrMetallicRoughness"]
    bcf = pbr["baseColorFactor"]
    for got, want in zip(bcf, [0.25, 0.5, 0.75, 1.0]):
        assert _approx(got, want), "baseColorFactor mismatch: %s vs %s" % (got, want)
    assert _approx(pbr.get("roughnessFactor", 1.0), 0.8), "roughnessFactor mismatch"
    # metallicFactor 0.0 != default 1.0 -> must be present
    assert _approx(pbr.get("metallicFactor", 1.0), 0.0), "metallicFactor mismatch"
    assert "baseColorTexture" in pbr, "material has no baseColorTexture"
    tex_idx = pbr["baseColorTexture"]["index"]

    # texture -> sampler + image
    tex = doc["textures"][tex_idx]
    assert "source" in tex, "texture has no source image"
    img = doc["images"][tex["source"]]
    assert img.get("uri") == "smoke_basecolor.png", \
        "image uri mismatch: %r" % img.get("uri")

    print("  [%s] round-trip OK (3 verts, 1 material, 1 texture)" % label)


def main():
    nodes, meshes, materials, roots = _build_descriptors()

    out_dir = os.path.join(HERE, "_smoke_out")
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    # --- .glb path ---------------------------------------------------------
    glb_path = os.path.join(out_dir, "smoke_export.glb")
    doc, bin_bytes = GX.build_gltf_doc(
        nodes, meshes, materials, roots,
        embed_images=False, gltf_external_bin=False,
    )
    GX.write_glb(glb_path, doc, bin_bytes)
    print("wrote", glb_path, "(%d bytes)" % os.path.getsize(glb_path))

    with open(glb_path, "rb") as f:
        g_glb = _gltf.loads(f.read(), base_dir=out_dir)
    _check_roundtrip(g_glb, "glb")

    # Verify the glb header is sane (magic + version) by re-reading raw bytes.
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
    gltf_path = os.path.join(out_dir, "smoke_export.gltf")
    written = GX.export_to_file(
        gltf_path, nodes, meshes, materials, roots,
        fmt="gltf", embed_images=False,
    )
    print("wrote", written)
    bin_sidecar = os.path.join(out_dir, "smoke_export.bin")
    assert os.path.isfile(bin_sidecar), "external .bin sidecar not written"

    g_gltf = _gltf.load(gltf_path)
    _check_roundtrip(g_gltf, "gltf")

    print()
    print("SUCCESS")


if __name__ == "__main__":
    main()
