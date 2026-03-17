# ScienceOS — GitHub Pages Site

This folder contains the static website for ScienceOS.

## Deploy to GitHub Pages

### Option A: Publish from this folder (recommended)

1. Push this repo to GitHub (e.g. `github.com/your-org/science-os`)
2. Go to **Settings → Pages**
3. Set **Source** to `Deploy from a branch`
4. Set **Branch** = `main`, **Folder** = `/site`
5. Click **Save** — your site will be live at `https://your-org.github.io/science-os/`

### Option B: Dedicated `<org>.github.io` repo (like MedOS)

1. Create a new GitHub repo named `<your-org>.github.io`
2. Copy `index.html` into the root of that repo
3. Push — GitHub Pages serves it automatically at `https://<your-org>.github.io/`

## Files

```
site/
└── index.html    ← Complete single-file website (no build step needed)
```

All dependencies (Tailwind CSS, Chart.js, Google Fonts) are loaded via CDN.
No npm, no bundler, no CI/CD required.
