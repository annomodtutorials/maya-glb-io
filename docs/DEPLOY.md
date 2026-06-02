# Deploying the Maya GLB I/O landing page

The page is a single self-contained file (`docs/index.html`) — inline CSS/JS,
no build step, no external CDNs. It drops onto any static host. Three paths:

---

## A. GitHub Pages (primary)

1. Make sure `docs/index.html` (this folder) is committed and pushed to the
   default branch (e.g. `main`) of `annomodtutorials/maya-glb-io`.
2. On GitHub: **Settings → Pages**.
3. Under **Build and deployment → Source**, choose **Deploy from a branch**.
4. Set **Branch** to `main` and the folder to **`/docs`**, then **Save**.
5. Wait ~1 minute. The site goes live at:
   `https://annomodtutorials.github.io/maya-glb-io/`
6. Add the demo GIFs and `og-image.png` to `docs/assets/` (see
   `assets/README.md`) and push — the placeholders are replaced automatically.

> Tip: the `canonical` / Open Graph URLs in `index.html` already point at the
> GitHub Pages URL. If you move to a custom domain, update those `<head>` URLs.

---

## B. itch.io (same page, zipped)

itch.io serves a zipped HTML project where the entry file is `index.html`.

1. Zip the **contents** of `docs/` (so `index.html` is at the **top level** of
   the zip, not inside a `docs/` folder). Include the `assets/` folder.
   - Windows: open `docs/`, select `index.html` + `assets/`, right-click →
     *Send to → Compressed (zipped) folder*.
   - macOS/Linux (from inside `docs/`): `zip -r ../maya-glb-io-site.zip . -x '*.md' 'CNAME*'`
2. On itch.io: **Dashboard → Create new project** (or edit an existing one).
3. Set **Kind of project** to **HTML**.
4. Under **Uploads**, upload the zip and tick **"This file will be played in
   the browser"**.
5. itch will ask which file to launch — choose **`index.html`**.
6. Set a viewport size (e.g. 1280 × 800) or enable **fullscreen**, then save
   and view. Because everything is inline, it works fully offline in itch's
   sandbox.

Target page: `https://annomodtutorials.itch.io/maya-glb-io`

---

## C. Custom domain (later)

1. Follow `CNAME.example` in this folder: copy it to a file literally named
   `CNAME` containing only your bare domain (e.g. `yourdomain.com`).
2. At your DNS provider:
   - **Apex** (`yourdomain.com`): four A records →
     `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153`.
   - **Subdomain** (`www` / `tools`): one CNAME record →
     `annomodtutorials.github.io`.
3. GitHub **Settings → Pages → Custom domain**: enter the domain (this also
   writes the `CNAME` file), wait for the DNS check, then tick **Enforce HTTPS**.
4. Update the `canonical`, `og:url` and JSON-LD `url` values in
   `index.html` to the new domain so SEO/social previews stay correct.

---

### Any other static host
Because the page has zero external dependencies, you can also drop `index.html`
(plus `assets/`) onto Netlify, Cloudflare Pages, S3, or any web server — no
configuration required.
