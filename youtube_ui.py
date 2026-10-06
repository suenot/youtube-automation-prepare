import time

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


async def fill_contenteditable(page, loc, text):
    try:
        await loc.click(timeout=4000)
        await page.wait_for_timeout(200)
        for combo in ("Meta+A", "Control+A"):
            try:
                await page.keyboard.press(combo)
            except Exception:
                pass
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(150)
        await page.keyboard.type(text, delay=0)
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
