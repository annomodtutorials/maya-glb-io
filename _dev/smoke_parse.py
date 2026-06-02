"""Maya-free smoke test for _gltf.py.

Run from system Python (any 3.7+):
    python _dev\smoke_parse.py

Loads every .glb in ../test_assets/ and prints a summary so we can catch
header/accessor/image bugs before booting Maya.
"""
import os
import sys

# Add scripts/ to path so we can import maya_glb_io._gltf
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, SCRIPTS)

from maya_glb_io import _gltf  # noqa: E402


def summarise(path):
    print("=" * 70)
    print("FILE:", path)
    try:
        g = _gltf.load(path)
    except Exception as e:
        print("  PARSE FAILED:", e)
        import traceback
        traceback.print_exc()
        return

    doc = g.doc
    print("  asset:", doc.get("asset", {}))
    print("  bin chunk:", "present (%d bytes)" % len(g.bin) if g.bin else "absent")
    print("  buffers:    %d" % len(doc.get("buffers", [])))
    print("  bufferViews:%d" % len(doc.get("bufferViews", [])))
    print("  accessors:  %d" % len(doc.get("accessors", [])))
    print("  images:     %d" % len(doc.get("images", [])))
    print("  textures:   %d" % len(doc.get("textures", [])))
    print("  materials:  %d" % len(doc.get("materials", [])))
    print("  meshes:     %d" % len(doc.get("meshes", [])))
    print("  nodes:      %d" % len(doc.get("nodes", [])))
    print("  scenes:     %d" % len(doc.get("scenes", [])))
    print("  animations: %d" % len(doc.get("animations", [])))
    print("  skins:      %d" % len(doc.get("skins", [])))
    print("  cameras:    %d" % len(doc.get("cameras", [])))
    print("  extensionsUsed:     ", doc.get("extensionsUsed", []))
    print("  extensionsRequired: ", doc.get("extensionsRequired", []))

    # Drill into first mesh + material to verify accessors decode
    if doc.get("meshes"):
        m0 = doc["meshes"][0]
        print("  --- mesh[0] ---")
        print("    name:", m0.get("name"))
        for i, prim in enumerate(m0.get("primitives", [])):
            print("    primitive[%d]:" % i)
            attrs = prim.get("attributes", {})
            print("      attributes:", list(attrs.keys()))
            print("      material:", prim.get("material"))
            print("      mode:", prim.get("mode", 4))
            # Try to read POSITION
            if "POSITION" in attrs:
                try:
                    pos = g.read_accessor(attrs["POSITION"])
                    print("      POSITION count:", len(pos))
                    if pos:
                        print("      POSITION[0]:", pos[0])
                        print("      POSITION[-1]:", pos[-1])
                except Exception as e:
                    print("      POSITION decode FAILED:", e)
            if "indices" in prim:
                try:
                    idx = g.read_accessor(prim["indices"])
                    print("      indices count:", len(idx), "max:", max(idx) if idx else "-")
                except Exception as e:
                    print("      indices decode FAILED:", e)

    if doc.get("materials"):
        print("  --- material[0] ---")
        print("    ", doc["materials"][0])

    if doc.get("images"):
        print("  --- image[0] ---")
        try:
            data, mime, ext = g.get_image_bytes(0)
            print("    mime:", mime, "ext:", ext, "bytes:", len(data))
        except Exception as e:
            print("    image[0] decode FAILED:", e)


def main():
    asset_dir = os.path.normpath(os.path.join(HERE, "..", "test_assets"))
    if not os.path.isdir(asset_dir):
        print("No test_assets directory:", asset_dir)
        sys.exit(1)

    for name in sorted(os.listdir(asset_dir)):
        if name.lower().endswith((".glb", ".gltf")):
            summarise(os.path.join(asset_dir, name))


if __name__ == "__main__":
    main()
