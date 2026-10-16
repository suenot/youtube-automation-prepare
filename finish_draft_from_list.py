"""Finish a stranded draft whose /edit page renders as an error page.

The draft banner lives on the video page, but Studio sometimes serves that
page as "oops, something went wrong" while the upload wizard state persists.
The content-list row still offers "Edit draft" — enter the wizard from there,
then reuse publish.py's dialog steps.
"""
import argparse
import asyncio
import json

import youtube_ui as ui
from camoufox_session import make_camoufox, prepare_page, logged_in_youtube, log
from channel import resolve_channel_id, select_channel
from publish import fill_details, click_next, set_visibility, save

STUDIO = "https://studio.youtube.com"

async def run(args):
    meta = json.load(open(args.metadata))
    async with make_camoufox(False) as context:
        page = await prepare_page(context)
        await page.goto(STUDIO, wait_until="commit", timeout=30_000)
        await page.wait_for_timeout(2500)
        if not await logged_in_youtube(page):
            raise RuntimeError("not logged into YouTube")
        wanted = await resolve_channel_id(page, args.channel_handle)
        await select_channel(page, channel_id=wanted, handle=args.channel_handle)

        await page.goto(f"{STUDIO}/channel/{wanted}/videos",
                        wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(8000)
        if not await ui.click_text_in_shadow(page, "edit draft", timeout_ms=15_000):
            raise RuntimeError("Edit draft row control never appeared")
        log("  clicked Edit draft on the list row")
        await page.wait_for_timeout(6000)

        if not await fill_details(page, meta, args.thumbnail, False, True):
            raise RuntimeError("fill_details failed")
        if not await click_next(page, 3, True):
            raise RuntimeError("click_next failed")
        if not await set_visibility(page, args.visibility, True):
            raise RuntimeError("set_visibility failed")
        ok = await save(page, True)
        log(f"RESULT: {'OK' if ok else 'FAILED'}")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("channel_handle")
    p.add_argument("--metadata", required=True)
    p.add_argument("--thumbnail", default="")
    p.add_argument("--visibility", default="private")
    return p.parse_args()


asyncio.run(run(parse_args()))
