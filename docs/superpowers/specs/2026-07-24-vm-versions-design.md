# VM version listing design

## Goal

Provide a discoverable command for listing stable Xcode versions that can be
used as the base image for `mise run vm:build`.

## Command

Add `mise run vm:versions`.

The task obtains an anonymous pull token from GitHub Container Registry, lists
the tags for `ghcr.io/cirruslabs/macos-tahoe-xcode`, and prints only stable
numeric tags, one per line in version order. It excludes prerelease tags such
as beta and release-candidate versions.

Example output:

```text
26
26.4.1
26.5
```

## Failure behavior

The task exits unsuccessfully if it cannot obtain a registry token, retrieve
the tag list, or parse that list. Shell command failures must remain visible to
the user rather than being converted into an empty version list.

## Documentation

Document `mise run vm:versions` in the README task table and the guest-image
building guide, with an example showing it before `mise run vm:build --xcode
<tag>`.

## Verification

Add a focused automated check that exercises the tag filter using a fixture
containing stable and prerelease tag names. Verify the task's shell syntax, and
run the new command against the live registry to confirm it returns stable
numeric versions.
