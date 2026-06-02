"""User-facing commands wired up by the menu / shelf / hotkeys."""
from __future__ import annotations

import json

import maya.api.OpenMaya as om
from maya import cmds

from maya_glb_io import _options_ui, _prefs, importer
from maya_glb_io._materials import list_targets, resolve_target


# Shape attribute that _build._stamp_variant_map writes: a JSON dict
# {variantName: shadingGroupName}. The switcher scans every mesh shape for it.
VARIANT_ATTR = "glbVariantMap"


FILE_FILTER = (
    "glTF 2.0 (*.glb *.gltf);;"
    "glTF Binary (*.glb);;"
    "glTF JSON (*.gltf);;"
    "All Files (*.*)"
)

EXPORT_FILE_FILTER = (
    "glTF Binary (*.glb);;"
    "glTF JSON (*.gltf);;"
    "All Files (*.*)"
)


def show_import_dialog():
    """Pop the file picker and import using saved preferences.

    For per-import overrides use the native File > Import dialog instead —
    that one shows the full Specifics panel for tweaking options.
    """
    paths = cmds.fileDialog2(
        fileMode=1,
        caption="Import glTF 2.0 (uses saved preferences)",
        fileFilter=FILE_FILTER,
        okCaption="Import",
    )
    if not paths:
        return None

    # Use saved preferences as options
    options = _prefs.get_all()
    return importer.import_file(paths[0], options=options)


def show_batch_import_dialog():
    paths = cmds.fileDialog2(
        fileMode=4,
        caption="Batch Import glTF 2.0 (uses saved preferences)",
        fileFilter=FILE_FILTER,
        okCaption="Import All",
    )
    if not paths:
        return []

    options = _prefs.get_all()
    return [importer.import_file(p, options=options) for p in paths]


def show_export_dialog(scope=None):
    """Pop a save dialog and export to glTF/GLB using saved export preferences.

    `scope`: "selection" or "scene". If None, falls back to the saved export
    preference (default "selection").

    For per-export option overrides use Maya's native File > Export
    Selection... / File > Export All... dialogs — those route through the file
    translator's writer() and (in a future iteration) a Specifics panel.
    """
    import traceback

    from maya_glb_io import exporter

    options = _prefs.get_all_export()
    if scope:
        options["scope"] = scope
    scope = options.get("scope", "selection")

    # Guard the most common "nothing happened" case up front: a selection
    # export with an empty selection. Tell the user instead of silently no-oping.
    if scope == "selection" and not cmds.ls(selection=True):
        cmds.confirmDialog(
            title="Export Selection",
            message=("Nothing is selected.\n\n"
                     "Select the object(s) you want to export first, then run "
                     "Export Selection again."),
            button=["OK"],
        )
        return None

    caption = ("Export Selection to glTF/GLB" if scope == "selection"
               else "Export Scene to glTF/GLB")
    # NOTE: fileDialog2 has NO 'defaultExtension' flag — passing it raises and
    # the dialog never opens. The extension comes from the file filter / what
    # the user types, and we infer the format from the resulting path below.
    paths = cmds.fileDialog2(
        fileMode=0,                  # 0 = save, file may or may not exist
        caption=caption,
        fileFilter=EXPORT_FILE_FILTER,
        okCaption="Export",
    )
    if not paths:
        return None

    path = paths[0]
    # Let the chosen extension override the saved format preference. If the user
    # typed no extension, append one based on the saved format preference.
    lower = path.lower()
    if lower.endswith(".gltf"):
        options["format"] = "gltf"
    elif lower.endswith(".glb"):
        options["format"] = "glb"
    else:
        fmt = options.get("format", "glb")
        path = path + ("." + ("gltf" if fmt == "gltf" else "glb"))

    try:
        if scope == "scene":
            result = exporter.export_all(path, options=options)
        else:
            result = exporter.export_selection(path, options=options)
    except Exception as exc:
        cmds.confirmDialog(
            title="Export Failed",
            message="glTF/GLB export failed:\n\n{0}".format(exc),
            button=["OK"],
        )
        om.MGlobal.displayError("[glb_io] export failed: " + traceback.format_exc())
        return None

    if not result or not result.get("path"):
        cmds.confirmDialog(
            title="Export — Nothing Written",
            message=("No file was written. The selection may contain no mesh "
                     "geometry, or the scene was empty.\n\nSee the Script Editor "
                     "for details."),
            button=["OK"],
        )
        return result

    cmds.confirmDialog(
        title="Export Complete",
        message=("Exported successfully:\n\n{0}\n\n"
                 "{1} node(s), {2} primitive(s), {3} material(s).").format(
            result.get("path"),
            result.get("node_count", 0),
            result.get("primitive_count", 0),
            result.get("material_count", 0),
        ),
        button=["OK"],
    )
    return result


def show_preferences():
    """Open the persistent import-preferences editor."""
    _options_ui.show_preferences_window()


def show_export_preferences():
    """Open the persistent export-preferences editor."""
    _options_ui.show_export_preferences_window()


