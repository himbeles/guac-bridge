# Licensing

Guac Bridge's own code, documentation, website and frog artwork are licensed under
[MIT](../LICENSE), copyright © 2026 Luis Riegger. This project license does not
replace the licenses of dependencies or Microsoft redistributables.

## Where to find the terms

- **CLI:** `guac-bridge licenses` prints the project license and third-party notices.
  This command runs locally without a gateway connection.
- **Helpers:** their notices accompany the client distributions. Deployment sends
  the helper executable; license viewing is provided by the client.
- **Windows manager and tray:** select **Licenses…** to open the same notices.
  Installed Microsoft package licenses and notices are included in this view.
- **Native archives:** `LICENSE` and `THIRD-PARTY-NOTICES.txt` accompany the executable.
- **Installer:** these files and each bundled Microsoft package's `LICENSE` and
  `NOTICE` are installed alongside the executable.
- **Wheels/PyPI:** the project license expression is MIT; both license texts are
  declared as `License-File` entries and carried in the wheel's `.dist-info/licenses`
  directory. Source distributions and Cargo packages carry them too.
- **Website:** the footer links to a local copy of the MIT license and the dependency
  report. The homepage itself distributes the project's HTML/CSS/SVG assets.

The dependency report is a conservative union across supported client/helper
platforms, optional features and build dependencies. Some entries therefore do
not apply to a particular binary. Its license texts preserve upstream copyright
notices. Exact crates.io source download links identify the corresponding
unmodified source, including the MPL-2.0 `option-ext` component. Vendored D-Bus is
used under its AFL-2.1 option; OpenSSL and ring carry their native component terms.
The Rust standard-library report includes its bundled component notices.

The Windows installer additionally redistributes Microsoft's Windows App Runtime
and WebView2 SDK components under their own terms, with the original package
LICENSE and NOTICE files. Portable packages and wheels use the separately installed
shared runtime and do not redistribute these DLLs.

## Updating the report

Install the pinned notice generator, then regenerate after dependency, Rust toolchain
or license changes (including a release version change in `Cargo.lock`):

```sh
cargo install cargo-about --version 0.9.3 --locked --features cli
python scripts/licenses.py
python scripts/licenses.py --check
```

`cargo-about` resolves the locked dependency graph across all supported targets,
uses reviewed SPDX choices from `licensing/about.toml`, and preserves component
license texts. The script adds vendored C notices and the active Rust toolchain's
`COPYRIGHT-library.html`, then compresses the report for embedding in the client. Review the
result and commit the report, compressed copy, packaged MIT license and
`licensing/manifest.json` together. Ordinary source builds require neither Python
nor cargo-about for license display.

Release CI checks the recorded inputs, toolchain and output hashes. It verifies
actual wheels, source distributions and portable archives and tests the installed
Windows license payload. Unknown dependency licenses or missing Microsoft notices
fail generation/build instead of silently omitting them.

Published PyPI files are immutable. Releases starting with v5.0.4 carry the full
notice report. Existing v5.0.3 wheels carry the project MIT license but lack the
additional report; upgrade to v5.0.4 or newer for the complete packaged notices.
The website also publishes the notices independently.
