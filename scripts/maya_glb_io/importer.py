"""Main import orchestration.

Public API:
    import_file(path, options=None) -> list[str] of top-level transform names
    import_bytes(data, source_label, options=None) -> list[str]
    import_bytes_full(data, source_label, options=None) -> dict
        {"top_transforms": [..], "primitives": [(shape_path, mat_idx), ..]}

`options` is a dict with these recognized keys (see _prefs.DEFAULTS for schema):
    target              str   shader target name or "" for preference
    scale               str   "auto" or a numeric string e.g. "100.0"
    useNamespace        bool  wrap import in a Maya namespace
    skipMaterials       bool  geometry only
    skipSkinning        bool  skip glTF skins -> Maya joints/skinCluster
                              (the Anno building pipeline sets this True
                               since rdm4 doesn't emit skin data for buildings)
    centerPivot         bool  set root pivot to bbox center
    centerOnOrigin      bool  translate root so bbox center sits at (0,0,0)
    convertSpecGloss    bool  handle KHR_materials_pbrSpecularGlossiness
    materialPrefix      str   prefix added to shader names

The build also accepts the snake_case aliases `skip_materials` and
`skip_skinning` for backward compatibility with existing callers (e.g. the
Anno addon already passes `{"skip_materials": True}`).

Skinning support
----------------
When a glTF node references both a `mesh` and a `skin`, the importer:
  - builds a Maya joint chain mirroring skin.joints (joints created at world
    level, siblings of mesh groups — standard rigging convention),
  - applies each joint's local TRS so the joint world transforms match the
    bind pose (= inverse of inverseBindMatrices),
  - creates a `skinCluster` with normalizeWeights=1 and maximumInfluences=4,
  - applies per-vertex weights via `cmds.skinPercent` (slow loop; TODO move
    to OpenMayaAnim bulk API for 50k+ vert meshes),
  - saves the bind pose via `dagPose -save -bindPose`.

Known limitations: no animation playback yet (separate feature). Rigid bind
pose works; user can pose joints by hand and the mesh follows. KHR mesh-
quantization is not yet honored on JOINTS_n/WEIGHTS_n beyond the standard
`normalized` flag.

Wraps the import in a single undo chunk so the whole operation reverses
with one Ctrl+Z.
"""
from __future__ import annotations

import os
import traceback
from typing import Any, Dict, List, Optional

import maya.api.OpenMaya as om
from maya import cmds


def import_file(path, options=None):
    # type: (str, Optional[Dict[str, Any]]) -> List[str]
    options = _normalize_options(options)
    om.MGlobal.displayInfo("[glb_io] importing: " + path)

    if not os.path.isfile(path):
        om.MGlobal.displayError("[glb_io] not a file: " + path)
        return []

    from maya_glb_io import _build, _gltf

    use_ns = bool(options.get("useNamespace"))

    # Namespace safety net.
    #
    # When the user imports through Maya's native File > Import dialog, Maya's
    # OWN "Use namespaces" checkbox (separate from our option) can wrap the
    # imported nodes — and occasionally the entire scene, including the default
    # cameras — into a namespace named after the file (e.g. "stonePyramid:").
    # We can't stop Maya from creating it, but we CAN flatten it back to the
    # root namespace afterwards.
    #
    # Strategy: snapshot the namespaces that exist now, force our own node
    # creation to happen in the root namespace, and then (deferred, so it runs
    # after Maya has finished applying its namespace) merge any namespace that
    # newly appeared back into root. The deferred merge no-ops on the clean
    # menu-import path because no new namespace appears there.
    ns_before = _list_namespaces()
    if not use_ns:
        try:
            cur = cmds.namespaceInfo(currentNamespace=True) or ":"
            if cur != ":":
                cmds.namespace(setNamespace=":")
        except Exception:
            pass

    cmds.undoInfo(openChunk=True, chunkName="glb_io import")
    try:
        gltf = _gltf.load(path)
        result = _build.build_scene(gltf, path, options=options)
        tops = result["top_transforms"]

        # Bake interaction: freezing transforms (makeIdentity) zeroes the
        # translate/rotate/scale channels — which is exactly where the imported
        # animCurves live. If the glTF carries animations AND we imported them,
        # baking would wipe the animation. Skip baking entirely in that case.
        bake = options.get("bakeTransforms", True)
        if bake and options.get("importAnimation", True) and gltf.doc.get("animations"):
            bake = False
            om.MGlobal.displayInfo(
                "[glb_io] animations present + imported; skipping transform "
                "bake so keyframes are preserved"
            )
        if bake:
            _bake_transforms(tops)

        # Material variants: if the glTF declares KHR_materials_variants, put a
        # live 'Material Variant' enum on the imported roots + arm a scriptJob so
        # the user switches looks straight from the Attribute Editor / Channel
        # Box (no menu round-trip). No-op for non-variant files.
        if (gltf.doc.get("extensions") or {}).get("KHR_materials_variants"):
            try:
                from maya_glb_io import commands as _cmds
                _cmds.setup_variant_switch(tops)
            except Exception as exc:
                om.MGlobal.displayWarning(
                    "[glb_io] variant switch setup failed: %s" % exc
                )

        om.MGlobal.displayInfo(
            "[glb_io] done: created %d top-level transform(s)" % len(tops)
        )
        return tops
    except Exception as exc:
        om.MGlobal.displayError("[glb_io] import failed: %s" % exc)
        om.MGlobal.displayError(traceback.format_exc())
        raise
    finally:
        cmds.undoInfo(closeChunk=True)
        if not use_ns:
            _schedule_namespace_flatten(ns_before)