def show_about():
    from maya_glb_io import __version__
    prefs = _prefs.get_all()
    cmds.confirmDialog(
        title="maya_glb_io",
        message=(
            "maya_glb_io " + __version__ + "\n"
            "Free MIT-licensed glTF 2.0 importer for Maya.\n\n"
            "Shader targets: " + ", ".join(list_targets()) + "\n"
            "Auto-pick right now: " + (resolve_target(None) or "(none)") + "\n\n"
            "Current preference defaults:\n"
            + "\n".join("  " + k + " = " + repr(v) for k, v in sorted(prefs.items()))
        ),
        button=["OK"],
    )


# ---------------------------------------------------------------------------
# KHR_materials_variants — material variant switching
# ---------------------------------------------------------------------------

def _iter_variant_shapes():
    """Yield (shape_full_path, {variantName: shadingGroupName}) for every mesh
    shape carrying a parseable `glbVariantMap` attribute. Shapes without the
    attribute (the common case) are skipped, so this is cheap and safe to run
    in any scene.
    """
    shapes = cmds.ls(type="mesh", long=True) or []
    for shape in shapes:
        if not cmds.attributeQuery(VARIANT_ATTR, node=shape, exists=True):
            continue
        raw = cmds.getAttr(shape + "." + VARIANT_ATTR)
        if not raw:
            continue
        try:
            mapping = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(mapping, dict) and mapping:
            yield shape, mapping


def list_material_variants():
    """Return the sorted union of every variant name available in the scene.

    Scans all mesh shapes' `glbVariantMap` attributes. Returns an empty list
    when no imported geometry declares variants.
    """
    names = set()
    for _shape, mapping in _iter_variant_shapes():
        names.update(mapping.keys())
    return sorted(names)


def set_active_material_variant(variant_name, quiet=False):
    """Switch every variant-aware shape in the scene to `variant_name`.

    For each shape whose stored map has an entry for `variant_name`, reassign
    the shape to that shading group (guarding that the SG still exists). Shapes
    with no entry for this variant are left on their current assignment.

    `quiet` suppresses the status message (used by the import-time pre-warm,
    which cycles all variants and would otherwise spam the script editor).

    Returns the number of shapes switched.
    """
    if not variant_name:
        return 0

    switched = 0
    missing_sg = 0
    for shape, mapping in _iter_variant_shapes():
        sg = mapping.get(variant_name)
        if not sg:
            continue
        if not cmds.objExists(sg):
            missing_sg += 1
            continue
        try:
            cmds.sets(shape, edit=True, forceElement=sg)
            switched += 1
        except Exception as exc:
            om.MGlobal.displayWarning(
                "[glb_io] could not assign '%s' on '%s': %s" % (sg, shape, exc)
            )

    if not quiet:
        msg = "[glb_io] variant '%s': switched %d shape(s)" % (variant_name, switched)
        if missing_sg:
            msg += " (%d skipped — shading group missing)" % missing_sg
        om.MGlobal.displayInfo(msg)
    return switched


def show_material_variants_dialog():
    """Open the material-variant switcher window."""
    _options_ui.show_variants_window()


def install_draco_decoder():
    """Pre-install the DracoPy decoder (one-time) with user feedback.

    Draco-compressed glTFs need DracoPy. The importer auto-installs it on the
    first Draco import, but that blocks Maya silently while pip runs. Running
    this from the menu first gives a heads-up + a clear result, so the actual
    import is fast and non-blocking afterwards.
    """
    from maya_glb_io import _draco
    if _draco.dracopy_available():
        cmds.confirmDialog(
            title="Draco Decoder",
            message="DracoPy is already installed — Draco / compressed glTFs "
                    "import normally.",
            button=["OK"],
        )
        return True
    proceed = cmds.confirmDialog(
        title="Install Draco Decoder",
        message=("This installs the DracoPy decoder (needed for Draco-compressed "
                 "glTFs) via pip. It's a one-time download (~5 MB, needs "
                 "internet). Maya may pause for up to a minute while it runs.\n\n"
                 "Install now?"),
        button=["Install", "Cancel"], defaultButton="Install",
        cancelButton="Cancel", dismissString="Cancel",
    )
    if proceed != "Install":
        return False
    ok = _draco.ensure_dracopy()
    cmds.confirmDialog(
        title="Draco Decoder",
        message=("DracoPy installed — Draco / compressed glTFs will now import."
                 if ok else
                 "Install failed (offline or pip blocked). See the Script Editor "
                 "for details. You can install manually in a terminal with:\n\n"
                 "  mayapy -m pip install DracoPy"),
        button=["OK"],
    )
    return ok


# ---------------------------------------------------------------------------
# Live Attribute-Editor variant switch (enum attribute + scriptJob)
# ---------------------------------------------------------------------------
#
# Instead of a menu button, the switch lives ON the imported object as an enum
# attribute "Material Variant". Selecting the object shows it in the Attribute
# Editor / Channel Box; changing the dropdown re-skins the scene INSTANTLY via a
# scriptJob. This matches the paid-plugin UX.

