# Maya GLB I/O

**The free, open-source glTF / GLB importer _and_ exporter for Autodesk Maya.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Maya 2022+](https://img.shields.io/badge/Maya-2022%2B-blue.svg)](https://www.autodesk.com/products/maya/)
[![Version 1.0.0](https://img.shields.io/badge/version-1.0.0-orange.svg)](CHANGELOG.md)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

Maya GLB I/O brings full glTF 2.0 in **both directions** to Maya — geometry, PBR materials,
material variants, rigs, animation, morph targets, cameras, lights, and Draco compression —
with **nothing locked behind a paywall**. It's a pure-Python plug-in (no heavy dependencies),
MIT-licensed, and built by a game modder for the Maya and game-modding community.

> Stop paying for a Maya GLB plugin.

---

## Screenshots / Demo

<!--
  OWNER TODO: drop a short demo GIF at docs/assets/demo.gif (e.g. importing a Sketchfab
  model, switching a material variant, exporting back out). Then this image will render.
  Suggested width ~900px. Keep it under ~10 MB so GitHub serves it inline.
-->
![Maya GLB I/O demo](docs/assets/demo.gif)

<!--
  OWNER TODO (optional): add a couple of stills too, e.g.
  docs/assets/import-materials.png  and  docs/assets/variant-switch.png
-->

---

## Features

### Import — geometry & scene
- `.glb` (binary) and `.gltf` (+ external `.bin`) — auto-detected.
- Full node hierarchy; multi-mesh and multi-primitive (per-material) meshes.
- UVs (correct V-flip), vertex colors, UV-derived tangents.
- **Drop-to-floor** — sits the model on the grid instead of floating at the origin (toggle).
- **Clean import** — bakes the source file's leftover root scale (e.g. the 0.01 unit node many
  exporters add) into a clean identity transform; no junk nodes. Skips rigged meshes safely.
- **Namespace-safe** — never pollutes the scene (auto-flattens Maya's File > Import namespace
  wrapping).
- Automatic unit conversion (glTF metres → Maya scene units).

### Import — materials
Per-import shader target: **openPBR / StingrayPBS / Arnold / Maya Phong**.
- Base color, metallic-roughness, normal, ambient occlusion, emissive.
- **Emissive** honors `KHR_materials_emissive_strength`, with tunable emission luminance (nits)
  so glTF emissives actually glow.
- **Transparency** — alpha BLEND / MASK → geometryOpacity / Arnold opacity.
- **Transmission / IOR / volume** (`KHR_materials_transmission` + `_ior` + `_volume`) → real
  refractive glass.
- **Clearcoat** (`KHR_materials_clearcoat`) → coat layer (weight / roughness / normal).
- **Sheen** (`KHR_materials_sheen`) → fuzz layer.
- **Anisotropy** (`KHR_materials_anisotropy`) → specular anisotropy (+ strength texture).
- **Iridescence** (`KHR_materials_iridescence`) → thin-film (thickness remapped to nm range).
- **Specular** (`KHR_materials_specular`) → specular weight / color.
- **Legacy spec-gloss** (`KHR_materials_pbrSpecularGlossiness`) — full support, mapped onto
  the native specular workflow (uses both diffuse + spec/gloss textures). Many old Sketchfab
  exports use this; most free tools drop everything but the diffuse.

### Import — material variants
- `KHR_materials_variants`: a **live "Material Variant" dropdown** added right onto the imported
  object. Switch looks instantly from the **Channel Box / Attribute Editor** (scriptJob-driven),
  pre-warmed so the first switch is instant.

### Import — rigs, animation, morphs
- **Skinning** — joints + skinCluster with bulk weight application (fast even on dense meshes).
- **Skeletal animation** — glTF samplers → animCurves, with euler unrolling (no flips on fast
  rigs) and correct fps mapping.
- **Morph targets / blendshapes** — glTF morph targets → Maya blendShapes + animated weights.

### Import — cameras, lights, compression
- **Cameras** — perspective + orthographic → Maya cameras (FOV → focal length, clip planes).
- **Lights** (`KHR_lights_punctual`) — directional / point / spot → Maya lights.
- **Draco** (`KHR_draco_mesh_compression`) — decompresses on import, auto-installing the DracoPy
  decoder one time. Most compressed Sketchfab GLBs fail to import in other free tools.
- **Mesh quantization** (`KHR_mesh_quantization`) — dequantized correctly.

### Export — Maya → glTF / GLB
- `.glb` and `.gltf` (+ external `.bin`); selection or whole scene.
- Materials → glTF metallic-roughness (from openPBR / StingrayPBS / aiStandardSurface / lambert /
  blinn / phong); textures embedded or referenced.
- Node hierarchy + transforms (pivots baked correctly; roots carry world transform).
- **Skinning export** — joints + inverse-bind matrices + JOINTS_0 / WEIGHTS_0.
- **Animation export** — animCurves → samplers.
- **Morph target export** — blendShapes → glTF morph targets + weights.
- Export Preferences UI.
- Faithfully **round-trips** with the importer.

---

## Maya GLB I/O vs. paid Maya GLB plugins

| Capability | Typical paid plugin | Maya GLB I/O |
|---|:---:|:---:|
| Import meshes / PBR / all shader targets | ✓ | ✓ |
| Skin + skeletal animation (import) | ✓ | ✓ |
| Morph targets | ✓ | ✓ |
| Material variants (live switch) | ✓ | ✓ (in the Attribute Editor) |
| Clearcoat / sheen / anisotropy / iridescence / transmission | ✓ | ✓ |
| Cameras + lights | ✓ | ✓ |
| Draco + quantization | ✓ | ✓ |
| Legacy spec-gloss | partial | ✓ full |
| **Full rig + anim + morph EXPORT** | partial / none | ✓ |
| **Source available** | ✗ | ✓ (MIT) |
| **Price** | paid | **free** |

---

## Install

Maya GLB I/O is a normal Maya plug-in package (`scripts/` + `plug-ins/`). Pick whichever of the
three methods below you find easiest — they all end with a **glTF 2.0** menu in Maya's main menu bar.

> Wherever you put the unzipped folder, **keep it there** — methods (a) and (b) point Maya at that
> location rather than copying the code. Don't delete it after installing.

### Method (a) — Drag-and-drop installer (easiest)

1. Go to the **[Releases page](https://github.com/annomodtutorials/maya-glb-io/releases/latest)** and
   download **`maya-glb-io-vX.Y.Z.zip`** (the named installer asset — *not* the "Source code" archives).
   Unzip it somewhere you won't move or delete (for example `C:\maya-plugins\maya-glb-io\` on Windows,
   or `~/maya-plugins/maya-glb-io/` on macOS/Linux).
2. Start Autodesk Maya.
3. Open the unzipped folder in your file browser (Windows Explorer / macOS Finder).
4. Find the file named **`install.py`** in the top level of the folder.
5. With Maya's window visible, **drag `install.py`** out of the file browser and **drop it into the
   Maya viewport** (the big 3D area).
6. A confirmation dialog appears saying the plug-in is installed. Click **OK**.
7. Look at the top menu bar in Maya — a new **glTF 2.0** menu is there. You're done.

The installer writes a small Maya *module* file that points at this folder, then loads the plug-in.
Next time you launch Maya the plug-in loads automatically — you only drag-and-drop once.

### Method (b) — Maya module (`.mod`) by hand

If you prefer to wire it up yourself, or the drag-and-drop method is blocked by your studio's setup:

1. Download and unzip the repository as in step 1 above. Note its full path.
2. Add that folder to Maya's `MAYA_MODULE_PATH` environment variable. The bundled
   **`maya_glb_io.mod`** at the repo root tells Maya where `scripts/` and `plug-ins/` live.
   - **Quick version:** copy `maya_glb_io.mod` into one of Maya's module folders, e.g.
     `Documents\maya\2024\modules\` on Windows (create the `modules` folder if it doesn't exist),
     then open the copied `.mod` in a text editor and change the path on the first line to the full
     path of where you unzipped this repo.
3. Restart Maya.
4. Open **Windows → Settings/Preferences → Plug-in Manager**, find **`maya_glb_io.py`**, and tick
   **Loaded** (and **Auto load** if you want it every session).
5. The **glTF 2.0** menu appears.

### Method (c) — Plug-in Manager only

If you just want to try it without touching modules or environment variables:

1. Download and unzip the repository.
2. In Maya open **Windows → Settings/Preferences → Plug-in Manager**.
3. Click **Browse**, navigate into the unzipped folder's **`plug-ins`** subfolder, and pick
   **`maya_glb_io.py`**.
4. Tick **Loaded**. The plug-in adds its own `scripts/` folder to Maya's Python path automatically,
   then installs the **glTF 2.0** menu.
   - Tip: tick **Auto load** too so it comes back next session.

---

## Usage

All commands live under the **glTF 2.0** menu (and `.glb` / `.gltf` also show up in Maya's native
**File → Import** dialog).

- **Import a model** — *glTF 2.0 → Import .glb / .gltf...* Pick a file and it imports using your
  saved Import Preferences. For per-file option tweaks (shader target, scale, drop-to-floor, etc.)
  use Maya's native **File → Import** instead — it shows the full options panel.
- **Batch import** — *glTF 2.0 → Batch Import...* to import several files at once.
- **Switch material variants** — after importing a model that uses `KHR_materials_variants`, select
  the object and change the **Material Variant** dropdown in the **Channel Box / Attribute Editor**.
  The scene re-skins instantly. After reopening a saved scene, run
  *glTF 2.0 → Material Variants: Find/Arm Switch* once to re-arm the live dropdown.
- **Draco (compressed) files** — these import automatically; the first time, the decoder is
  downloaded for you. To get it out of the way ahead of time, run
  *glTF 2.0 → Install Draco Decoder (one-time)...*
- **Export selection** — select your objects, then *glTF 2.0 → Export Selection to glTF/GLB...*
  Choose `.glb` or `.gltf` in the save dialog.
- **Export the whole scene** — *glTF 2.0 → Export Scene to glTF/GLB...*
- **Preferences** — *glTF 2.0 → Import Preferences...* and *Export Preferences...* set the defaults
  used by the quick Import/Export menu items.

---

## Known limitations

- Advanced material layers (clearcoat / sheen / anisotropy / iridescence) render fully in
  **Arnold**; Viewport 2.0 shows them only partially.
- **Draco** needs a one-time internet connection to auto-install the DracoPy decoder.
- Imported light intensity is approximate (glTF physical units → Maya); artist-tweakable.
- A mesh whose skin weights are compressed *inside* Draco imports as static geometry (rare).
- Animation is imported / exported as per-frame samples with euler rotation (no glTF curve tangents).
- Maya rejects non-ASCII node names → such nodes are renamed to a safe ASCII name.

---

## Requirements

- **Autodesk Maya 2022 or newer** (tested through 2024 / 2025 / 2026), Python 3.
- **Windows, macOS, or Linux** (anywhere Maya runs).
- An **internet connection once** the first time you import a Draco-compressed file (to fetch the
  DracoPy decoder). Everything else works fully offline.
- No heavy dependencies — the parser / writer are pure Python.

---

## Roadmap

- File Type Specific Options panel for **Export** (per-export overrides like the Import panel).
- A pure-Python Draco path so the one-time decoder download is no longer needed.
- Optional glTF animation curve tangents (currently per-frame sampled).

(Suggestions welcome — open an issue.)

---

## Contributing

Bug reports, test files, and pull requests are welcome. See **[CONTRIBUTING.md](CONTRIBUTING.md)**
for how to file a good issue and how to run the Maya-free smoke tests before sending a PR.

---

## License

Released under the **MIT License** — free for any use, including commercial. See [LICENSE](LICENSE).

---

## Credits

- Built by **LordJacko** — made by a modder, for everyone.
- For the Maya and game-modding community (Anno and beyond).
- Khronos Group glTF sample assets are used for testing.
