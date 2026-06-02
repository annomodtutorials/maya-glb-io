# Contributing to Maya GLB I/O

Thanks for helping make the free glTF / GLB plug-in for Maya better. Bug reports, test files, and
pull requests are all welcome.

## Reporting bugs

Please open a GitHub issue using the **Bug report** template. The most useful reports include:

- Your **Maya version** (e.g. 2024.2) and **OS** (Windows / macOS / Linux).
- The **`.glb` / `.gltf` file** that triggers the problem, if you're able to share it. A small
  repro file is worth a thousand words. (If it's confidential, a screenshot or a stripped-down file
  is still helpful.)
- The full **Script Editor output** — open *Windows → General Editors → Script Editor* and copy
  everything printed when the bug happens (errors, warnings, tracebacks).
- What you expected vs. what actually happened.

## Testing (no Maya required)

The parser, writer, and most of the conversion logic are pure Python and can be exercised
**without launching Maya**, using your system Python (3.7+). The smoke scripts live in `_dev/`:

```
python _dev/smoke_parse.py           # parse every test .glb / .gltf and print a summary
python _dev/smoke_export.py          # build a glTF in memory and round-trip it through the reader
python _dev/smoke_skin_import.py     # skinning import path
python _dev/smoke_anim_import.py     # animation import path
python _dev/smoke_morph_import.py    # morph-target import path
python _dev/smoke_cameras_lights.py  # cameras + KHR_lights_punctual
python _dev/smoke_variants.py        # KHR_materials_variants
python _dev/smoke_draco.py           # Draco decode (needs DracoPy installed)
python _dev/smoke_skin_anim_export.py# skin + animation export round-trip
```

Run the relevant smoke script(s) before opening a PR and make sure they still print SUCCESS / a sane
summary. Some scripts read sample files from `test_assets/` — drop your own `.glb` files there if the
folder is empty (large binaries are not committed to the repo).

For anything that actually touches Maya (UI, skinClusters, blendShapes), please also test inside
Maya 2022+ and mention in the PR which Maya version you tried.

## Code style

- Python 3, 4-space indentation, no tabs.
- Keep modules importable from system Python where they don't truly need Maya (so the smoke tests
  keep working). Maya-only code goes behind a `from maya import cmds` import inside the function /
  module that needs it.
- Match the surrounding style; prefer small, focused functions with a short docstring.
- No new third-party dependencies in the import/export core. Optional helpers (like DracoPy) are
  auto-installed at runtime, not required to load the plug-in.

## Pull request process

1. Fork the repo and create a branch off `main` (e.g. `fix/draco-quantized-skin`).
2. Make your change. Add or update a smoke script if it's testable without Maya.
3. Run the smoke scripts that cover your area and confirm they pass.
4. Open a PR describing **what** changed and **why**, with before/after notes and the Maya version
   you tested in. Link any related issue.
5. Keep PRs focused — one logical change per PR is easiest to review.

By contributing you agree your contributions are licensed under the project's MIT License.
