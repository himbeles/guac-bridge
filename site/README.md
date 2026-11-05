# Website

Edit `index.html` and `style.css` directly. The frog is the application's SVG logo.
No JavaScript, framework or package installation is required.

`build.py` replaces the three `@@…@@` placeholders with the latest stable
release's version, release URL and available native download links. Missing
platforms are omitted until a release includes them.

To preview with Python and GitHub CLI installed, run from the repository root:

```sh
gh release view --repo himbeles/guac-bridge --json tagName,url,isDraft,isPrerelease,assets > /tmp/guac-release.json
python site/build.py /tmp/guac-release.json
python -m http.server 8000 --directory _site
```

Open <http://localhost:8000>. The generated `_site` directory is disposable.

The Website action deploys to GitHub Pages after site changes on main, a published
release or a successful release workflow. The workflow completion trigger also
handles releases created with `GITHUB_TOKEN`, which do not emit another workflow's
release trigger. Beta releases keep the website on the latest stable downloads.
Select **GitHub Actions** as the Pages source in repository settings.
