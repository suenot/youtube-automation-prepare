#!/usr/bin/env python3
"""Set visibility of an existing (non-draft) video by clicking through Studio.

    python set_visibility.py @marketmaker-cc VIDEO_ID [--visibility public]
"""
import argparse
import asyncio
import random
import sys

from camoufox_session import make_camoufox, prepare_page, log
from channel import select_channel
import youtube_ui as ui


async def click_text(page, text, tries=10, dx=0):
    for _ in range(tries):
        pos = await page.evaluate(ui.JS_FIND_RECT, {"text": text})
        if pos:
            await page.mouse.move(pos["x"] - random.randint(4, 18), pos["y"] - random.randint(2, 8))
            await page.wait_for_timeout(random.randint(300, 800))
            await page.mouse.move(pos["x"] + dx, pos["y"])
            await page.wait_for_timeout(random.randint(400, 900))
            await page.mouse.down()
            await page.wait_for_timeout(random.randint(60, 140))
            await page.mouse.up()
            return True
        await page.mouse.move(700, 400)
        await page.mouse.wheel(0, random.randint(350, 500))
        await page.wait_for_timeout(random.randint(1200, 2000))
    return False


async def run(args):
    label = args.visibility.capitalize()
    async with make_camoufox(args.headless) as ctx:
        page = await prepare_page(ctx)
        target = await select_channel(page, None, args.channel_handle)
        if not target:
            return 1
        # Visibility is edited from the row in Content (the details page does not
        # render the Visibility card in every browser build).
        if "studio.youtube.com" not in page.url:
            await page.goto("https://studio.youtube.com", wait_until="domcontentloaded",
                            timeout=60_000)
            await page.wait_for_timeout(random.randint(5000, 8000))
        if not await click_text(page, "Content"):
            log("  Content not found")
            return 1
        await page.wait_for_timeout(random.randint(4000, 6000))
        await page.screenshot(path="debug/vis_1_list.png")
        cur_label = args.current.capitalize()
        if not await click_text(page, cur_label, tries=3, dx=48):
            log(f"  row visibility '{cur_label}' not found")
            return 1
        await page.wait_for_timeout(2500)
        await page.screenshot(path="debug/vis_2_dialog.png")
        if args.dry:
            log("  dry run: stopped with the dialog open")
            await page.wait_for_timeout(8000)
            return 0
        if not await click_text(page, label):
            log(f"  option {label} not found")
            return 1
        await page.wait_for_timeout(1500)
        await click_text(page, "Done", tries=1)
        await page.wait_for_timeout(2500)
        await page.screenshot(path="debug/vis_3_done.png")
        if not await click_text(page, "Save"):
            log("  Save not found")
            return 1
        await page.wait_for_timeout(5000)
        await page.screenshot(path="debug/vis_4_saved.png")
        log(f"{args.video_id} visibility -> {label} (verify in Studio)")
        return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("channel_handle")
    p.add_argument("video_id")
    p.add_argument("--visibility", default="public", choices=["private", "unlisted", "public"])
    p.add_argument("--current", default="private",
                   choices=["private", "unlisted", "public"],
                   help="current visibility of the row to change (first match in Content)")
    p.add_argument("--dry", action="store_true", help="stop once the dialog is open")
    p.add_argument("--headless", action="store_true")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
