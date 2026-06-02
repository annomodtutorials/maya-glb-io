# Changelog

All notable changes to Maya GLB I/O are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-06-03

First public release — a complete, free, MIT-licensed glTF 2.0 importer **and** exporter for
Autodesk Maya 2022+.

### Added — Import
- `.glb` (binary) and `.gltf` (+ external `.bin`) import, auto-detected.
- Full node hierarchy; multi-mesh and multi-primitive (per-material) meshes.
- UVs (correct V-flip), vertex colors, and UV-derived tangents.
- Drop-to-floor toggle and a clean-import bake that removes leftover root scale / junk nodes.
- Namespace-safe import (auto-flattens Maya's File > Import namespace wrapping).
- Automatic unit conversion (glTF metres → Maya scene units).
- Per-import shader target: openPBR / StingrayPBS / Arnold / Maya Phong.
- PBR materials: base color, metallic-roughness, normal, ambient occlusion, emissive.
- Emissive with `KHR_materials_emissive_strength` and tunable emission luminance (nits).
- Transparency (alpha BLEND / MASK).
- `KHR_materials_transmission` + `_ior` + `_volume` → refractive glass.
- `KHR_materials_clearcoat`, `_sheen`, `_anisotropy`, `_iridescence`, `_specular`.
- Full legacy `KHR_materials_pbrSpecularGlossiness` (diffuse + spec/gloss).
- `KHR_materials_variants` → live "Material Variant" dropdown in the Channel Box / Attribute Editor
  (scriptJob-driven, pre-warmed for instant switching).
- Skinning (joints + skinCluster, bulk weights) and skeletal animation (with euler unrolling, fps
  mapping).
- Morph targets / blendshapes with animated weights.
- Cameras (perspective + orthographic) and `KHR_lights_punctual` (directional / point / spot).
- `KHR_draco_mesh_compression` decode (auto-installs the DracoPy decoder once).
- `KHR_mesh_quantization` dequantization.

### Added — Export
- `.glb` and `.gltf` (+ external `.bin`); selection or whole scene.
- Materials → glTF metallic-roughness (from openPBR / StingrayPBS / aiStandardSurface / lambert /
  blinn / phong); embedded or referenced textures.
- Node hierarchy + transforms (pivots baked; roots carry world transform).
- Skinning export (joints + inverse-bind matrices + JOINTS_0 / WEIGHTS_0).
- Animation export (animCurves → samplers) and morph-target export (blendShapes → glTF morph
  targets + weights).
- Faithful round-trip with the importer.

### Added — UX / install
- Top-level **GLB I/O** menu and File > Import file translator with an options panel.
- Import and Export Preferences windows; one-time Draco-decoder installer.
- Drag-and-drop `install.py`, a Maya module `.mod`, and Plug-in Manager loading.

[1.0.0]: https://github.com/annomodtutorials/maya-glb-io/releases/tag/v1.0.0
