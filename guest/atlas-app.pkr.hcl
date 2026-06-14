// Builds the CI guest image for JanC/tuist-app.
//
// Base = cirruslabs macOS+Xcode image (user/pass admin/admin, Xcode preinstalled),
// so all we bake in is the GitHub Actions runner binary. The runner is NOT
// configured/registered here — ekiden's host loop does that per job (ephemeral).
//
// After a successful build, push it to the local registry:
//   tart push --insecure atlas-app-ci localhost:5000/atlas-app-ci:latest

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

source "tart-cli" "atlas-app" {
  vm_base_name = "ghcr.io/cirruslabs/macos-tahoe-xcode:${var.xcode_image_tag}"
  vm_name      = "atlas-app-ci"
  cpu_count    = 4
  memory_gb    = 8
  disk_size_gb = 100
  ssh_username = "admin"
  ssh_password = "admin"
  ssh_timeout  = "120s"
}

build {
  sources = ["source.tart-cli.atlas-app"]

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
}
