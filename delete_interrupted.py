#!/usr/bin/env python3
"""Delete "Upload interrupted" draft rows from a channel's Content list.

    delete_interrupted.py @marketmaker-zh --title "切片内部：子订单执行战术与队列位置"

An interrupted upload has no usable video id, so the row is found by its title
AND its "Upload interrupted" notice; any other row is never touched. The row's
"Delete video" button opens the usual confirm dialog.
"""
import argparse
import asyncio
import sys

from camoufox_session import make_camoufox, prepare_page, log, shot
from channel import select_channel

JS_ROWS = """title => {
  const walk = (n, out) => { for (const e of n.querySelectorAll('*')) { if (e.shadowRoot) walk(e.shadowRoot, out); out.push(e); } };
  const all = []; walk(document, all);
  const rows = all.filter(e => e.tagName === 'YTCP-VIDEO-ROW');
  const res = [];
  for (const r of rows) {
    const o = []; walk(r, o);
    const txt = o.filter(e => e.children.length === 0).map(e => (e.textContent || '').trim()).join(' | ');
    if (!txt.includes(title) || !/Upload interrupted/i.test(txt)) continue;
    const b = o.find(e => e.tagName === 'YTCP-BUTTON' && /Delete video/i.test(e.textContent || ''));
    if (!b) continue;
    r.scrollIntoView({block: 'center'});
    const q = b.getBoundingClientRect();
    res.push({x: q.x + q.width / 2, y: q.y + q.height / 2, txt: txt.slice(0, 120)});
  }
  return res;
}"""


async def click_at(page, pos):
    await page.mouse.move(pos["x"] - 12, pos["y"])
    await page.wait_for_timeout(400)
    await page.mouse.move(pos["x"], pos["y"])
    await page.wait_for_timeout(300)
    await page.mouse.down()
    await page.wait_for_timeout(90)
    await page.mouse.up()


async def run(args):
    async with make_camoufox(False) as ctx:
        page = await prepare_page(ctx)
        await select_channel(page, None, args.channel_handle)
        await page.goto("https://studio.youtube.com", wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        await page.get_by_text("Content", exact=True).first.click()
        await page.wait_for_timeout(6000)
        rows = await page.evaluate(JS_ROWS, args.title)
        log(f"  interrupted rows matching title: {len(rows)}")
        if len(rows) != 1:
            await shot(page, "interrupted_rows", True)
            raise RuntimeError(f"expected exactly 1 row, found {len(rows)}")
        log(f"  row: {rows[0]['txt']}")
        await click_at(page, rows[0])
        await page.wait_for_timeout(2500)
        try:
            await page.get_by_text("I understand that deleting", exact=False).first.click(timeout=6000)
        except Exception:
            log("  could not tick the confirmation box")
        await page.wait_for_timeout(800)
        await shot(page, "interrupted_confirm", True)
        done = False
        for label in ("Delete forever", "Delete video", "Delete"):
            btn = page.locator(f"ytcp-button:has-text('{label}'), button:has-text('{label}')")
            for i in range(await btn.count()):
                el = btn.nth(i)
                try:
                    if not await el.is_visible() or not await el.is_enabled():
                        continue
                    await el.click(timeout=6000, force=True)
                    done = True
                    break
                except Exception:
                    continue
            if done:
                break
        if not done:
            raise RuntimeError("could not press the confirm button")
        await page.wait_for_timeout(6000)
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(7000)
        left = await page.evaluate(JS_ROWS, args.title)
        log(f"  RESULT: {'DELETED' if not left else 'STILL THERE'}")
        return 0 if not left else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("channel_handle")
    p.add_argument("--title", required=True)
    sys.exit(asyncio.run(run(p.parse_args())))
