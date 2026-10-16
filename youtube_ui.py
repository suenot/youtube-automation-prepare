import time

import human

JS_FIND_TEXT = r"""needle => {
  // Finds the smallest visible element whose text is exactly the needle,
  // across open shadow roots, and returns it. It only reads: Studio's controls
  // used to be driven by dispatching MouseEvents at whatever this found, and a
  // dispatched event carries isTrusted: false, which is a page's clearest
  // possible sign that nobody clicked anything. The caller clicks the returned
  // element with the real mouse instead.
  const seen = new Set(), items = [];
  const want = needle.toLowerCase();
  function walk(root) {
    let els; try { els = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of els) {
      if (seen.has(el)) continue; seen.add(el);
      const box = el.getBoundingClientRect();
      const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
      if (box.width && box.height && text.toLowerCase() === want)
        items.push({el, area: box.width * box.height});
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  }
  walk(document);
  items.sort((a, b) => a.area - b.area);
  return items.length ? items[0].el : null;
}"""

JS_SCROLLABLE = r"""() => {
  // The tallest scrollable container on screen: a Studio dropdown mounts its
  // options lazily, so the wanted one may not exist until the list scrolls.
  const seen = new Set(), lists = [];
  function walk(root) {
    let els; try { els = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of els) {
      if (seen.has(el)) continue; seen.add(el);
      const box = el.getBoundingClientRect();
      if (el.scrollHeight > el.clientHeight + 40 && el.clientHeight > 200
          && box.width && box.height) lists.push({el, area: box.width * box.height});
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  }
  walk(document);
  lists.sort((a, b) => b.area - a.area);
  return lists.length ? lists[0].el : null;
}"""

JS_ALL_TEXT = r"""
() => {
  const acc=[]; const seen=new Set();
  function walk(root){
    if(!root||seen.has(root))return; seen.add(root);
    for(const n of (root.childNodes||[])){
      if(n.nodeType===3){const t=(n.textContent||'').trim(); if(t)acc.push(t);}
      else if(n.nodeType===1){ if(n.shadowRoot) walk(n.shadowRoot); walk(n); }
    }
  }
  walk(document.body); return acc.join(' ').toLowerCase();
}
"""

JS_DEEP_DUMP = r"""
() => {
  const out=[]; const seen=new Set();
  function walk(root){
    let els; try{els=root.querySelectorAll('*')}catch(e){return}
    for(const el of els){
      if(seen.has(el))continue; seen.add(el);
      const tag=el.tagName.toLowerCase();
      const aria=el.getAttribute&&el.getAttribute('aria-label');
      const id=el.id||''; const nm=el.getAttribute&&el.getAttribute('name');
      const role=el.getAttribute&&el.getAttribute('role');
      const ok=['button','input','textarea','a','tp-yt-paper-item',
        'tp-yt-paper-radio-button','ytcp-button'].includes(tag)||aria||role==='radio';
      if(ok){const r=el.getBoundingClientRect?el.getBoundingClientRect():{width:0,height:0,x:0,y:0};
        if(r.width>0&&r.height>0)
          out.push({tag,id,name:nm||'',role:role||'',aria:(aria||'').slice(0,50),
            text:(el.innerText||el.textContent||'').trim().slice(0,40),
            x:Math.round(r.x),y:Math.round(r.y)});}
      if(el.shadowRoot)walk(el.shadowRoot);
    }
  }
  walk(document); return out;
}
"""


async def all_text(page):
    return await page.evaluate(JS_ALL_TEXT)


async def verify_gate_present(page):
    t = await all_text(page)
    return ("verify it's you" in t or "verify it’s you" in t
            or "confirm it's really you" in t or "confirm it’s really you" in t)


