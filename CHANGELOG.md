# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic
Versioning.

## [Unreleased]

### Fixed

- Click Studio's controls with the mouse instead of dispatching `MouseEvent` sequences at the elements found in the shadow DOM. A dispatched event reaches the handler carrying `isTrusted: false`, which is a page's plainest sign that nobody clicked anything. The shadow-DOM search stays; it only reads. Affects `finish_draft_from_list.py`, `set_video_language.py` and `upload_subtitles.py`.
- Type the upload title and description instead of trying `insert_text`, then `fill`, then `type(delay=0)`, then `execCommand("insertText")`. Measured on a probe page, all four put the text in with a single `input` event and not one keystroke. Typing produces the per-character input events Polymer listens for, so the persisted model is updated the same way; the existing read-back verification is unchanged, so a field that does not take the text still fails.
- Scroll the virtualized language dropdown with the wheel rather than by assigning `scrollTop` from page script.

### Added

- `human.py`, the input module measured and documented in `docs-site/@docs/guides/what-a-page-sees-when-we-type.md`, kept in step with the copy in `tiktok-automation`: an eased approach to a control, a pause before pressing, typing with a varying interval, insertion for what a keyboard cannot produce, and a real clipboard paste, which is the default for long, multi-line or non-ASCII text (`allow_clipboard=False` turns it off).
- `youtube_ui.click_text_in_shadow` and `youtube_ui.wheel_scroll_list`.
- Test that fails the build if any module in the repository fabricates a browser event or calls `execCommand` again, and a test that the upload field is typed into rather than filled.

### Changed

- Rename the repository to `youtube-automation-prepare` and update the README
  project name and pipeline links.

## [0.2.1] - 2026-10-08

### Added

- Refresh a saved, channel-bound Camoufox session from a read-only Firefox
  cookie snapshot with `login.py --refresh`. Keep the profile fingerprint,
  channel binding and publication journal; restore the previous cookie files
  if authentication or channel verification fails. Retain the private recovery
  copy if rollback itself fails.

### Fixed

- Treat a Google sign-in or account-chooser redirect as signed out even when
  stale SID cookies remain. Require an authenticated Studio channel or video page and
  visible account control before continuing.

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
