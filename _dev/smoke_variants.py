"""Maya-free smoke test for KHR_materials_variants parsing in _gltf.py.

Run from system Python (any 3.7+), from the maya_glb_io dir:
    python _dev\smoke_variants.py

Hand-builds a minimal glTF doc with two material variants ("red"/"blue"),
two materials, and one mesh primitive that maps material 0 -> variant 0 and
material 1 -> variant 1, with material 0 as the base fallback. Then asserts the
parser exposes the variant names in order and resolves each variant id to the
right material index. Pure parser test — no Maya, no _build.
"""
import os
import sys

# Add scripts/ to path so we can import maya_glb_io._gltf
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, SCRIPTS)

from maya_glb_io import _gltf  # noqa: E402


def _build_variant_doc():
    """A minimal in-memory glTF document exercising KHR_materials_variants."""
    return {
        "asset": {"version": "2.0"},
        "extensionsUsed": ["KHR_materials_variants"],
        "extensions": {
            "KHR_materials_variants": {
                "variants": [{"name": "red"}, {"name": "blue"}],
            }
        },
        "materials": [
            {"name": "matRed"},
            {"name": "matBlue"},
        ],
        "meshes": [
            {
                "name": "sneaker",
                "primitives": [
                    {
                        "attributes": {"POSITION": 0},
                        "material": 0,  # base / fallback material
                        "extensions": {
                            "KHR_materials_variants": {
                                "mappings": [
                                    {"material": 0, "variants": [0]},
                                    {"material": 1, "variants": [1]},
                                ]
                            }
                        },
                    }
                ],
            }
        ],
    }


def _build_plain_doc():
    """A doc WITHOUT the extension — must report no variants (additive check)."""
    return {
        "asset": {"version": "2.0"},
        "materials": [{"name": "matOnly"}],
        "meshes": [
            {"primitives": [{"attributes": {"POSITION": 0}, "material": 0}]}
        ],
    }


def main():
    failures = []

    def check(cond, label):
        status = "ok  " if cond else "FAIL"
        print("  [%s] %s" % (status, label))
        if not cond:
            failures.append(label)

    print("=" * 70)
    print("KHR_materials_variants parser smoke test")
    print("=" * 70)

    # --- variant document ---------------------------------------------------
    g = _gltf.Gltf(_build_variant_doc(), None, "")

    names = g.variant_names()
    print("  variant_names():", names)
    check(names == ["red", "blue"], 'variant_names() == ["red", "blue"]')
    check(g.has_variants() is True, "has_variants() is True")

    prim = g.doc["meshes"][0]["primitives"][0]

    mappings = g.primitive_variant_mappings(prim)
    check(len(mappings) == 2, "primitive_variant_mappings returns 2 mappings")

    mat_v0 = g.primitive_variant_material(prim, 0)
    mat_v1 = g.primitive_variant_material(prim, 1)
    print("  variant 0 -> material", mat_v0)
    print("  variant 1 -> material", mat_v1)
    check(mat_v0 == 0, "variant id 0 resolves to material 0")
    check(mat_v1 == 1, "variant id 1 resolves to material 1")

    # An unmapped variant id falls through to None (caller uses base material).
    check(
        g.primitive_variant_material(prim, 99) is None,
        "unmapped variant id resolves to None",
    )
    check(prim.get("material") == 0, "primitive base material is 0 (fallback)")

    # --- plain document (no extension) -------------------------------------
    g2 = _gltf.Gltf(_build_plain_doc(), None, "")
    check(g2.variant_names() == [], "plain doc: variant_names() == []")
    check(g2.has_variants() is False, "plain doc: has_variants() is False")
    plain_prim = g2.doc["meshes"][0]["primitives"][0]
    check(
        g2.primitive_variant_mappings(plain_prim) == [],
        "plain doc: primitive_variant_mappings() == []",
    )
    check(
        g2.primitive_variant_material(plain_prim, 0) is None,
        "plain doc: primitive_variant_material() is None",
    )

    print("-" * 70)
    if failures:
        print("FAILURES (%d):" % len(failures))
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("SUCCESS")


if __name__ == "__main__":
    main()
