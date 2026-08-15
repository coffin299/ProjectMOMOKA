# MOMOKA website (`webpage` branch)

Official site: **https://momoka-project.com/**

## URL structure

Docs use clean paths (no `.html` in the browser):

| Path | File |
|---|---|
| `/` | `index.html` |
| `/faq/` | `faq/index.html` |
| `/troubleshooting/` | `troubleshooting/index.html` |
| `/commands/` | `commands/index.html` |
| `/terms/` | `terms/index.html` |
| `/privacy/` | `privacy/index.html` |

Legacy `*.html` files at the repo root are thin redirects to the clean paths. Shared assets stay at `/style.css`, `/script.js`, `/assets/...`.

Do not commit local filesystem paths (`C:\...`, `file://...`). Use site-relative (`/faq/`) or public URLs (`https://momoka-project.com/...`) only.

## Updating the site

Use a separate worktree to avoid repeatedly switching branches:

```bash
git switch master
git worktree add ../ProjectMOMOKA-webpage webpage
```

Then update the website separately:

```bash
cd ../ProjectMOMOKA-webpage

# Edit HTML, CSS, JavaScript, etc.

git add .
git commit -m "Update website"
git push origin webpage
```
