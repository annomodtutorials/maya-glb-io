"""Maya GLB I/O - drag-and-drop installer.

HOW TO USE
==========
1. Start Autodesk Maya.
2. Drag THIS file (install.py) from your file browser into the Maya viewport
   (the big 3D area).
3. A confirmation dialog appears. Click OK.
4. A "GLB I/O" menu shows up in Maya's main menu bar.

WHAT IT DOES
============
When you drop a Python file into Maya, Maya looks for a function called
``onMayaDroppedPythonFile`` in it and calls that. We use it to:

  1. Locate this repository (the folder install.py lives in).
  2. Write a small Maya *module* file (maya_glb_io.mod) into the user's Maya
     ``modules`` directory. That .mod tells Maya where this repo's ``scripts/``
     and ``plug-ins/`` folders are, so the plug-in is found on every launch.
  3. Make the plug-in loadable in the CURRENT session (without restarting) by
     adding ``scripts/`` to ``sys.path`` and ``plug-ins/`` to the plug-in path.
  4. Load the plug-in and set it to auto-load next time.
  5. Confirm with a dialog.

The repo is NOT copied anywhere - the .mod points back at wherever you unzipped
it, so keep this folder where it is after installing.

This file is intentionally dependency-light and defensive: every step is wrapped
so a single failure produces a clear message instead of a silent no-op.
"""

import os
import sys
import traceback


PLUGIN_NAME = "maya_glb_io"          # the .py file in plug-ins/ (without extension)
MODULE_NAME = "maya_glb_io"          # name used in the .mod file
MODULE_VERSION = "1.0.1"


def _repo_root():
    """Absolute path to the folder this installer lives in (the repo root)."""
    try:
        here = os.path.abspath(__file__)
    except NameError:
        # __file__ is undefined in some exec contexts; fall back to inspect.
        import inspect
        here = os.path.abspath(inspect.getfile(_repo_root))
    return os.path.dirname(here)


def _maya_app_dir():
    """The user's Maya application directory (where prefs/modules live).

    Prefer Maya's own answer (``cmds.internalVar(userAppDir=True)``); fall back
    to the common per-OS location if cmds isn't available for some reason.
    """
    try:
        from maya import cmds
        app_dir = cmds.internalVar(userAppDir=True)
        if app_dir:
            return os.path.normpath(app_dir)
    except Exception:
        pass

    # Fallback: ~/Documents/maya (Windows) or ~/Library/Preferences/Autodesk/maya
    # (macOS) or ~/maya (Linux). internalVar above almost always succeeds, so
    # this is just a safety net.
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        return os.path.join(home, "Documents", "maya")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Preferences", "Autodesk", "maya")
    return os.path.join(home, "maya")


def _modules_dir():
    """The Maya ``modules`` directory, created if it doesn't exist."""
    modules = os.path.join(_maya_app_dir(), "modules")
    if not os.path.isdir(modules):
        os.makedirs(modules)
    return modules


def _write_mod_file(repo_root, modules_dir):
    """Write maya_glb_io.mod into the user's modules dir pointing at repo_root.

    Returns the full path of the written .mod file.
    """
    # Maya .mod files want forward slashes and are happiest with an absolute path.
    repo_fwd = repo_root.replace("\\", "/")
    mod_path = os.path.join(modules_dir, MODULE_NAME + ".mod")
    contents = (
        "+ {name} {ver} {root}\n"
        "PYTHONPATH +:= scripts\n"
        "MAYA_PLUG_IN_PATH +:= plug-ins\n"
        "XBMLANGPATH +:= icons\n"
    ).format(name=MODULE_NAME, ver=MODULE_VERSION, root=repo_fwd)

    with open(mod_path, "w") as fh:
        fh.write(contents)
    return mod_path


