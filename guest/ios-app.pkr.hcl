// Builds the CI guest image for JanC/tuist-app.
//
// Base = cirruslabs macOS+Xcode image (user/pass admin/admin, Xcode preinstalled).
// On top we bake the per-job CI setup the iOS repos repeat on every run:
//   - GitHub Actions runner binary (NOT configured here — ekiden registers it per job, ephemeral)
//   - Metal toolchain + Xcode first-launch (mirrors ios-app ci_scripts/ci_post_clone.sh)
//   - mise + pinned iOS toolchain (tuist/swiftlint/swiftformat/uv/periphery/firebase-tools/xcbeautify)
//   - Maestro CLI + Temurin 21 (ios-app e2e-nightly)
// We deliberately do NOT bake a warm SPM/Tuist module cache — external modules
// come from Tuist Cloud remote cache (see ios-app tuist-cache-warm.yml), and a
// baked cache would bloat the snapshot and go stale on every dependency bump.
//
// The produced VM is named ios-app-ci-<xcode_image_tag> (e.g. ios-app-ci-26.3)
// so multiple Xcode versions can coexist locally during an upgrade — there are no
// image tags without a registry, so the version lives in the name. Point ekiden's
// REGISTRY_IMAGE_NAME at the one you want.
//
// Multi-host only: push to the local registry afterwards, e.g.
//   tart push --insecure ios-app-ci-26.3 localhost:5001/ios-app-ci:26.3

packer {
  required_plugins {
    tart = {
      version = ">= 1.0.0"
      source  = "github.com/cirruslabs/tart"
    }
  }
}

variable "xcode_image_tag" {
  type        = string
  default     = "26.3"
  description = "Tag of ghcr.io/cirruslabs/macos-tahoe-xcode to base on. Pinned, not 'latest'."
}

source "tart-cli" "ios-app" {
  vm_base_name = "ghcr.io/cirruslabs/macos-tahoe-xcode:${var.xcode_image_tag}"
  vm_name      = "ios-app-ci-${var.xcode_image_tag}"
  cpu_count    = 4
  memory_gb    = 8
  disk_size_gb = 120
  ssh_username = "admin"
  ssh_password = "admin"
  ssh_timeout  = "120s"
}

build {
  sources = ["source.tart-cli.ios-app"]

  // GitHub Actions runner — download only, no config.sh here.
  provisioner "shell" {
    inline = [
      "source ~/.zprofile || true",
      "cd $HOME",
      "mkdir -p actions-runner && cd actions-runner",
      "RUNNER_TAG=$(curl -sL https://api.github.com/repos/actions/runner/releases/latest | jq -r '.tag_name')",
      "RUNNER_VERSION=$${RUNNER_TAG:1}",
      "echo \"Installing GitHub Actions runner $RUNNER_VERSION\"",
      "curl -O -L https://github.com/actions/runner/releases/download/$RUNNER_TAG/actions-runner-osx-arm64-$RUNNER_VERSION.tar.gz",
      "tar xzf ./actions-runner-osx-arm64-$RUNNER_VERSION.tar.gz",
      "rm actions-runner-osx-arm64-$RUNNER_VERSION.tar.gz",
    ]
  }

  // Metal toolchain + Xcode first-launch. Mirrors ios-app ci_scripts/ci_post_clone.sh,
  // which both iOS repos run on every job via the setup-ios-toolchain composite action.
  // Baking it here makes that step a no-op at job time (the download is large and flaky).
  provisioner "shell" {
    inline = [
      "source ~/.zprofile || true",
      "echo \"Xcode: $(xcodebuild -version)\"",
      "TOOLCHAIN_DIR=$(mktemp -d)/MetalToolchain && mkdir -p \"$TOOLCHAIN_DIR\"",
      "if xcodebuild -help 2>&1 | grep -q -- '-downloadComponent'; then",
      "  xcodebuild -downloadComponent metalToolchain -exportPath \"$TOOLCHAIN_DIR\" || xcodebuild -downloadComponent MetalToolchain -exportPath \"$TOOLCHAIN_DIR\" || echo 'Metal toolchain download skipped (may be preinstalled)'",
      "  if ls \"$TOOLCHAIN_DIR\"/MetalToolchain-*.exportedBundle >/dev/null 2>&1; then",
      "    xcodebuild -importComponent metalToolchain -importPath \"$TOOLCHAIN_DIR\"/MetalToolchain-*.exportedBundle || xcodebuild -importComponent MetalToolchain -importPath \"$TOOLCHAIN_DIR\"/MetalToolchain-*.exportedBundle || echo 'Metal toolchain import failed'",
      "  fi",
      "fi",
      "xcodebuild -runFirstLaunch || true",
      "defaults write com.apple.dt.Xcode IDESkipMacroFingerprintValidation -bool YES",
      "defaults write com.apple.dt.Xcode IDEPackageSupportUseBuiltinSCM YES || true",
    ]
  }

  // mise + the iOS toolchain pinned in ios-app/mise.toml. jdx/mise-action runs
  // `mise install` on every job; pre-populating ~/.local/share/mise makes it a
  // cache hit. Versions kept in sync with ios-app/mise.toml [tools]; both iOS
  // repos re-pin tuist via their own mise.toml, so a drift just triggers a
  // harmless re-fetch at job time rather than a failure.
  provisioner "shell" {
    inline = [
      "source ~/.zprofile || true",
      "curl https://mise.run | sh",
      "MISE=\"$HOME/.local/bin/mise\"",
      "$MISE use -g tuist@4.152.0",
      "$MISE use -g swiftlint@0.63.3",
      "$MISE use -g swiftformat@0.60.1",
      "$MISE use -g uv@0.8.14",
      "$MISE use -g aqua:firebase/firebase-tools@latest",
      "$MISE use -g github:cpisciotta/xcbeautify@latest",
      "$MISE use -g github:peripheryapp/periphery@3.7.4",
      "$MISE install",
      "$MISE reshim",
      // Put mise's shims on PATH via a plain export (valid in sh and zsh, so
      // re-sourcing ~/.zprofile in later provisioners can't break). Avoid
      // `mise activate zsh`, which emits zsh-only syntax (autoload) that errors
      // when ~/.zprofile is sourced under Packer's /bin/sh.
      "grep -q 'mise/shims' ~/.zprofile || echo 'export PATH=\"$HOME/.local/share/mise/shims:$PATH\"' >> ~/.zprofile",
    ]
  }

  // E2E (Maestro) dependencies, used by ios-app e2e-nightly.yml.
  // Temurin 21 (setup-java@v4 java-version 21) + the Maestro CLI into ~/.maestro.
  provisioner "shell" {
    inline = [
      "source ~/.zprofile || true",
      "$HOME/.local/bin/mise use -g java@temurin-21",
      "$HOME/.local/bin/mise install",
      "curl -Ls https://get.maestro.mobile.dev | bash",
      "grep -q '.maestro/bin' ~/.zprofile || echo 'export PATH=\"$HOME/.maestro/bin:$PATH\"' >> ~/.zprofile",
    ]
  }
}
