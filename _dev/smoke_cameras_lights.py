"""Maya-free smoke test for glTF CAMERA + LIGHT parsing (_gltf.py helpers).

Run from system Python (any 3.7+), from the maya_glb_io dir:
    python _dev\smoke_cameras_lights.py

What it does (no Maya in the loop):
  1. Hand-builds a minimal glTF dict in memory with:
       - cameras[0] : perspective (yfov/aspectRatio/znear/zfar)
       - cameras[1] : orthographic (xmag/ymag/znear/zfar)
       - KHR_lights_punctual.lights = [directional, point, spot]
       - nodes referencing each camera / light (plus a plain node with neither)
  2. Wraps the dict as a Gltf (no .glb container, no BIN chunk).
  3. Asserts camera_def() / node_camera() / light_defs() / light_def() /
     node_light() return the right types, fields, and resolved indices.
  4. Asserts the no-camera/no-light cases return None / [] (the Anno no-op path).

This covers ONLY the parser layer (same split as smoke_parse.py /
smoke_anim_import.py). The Maya builder (_build._build_camera / _build_light)
can't run without Maya.

Prints SUCCESS on full agreement; raises (non-zero exit) on any mismatch.
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, SCRIPTS)

from maya_glb_io import _gltf  # noqa: E402


def _approx(a, b, eps=1e-6):
    return abs(a - b) <= eps


def _build_doc():
    """Minimal glTF: 2 cameras, 3 punctual lights, nodes wiring them up."""
    return {
        "asset": {"version": "2.0"},
        "extensionsUsed": ["KHR_lights_punctual"],
        "scene": 0,
        "scenes": [{"nodes": [0, 1, 2, 3, 4, 5]}],
        "cameras": [
            {
                "type": "perspective",
                "name": "PerspCam",
                "perspective": {
                    "yfov": 0.6911112070083618,   # ~39.6 deg vertical FOV
                    "aspectRatio": 1.7777,
                    "znear": 0.1,
                    "zfar": 1000.0,
                },
            },
            {
                "type": "orthographic",
                "name": "OrthoCam",
                "orthographic": {
                    "xmag": 5.0,
                    "ymag": 5.0,
                    "znear": 0.01,
                    "zfar": 200.0,
                },
            },
        ],
        "extensions": {
            "KHR_lights_punctual": {
                "lights": [
                    {
                        "type": "directional",
                        "name": "Sun",
                        "color": [1.0, 0.95, 0.9],
                        "intensity": 1000.0,
                    },
                    {
                        "type": "point",
                        "name": "Bulb",
                        "color": [0.2, 0.4, 1.0],
                        "intensity": 800.0,
                        "range": 25.0,
                    },
                    {
                        "type": "spot",
                        "name": "Spot",
                        "color": [1.0, 1.0, 1.0],
                        "intensity": 1500.0,
                        "spot": {
                            "innerConeAngle": 0.2,
                            "outerConeAngle": 0.5,
                        },
                    },
                ]
            }
        },
        "nodes": [
            {"name": "persp_node", "camera": 0},
            {"name": "ortho_node", "camera": 1},
            {"name": "sun_node",
             "extensions": {"KHR_lights_punctual": {"light": 0}}},
            {"name": "bulb_node",
             "extensions": {"KHR_lights_punctual": {"light": 1}}},
            {"name": "spot_node",
             "extensions": {"KHR_lights_punctual": {"light": 2}}},
            {"name": "plain_node"},   # no camera, no light
        ],
    }


def main():
    doc = _build_doc()
    g = _gltf.Gltf(doc, None, "")
    nodes = doc["nodes"]

    # --- cameras -------------------------------------------------------------
    c0 = g.camera_def(0)
    print("camera_def(0):", c0)
    assert c0 is not None, "camera_def(0) returned None"
    assert c0["type"] == "perspective", "cam0 type mismatch: %r" % c0["type"]
    p = c0["perspective"]
    assert _approx(p["yfov"], 0.6911112070083618), "yfov mismatch: %r" % p["yfov"]
    assert _approx(p["znear"], 0.1), "znear mismatch: %r" % p["znear"]
    assert _approx(p["zfar"], 1000.0), "zfar mismatch: %r" % p["zfar"]

    c1 = g.camera_def(1)
    print("camera_def(1):", c1)
    assert c1 is not None, "camera_def(1) returned None"
    assert c1["type"] == "orthographic", "cam1 type mismatch: %r" % c1["type"]
    o = c1["orthographic"]
    assert _approx(o["xmag"], 5.0), "xmag mismatch: %r" % o["xmag"]
    assert _approx(o["znear"], 0.01), "ortho znear mismatch: %r" % o["znear"]
    assert _approx(o["zfar"], 200.0), "ortho zfar mismatch: %r" % o["zfar"]

    # out-of-range / missing camera -> None
    assert g.camera_def(99) is None, "out-of-range camera_def should be None"
    assert g.camera_def(-1) is None, "negative camera_def should be None"
    assert g.camera_def(None) is None, "None camera_def should be None"

    # node_camera resolves the right index, None when absent
    assert g.node_camera(nodes[0]) == 0, "persp node camera index mismatch"
    assert g.node_camera(nodes[1]) == 1, "ortho node camera index mismatch"
    assert g.node_camera(nodes[5]) is None, "plain node should have no camera"
    print("node_camera resolves correctly (0, 1, None)")

    # --- lights --------------------------------------------------------------
    lights = g.light_defs()
    print("light_defs count:", len(lights))
    assert len(lights) == 3, "expected 3 lights, got %d" % len(lights)

    l_dir = g.light_def(0)
    assert l_dir["type"] == "directional", "light0 type mismatch"
    assert _approx(l_dir["intensity"], 1000.0), "light0 intensity mismatch"
    assert l_dir["color"] == [1.0, 0.95, 0.9], "light0 color mismatch"

    l_pt = g.light_def(1)
    assert l_pt["type"] == "point", "light1 type mismatch"
    assert _approx(l_pt["range"], 25.0), "light1 range mismatch"

    l_sp = g.light_def(2)
    print("light_def(2):", l_sp)
    assert l_sp["type"] == "spot", "light2 type mismatch"
    spot = l_sp["spot"]
    assert _approx(spot["innerConeAngle"], 0.2), "inner cone mismatch"
    assert _approx(spot["outerConeAngle"], 0.5), "outer cone mismatch"

    # out-of-range / missing light -> None
    assert g.light_def(99) is None, "out-of-range light_def should be None"
    assert g.light_def(-1) is None, "negative light_def should be None"
    assert g.light_def(None) is None, "None light_def should be None"

    # node_light resolves the right index, None when absent
    assert g.node_light(nodes[2]) == 0, "sun node light index mismatch"
    assert g.node_light(nodes[3]) == 1, "bulb node light index mismatch"
    assert g.node_light(nodes[4]) == 2, "spot node light index mismatch"
    assert g.node_light(nodes[5]) is None, "plain node should have no light"
    assert g.node_light(nodes[0]) is None, "camera node should have no light"
    print("node_light resolves correctly (0, 1, 2, None)")

    # --- sanity: the conversions the builder will do (no Maya needed) --------
    # Perspective yfov -> Maya focal length (vertical aperture 0.945 in).
    yfov = c0["perspective"]["yfov"]
    focal_mm = (0.945 * 25.4) / (2.0 * math.tan(yfov / 2.0))
    print("derived focal length (mm) for cam0:", round(focal_mm, 3))
    assert 30.0 < focal_mm < 40.0, "focal length out of expected range: %r" % focal_mm

    # Spot outerConeAngle (half, rad) -> Maya coneAngle (full, deg).
    cone_deg = math.degrees(spot["outerConeAngle"]) * 2.0
    print("derived spot coneAngle (deg):", round(cone_deg, 3))
    assert 55.0 < cone_deg < 60.0, "cone angle out of expected range: %r" % cone_deg
    penumbra_deg = math.degrees(spot["outerConeAngle"] - spot["innerConeAngle"])
    print("derived spot penumbraAngle (deg):", round(penumbra_deg, 3))
    assert penumbra_deg > 0.0, "penumbra should be positive"

    # --- no-extension doc -> [] (Anno no-op path) ---------------------------
    g_empty = _gltf.Gltf({"asset": {"version": "2.0"}}, None, "")
    assert g_empty.light_defs() == [], "no-extension doc should give no lights"
    assert g_empty.camera_def(0) is None, "no-cameras doc should give None"
    assert g_empty.node_camera({"name": "x"}) is None, "no camera key -> None"
    assert g_empty.node_light({"name": "x"}) is None, "no light ext -> None"
    print("no-camera/no-light doc returns None / [] (Anno no-op path OK)")

    print()
    print("SUCCESS")


if __name__ == "__main__":
    main()
