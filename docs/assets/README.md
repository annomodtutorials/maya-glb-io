# Landing-page assets

Drop the images/GIFs below into this folder (`docs/assets/`). The landing page
(`docs/index.html`) references each by these **exact filenames**. Until a file
exists, the page shows a labeled placeholder in its slot — so the page never
looks broken, but real captures make it shine.

Keep every file reasonably small so the page stays fast on GitHub Pages and
itch.io (aim for < 8 MB per GIF; compress with gifski / ezgif if needed).

| Filename | What to capture | Recommended size |
|---|---|---|
| `og-image.png` | Social-share card. Product name **Maya GLB I/O**, the tagline, the accent color, on a dark background. Used by Open Graph / Twitter (Discord, X, Slack link previews). | **1200 × 630 px** PNG |
| `demo-hero.gif` | Wide hero clip: `File > Import` a `.glb`, the model snaps onto the grid fully shaded with materials. The "wow" shot. | ~1600 × 900 px, looping, < 8 MB |
| `demo-import.gif` | Importing a compressed (Draco) / Sketchfab GLB that other tools choke on — show it just opening. | ~1000 × 650 px, looping, < 6 MB |
| `demo-variants.gif` | Switching the live **Material Variant** dropdown in the Attribute Editor and the look changing instantly in the viewport. | ~1000 × 650 px, looping, < 6 MB |
| `demo-export.gif` | Exporting a rigged, animated character to GLB via the **GLB I/O** menu (then optionally re-importing to show the round-trip). | ~1000 × 650 px, looping, < 6 MB |

## Notes
- Filenames are case-sensitive on GitHub Pages — match them exactly.
- A still PNG/JPG works in any slot if you don't have a GIF yet (just keep the
  same filename, e.g. save a `demo-hero.gif` that is actually a single frame, or
  edit the `src` in `index.html` to point at a `.png`).
- For `og-image.png`, 1200 × 630 is the standard Open Graph ratio; keep important
  text away from the outer ~5% so nothing gets cropped in previews.
- Optional favicon: if you add `favicon.png` / `favicon.ico` here you can wire it
  up with a `<link rel="icon">` in `index.html` (not currently referenced, so the
  page stays 100% self-contained by default).
