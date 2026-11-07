#!/usr/bin/env python3
"""Render the hand-edited site using `gh release view --json ...` output."""
import argparse
import html
import json
from pathlib import Path
import shutil

REPOSITORY = "https://github.com/himbeles/guac-bridge"
DOWNLOADS = (
    ("windows-x86_64-setup.exe", "Windows", "x64 · Installer"),
    ("macos-aarch64.tar.gz", "macOS", "Apple Silicon"),
    ("linux-x86_64.tar.gz", "Linux", "x64 · Archive"),
)


def render(release):
    if release.get("isDraft") or release.get("isPrerelease"):
        raise ValueError("The website requires a published stable release")
    tag = release["tagName"]
    url = release["url"]
    if not url.startswith(REPOSITORY + "/releases/tag/"):
        raise ValueError("Unexpected release URL")
    links = []
    for suffix, label, detail in DOWNLOADS:
        name = f"guac-bridge-{tag.removeprefix('v')}-{suffix}"
        matches = [asset for asset in release["assets"] if asset["name"] == name]
        if not matches:
            continue  # Older releases may not include every platform.
        if len(matches) != 1:
            raise ValueError(f"Duplicate download: {name}")
        asset_url = matches[0]["url"]
        if not asset_url.startswith(REPOSITORY + "/releases/download/"):
            raise ValueError("Unexpected download URL")
        links.append(
            f'<a class="download" href="{html.escape(asset_url, quote=True)}">'
            f'<strong>{label} <span aria-hidden="true">↓</span></strong>'
            f'<span>{detail}</span></a>'
        )
    if not links:
        raise ValueError("Release has no supported downloads")
    source = Path(__file__).with_name("index.html").read_text()
    return (source.replace("@@RELEASE_URL@@", html.escape(url, quote=True))
            .replace("@@VERSION@@", html.escape(tag))
            .replace("@@DOWNLOADS@@", "\n        ".join(links)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_json", type=Path)
    parser.add_argument("--output", type=Path, default=Path("_site"))
    args = parser.parse_args()
    page = render(json.loads(args.release_json.read_text()))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "index.html").write_text(page)
    root = Path(__file__).resolve().parents[1]
    shutil.copyfile(root / "LICENSE", args.output / "license.txt")
    shutil.copyfile(root / "crates/guac-bridge/assets/THIRD-PARTY-NOTICES.txt", args.output / "third-party-notices.txt")
    for name in ("style.css", "frog.svg"):
        shutil.copyfile(Path(__file__).with_name(name), args.output / name)


if __name__ == "__main__":
    main()
