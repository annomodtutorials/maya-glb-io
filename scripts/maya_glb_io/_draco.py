"""KHR_draco_mesh_compression decode wrapper (Maya-free, optional dependency).

Draco is Google's mesh-compression codec. glTFs exported by Sketchfab,
Blender (with "Compression" on), the glTF-Pipeline, etc. ship their vertex
data as a Draco bitstream referenced by `KHR_draco_mesh_compression` instead of
plain accessors, so without a Draco decoder those meshes import EMPTY.

We decode via the `DracoPy` pip package — a self-contained C++/Cython wheel
(NOT a Maya module), so this whole layer runs in the same Maya-free space as
_gltf.py once DracoPy is installed. The ONLY Maya-side piece is the lazy
auto-install (`ensure_dracopy`), which shells `mayapy -m pip install` exactly
like the addon's Pillow installer — but everything here degrades gracefully and
NEVER raises on a missing/uninstallable dependency: callers check the bool and
warn+skip.

API:
  dracopy_available() -> bool   : is DracoPy importable right now?
  ensure_dracopy()    -> bool   : importable, or installed-then-importable
  decode(buffer_bytes) -> dict  : normalized {"POSITION", "NORMAL",
                                  "TEXCOORD_0", "indices"} (None for absent
                                  optional attributes). Raises DracoError if
                                  DracoPy is unavailable or the buffer is bad.

This module deliberately has NO top-level `import DracoPy` and NO `import maya`
— both are imported lazily inside the functions so _gltf can import _draco
(and the smoke tests can import both) without either dependency present.
"""
from __future__ import annotations

import importlib
import sys


class DracoError(Exception):
    """Raised when a Draco primitive can't be decoded (no DracoPy / bad data).

    Always catchable by the caller, which warns + skips the primitive rather
    than aborting the whole import.
    """
    pass


def _warn(msg):
    """Print a warning via Maya's console if available, else stdout.

    Keeps the module Maya-free: we only touch maya.api if it's already
    importable (i.e. we're running inside Maya), otherwise fall back to print
    so the smoke tests stay silent-friendly.
    """
    try:
        import maya.api.OpenMaya as om  # noqa: F401
        om.MGlobal.displayWarning("[glb_io] " + msg)
    except Exception:
        print("[glb_io] " + msg)


def dracopy_available():
    # type: () -> bool
    """True if `import DracoPy` succeeds in the current interpreter."""
    try:
        import DracoPy  # noqa: F401
        return True
    except Exception:
        return False


def ensure_dracopy():
    # type: () -> bool
    """Make DracoPy importable, pip-installing it into THIS interpreter if needed.

    Flow (mirrors the addon's Pillow auto-installer):
      1. If DracoPy already imports, return True immediately (fast path — does
         NOT slow non-Draco imports because callers only reach here on a Draco
         primitive).
      2. Otherwise run `<this-python> -m pip install --user DracoPy`. Inside
         Maya, sys.executable is mayapy, so the wheel lands in Maya's user
         site-packages and becomes importable without a restart. If `--user`
         fails (e.g. a venv where user-site is disabled), retry WITHOUT it.
      3. Invalidate import caches and retry the import.

    Returns True on success, False on any failure (offline, pip blocked, no
    matching wheel for this Python). NEVER raises — the caller warns + skips.
    """
    if dracopy_available():
        return True

    import subprocess

    creationflags = 0
    if sys.platform == "win32":
        # Don't pop a console window when launched from Maya's GUI.
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    _warn(
        "Draco-compressed mesh found; installing the DracoPy decoder via: "
        "%s -m pip install --user DracoPy (one-time, needs internet)..."
        % sys.executable
    )

    # Try --user first (lands in the user site, doesn't need admin), then a
    # plain install as a fallback for environments where --user is rejected.
    attempts = (
        [sys.executable, "-m", "pip", "install", "--user", "DracoPy"],
        [sys.executable, "-m", "pip", "install", "DracoPy"],
    )

    installed = False
    last_output = ""
    for cmd in attempts:
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,   # text mode, 3.6-compatible spelling
                creationflags=creationflags,
                timeout=300,               # 5 min hard limit
            )
        except Exception as exc:
            last_output = "could not launch pip: %s" % exc
            continue
        last_output = res.stdout or ""
        if res.returncode == 0:
            installed = True
            break

    if not installed:
        _warn(
            "DracoPy install failed (offline or pip blocked?). "
            "Draco-compressed meshes will be skipped. Install it manually with:"
            "  %s -m pip install DracoPy\npip output:\n%s"
            % (sys.executable, (last_output or "")[-1500:])
        )
        return False

    # Fresh files just landed in site-packages — clear the import caches so the
    # interpreter sees the new package without a restart, then retry.
    importlib.invalidate_caches()
    if dracopy_available():
        _warn("DracoPy installed and importable; decoding Draco meshes.")
        return True

    _warn(
        "pip reported success but `import DracoPy` still fails. "
        "Try restarting Maya. Draco meshes skipped for now."
    )
    return False


