"""Maya plug-in entry point for the free MIT glTF 2.0 importer.

Loaded by Maya's Plug-in Manager. Registers an MPxFileTranslator so
.glb / .gltf files appear in File > Import (with a full options panel
in File Type Specific Options), and installs a top-level GLB I/O menu.

API 1.0 (`maya.OpenMaya` + `maya.OpenMayaMPx`) because MPxFileTranslator
is only exposed in 1.0. Internal modules use API 2.0 freely.
"""
import os
import sys
import traceback

import maya.OpenMaya as om
import maya.OpenMayaMPx as ompx
import maya.mel as mel


PLUGIN_NAME = "maya_glb_io"
PLUGIN_VERSION = "1.0.1"
PLUGIN_VENDOR = "annomodtutorials"

# Name of the MEL global proc Maya calls to build the File Specifics panel.
# Defined inline (via mel.eval) during initializePlugin so we don't need to
# place a .mel file on MAYA_SCRIPT_PATH.
OPTIONS_PROC = "glbIOFileImportOptions"


def _bootstrap_sys_path():
    """Add scripts/ next to plug-ins/ to sys.path if the .mod file didn't."""
    plugin_file = None
    try:
        plugin_file = __file__
    except NameError:
        import inspect
        try:
            plugin_file = inspect.getfile(_bootstrap_sys_path)
        except (TypeError, OSError):
            return

    if not plugin_file:
        return

    plugin_dir = os.path.dirname(os.path.abspath(plugin_file))
    scripts_dir = os.path.normpath(os.path.join(plugin_dir, os.pardir, "scripts"))
    if os.path.isdir(scripts_dir) and scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)


def _define_options_mel_proc():
    """Install the global MEL proc Maya calls for the File Specifics panel.

    The proc just hands the four args to Python. Using a raw triple-quoted
    Python string passed via repr() to MEL keeps escaping sane.
    """
    mel.eval("""
global proc int """ + OPTIONS_PROC + """(
    string $parent, string $action,
    string $initialSettings, string $resultCallback)
{
    string $cmd = "import maya_glb_io._options_ui as ui; ";
    $cmd += "ui.handle(";
    $cmd += "r'''" + $parent + "''',";
    $cmd += "r'''" + $action + "''',";
    $cmd += "r'''" + $initialSettings + "''',";
    $cmd += "r'''" + $resultCallback + "''')";
    python($cmd);
    return 1;
}
""")


def _force_reload_modules():
    """Reload every maya_glb_io.* module so re-enabling the plug-in picks up
    source-code changes without a full Maya restart.

    Maya's Plug-in Manager runs initializePlugin again on retick but does NOT
    re-import already-loaded Python modules — they stay cached in sys.modules.
    For active development we want untick/retick to truly reload.
    """
    import importlib
    import sys
    prefix = "maya_glb_io"
    to_reload = sorted(
        (m for m in list(sys.modules) if m == prefix or m.startswith(prefix + ".")),
        key=lambda m: m.count("."),
        reverse=True,  # children before parents
    )
    for m in to_reload:
        try:
            importlib.reload(sys.modules[m])
        except Exception:
            pass


def initializePlugin(plugin_mobject):
    _bootstrap_sys_path()
    _force_reload_modules()
    fn = ompx.MFnPlugin(plugin_mobject, PLUGIN_VENDOR, PLUGIN_VERSION, "Any")

    try:
        _define_options_mel_proc()
    except Exception:
        om.MGlobal.displayError(
            "[" + PLUGIN_NAME + "] failed to define MEL options proc:\n"
            + traceback.format_exc()
        )

    try:
        from maya_glb_io.file_translator import GLBFileTranslator

        fn.registerFileTranslator(
            GLBFileTranslator.NAME,
            None,
            GLBFileTranslator.creator,
            OPTIONS_PROC,         # MEL proc that builds the Specifics panel
            "",                   # default options string
            True,
        )
    except Exception:
        om.MGlobal.displayError(
            "[" + PLUGIN_NAME + "] file translator register failed:\n"
            + traceback.format_exc()
        )
        raise

    try:
        from maya_glb_io import menu
        menu.install()
    except Exception:
        om.MGlobal.displayWarning(
            "[" + PLUGIN_NAME + "] menu install failed (non-fatal):\n"
            + traceback.format_exc()
        )

    # One-time, throttled, fail-silent "newer version available" check.
    try:
        import maya.utils
        from maya_glb_io import _update_check
        maya.utils.executeDeferred(_update_check.auto_check)
    except Exception:
        pass


def uninitializePlugin(plugin_mobject):
    fn = ompx.MFnPlugin(plugin_mobject)

    try:
        from maya_glb_io import menu
        menu.remove()
    except Exception:
        pass

    try:
        from maya_glb_io.file_translator import GLBFileTranslator
        fn.deregisterFileTranslator(GLBFileTranslator.NAME)
    except Exception:
        om.MGlobal.displayError(
            "[" + PLUGIN_NAME + "] file translator deregister failed:\n"
            + traceback.format_exc()
        )
        raise
