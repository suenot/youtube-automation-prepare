#!/usr/bin/env python3
"""Set custom thumbnails on already-published videos.

publish.py sometimes reports "thumbnail input not ready; skipping" — the
uploader element mounts late in the details dialog — which leaves the video on
an auto-generated frame. This sets them after the fact, in batch.

    python set_thumbnail.py @marketmaker-cc VIDEO_ID thumb.png [VIDEO_ID thumb.png ...]

The file input is hidden, so the locator API refuses to touch it (actionability
check never passes). Grab an ElementHandle instead — set_input_files on a handle
works on hidden inputs.

YouTube caps custom thumbnails at roughly 10 per day per channel; past that the
"Build your channel history" modal appears and the save silently does nothing.
"""
import asyncio
import sys
from pathlib import Path

from camoufox_session import make_camoufox, prepare_page, log
from channel import select_channel

STUDIO_VIDEO = "https://studio.youtube.com/video/{vid}/edit"


async def set_one(page, vid: str, png: Path) -> bool:
    from youtube_ui import open_video_via_ui
    if not await open_video_via_ui(page, vid, log):
        return False
    await page.wait_for_timeout(4000)

    el = (await page.query_selector("ytcp-thumbnail-uploader input[type='file']")
          or await page.query_selector("input[type='file']"))
    if el is None:
        log(f"  {vid} no file input found")
        return False
    await el.set_input_files(str(png))
    await page.wait_for_timeout(5000)

    for name in ("Save", "SAVE"):
        try:
            await page.get_by_role("button", name=name).first.click(timeout=6000)
            await page.wait_for_timeout(4000)
            log(f"{vid} SAVED")
            return True
        except Exception:
            continue
    shot = Path(__file__).parent / "debug" / f"thumb_fail_{vid}.png"
    shot.parent.mkdir(exist_ok=True)
    try:  # the thumbnail section sits below the fold: show it, not the description
        await page.locator("ytcp-thumbnail-uploader, #still-picker").first.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        log("  no thumbnail uploader on the page")
    await page.screenshot(path=str(shot))
    log(f"  {vid} could not click Save (screenshot: {shot})")
    return False


async def main(handle: str, pairs: list[tuple[str, Path]]) -> int:
    failed = []
    async with make_camoufox(False) as ctx:
        page = await prepare_page(ctx)
        await select_channel(page, handle=handle)
        for vid, png in pairs:
            try:
                if not await set_one(page, vid, png):
                    failed.append(vid)
            except Exception as e:  # noqa: BLE001
                log(f"  {vid} ERROR: {str(e).splitlines()[0][:80]}")
                failed.append(vid)
    if failed:
        log(f"failed: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) < 3 or len(args) % 2 == 0:
        sys.exit(__doc__)
    handle, rest = args[0], args[1:]
    pairs = []
    for vid, png in zip(rest[0::2], rest[1::2]):
        p = Path(png)
        if not p.is_file():
            sys.exit(f"no such thumbnail: {p}")
        pairs.append((vid, p))
    sys.exit(asyncio.run(main(handle, pairs)))
