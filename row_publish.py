#!/usr/bin/env python3
"""Make one video public or schedule it, via its row in Studio's Content list.

The video-details page of the current Studio build has no Visibility card, so
this drives the row's visibility dialog (Save or publish / Schedule).  The row is
found by video id, never by position.

    row_publish.py @marketmaker-ru VIDEO_ID --mode public
    row_publish.py @marketmaker-ru VIDEO_ID --mode schedule --date 2026-11-08 \
        --time "4:00 PM" --tz Moscow
"""
import argparse
import asyncio
import re
import sys

from camoufox_session import make_camoufox, prepare_page, log, shot
from channel import select_channel
import schedule_video as sv

JS_ROW = """vid => {
  const walk = (n, out) => { for (const e of n.querySelectorAll('*')) { if (e.shadowRoot) walk(e.shadowRoot, out); out.push(e); } };
  const all = []; walk(document, all);
  const a = all.find(e => e.tagName === 'A' && (e.getAttribute('href') || '').includes('/video/' + vid + '/'));
  if (!a) return null;
  let r = a;
  while (r && r.tagName !== 'YTCP-VIDEO-ROW') r = r.getRootNode().host || r.parentElement;
  if (!r) return null;
  r.scrollIntoView({block: 'center'});
  const o = []; walk(r, o);
  const v = o.filter(e => e.children.length === 0 &&
    /^(Private|Public|Unlisted|Scheduled)$/.test((e.textContent || '').trim())).pop();
  if (!v) return null;
  window.__visLeaf = v;
  const b = v.getBoundingClientRect();
  return {x: b.x + b.width / 2, y: b.y + b.height / 2, state: v.textContent.trim()};
}"""


async def find_row(page, vid):
    for _ in range(14):
        pos = await page.evaluate(JS_ROW, vid)
        if pos:
            await page.wait_for_timeout(500)
            return await page.evaluate(JS_ROW, vid)
        await page.mouse.move(700, 400)
        await page.mouse.wheel(0, 500)
        await page.wait_for_timeout(1500)
    return None


async def click_at(page, pos):
    await page.mouse.move(pos["x"] - 12, pos["y"])
    await page.wait_for_timeout(500)
    await page.mouse.move(pos["x"], pos["y"])
    await page.wait_for_timeout(300)
    await page.mouse.down()
    await page.wait_for_timeout(90)
    await page.mouse.up()


async def open_content(page, handle):
    await select_channel(page, None, handle)
    await page.goto("https://studio.youtube.com", wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)
    await page.get_by_text("Content", exact=True).first.click()
    await page.wait_for_timeout(6000)


async def set_zone(page, name):
    await page.get_by_text("Time zone", exact=True).first.click()
    await page.wait_for_timeout(1200)
    item = page.get_by_text(re.compile(r"^\(GMT[+-]\d\d:\d\d\) " + re.escape(name))).first
    await item.scroll_into_view_if_needed()
    await item.click()
    await page.wait_for_timeout(800)


async def press(page, label):
    btn = await sv.first_visible((
        page.get_by_role("button", name=label, exact=True),
        page.locator("ytcp-button").filter(has_text=re.compile(rf"^{label}$")),
    ))
    if btn is None or await btn.is_disabled():
        raise RuntimeError(f"{label} button missing or disabled")
    await sv.dom_click(btn)


async def run(args):
    async with make_camoufox(False) as ctx:
        page = await prepare_page(ctx)
        await open_content(page, args.channel_handle)
        pos = await find_row(page, args.video_id)
        if not pos:
            raise RuntimeError("row for video id not found in Content list")
        log(f"  row {args.video_id}: {pos['state']}")
        if pos["state"] != "Private":
            raise RuntimeError(f"expected Private, row says {pos['state']}")
        for attempt in range(6):
            if attempt % 2 == 0:
                await click_at(page, pos)
            else:
                await page.evaluate("""() => { const v = window.__visLeaf; if (!v) return;
                  let t = v; for (let i = 0; i < 6 && t; i++) { t = t.parentElement || (t.getRootNode() && t.getRootNode().host); if (t && /visibility/i.test(t.tagName + ' ' + t.className)) break; }
                  (t || v).click(); }""")
            try:
                await page.get_by_text("Save or publish", exact=True).wait_for(timeout=4000)
                break
            except Exception:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(800)
                pos = await find_row(page, args.video_id) or pos
        else:
            await shot(page, "row_nodialog", True)
            raise RuntimeError(f"visibility dialog did not open (pos {pos})")
        if args.mode == "public":
            radio = await sv.first_visible((
                page.locator('tp-yt-paper-radio-button[name="PUBLIC"]'),
                page.locator("tp-yt-paper-radio-button").filter(has_text=re.compile(r"^\\s*Public")),
            ))
            if radio is None:
                raise RuntimeError("Public radio not found")
            await sv.dom_click(radio)
            await page.wait_for_timeout(600)
            if args.dry_run:
                await shot(page, "row_public_dry", True)
                log("  DRY RUN: Public selected, not saved")
                return
            await press(page, "Publish")
        else:
            date_text = sv.display_date(args.date)
            time_text = sv.display_time(args.time)
            await sv.open_schedule(page)
            if args.tz:
                await set_zone(page, args.tz)
            await sv.choose_date(page, date_text)
            await sv.fill_time(page, time_text)
            await shot(page, "row_sched_filled", True)
            log(f"  filled: {await sv.schedule_values(page)} tz={args.tz}")
            if args.dry_run:
                log("  DRY RUN: not saved")
                return
            await press(page, "Schedule")
        await page.wait_for_timeout(5000)
        # a confirmation dialog may follow; close it if it has a Close button
        for label in ("Close", "Done"):
            btn = page.get_by_role("button", name=label, exact=True)
            if await btn.count() and await btn.first.is_visible():
                await btn.first.click()
                await page.wait_for_timeout(1500)
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)
        await page.get_by_text("Content", exact=True).first.click()
        await page.wait_for_timeout(5000)
        after = await find_row(page, args.video_id)
        await shot(page, "row_after", True)
        log(f"RESULT {args.video_id}: {after['state'] if after else 'row lost'}")
        want = "Public" if args.mode == "public" else "Scheduled"
        if not after or after["state"] != want:
            raise RuntimeError(f"state is {after and after['state']}, wanted {want}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("channel_handle")
    p.add_argument("video_id")
    p.add_argument("--mode", choices=["public", "schedule"], required=True)
    p.add_argument("--date")
    p.add_argument("--time")
    p.add_argument("--tz", default="")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.mode == "schedule" and not (args.date and args.time):
        p.error("--date and --time required")
    try:
        asyncio.run(run(args))
    except (RuntimeError, ValueError) as exc:
        log(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
