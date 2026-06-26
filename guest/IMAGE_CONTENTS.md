# What's baked into the guest image

The CI guest image (`ios-app-ci`) is built by [`ios-app.pkr.hcl`](ios-app.pkr.hcl)
on top of the prebuilt cirruslabs Xcode base, then cloned fresh by ekiden for
every job. Each clone is **ephemeral** — it runs one job and is destroyed — so
anything a job would otherwise download or set up on every run is a good
candidate to bake once here.

We bake a single overlay of pure, deterministic CI setup; we deliberately do
**not** bake anything volatile (sources, resolved packages, signing material).

## Base image

`ghcr.io/cirruslabs/macos-tahoe-xcode:<xcode_image_tag>` (default `26.3`,
overridable via `-var xcode_image_tag=...`).

| Preinstalled by the base (we don't bake) | Notes |
|------------------------------------------|-------|
| macOS Tahoe (macOS 26) | |
| Xcode 26.x + iOS Simulator runtime | matches the `xcode-version: "26"` the workflows select |
| User `admin` / password `admin` | ekiden SSHes in with these |
| Homebrew + common CLI tooling | |

## Baked on top

| Layer | Contents | Why baked |
|-------|----------|-----------|
| GitHub Actions runner | Latest `actions-runner-osx-arm64` → `~/actions-runner` (downloaded, **not** configured) | ekiden registers/deregisters per job; baking the binary skips a per-job download |
| Metal toolchain + Xcode first-launch | `metalToolchain` download+import, `xcodebuild -runFirstLaunch`, `IDESkipMacroFingerprintValidation` / builtin-SCM defaults | Mirrors `ios-app/ci_scripts/ci_post_clone.sh` run on every job; the Metal download is large and flaky |
| mise + iOS toolchain | mise; `tuist 4.152.0`, `swiftlint 0.63.3`, `swiftformat 0.60.1`, `uv 0.8.14`, `periphery 3.7.4`, `firebase-tools` (latest), `xcbeautify` (latest); activated in `~/.zprofile` | `jdx/mise-action` runs `mise install` per job — pre-populating `~/.local/share/mise` makes it a cache hit |
| E2E (Maestro) | Maestro CLI → `~/.maestro`; Temurin 21 (Java) via mise | Used by `ios-app/.github/workflows/e2e-nightly.yml` |

## Version sync with the iOS repos

Pinned tool versions track `ios-app/mise.toml`. Both `ios-app` and
`ios-lab-app` re-pin their toolchain via their own `mise.toml`, so a drift
between this image and a repo just makes `mise install` re-fetch the correct
version at job time — a slower job, not a failure. Keep the versions here in
sync with `ios-app/mise.toml` to keep the common path warm.
