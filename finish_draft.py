#!/usr/bin/env python3
"""Finish an upload that publish.py left stranded as a draft.

When the details wizard fails to reach the visibility step, the upload survives
as a draft: no title, no description, not public. Re-uploading makes a duplicate
of a video that is already on the channel, so finish the existing one instead.

    python finish_draft.py @marketmaker-zh VIDEO_ID metadata.json [--visibility public]

The edit page cannot fix a draft -- it disables everything with "Feature
unavailable while video is in a draft state" -- but its "Edit draft" button
reopens the very wizard that failed, with the uploaded file already attached.
"""
import argparse
import asyncio
import sys
import time

from camoufox_session import make_camoufox, prepare_page, log
from channel import select_channel
from metadata import load_metadata
from publish import fill_details, click_next, set_visibility, save
import youtube_ui as ui

STUDIO_VIDEO = "https://studio.youtube.com/video/{vid}/edit"
DRAFT_BANNER_SELECTOR = "ytcp-banner ytcp-button#action-1, ytcp-button#action-1"


async def wait_for_draft_banner(page, timeout_ms=30_000):
    """Wait for Studio's late-mounted draft banner and return its action."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        text = await ui.all_text(page)
        button = page.locator(DRAFT_BANNER_SELECTOR)
        if "draft state" in text.lower() and await button.count() > 0:
            return button.first
        await page.wait_for_timeout(500)
    return None


async def click_draft_banner(page, button):
    try:
        await button.click(timeout=4000, force=True)
        return True
    except Exception:
        return await ui.mouse_click(page, button)


async def finish(page, vid: str, meta, thumbnail: str, visibility: str,
                 made_for_kids: bool, debug: bool) -> bool:
    if not await ui.open_draft_via_ui(page, log):
        return False
    await page.wait_for_timeout(3000)

    if not await fill_details(page, meta, thumbnail, made_for_kids, debug):
        return False
    if not await click_next(page, 3, debug):
        return False
    if not await set_visibility(page, visibility, debug):
        return False
    return await save(page, debug)


async def main(args) -> int:
    meta = load_metadata(args.metadata, args.title, args.description, args.tags)
    async with make_camoufox(args.headless) as ctx:
        page = await prepare_page(ctx)
        await select_channel(page, handle=args.channel_handle)
        ok = await finish(page, args.video_id, meta, args.thumbnail,
                          args.visibility, args.made_for_kids, args.debug)
    log(f"RESULT: {'finished' if ok else 'FAILED'} {args.video_id}")
    return 0 if ok else 1


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("channel_handle")
    p.add_argument("video_id")
    p.add_argument("--metadata", default="")
    p.add_argument("--title", default="")
    p.add_argument("--description", default="")
    p.add_argument("--tags", default="")
    p.add_argument("--thumbnail", default="")
    p.add_argument("--visibility", default="public")
    p.add_argument("--made-for-kids", action="store_true")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--debug", action="store_true")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