def _prime_current_session(repo_root):
    """Make the plug-in loadable RIGHT NOW, without a Maya restart.

    Adds ``scripts/`` to ``sys.path`` and registers ``plug-ins/`` on Maya's
    plug-in search path so ``loadPlugin`` can find it this session. (The .mod
    handles all future sessions.)
    """
    scripts_dir = os.path.join(repo_root, "scripts")
    plugins_dir = os.path.join(repo_root, "plug-ins")

    if os.path.isdir(scripts_dir) and scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    # Add plug-ins/ to MAYA_PLUG_IN_PATH so loadPlugin by name resolves it.
    if os.path.isdir(plugins_dir):
        existing = os.environ.get("MAYA_PLUG_IN_PATH", "")
        parts = existing.split(os.pathsep) if existing else []
        if plugins_dir not in parts:
            os.environ["MAYA_PLUG_IN_PATH"] = (
                plugins_dir + (os.pathsep + existing if existing else "")
            )
    return plugins_dir


def _load_plugin(plugins_dir):
    """Load (or reload) the plug-in and turn on auto-load.

    Loads by absolute path so it works even before Maya rescans its module
    paths. Returns a short status string for the success dialog.
    """
    from maya import cmds

    plugin_file = os.path.join(plugins_dir, PLUGIN_NAME + ".py")

    # If a previous version is already loaded, unload it first so the new code
    # takes effect cleanly.
    try:
        if cmds.pluginInfo(PLUGIN_NAME, query=True, loaded=True):
            cmds.unloadPlugin(PLUGIN_NAME)
    except Exception:
        # Not loaded / unknown name - fine, we'll load fresh below.
        pass

    # Prefer loading by full path (most robust right after install); fall back
    # to loading by name if the path form isn't accepted.
    try:
        cmds.loadPlugin(plugin_file)
    except Exception:
        cmds.loadPlugin(PLUGIN_NAME + ".py")

    # Make it come back automatically next time Maya starts.
    try:
        cmds.pluginInfo(PLUGIN_NAME, edit=True, autoload=True)
    except Exception:
        pass

    return plugin_file


def _confirm(title, message):
    """Best-effort dialog; falls back to a printed message if UI is unavailable."""
    try:
        from maya import cmds
        cmds.confirmDialog(title=title, message=message, button=["OK"])
    except Exception:
        print("[%s] %s\n%s" % (MODULE_NAME, title, message))


def onMayaDroppedPythonFile(*args):
    """Entry point Maya calls when this file is dropped into the viewport."""
    try:
        repo_root = _repo_root()

        # Sanity check: make sure we're actually in the repo (scripts + plug-ins).
        scripts_dir = os.path.join(repo_root, "scripts")
        plugins_dir = os.path.join(repo_root, "plug-ins")
        if not (os.path.isdir(scripts_dir) and os.path.isdir(plugins_dir)):
            _confirm(
                "Maya GLB I/O - install failed",
                "Could not find the 'scripts' and 'plug-ins' folders next to "
                "install.py.\n\nMake sure you unzipped the whole repository and "
                "dropped the install.py that sits at the top of it.\n\n"
                "Looked in:\n" + repo_root,
            )
            return

        # 1. Persistent install via a .mod file.
        modules_dir = _modules_dir()
        mod_path = _write_mod_file(repo_root, modules_dir)

        # 2. Make it work in this session immediately.
        live_plugins_dir = _prime_current_session(repo_root)

        # 3. Load it now + auto-load later.
        plugin_file = _load_plugin(live_plugins_dir)

        _confirm(
            "Maya GLB I/O installed",
            "Maya GLB I/O " + MODULE_VERSION + " is installed and loaded.\n\n"
            "A 'GLB I/O' menu is now in Maya's main menu bar.\n\n"
            "Installed from:\n  " + repo_root + "\n\n"
            "Module file written to:\n  " + mod_path + "\n\n"
            "Keep this folder where it is - Maya points at it rather than "
            "copying the code. The plug-in will load automatically next time "
            "you start Maya.",
        )
    except Exception:
        _confirm(
            "Maya GLB I/O - install failed",
            "Something went wrong during install. The full error is below and "
            "in the Script Editor:\n\n" + traceback.format_exc(),
        )


# Allow running the file directly from a Python tab in the Script Editor too
# (not just drag-and-drop), e.g. exec(open(r'.../install.py').read()).
if __name__ == "__main__":
    onMayaDroppedPythonFile()
