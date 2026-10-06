# youtube-automation-prepare

Publish a finished video to **YouTube** by driving **YouTube Studio** through an
anti-detect **Camoufox** browser session — **no Data API, no OAuth, no API keys**.
It reuses a logged-in YouTube session (its own persistent Camoufox profile),
handles the "Verify it's you" gate, selects the target channel, pre-checks the
video length against the unverified-channel limit, and verifies the upload
actually published.

<p align="center">
  <img src="assets/video-publisher_comic.png" alt="youtube-automation-prepare — publish to YouTube through a browser session, no API keys" width="820">
</p>

## 🏭 The Content Factory

youtube-automation-prepare is the **final stage (4)** of an automated pipeline that turns a
**blog article into a published YouTube video** — no API keys, driven end-to-end
through logged-in browser sessions (Camoufox) and local media tooling.

| # | Stage | Repo | What it does |
|---|-------|------|--------------|
| 1 | Generate | [gaia](https://github.com/suenot/gaia) | Drive NotebookLM / Gemini / Flow from a logged-in session → audio overview + slide deck |
| 2 | Build | [video-maker](https://github.com/suenot/video-maker) | Audio narration + slide-deck PDF → synced MP4 (+ SRT, thumbnail) |
| 3 | Describe | [youtube-automation-metadata](https://github.com/suenot/youtube-automation-metadata) | Video + article → YouTube title / description / tags / chapter timestamps |
| **4** | **Publish** | **[youtube-automation-prepare](https://github.com/suenot/youtube-automation-prepare)** ⬅ *this repo* | Drive YouTube Studio → upload with metadata, channel switch, visibility |

**Flow:** `article → gaia → video-maker → youtube-automation-metadata → youtube-automation-prepare → YouTube`
(the published video is then embedded back into the blog article).

> ⚠️ **ACCOUNT SAFETY — READ THIS**
> The Camoufox profile (`.camoufox_profile/`) holds **live Google session
> cookies**. Anyone who gets it **controls your YouTube/Google account**. It is
> git-ignored and a pre-commit hook blocks it — **never** commit it, the
> fingerprint (`.camoufox_fp.pkl`), `debug/` screenshots (they show your logged-in
> account), or any `*.sqlite` / `cookies*.json`. This repo is public; keep the
> session private.

## Disclaimer

For personal/educational use. Automating YouTube may violate its Terms of
Service and can get an account rate-limited or suspended. Use your own account,
one session at a time, at your own risk. Provided AS IS.

## Setup

Use Python **3.11–3.13**. The pinned Camoufox 0.5.4 package uses Playwright
1.59–1.60, matching the current Camoufox browser protocol.

```bash
python3.11 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python -m camoufox fetch      # one-time: download the Camoufox browser

# One-time: sign into the TARGET YouTube account (persists in .camoufox_profile/)
venv/bin/python login.py
```

`ffmpeg` (for `ffprobe`) must be on `PATH` for the length pre-check.

## Usage

```bash
# Publish a video_maker bundle as a private draft to a specific channel
venv/bin/python publish.py \
    --video    ../video_maker/output/SLUG/SLUG.mp4 \
    --metadata ../video_maker/output/SLUG/SLUG_metadata.json \
    --thumbnail ../video_maker/output/SLUG/SLUG_thumbnail.png \
    --channel-handle @your-channel \
    --visibility private --debug

# Manual, unlisted, on a channel by id
venv/bin/python publish.py --video clip.mp4 --title "Hi" \
    --channel-id UCxxxxxxxx --visibility unlisted
```

### Flags

`--video` (required), `--metadata` (video_maker JSON), `--thumbnail`,
`--title`/`--description`/`--tags` (overrides), `--channel-id` /
`--channel-handle`, `--visibility {private,unlisted,public}` (default
**private**), `--made-for-kids` (default: not for kids), `--allow-long`
(bypass the 15-min unverified block), `--verify-wait`, `--keep-open`, `--debug`.

### Schedule an uploaded video

Scheduling uses the video's Studio edit page, so the exact video id is the
source of truth and content-list hover tooltips cannot intercept a click:

```bash
venv/bin/python schedule_video.py \
  --channel-handle @your-channel \
  --video-id VIDEO_ID \
  --date 2026-08-03 \
  --time '5:00 PM' \
  --expect-timezone 'Pacific|GMT-7|PDT|PT'
```

Add `--dry-run --debug` to fill and screenshot the schedule without saving it.
The command verifies the active channel, timezone, accepted date/time and the
values stored by Studio after saving. Re-running the same schedule is a no-op.

### Exit codes

`0` ok · `2` bad args · `3` not logged in · `4` couldn't start upload ·
`5` details failed · `6` couldn't finish/verify · `7` blocked by "Verify it's
you" (clear it once in the window with `--keep-open`) · `8` pre-check failed
(video too long for an unverified channel — verify the channel or `--allow-long`).

## Why videos get rejected as "Processing abandoned"

Unverified channels can't upload videos longer than **15 minutes**. The
pre-check blocks these before wasting an upload. Verify the channel at
youtube.com/verify, or pass `--allow-long` if the channel is already verified.