def _to_xyz_list(arr):
    """Reshape a points/normals array (flat or Nx3 / ndarray or list) into a
    list of (x, y, z) float tuples."""
    # numpy ndarray (the DracoPy 2.x path) — Nx3 or flat.
    try:
        import numpy as _np  # noqa
        if isinstance(arr, _np.ndarray):
            a = arr
            if a.ndim == 2 and a.shape[1] == 3:
                return [(float(r[0]), float(r[1]), float(r[2])) for r in a]
            flat = a.reshape(-1)
            return [
                (float(flat[i]), float(flat[i + 1]), float(flat[i + 2]))
                for i in range(0, len(flat) - 2, 3)
            ]
    except Exception:
        pass

    # Plain Python sequence fallback (older DracoPy returned flat lists or
    # lists of 3-tuples).
    if not arr:
        return []
    first = arr[0]
    if isinstance(first, (list, tuple)) and len(first) >= 3:
        return [(float(p[0]), float(p[1]), float(p[2])) for p in arr]
    return [
        (float(arr[i]), float(arr[i + 1]), float(arr[i + 2]))
        for i in range(0, len(arr) - 2, 3)
    ]


def _to_uv_list(arr):
    """Reshape a tex_coord array (flat or Nx2) into a list of (u, v) tuples."""
    try:
        import numpy as _np  # noqa
        if isinstance(arr, _np.ndarray):
            a = arr
            if a.ndim == 2 and a.shape[1] >= 2:
                return [(float(r[0]), float(r[1])) for r in a]
            flat = a.reshape(-1)
            return [
                (float(flat[i]), float(flat[i + 1]))
                for i in range(0, len(flat) - 1, 2)
            ]
    except Exception:
        pass

    if not arr:
        return []
    first = arr[0]
    if isinstance(first, (list, tuple)) and len(first) >= 2:
        return [(float(p[0]), float(p[1])) for p in arr]
    return [
        (float(arr[i]), float(arr[i + 1]))
        for i in range(0, len(arr) - 1, 2)
    ]


def _to_index_list(faces):
    """Flatten a faces array (Mx3 or already-flat) into a flat triangle index
    list [i, i, i, ...]."""
    try:
        import numpy as _np  # noqa
        if isinstance(faces, _np.ndarray):
            return [int(v) for v in faces.reshape(-1)]
    except Exception:
        pass

    if not faces:
        return []
    first = faces[0]
    if isinstance(first, (list, tuple)):
        out = []
        for tri in faces:
            out.extend(int(v) for v in tri)
        return out
    return [int(v) for v in faces]


def decode(buffer_bytes):
    # type: (bytes) -> dict
    """Decode a Draco bitstream into normalized glTF-semantic arrays.

    Returns a dict:
      {
        "POSITION":   [(x, y, z), ...],     # always present on a valid mesh
        "NORMAL":     [(x, y, z), ...] | None,
        "TEXCOORD_0": [(u, v), ...]   | None,
        "indices":    [i, i, i, ...],       # flat triangle list
      }

    POSITION/NORMAL/TEXCOORD_0 are mapped from DracoPy's typed accessors
    (.points / .normals / .tex_coord), which resolve the Draco attributes by
    semantic type — exactly the mapping the glTF extension's `attributes` table
    expresses (glTF semantic -> Draco attribute id). Optional attributes the
    stream doesn't carry come back as None.

    Raises DracoError if DracoPy isn't importable or the buffer fails to decode.
    """
    try:
        import DracoPy
    except Exception as exc:
        raise DracoError(
            "DracoPy is not installed; cannot decode KHR_draco_mesh_compression "
            "(%s)" % exc
        )

    try:
        mesh = DracoPy.decode(bytes(buffer_bytes))
    except Exception as exc:
        raise DracoError("DracoPy failed to decode Draco buffer: %s" % exc)

    points = getattr(mesh, "points", None)
    faces = getattr(mesh, "faces", None)
    normals = getattr(mesh, "normals", None)
    tex = getattr(mesh, "tex_coord", None)

    if points is None:
        raise DracoError("decoded Draco stream has no POSITION data")

    result = {
        "POSITION": _to_xyz_list(points),
        "NORMAL": _to_xyz_list(normals) if normals is not None else None,
        "TEXCOORD_0": _to_uv_list(tex) if tex is not None else None,
        "indices": _to_index_list(faces) if faces is not None else [],
    }
    return result
