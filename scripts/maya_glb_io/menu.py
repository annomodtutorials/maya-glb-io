"""Top-level 'glTF 2.0' menu installed in Maya's main window."""
from __future__ import annotations

from maya import cmds, mel


MENU_ID = "glbIO_glTFMenu"
MENU_LABEL = "glTF 2.0"


def install():
    if cmds.menu(MENU_ID, exists=True):
        cmds.deleteUI(MENU_ID, menu=True)

    main_window = mel.eval("$tmp = $gMainWindow")
    cmds.menu(MENU_ID, parent=main_window, label=MENU_LABEL, tearOff=True)

    cmds.menuItem(
        parent=MENU_ID,
        label="Import .glb / .gltf...",
        annotation="Open glTF import dialog (uses saved preferences)",
        command="from maya_glb_io import commands; commands.show_import_dialog()",
    )
    cmds.menuItem(
        parent=MENU_ID,
        label="Batch Import...",
        annotation="Import several glTF files in one go",
        command="from maya_glb_io import commands; commands.show_batch_import_dialog()",
    )

    cmds.menuItem(parent=MENU_ID, divider=True)

    cmds.menuItem(
        parent=MENU_ID,
        label="Export Selection to glTF/GLB...",
        annotation="Export the selected transforms to a .glb or .gltf file",
        command="from maya_glb_io import commands; commands.show_export_dialog('selection')",
    )
    cmds.menuItem(
        parent=MENU_ID,
        label="Export Scene to glTF/GLB...",
        annotation="Export every top-level transform in the scene",
        command="from maya_glb_io import commands; commands.show_export_dialog('scene')",
    )

    cmds.menuItem(parent=MENU_ID, divider=True)

    cmds.menuItem(
        parent=MENU_ID,
        label="Material Variants: Find/Arm Switch",
        annotation="Imports auto-add a 'Material Variant' dropdown to the object "
                   "(switch it live in the Channel Box / Attribute Editor). Use "
                   "this only to re-arm + select the switch after reopening a "
                   "saved scene (scriptJobs don't persist across save/load).",
        command="from maya_glb_io import commands; commands.select_variant_control()",
    )

    cmds.menuItem(
        parent=MENU_ID,
        label="Install Draco Decoder (one-time)...",
        annotation="Pre-install the DracoPy decoder needed for Draco-compressed "
                   "glTFs (common on Sketchfab). The importer installs it "
                   "automatically on first use too, but doing it here gives a "
                   "heads-up so Maya doesn't appear frozen mid-import.",
        command="from maya_glb_io import commands; commands.install_draco_decoder()",
    )

    cmds.menuItem(parent=MENU_ID, divider=True)

    cmds.menuItem(
        parent=MENU_ID,
        label="Import Preferences...",
        annotation="Set default import options (shader target, scale, etc.)",
        command="from maya_glb_io import commands; commands.show_preferences()",
    )
    cmds.menuItem(
        parent=MENU_ID,
        label="Export Preferences...",
        annotation="Set default export options (format, textures, skinning, etc.)",
        command="from maya_glb_io import commands; commands.show_export_preferences()",
    )

    cmds.menuItem(parent=MENU_ID, divider=True)

    cmds.menuItem(
        parent=MENU_ID,
        label="About maya_glb_io...",
        command="from maya_glb_io import commands; commands.show_about()",
    )


def remove():
    if cmds.menu(MENU_ID, exists=True):
        cmds.deleteUI(MENU_ID, menu=True)