def import_bytes(data, source_label, options=None):
    # type: (bytes, str, Optional[Dict[str, Any]]) -> List[str]
    """Same as import_file but reads from an in-memory glb byte buffer."""
    result = import_bytes_full(data, source_label, options=options)
    return result["top_transforms"]


def import_bytes_full(data, source_label, options=None):
    # type: (bytes, str, Optional[Dict[str, Any]]) -> dict
    """Like import_bytes but returns the full result dict including primitive
    metadata. Used by the Anno addon (which needs the primitive metadata to
    map cfg materials onto the right Maya mesh shapes)."""
    options = _normalize_options(options)
    om.MGlobal.displayInfo("[glb_io] importing in-memory glb: " + source_label)

    from maya_glb_io import _build, _gltf

    cmds.undoInfo(openChunk=True, chunkName="glb_io import (bytes)")
    try:
        gltf = _gltf.loads(data, base_dir="")
        return _build.build_scene(gltf, source_label, options=options)
    except Exception as exc:
        om.MGlobal.displayError("[glb_io] in-memory import failed: %s" % exc)
        om.MGlobal.displayError(traceback.format_exc())
        raise
    finally:
        cmds.undoInfo(closeChunk=True)


def _bake_transforms(tops):
    """Freeze each top transform so imported geometry has a clean identity
    transform (no leftover node scale/translate/rotate — e.g. the 0.01 root
    scale many glTF exporters bake in for unit conversion).

    Skips any subtree that contains a skinCluster: freezing a bound mesh would
    break its bind pose. Failures are non-fatal (the geometry is still imported,
    just not frozen)."""
    for t in tops:
        if not cmds.objExists(t):
            continue
        try:
            shapes = cmds.listRelatives(
                t, allDescendents=True, type="mesh", fullPath=True
            ) or []
            skinned = False
            for sh in shapes:
                if cmds.listConnections(sh, type="skinCluster"):
                    skinned = True
                    break
            if skinned:
                om.MGlobal.displayInfo(
                    "[glb_io] '%s' is skinned; leaving transforms unbaked" % t
                )
                continue
            cmds.makeIdentity(
                t, apply=True, translate=True, rotate=True, scale=True,
                normal=0, preserveNormals=True,
            )
        except Exception as exc:
            om.MGlobal.displayWarning(
                "[glb_io] could not bake transforms on '%s': %s" % (t, exc)
            )


def _list_namespaces():
    """Return the set of all namespaces currently in the scene (recursive)."""
    try:
        found = cmds.namespaceInfo(":", listOnlyNamespaces=True, recurse=True) or []
        return set(found)
    except Exception:
        try:
            return set(cmds.namespaceInfo(listOnlyNamespaces=True) or [])
        except Exception:
            return set()


# Maya-internal namespaces that must never be merged/removed.
_PROTECTED_NAMESPACES = {"UI", "shared", ":UI", ":shared"}


def _flatten_new_namespaces(ns_before):
    """Merge every namespace that appeared since `ns_before` back into the root
    namespace, then delete it. Default cameras/sets that Maya may have swept into
    a namespace come straight back to root with their original names (there are
    no root-level duplicates to clash with, since they were *renamed* into the
    namespace rather than copied)."""
    try:
        now = _list_namespaces()
    except Exception:
        return
    new = [n for n in (now - ns_before) if n not in _PROTECTED_NAMESPACES]
    # Deepest namespaces first so nested ones merge cleanly into their parent.
    for ns in sorted(new, key=lambda n: n.count(":"), reverse=True):
        try:
            if cmds.namespace(exists=ns):
                cmds.namespace(removeNamespace=ns, mergeNamespaceWithRoot=True)
        except Exception as exc:
            om.MGlobal.displayWarning(
                "[glb_io] could not flatten namespace '%s': %s" % (ns, exc)
            )
    try:
        cmds.namespace(setNamespace=":")
    except Exception:
        pass


def _schedule_namespace_flatten(ns_before):
    """Run _flatten_new_namespaces AFTER the current import finishes.

    Maya's File > Import applies its own "Use namespaces" wrapping around the
    nodes our reader created, and that happens after our reader returns — so we
    defer the cleanup to catch it. evalDeferred runs once Maya is idle again.
    On the menu-import path (no Maya namespace wrapping) this simply no-ops.
    """
    snapshot = set(ns_before)

    def _run():
        _flatten_new_namespaces(snapshot)

    try:
        cmds.evalDeferred(_run, lowestPriority=True)
    except Exception:
        # If evalDeferred isn't available for some reason, run inline.
        _run()


def _normalize_options(options):
    """Resolve a partial options dict into a complete one (filled from prefs).

    Also resolves the special "target=''" sentinel (use saved preference) and
    the "scale='auto'" sentinel (auto-detect from scene unit, done in _build).
    """
    from maya_glb_io import _prefs
    base = _prefs.get_all()
    if options:
        for k, v in options.items():
            # Translate empty-string sentinels to "use the saved pref"
            if k == "target" and (v == "" or v is None):
                continue
            base[k] = v

    # Resolve target sentinel — "" means "use saved preference"; if pref is also
    # empty, that's interpreted as "auto" downstream.
    if not base.get("target"):
        base["target"] = "auto"

    # Translate "scale": "auto" stays as auto (None passed to _build), otherwise
    # try to convert to float; fall back to auto on parse errors.
    scale = base.get("scale", "auto")
    if isinstance(scale, str) and scale.lower() == "auto":
        base["_scale_resolved"] = None
    else:
        try:
            base["_scale_resolved"] = float(scale)
        except (TypeError, ValueError):
            base["_scale_resolved"] = None

    # "auto" target sentinel -> let _materials.resolve_target() pick
    if base["target"] == "auto":
        base["_target_resolved"] = None
    else:
        base["_target_resolved"] = base["target"]

    return base