async def click_text(page, words, timeout_ms=4000):
    sel = ("button, [role=button], [role=menuitem], [role=radio], "
           "tp-yt-paper-item, ytcp-button, a")
    try:
        await page.wait_for_selector(sel, timeout=timeout_ms)
    except Exception:
        pass
    loc = page.locator(sel)
    n = await loc.count()
    for i in range(n):
        el = loc.nth(i)
        try:
            if not await el.is_visible():
                continue
            hay = ((await el.get_attribute("aria-label") or "") + " "
                   + (await el.inner_text() or "")).lower()
            if any(w.lower() in hay for w in words):
                # force=True: Studio's transparent overlay (cdk/iron backdrop)
                # otherwise eats the pointer at the button's center, so the click
                # returns "success" but the handler never fires (the dialog stays
                # open). force still dispatches a real trusted event at center.
                await el.click(timeout=3000, force=True)
                return True
        except Exception:
            continue
    return False


async def find_in_shadow(page, needle):
    """The smallest visible element whose text is exactly `needle`, or None."""
    handle = await page.evaluate_handle(JS_FIND_TEXT, needle)
    element = handle.as_element()
    if element is None:
        await handle.dispose()
    return element


async def click_text_in_shadow(page, needle, timeout_ms=4000):
    """Click a Studio control by its exact text, with the real mouse.

    Studio's Polymer controls live behind shadow roots, which is why the
    element is found in page script. The click itself is not: a dispatched
    MouseEvent reaches the handler carrying isTrusted: false.
    """
    deadline = time.time() + timeout_ms / 1000
    while True:
        element = await find_in_shadow(page, needle)
        if element is not None:
            try:
                await human.click(page, element)
                return True
            except Exception:
                pass
            finally:
                await element.dispose()
        if time.time() >= deadline:
            return False
        await page.wait_for_timeout(300)


async def wheel_scroll_list(page, delta=420):
    """Scroll the tallest scrollable container with the wheel, over its centre.

    Setting scrollTop from page script scrolls without anyone touching the
    wheel; this leaves the wheel events a person leaves.
    """
    handle = await page.evaluate_handle(JS_SCROLLABLE)
    element = handle.as_element()
    if element is None:
        await handle.dispose()
        return False
    try:
        box = await element.bounding_box()
        if not box:
            return False
        await human.move_to(page, box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2)
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(350)
        return True
    finally:
        await element.dispose()


async def fill_contenteditable(page, loc, text):
    try:
        await human.click(page, loc)
        await page.wait_for_timeout(200)
        for combo in ("Meta+A", "Control+A"):
            try:
                await page.keyboard.press(combo)
            except Exception:
                pass
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(150)
        # type(delay=0) is a metronome with no gap at all between keystrokes.
        await human.type_segments(page, text)
        await page.wait_for_timeout(300)
        return True
    except Exception:
        return False


async def first_present(page, selectors, timeout_ms=15000):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for sel in selectors:
            loc = page.locator(sel)
            try:
                if await loc.count() > 0:
                    return loc.first
            except Exception:
                continue
        await page.wait_for_timeout(500)
    return None


async def first_visible(page, selectors, timeout_ms=15000):
    """Return the first visible match, not a hidden duplicate behind a modal."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for sel in selectors:
            loc = page.locator(sel)
            try:
                for i in range(await loc.count()):
                    candidate = loc.nth(i)
                    if await candidate.is_visible():
                        return candidate
            except Exception:
                continue
        await page.wait_for_timeout(500)
    return None


async def mouse_click(page, loc):
    """Click via the mouse at the element's centre.

    Locator.click() reports the polymer controls in Studio as unactionable and
    times out even when they are visible and unobstructed; a real mouse event at
    the same coordinates goes through. Returns False when the element has no box
    (detached or display:none), so callers can tell "not there" from "clicked".
    """
    try:
        box = await loc.bounding_box()
    except Exception:
        return False
    if not box:
        return False
    await page.mouse.click(box["x"] + box["width"] / 2,
                           box["y"] + box["height"] / 2)
    return True


async def dismiss_overlays(page):
    for txt in ("Got it", "Dismiss", "No thanks", "Skip", "Not now",
                "Continue", "I agree", "Accept all"):
        try:
            b = page.locator(f"ytcp-button:has-text('{txt}'), button:has-text('{txt}')")
            if await b.count() > 0 and await b.first.is_visible():
                await b.first.click(timeout=1500)
                await page.wait_for_timeout(400)
        except Exception:
            continue
