"""Recover the video ID of an already-uploaded (private) video via Studio's
content list. Use when publish.py uploaded successfully but failed to scrape the
VIDEO_ID from the save dialog. Does NOT upload — avoids duplicates.

Usage: python3 recover_id.py "<full video title>" [@channel-handle]   (default @marketmaker-cc)
"""
import asyncio, sys
from camoufox_session import make_camoufox, prepare_page, logged_in_youtube, log
from channel import select_channel
from publish import find_by_title, STUDIO

CHANNELS = {"@marketmaker-cc": "UCbPEVsO_M-axL0mylsoTADw"}  # others are resolved from the handle


async def main():
    title = sys.argv[1] if len(sys.argv) > 1 else ""
    handle = sys.argv[2] if len(sys.argv) > 2 else "@marketmaker-cc"
    async with make_camoufox(False) as ctx:
        page = await prepare_page(ctx)
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))
        await page.goto(STUDIO, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(4000)
        if not await logged_in_youtube(page):
            log("ERROR: not logged in"); print("VIDEO_ID: NOT_LOGGED_IN"); return
        chan = await select_channel(page, channel_id=CHANNELS.get(handle), handle=handle)
        vid = await find_by_title(page, chan, title)
        print("VIDEO_ID:", vid or "NOT_FOUND")


asyncio.run(main())
