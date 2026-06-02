"""File > Import Specifics UI for the glTF 2.0 importer.

Maya calls a MEL global proc registered as `optionsScript` when building
the Import dialog's File Type Specific Options panel. That MEL proc is a
thin wrapper that delegates here (see plug-ins/maya_glb_io.py for the
mel.eval-defined proc).

The MEL proc invokes us with one of three actions:
    "post"  - build UI in the given parent formLayout
    "query" - read UI state, serialise to options string, call back to Maya

When user clicks Import, Maya stores the queried options string and
hands it back to file_translator.reader() as the optionsString parameter.

The same UI is reused for the standalone Preferences dialog (with a
Save/Cancel button row, no Maya formLayout integration).
"""
from __future__ import annotations

from maya import cmds, mel

from maya_glb_io import _prefs


# Stable widget names so the query side can find them
_PANEL_PREFIX = "glbIO_opt_"
_W_TARGET = _PANEL_PREFIX + "target"
_W_SCALE = _PANEL_PREFIX + "scale"
_W_USE_NAMESPACE = _PANEL_PREFIX + "useNamespace"
_W_SKIP_MATS = _PANEL_PREFIX + "skipMaterials"
_W_SKIP_SKIN = _PANEL_PREFIX + "skipSkinning"
_W_IMPORT_ANIM = _PANEL_PREFIX + "importAnimation"
_W_CENTER_PIVOT = _PANEL_PREFIX + "centerPivot"
_W_CENTER_ORIGIN = _PANEL_PREFIX + "centerOnOrigin"
_W_DROP_FLOOR = _PANEL_PREFIX + "dropToFloor"
_W_BAKE = _PANEL_PREFIX + "bakeTransforms"
_W_SPECGLOSS = _PANEL_PREFIX + "convertSpecGloss"
_W_MAT_PREFIX = _PANEL_PREFIX + "materialPrefix"
_W_EMISSION_LUM = _PANEL_PREFIX + "emissionLuminance"


# Shader-target dropdown mapping. Empty string == "use the persisted preference".
TARGET_OPTIONS = [
    ("Use Default Preference", ""),
    ("Auto (by Maya version)", "auto"),
    ("StingrayPBS",            "stingray"),
    ("openPBR",                "openpbr"),
    ("Phong (legacy)",         "phong"),
    ("Arnold (aiStandardSurface)", "arnold"),
]


SCALE_OPTIONS = [
    ("Auto (detect scene unit)", "auto"),
    ("1.0 (glTF native meters)", "1.0"),
    ("100.0 (meters -> cm)",     "100.0"),
]


# Export-side widget names — a distinct prefix so they never collide with the
# import panel widgets (both can exist at once if both windows are open).
_EXP_PREFIX = "glbIO_expopt_"
_WX_FORMAT = _EXP_PREFIX + "format"
_WX_EMBED = _EXP_PREFIX + "embedTextures"
_WX_CONVERT_UNITS = _EXP_PREFIX + "convertUnits"
_WX_COPY_TEX = _EXP_PREFIX + "copyTextures"
_WX_APPLY_XFORM = _EXP_PREFIX + "applyTransforms"
_WX_EXPORT_SKIN = _EXP_PREFIX + "exportSkin"
_WX_EXPORT_ANIM = _EXP_PREFIX + "exportAnimation"
_WX_YUP = _EXP_PREFIX + "yUp"


FORMAT_OPTIONS = [
    ("glTF Binary (.glb)", "glb"),
    ("glTF + .bin (.gltf)", "gltf"),
]


# ---------------------------------------------------------------------------
# MEL-callable entry
# ---------------------------------------------------------------------------

def handle(parent, action, initial_settings, result_callback):
    """Dispatch from the MEL optionsScript proc."""
    if action == "post":
        build_ui(parent, initial_settings)
    elif action == "query":
        result = query_ui()
        # Escape backslashes / double quotes for the MEL eval round-trip
        escaped = result.replace("\\", "\\\\").replace('"', '\\"')
        mel.eval(result_callback + ' "' + escaped + '"')
    return 1


