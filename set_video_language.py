"""Set video language via the Languages tab dialog.

The Studio language dropdown is scroll-virtualized: an option's element only
exists in the DOM while scrolled near. Open the dropdown, scroll the list
container until the wanted option mounts, dispatch a real click sequence on it,
then press Confirm.
"""
import argparse
import asyncio

from camoufox_session import make_camoufox, prepare_page, logged_in_youtube, log
from channel import resolve_channel_id, select_channel
import youtube_ui as ui

STUDIO = "https://studio.youtube.com"

async def pick_option(page, language, passes=40):
    """Wheel the virtualized list until the option mounts, then click it.

    The option is clicked with the mouse. Dispatching a MouseEvent at it would
    be simpler and would arrive with isTrusted: false.
    """
    for attempt in range(passes):
        if await ui.click_text_in_shadow(page, language, timeout_ms=600):
            log(f"  picked {language!r} after {attempt} scrolls")
            return True
        if not await ui.wheel_scroll_list(page):
            return False
    return False


async def run(args):
    async with make_camoufox(False) as context:
        page = await prepare_page(context)
        await page.goto(STUDIO, wait_until="commit", timeout=30_000)
        await page.wait_for_timeout(2500)
        if not await logged_in_youtube(page):
            raise RuntimeError("not logged into YouTube")
        wanted = await resolve_channel_id(page, args.channel_handle)
        await select_channel(page, channel_id=wanted, handle=args.channel_handle)

        await page.goto(f"{STUDIO}/video/{args.video_id}/translations",
                        wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(8000)
        await ui.dismiss_overlays(page)

        if not await ui.click_text_in_shadow(page, "set language", timeout_ms=10_000):
            raise RuntimeError("Set language control not found")
        await page.wait_for_timeout(1500)

        if not await pick_option(page, args.language):
            raise RuntimeError(f"option {args.language!r} not reachable")
        await page.wait_for_timeout(1200)
        if not await ui.click_text_in_shadow(page, "confirm", timeout_ms=10_000):
            raise RuntimeError("Confirm control not found")
        await page.wait_for_timeout(3000)

        text = (await ui.all_text(page)).lower()
        ok = f"{args.language.lower()} (video language)" in text
        log(f"RESULT: {'OK' if ok else 'NOT_CONFIRMED'}")
        await page.screenshot(path="debug/set_language_result.png")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--channel-handle", required=True)
    p.add_argument("--video-id", required=True)
    p.add_argument("--language", default="English")
    return p.parse_args()


asyncio.run(run(parse_args()))
