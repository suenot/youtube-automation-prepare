#!/usr/bin/env python3
"""Put videos into one playlist, in the given order, from each video's details page.

The playlist is created on the first video if no playlist with that exact title
exists yet; it is never created twice.  Default video order is set to
"Date added (oldest)" so the playlist follows the order the videos are added in,
which is the course order passed on the command line.

    playlist.py @marketmaker-cc --title "Complex Arbitrage in Rust" \
        --description "..." --ids=Kskvwt7ethI,k2Qckzv6uSM,...

Use --ids=... (with "=") because some video ids start with "-".
"""
import argparse
import asyncio
import re
import sys

from camoufox_session import make_camoufox, prepare_page, log, shot
from channel import select_channel
from publish import STUDIO
import schedule_video as sv

JS_STATE = """title => {
  const walk = (n, out) => { for (const e of n.querySelectorAll('*')) { if (e.shadowRoot) walk(e.shadowRoot, out); out.push(e); } };
  const all = []; walk(document, all);
  const dlg = all.find(e => e.tagName === 'YTCP-PLAYLIST-DIALOG');
  if (!dlg) return {dialog: false};
  const box = []; walk(dlg, box);
  const labels = box.filter(e => e.children.length === 0 && (e.textContent || '').trim().length > 0 &&
    e.closest && e.offsetParent !== null);
  const names = [];
  let hit = null;
  for (const cb of box.filter(e => e.tagName === 'YTCP-CHECKBOX-LIT' || e.getAttribute('role') === 'checkbox')) {
    let row = cb.parentElement || cb.getRootNode().host;
    for (let i = 0; i < 4 && row && !(row.textContent || '').trim(); i++) row = row.parentElement;
    const text = (row && row.textContent || '').trim().replace(/\\s+/g, ' ');
    names.push(text);
    if (text === title || text.startsWith(title + ' ')) {
      const checked = cb.getAttribute('aria-checked') === 'true' || cb.hasAttribute('checked');
      const b = cb.getBoundingClientRect();
      hit = {checked, x: b.x + b.width / 2, y: b.y + b.height / 2};
    }
  }
  return {dialog: true, names, hit};
}"""


NEW_BTN = re.compile(r"^\s*(New|Create) playlist\s*$")


async def open_details(page, vid):
    await page.goto(f"{STUDIO}/video/{vid}/edit", wait_until="domcontentloaded", timeout=60000)
    await sv.wait_for_edit_surface(page)
    await page.wait_for_timeout(4000)


async def open_dialog(page):
    pl = page.locator("ytcp-video-metadata-playlists").first
    await pl.scroll_into_view_if_needed()
    await page.wait_for_timeout(800)
    await pl.locator("ytcp-dropdown-trigger, #trigger, [role=button]").first.click()
    # the ytcp-playlist-dialog host reports hidden; wait for its visible content instead
    try:
        # "New playlist" when the channel has playlists, "Create playlist" when it has none
        await page.get_by_text(NEW_BTN).last.wait_for(state="visible", timeout=15000)
    except Exception:
        await shot(page, "pl_nodialog", True)
        raise RuntimeError("playlist dialog did not open")
    await page.wait_for_timeout(2000)


async def create_playlist(page, title, description):
    dlg = page.locator("ytcp-playlist-dialog")
    await dlg.get_by_text(NEW_BTN).last.click()
    await page.wait_for_timeout(1200)
    await page.locator("tp-yt-paper-item").filter(has_text=re.compile(r"^\s*New playlist\s*$")).last.click()
    await page.wait_for_timeout(2500)
    create = page.locator("ytcp-playlist-creation-dialog, tp-yt-paper-dialog").filter(
        has_text="Create a new playlist").last
    await create.wait_for(state="visible", timeout=15000)
    boxes = create.locator("#textbox, [contenteditable=true], textarea")
    await boxes.nth(0).click()
    await page.keyboard.type(title, delay=20)
    if description:
        await boxes.nth(1).click()
        await page.keyboard.type(description, delay=5)
    # Default video order: the order videos are added in (= course order)
    try:
        await create.get_by_text("Date published (newest)", exact=True).first.click()
        await page.wait_for_timeout(1000)
        await page.locator("tp-yt-paper-item").filter(
            has_text=re.compile(r"^\s*Date added \(oldest\)\s*$")).last.click()
        await page.wait_for_timeout(800)
    except Exception as exc:  # order can be fixed later on the playlist page
        log(f"  WARN could not set default order: {exc}")
    await shot(page, "pl_create_filled", True)
    btn = create.get_by_role("button", name="Create", exact=True)
    if await btn.is_disabled():
        raise RuntimeError("Create button disabled")
    await btn.click()
    await page.wait_for_timeout(4000)


async def save_details(page):
    save = await sv.first_visible((
        page.locator("ytcp-button#save"),
        page.get_by_role("button", name="Save", exact=True),
    ))
    if save is None:
        raise RuntimeError("Save button not found")
    if await save.is_disabled():
        log("  Save disabled: nothing changed")
        return
    await sv.dom_click(save)
    await sv.wait_for_save_landed(page, save)


async def add_one(page, vid, title, description, allow_create):
    await open_details(page, vid)
    await open_dialog(page)
    st = await page.evaluate(JS_STATE, title)
    log(f"  {vid}: playlists {st.get('names')}")
    created = False
    if not st.get("hit"):
        if not allow_create:
            raise RuntimeError(f"playlist '{title}' not in list for {vid}")
        await create_playlist(page, title, description)
        created = True
        st = await page.evaluate(JS_STATE, title)
        if not st.get("dialog"):
            await open_dialog(page)
            st = await page.evaluate(JS_STATE, title)
        if not st.get("hit"):
            await shot(page, "pl_after_create", True)
            raise RuntimeError("playlist created but not found in the list")
    if not st["hit"]["checked"]:
        await page.mouse.click(st["hit"]["x"], st["hit"]["y"])
        await page.wait_for_timeout(1000)
        st = await page.evaluate(JS_STATE, title)
        if not st["hit"]["checked"]:
            await shot(page, "pl_nocheck", True)
            raise RuntimeError("checkbox did not turn on")
    await page.locator("ytcp-playlist-dialog").get_by_role("button", name="Done", exact=True).last.click()
    await page.wait_for_timeout(1500)
    await save_details(page)
    log(f"  {vid}: in playlist{' (created)' if created else ''}")
    return created


async def run(args):
    ids = [v.strip() for v in args.ids.split(",") if v.strip()]
    async with make_camoufox(False) as ctx:
        page = await prepare_page(ctx)
        await page.goto(STUDIO, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)
        await select_channel(page, handle=args.channel_handle)
        allow_create = True
        for vid in ids:
            if await add_one(page, vid, args.title, args.description, allow_create):
                pass
            allow_create = False  # never create a second playlist with the same title
        log(f"RESULT {args.channel_handle}: {len(ids)} videos in '{args.title}'")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("channel_handle")
    p.add_argument("--title", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--ids", required=True, help="comma-separated, in watching order; pass as --ids=...")
    args = p.parse_args()
    try:
        asyncio.run(run(args))
    except (RuntimeError, ValueError) as exc:
        log(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