# ---------------------------------------------------------------------------
# UI build / query
# ---------------------------------------------------------------------------

def build_ui(parent, initial_settings):
    """Populate the File Type Specific Options panel inside the given parent layout."""
    opts = _resolve_initial_options(initial_settings)

    cmds.setParent(parent)

    outer = cmds.columnLayout(adjustableColumn=True, rowSpacing=4)

    cmds.frameLayout(
        label="glTF 2.0 Import",
        collapsable=False,
        marginHeight=6,
        marginWidth=8,
    )
    cmds.columnLayout(adjustableColumn=True, rowSpacing=3)

    _option_menu(_W_TARGET, "Shader Target", TARGET_OPTIONS, opts.get("target", ""))
    _option_menu(_W_SCALE, "Unit Scale", SCALE_OPTIONS, opts.get("scale", "auto"))
    cmds.separator(height=8, style="none")
    _checkbox(_W_USE_NAMESPACE, "Use Namespace (prefix:)", opts.get("useNamespace", False))
    _checkbox(_W_SKIP_MATS, "Skip Materials (geometry only)", opts.get("skipMaterials", False))
    _checkbox(_W_SKIP_SKIN, "Skip Skinning (no skinCluster)", opts.get("skipSkinning", False))
    _checkbox(_W_IMPORT_ANIM, "Import Animation (keyframes)", opts.get("importAnimation", True))
    cmds.separator(height=4, style="none")
    _checkbox(_W_CENTER_PIVOT, "Center Pivot on bbox", opts.get("centerPivot", False))
    _checkbox(_W_CENTER_ORIGIN, "Center Geometry on Origin", opts.get("centerOnOrigin", False))
    _checkbox(_W_DROP_FLOOR, "Drop to Floor (sit on grid)", opts.get("dropToFloor", True))
    _checkbox(_W_BAKE, "Bake transforms on import (clean)", opts.get("bakeTransforms", True))

    cmds.setParent("..")  # columnLayout
    cmds.setParent("..")  # frameLayout

    cmds.frameLayout(
        label="Advanced",
        collapsable=True,
        collapse=True,
        marginHeight=6,
        marginWidth=8,
    )
    cmds.columnLayout(adjustableColumn=True, rowSpacing=3)

    _checkbox(_W_SPECGLOSS, "Convert spec-gloss to metal-rough", opts.get("convertSpecGloss", True))
    _text_field(_W_MAT_PREFIX, "Material Name Prefix", opts.get("materialPrefix", ""))
    _text_field(_W_EMISSION_LUM, "Emission Luminance (nits)",
                str(opts.get("emissionLuminance", "1000.0")))

    cmds.setParent("..")  # columnLayout
    cmds.setParent("..")  # frameLayout

    cmds.setParent("..")  # outer columnLayout

    return outer


def query_ui():
    """Read every UI widget, return the serialised options string."""
    opts = {
        "target":           _query_option_menu(_W_TARGET, TARGET_OPTIONS),
        "scale":            _query_option_menu(_W_SCALE, SCALE_OPTIONS),
        "useNamespace":     _query_checkbox(_W_USE_NAMESPACE),
        "skipMaterials":    _query_checkbox(_W_SKIP_MATS),
        "skipSkinning":     _query_checkbox(_W_SKIP_SKIN),
        "importAnimation":  _query_checkbox(_W_IMPORT_ANIM),
        "centerPivot":      _query_checkbox(_W_CENTER_PIVOT),
        "centerOnOrigin":   _query_checkbox(_W_CENTER_ORIGIN),
        "dropToFloor":      _query_checkbox(_W_DROP_FLOOR),
        "bakeTransforms":   _query_checkbox(_W_BAKE),
        "convertSpecGloss": _query_checkbox(_W_SPECGLOSS),
        "materialPrefix":   _query_text_field(_W_MAT_PREFIX),
        "emissionLuminance": _query_text_field(_W_EMISSION_LUM),
    }
    return _prefs.serialize(opts)