VARIANT_ENUM_ATTR = "materialVariant"

# node full-path -> scriptJob id, so we can avoid duplicate jobs / re-arm.
_VARIANT_JOBS = {}


def _variant_control_candidates():
    """Top-level transform ancestor of every variant-aware shape (deduped).
    These are the natural nodes to host the switch — the things you select."""
    tops = set()
    for shape, _m in _iter_variant_shapes():
        parent = cmds.listRelatives(shape, parent=True, fullPath=True)
        node = parent[0] if parent else None
        while node:
            up = cmds.listRelatives(node, parent=True, fullPath=True)
            if not up:
                break
            node = up[0]
        if node:
            tops.add(node)
    return sorted(tops)


def _ensure_variant_enum(node, variants):
    """Add (or refresh) the 'materialVariant' enum attr on `node`."""
    enum_str = ":".join(variants)
    if cmds.attributeQuery(VARIANT_ENUM_ATTR, node=node, exists=True):
        try:
            cmds.addAttr(node + "." + VARIANT_ENUM_ATTR, edit=True, enumName=enum_str)
        except Exception:
            pass
    else:
        cmds.addAttr(node, longName=VARIANT_ENUM_ATTR, niceName="Material Variant",
                     attributeType="enum", enumName=enum_str, keyable=False)
        try:
            cmds.setAttr(node + "." + VARIANT_ENUM_ATTR, edit=True, channelBox=True)
        except Exception:
            pass


def _apply_variant_from_enum(node):
    """Read the node's enum and switch the scene to that variant."""
    try:
        if not cmds.attributeQuery(VARIANT_ENUM_ATTR, node=node, exists=True):
            return
        idx = cmds.getAttr(node + "." + VARIANT_ENUM_ATTR)
        names = (cmds.attributeQuery(VARIANT_ENUM_ATTR, node=node,
                                     listEnum=True) or [""])[0].split(":")
        if 0 <= idx < len(names):
            set_active_material_variant(names[idx])
    except Exception as exc:
        om.MGlobal.displayWarning("[glb_io] variant apply failed: %s" % exc)


def _arm_variant_scriptjob(node):
    """(Re)arm a scriptJob that fires whenever the node's variant enum changes."""
    old = _VARIANT_JOBS.pop(node, None)
    if old is not None:
        try:
            cmds.scriptJob(kill=old, force=True)
        except Exception:
            pass
    attr = node + "." + VARIANT_ENUM_ATTR
    jid = cmds.scriptJob(
        attributeChange=[attr, (lambda n=node: _apply_variant_from_enum(n))],
        killWithScene=True,
    )
    _VARIANT_JOBS[node] = jid


def setup_variant_switch(nodes=None):
    """Put a live 'Material Variant' enum on the import's top transform(s) and
    arm the scriptJob so the Attribute Editor dropdown switches instantly.

    Called automatically after an import that has variants. `nodes` may be a
    single transform, a list, or None (auto-pick the variant-aware roots).
    Returns the list of control nodes that now carry the switch.
    """
    variants = list_material_variants()
    if not variants:
        return []
    if nodes is None:
        nodes = _variant_control_candidates()
    elif isinstance(nodes, str):
        nodes = [nodes]

    controls = []
    for node in (nodes or []):
        if not node or not cmds.objExists(node):
            continue
        _ensure_variant_enum(node, variants)
        _arm_variant_scriptjob(node)
        controls.append(node)

    if controls:
        # Pre-warm: assign every variant once (with a viewport refresh) so Maya
        # compiles each variant's shader + loads its textures NOW. Otherwise the
        # FIRST live switch shows a brief blank/uncompiled flash before caching.
        # Capped so a huge variant set doesn't stall the import.
        if 1 < len(variants) <= 16:
            try:
                for v in variants:
                    set_active_material_variant(v, quiet=True)
                    cmds.refresh()
            except Exception:
                pass
        _apply_variant_from_enum(controls[0])
        om.MGlobal.displayInfo(
            "[glb_io] %d material variant(s) — select the object and use the "
            "'Material Variant' dropdown in the Channel Box / Attribute Editor "
            "to switch: %s" % (len(variants), ", ".join(variants))
        )
    return controls


def select_variant_control():
    """Menu helper: (re)arm the switch and select the control node(s) so the
    Attribute Editor shows the 'Material Variant' dropdown. Use this after
    reopening a saved scene (scriptJobs don't persist across save/load)."""
    controls = setup_variant_switch()
    if not controls:
        cmds.confirmDialog(
            title="Material Variants",
            message="No material variants found in this scene.\n\n"
                    "Import a glTF that uses KHR_materials_variants first.",
            button=["OK"],
        )
        return
    cmds.select(controls, replace=True)
    cmds.confirmDialog(
        title="Material Variants",
        message=("Selected the variant control node(s).\n\n"
                 "In the Channel Box / Attribute Editor, change the "
                 "'Material Variant' dropdown to switch looks instantly."),
        button=["OK"],
    )
