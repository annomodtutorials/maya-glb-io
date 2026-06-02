"""Version + plug-in detection shim for Maya 2022 / 2023 / 2024 (and forward).

Every conditional that depends on Maya, Python, Qt, or which renderer plug-ins
are loaded lives here so the rest of the codebase stays straightforward.

Floor: Maya 2022 (Python 3.7, PySide2). Anything younger is unsupported.
"""
from __future__ import annotations

import sys

from maya import cmds


# --- Maya / Python -----------------------------------------------------------

def maya_version() -> int:
    """Major version year, e.g. 2024."""
    raw = cmds.about(version=True)
    return int(raw.split()[0])


def python_version() -> tuple:
    return sys.version_info[:2]


# --- Qt ----------------------------------------------------------------------

def qt_binding() -> str:
    """'PySide2' for Maya 2022-2024, 'PySide6' for Maya 2025+."""
    if maya_version() >= 2025:
        return "PySide6"
    return "PySide2"


def import_qt():
    """Returns (QtCore, QtGui, QtWidgets, wrapInstance) for the active Qt binding."""
    if qt_binding() == "PySide6":
        from PySide6 import QtCore, QtGui, QtWidgets
        from shiboken6 import wrapInstance
    else:
        from PySide2 import QtCore, QtGui, QtWidgets
        from shiboken2 import wrapInstance
    return QtCore, QtGui, QtWidgets, wrapInstance


def maya_main_window():
    """Return the Maya main window wrapped as a Qt QWidget."""
    from maya import OpenMayaUI
    _, _, QtWidgets, wrapInstance = import_qt()
    ptr = OpenMayaUI.MQtUtil.mainWindow()
    if ptr is None:
        return None
    return wrapInstance(int(ptr), QtWidgets.QWidget)


# --- Renderer / shader-plug-in detection ------------------------------------
#
# These functions only REPORT availability. They never decide which shader
# graph to build — that's the importer's "shader target" choice (see
# project-glb-importer-shader-targets memory).

def _plugin_loaded(name: str) -> bool:
    if not cmds.pluginInfo(name, query=True, registered=True):
        return False
    return bool(cmds.pluginInfo(name, query=True, loaded=True))


def ensure_plugin_loaded(name: str) -> bool:
    """Try to load a plug-in by name. Returns True if loaded after the call."""
    if _plugin_loaded(name):
        return True
    try:
        cmds.loadPlugin(name, quiet=True)
    except RuntimeError:
        return False
    return _plugin_loaded(name)


# Per-renderer / per-shader-system availability checks. v0.1 only uses
# stingray_pbs_available; the others stand by for the v0.2 multi-target picker.

def stingray_pbs_available() -> bool:
    """StingrayPBS node type — provided by `shaderFXPlugin`. Bundled with Maya 2017+."""
    return ensure_plugin_loaded("shaderFXPlugin") or "StingrayPBS" in (cmds.allNodeTypes() or [])


def openpbr_available() -> bool:
    """openPBRSurface node type — Maya 2025+ only."""
    return "openPBRSurface" in (cmds.allNodeTypes() or [])


def arnold_available() -> bool:
    """MtoA — `mtoa` plug-in. Bundled but user can disable."""
    return _plugin_loaded("mtoa")


def redshift_available() -> bool:
    return _plugin_loaded("redshift4maya")


def vray_available() -> bool:
    return _plugin_loaded("vrayformaya")


def renderman_available() -> bool:
    return _plugin_loaded("RenderMan_for_Maya")