# ---------------------------------------------------------------------------
# Widget helpers
# ---------------------------------------------------------------------------

def _option_menu(name, label, options, current_value):
    """options = [(label_text, internal_value), ...]."""
    if cmds.optionMenuGrp(name, exists=True):
        cmds.deleteUI(name)
    cmds.optionMenuGrp(name, label=label, columnWidth2=(300, 220), columnAlign2=("right", "left"))
    sel_idx = 1
    for i, (label_text, value) in enumerate(options, start=1):
        cmds.menuItem(label=label_text)
        if value == current_value:
            sel_idx = i
    cmds.optionMenuGrp(name, edit=True, select=sel_idx)


def _query_option_menu(name, options):
    if not cmds.optionMenuGrp(name, exists=True):
        return options[0][1]
    sel = cmds.optionMenuGrp(name, query=True, select=True) or 1
    return options[sel - 1][1]


def _checkbox(name, label, value):
    if cmds.checkBoxGrp(name, exists=True):
        cmds.deleteUI(name)
    cmds.checkBoxGrp(
        name,
        label=label,
        value1=bool(value),
        columnWidth2=(300, 80),
        columnAlign2=("right", "left"),
    )


def _query_checkbox(name):
    if not cmds.checkBoxGrp(name, exists=True):
        return False
    return bool(cmds.checkBoxGrp(name, query=True, value1=True))


def _text_field(name, label, value):
    if cmds.textFieldGrp(name, exists=True):
        cmds.deleteUI(name)
    cmds.textFieldGrp(
        name,
        label=label,
        text=value or "",
        columnWidth2=(300, 220),
        columnAlign2=("right", "left"),
    )


def _query_text_field(name):
    if not cmds.textFieldGrp(name, exists=True):
        return ""
    return cmds.textFieldGrp(name, query=True, text=True) or ""


def _resolve_initial_options(initial_settings):
    """If `initial_settings` is non-empty, parse it. Otherwise pull defaults from prefs.

    This lets a fresh File > Import dialog open already populated with the user's
    saved preferences (set via glTF 2.0 > Preferences...).
    """
    if initial_settings:
        return _prefs.parse(initial_settings)
    return _prefs.get_all()


# ---------------------------------------------------------------------------
# Standalone Preferences window (separate from File > Import)
# ---------------------------------------------------------------------------

PREFS_WIN_ID = "glbIOPreferencesWindow"


def show_preferences_window():
    if cmds.window(PREFS_WIN_ID, exists=True):
        cmds.deleteUI(PREFS_WIN_ID)

    cmds.window(
        PREFS_WIN_ID,
        title="glTF 2.0 Importer Preferences",
        widthHeight=(560, 470),
        sizeable=True,
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=6, columnAttach=("both", 10))
    cmds.text(
        label="Defaults applied to every import. Override per-import in File > Import.",
        align="left",
        wordWrap=True,
        height=30,
    )

    # Reuse the same widgets as File Specifics — fresh build inside the window
    build_ui(cmds.columnLayout(adjustableColumn=True), "")

    cmds.setParent("..")  # outer columnLayout from build_ui

    cmds.separator(height=10, style="in")

    cmds.rowLayout(numberOfColumns=4, columnAlign4=("center",) * 4, adjustableColumn=4)
    cmds.button(label="Save", width=100, command=_save_prefs_and_close)
    cmds.button(label="Reset to Defaults", width=140, command=_reset_prefs_and_rebuild)
    cmds.button(label="Cancel", width=100, command=_close_prefs_window)
    cmds.text(label="", width=10)
    cmds.setParent("..")

    cmds.showWindow(PREFS_WIN_ID)


def _save_prefs_and_close(*args):
    serialised = query_ui()
    parsed = _prefs.parse(serialised)
    for k, v in parsed.items():
        _prefs.set_(k, v)
    cmds.deleteUI(PREFS_WIN_ID)
    cmds.confirmDialog(title="Saved", message="Import preferences updated.", button=["OK"])


