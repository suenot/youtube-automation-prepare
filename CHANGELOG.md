# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic
Versioning.

## [Unreleased]

### Changed

- Rename the repository to `youtube-automation-prepare` and update the README
  project name and pipeline links.

## [0.2.0] - 2026-10-07

### Added

- Import a scoped, read-only Firefox cookie snapshot into Camoufox only after
  verifying the expected authenticated YouTube channel.
- Verify the saved session with `login.py --status`, run publication preflight
  without uploading, and require Public explicitly with `--require-public`.
- Persist started requests before upload with `--request-id` to prevent a
  second upload after a crash, uncertain result or verified publication.

### Fixed

- Stop on unresolved/mismatched channels, unreadable duplicate lists and upload
  inspection failures; recheck channel and visibility before submitting.
- Require the exact saved video's title, description and visibility to match
  before reporting publication success.
- Pin Camoufox 0.5.7 and Playwright 1.60.0 to the paired browser release, retain a
  stable fingerprint and prevent concurrent use of the persistent profile.

## [0.1.10] - 2026-08-16

### Fixed

- Refuse `Next` and `Save` while any visible input, textarea, select, or
  contenteditable in the active upload wizard is invalid or visually red.
- Persist Studio descriptions through the Polymer input event path and verify
  exact title and description values before advancing.

## [0.1.9] - 2026-08-16

### Fixed

- Fill the visible title and description fields in the draft wizard instead of
  hidden duplicates from the editor behind it.

## [0.1.8] - 2026-08-16

### Fixed

- Wait for Studio's draft banner and open its shadow-DOM action before editing
  a stranded upload.

## [0.1.7] - 2026-08-15

### Fixed

- Detect invalid or visually red inputs, textareas, and selectors before the
  upload flow presses `Next`, and stop with field-level diagnostics.
- Focus and verify upload title and description fields before advancing.
- Verify title and description after upload, and support the current Studio Save control.

- Activate Studio's visible "Select files" control when the upload modal has
  mounted without exposing its file input yet.

## [0.1.4] - 2026-08-11

### Fixed

- Verify that Public visibility is saved before closing Studio.

## [0.1.3] - 2026-08-11

### Fixed

- Wait for the Studio video editor to mount before changing visibility.
- Preserve Studio's selected timed-caption option before continuing.

## [0.1.2] - 2026-08-11

### Fixed

- Upload timed caption files through Studio's current `Upload manual` dialog.

## [0.1.1] - 2026-08-11

### Fixed

- Open the target channel's upload route directly instead of returning through
  the Studio dashboard.
- Stop after one bounded upload-dialog attempt when Studio does not mount a
  file input.

## [0.1.0] - 2026-08-11

### Fixed

- Wait for the active YouTube Studio channel before using channel controls.
- Wait for the upload launcher to become visible before starting an upload.
