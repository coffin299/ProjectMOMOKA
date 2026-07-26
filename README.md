# Updating the site
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