def _reset_prefs_and_rebuild(*args):
    _prefs.reset_all()
    cmds.deleteUI(PREFS_WIN_ID)
    show_preferences_window()


def _close_prefs_window(*args):
    if cmds.window(PREFS_WIN_ID, exists=True):
        cmds.deleteUI(PREFS_WIN_ID)


# ---------------------------------------------------------------------------
# Export UI build / query (separate widgets from the import side)
# ---------------------------------------------------------------------------

def build_export_ui(parent, opts):
    """Populate the given parent layout with the export option widgets.

    `opts` is a dict of current values (typically from _prefs.get_all_export()).
    Reuses the generic _option_menu/_checkbox helpers.
    """
    cmds.setParent(parent)

    outer = cmds.columnLayout(adjustableColumn=True, rowSpacing=4)

    cmds.frameLayout(
        label="glTF 2.0 Export",
        collapsable=False,
        marginHeight=6,
        marginWidth=8,
    )
    cmds.columnLayout(adjustableColumn=True, rowSpacing=3)

    _option_menu(_WX_FORMAT, "File Format", FORMAT_OPTIONS, opts.get("format", "glb"))
    cmds.separator(height=8, style="none")
    _checkbox(_WX_EMBED, "Embed Textures", opts.get("embedTextures", False))
    _checkbox(_WX_CONVERT_UNITS, "Convert Units to Metres", opts.get("convertUnits", True))
    _checkbox(_WX_COPY_TEX, "Copy Textures Next to File", opts.get("copyTextures", True))
    _checkbox(_WX_APPLY_XFORM, "Bake Transforms into Verts", opts.get("applyTransforms", False))
    cmds.separator(height=4, style="none")
    _checkbox(_WX_EXPORT_SKIN, "Export Skinning (skinClusters)", opts.get("exportSkin", True))
    _checkbox(_WX_EXPORT_ANIM, "Export Animation (keyframes)", opts.get("exportAnimation", True))

    cmds.setParent("..")  # columnLayout
    cmds.setParent("..")  # frameLayout

    cmds.setParent("..")  # outer columnLayout

    return outer


def query_export_ui():
    """Read every export UI widget, return the serialised options string."""
    opts = {
        "format":          _query_option_menu(_WX_FORMAT, FORMAT_OPTIONS),
        "embedTextures":   _query_checkbox(_WX_EMBED),
        "convertUnits":    _query_checkbox(_WX_CONVERT_UNITS),
        "copyTextures":    _query_checkbox(_WX_COPY_TEX),
        "applyTransforms": _query_checkbox(_WX_APPLY_XFORM),
        "exportSkin":      _query_checkbox(_WX_EXPORT_SKIN),
        "exportAnimation": _query_checkbox(_WX_EXPORT_ANIM),
    }
    return _prefs.serialize(opts)


# ---------------------------------------------------------------------------
# Standalone Export Preferences window
# ---------------------------------------------------------------------------
#
# NOTE: Maya's MPxFileTranslator shares ONE optionsScript between import and
# export, so we cannot cleanly show a distinct File > Export Specifics panel.
# This standalone window is the deliverable; the menu's Export Selection/Scene
# commands consume the saved prefs via _prefs.get_all_export().

EXPORT_PREFS_WIN_ID = "glbIOExportPreferencesWindow"


