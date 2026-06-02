"""MPxFileTranslator subclass for .glb / .gltf, registered by the plug-in.

Maya's File > Import dialog routes here. The Specifics panel UI is built
by maya_glb_io._options_ui via a MEL proc defined in the plug-in entry.
Actual import logic is delegated to maya_glb_io.importer.import_file.

Uses API 1.0 because MPxFileTranslator isn't in API 2.0.
"""
from __future__ import annotations

import maya.OpenMaya as om
import maya.OpenMayaMPx as ompx


class GLBFileTranslator(ompx.MPxFileTranslator):

    NAME = "glTF 2.0"

    def __init__(self):
        ompx.MPxFileTranslator.__init__(self)

    @staticmethod
    def creator():
        return ompx.asMPxPtr(GLBFileTranslator())

    # --- capabilities --------------------------------------------------------

    def haveReadMethod(self):
        return True

    def haveWriteMethod(self):
        return True  # export added in v0.3

    def haveNamespaceSupport(self):
        return True

    def defaultExtension(self):
        return "glb"

    def filter(self):
        return "*.glb;;*.gltf"

    def identifyFile(self, fileObject, buffer, size):
        try:
            if buffer and len(buffer) >= 4:
                head = bytes(buffer[:4]) if isinstance(buffer, (bytes, bytearray)) \
                       else buffer[:4].encode("latin-1", errors="ignore")
                if head == b"glTF":
                    return ompx.MPxFileTranslator.kIsMyFileType
        except Exception:
            pass
        name = fileObject.expandedFullName().lower()
        if name.endswith(".glb") or name.endswith(".gltf"):
            return ompx.MPxFileTranslator.kIsMyFileType
        return ompx.MPxFileTranslator.kNotMyFileType

    # --- read ----------------------------------------------------------------

    def reader(self, fileObject, optionsString, accessMode):
        from maya_glb_io import _prefs
        from maya_glb_io.importer import import_file

        path = fileObject.expandedFullName()
        options = _prefs.parse(optionsString) if optionsString else _prefs.get_all()

        # Namespace handling (including flattening Maya's own File > Import
        # "Use namespaces" wrapping back to root when our option is off) is done
        # centrally inside import_file via a deferred cleanup — see
        # importer._schedule_namespace_flatten. Nothing to do here.
        try:
            import_file(path, options=options)
        except Exception as exc:
            om.MGlobal.displayError("[glb_io] import failed: " + str(exc))
            return False

        return True

    # --- write ---------------------------------------------------------------

    def writer(self, fileObject, optionsString, accessMode):
        """Export the scene (or active selection) to glTF/GLB.

        Routed here by File > Export All / Export Selection. `accessMode` tells
        us the scope: kExportActiveAccessMode = selection, otherwise everything.
        The format (.glb vs .gltf) is inferred from the chosen file extension.
        """
        from maya_glb_io import _prefs
        from maya_glb_io import exporter

        path = fileObject.expandedFullName()

        # Merge saved export prefs with any options string from the UI.
        options = _prefs.get_all_export()
        if optionsString:
            options.update(_prefs.parse(optionsString))

        # Format follows the file extension the user picked.
        lower = path.lower()
        if lower.endswith(".gltf"):
            options["format"] = "gltf"
        elif lower.endswith(".glb"):
            options["format"] = "glb"

        # Scope from accessMode (selection vs whole scene).
        export_selection = (accessMode == ompx.MPxFileTranslator.kExportActiveAccessMode)

        try:
            if export_selection:
                result = exporter.export_selection(path, options=options)
            else:
                result = exporter.export_all(path, options=options)
        except Exception as exc:
            import traceback
            om.MGlobal.displayError("[glb_io] export failed: " + str(exc))
            om.MGlobal.displayError(traceback.format_exc())
            return False

        if not result or not result.get("path"):
            return False
        return True