def show_export_preferences_window():
    if cmds.window(EXPORT_PREFS_WIN_ID, exists=True):
        cmds.deleteUI(EXPORT_PREFS_WIN_ID)

    cmds.window(
        EXPORT_PREFS_WIN_ID,
        title="glTF 2.0 Exporter Preferences",
        widthHeight=(560, 400),
        sizeable=True,
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=6, columnAttach=("both", 10))
    cmds.text(
        label="Defaults applied to every export (Export Selection / Export Scene).",
        align="left",
        wordWrap=True,
        height=30,
    )

    build_export_ui(cmds.columnLayout(adjustableColumn=True), _prefs.get_all_export())

    cmds.setParent("..")  # outer columnLayout from build_export_ui

    cmds.separator(height=10, style="in")

    cmds.rowLayout(numberOfColumns=4, columnAlign4=("center",) * 4, adjustableColumn=4)
    cmds.button(label="Save", width=100, command=_save_export_prefs_and_close)
    cmds.button(label="Reset to Defaults", width=140, command=_reset_export_prefs_and_rebuild)
    cmds.button(label="Cancel", width=100, command=_close_export_prefs_window)
    cmds.text(label="", width=10)
    cmds.setParent("..")

    cmds.showWindow(EXPORT_PREFS_WIN_ID)


def _save_export_prefs_and_close(*args):
    serialised = query_export_ui()
    parsed = _prefs.parse(serialised)
    for k, v in parsed.items():
        _prefs.set_export(k, v)
    cmds.deleteUI(EXPORT_PREFS_WIN_ID)
    cmds.confirmDialog(title="Saved", message="Export preferences updated.", button=["OK"])


def _reset_export_prefs_and_rebuild(*args):
    _prefs.reset_all_export()
    cmds.deleteUI(EXPORT_PREFS_WIN_ID)
    show_export_preferences_window()


def _close_export_prefs_window(*args):
    if cmds.window(EXPORT_PREFS_WIN_ID, exists=True):
        cmds.deleteUI(EXPORT_PREFS_WIN_ID)


# ---------------------------------------------------------------------------
# Material Variants switcher window (KHR_materials_variants)
# ---------------------------------------------------------------------------

VARIANTS_WIN_ID = "glbIOMaterialVariantsWindow"
_W_VARIANT_MENU = "glbIO_variantMenu"


def show_variants_window():
    """Open a small window to switch the scene's active KHR_materials_variants
    variant. Lists the variants found across all imported shapes; picking one
    and clicking Apply reassigns every variant-aware shape to that variant's
    stored shading group.
    """
    # Import lazily to avoid an import cycle (commands imports _options_ui).
    from maya_glb_io import commands

    if cmds.window(VARIANTS_WIN_ID, exists=True):
        cmds.deleteUI(VARIANTS_WIN_ID)

    cmds.window(
        VARIANTS_WIN_ID,
        title="glTF Material Variants",
        widthHeight=(360, 150),
        sizeable=True,
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=8, columnAttach=("both", 12))
    cmds.separator(height=6, style="none")

    variants = commands.list_material_variants()

    if not variants:
        cmds.text(
            label="No material variants in this scene.",
            align="left",
            wordWrap=True,
            height=40,
        )
        cmds.separator(height=6, style="none")
        cmds.button(label="Close", command=_close_variants_window)
        cmds.showWindow(VARIANTS_WIN_ID)
        return

    cmds.text(
        label="Switch the active material variant for all imported geometry:",
        align="left",
        wordWrap=True,
        height=30,
    )

    if cmds.optionMenu(_W_VARIANT_MENU, exists=True):
        cmds.deleteUI(_W_VARIANT_MENU)
    cmds.optionMenu(_W_VARIANT_MENU, label="Variant")
    for v in variants:
        cmds.menuItem(label=v)

    cmds.separator(height=6, style="none")
    cmds.button(label="Apply", height=28, command=_apply_variant)

    cmds.showWindow(VARIANTS_WIN_ID)


def _apply_variant(*args):
    from maya_glb_io import commands

    if not cmds.optionMenu(_W_VARIANT_MENU, exists=True):
        return
    selected = cmds.optionMenu(_W_VARIANT_MENU, query=True, value=True)
    if not selected:
        return
    count = commands.set_active_material_variant(selected)
    cmds.confirmDialog(
        title="Material Variant",
        message="Variant '{0}' applied to {1} shape(s).".format(selected, count),
        button=["OK"],
    )


def _close_variants_window(*args):
    if cmds.window(VARIANTS_WIN_ID, exists=True):
        cmds.deleteUI(VARIANTS_WIN_ID)
