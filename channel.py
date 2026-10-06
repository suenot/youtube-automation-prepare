import re
from camoufox_session import log

STUDIO = "https://studio.youtube.com"


def normalize_handle(handle):
    h = handle.strip().rstrip("/")
    h = h.split("/")[-1]
    if not h.startswith("@"):
        h = "@" + h
    return h


def channel_id_from_url(url):
    m = re.search(r"/channel/(UC[\w-]+)", url or "")
    return m.group(1) if m else None


async def wait_for_channel_context(page, timeout_ms=15_000):
    """Wait for Studio's root URL to redirect to its active channel."""
    interval_ms = 500
    for _ in range(max(1, timeout_ms // interval_ms)):
        active = channel_id_from_url(page.url)
        if active:
            return active
        await page.wait_for_timeout(interval_ms)
    return channel_id_from_url(page.url)


async def resolve_channel_id(page, handle):
    """Resolve @handle -> UC... via the public channel page."""
    h = normalize_handle(handle)
    await page.goto(f"https://www.youtube.com/{h}",
                    wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(2500)
    html = await page.content()
    m = re.search(r'"channelMetadataRenderer"\s*:\s*\{.*?'
                  r'"externalId"\s*:\s*"(UC[\w-]+)"', html, re.S)
    return m.group(1) if m else None


async def _strip_backdrops(page):
    """Remove transient cdk-overlay-backdrop elements that intercept pointer
    events on the 2026 Studio UI, so a real click can land on the target."""
    try:
        await page.evaluate(
            "document.querySelectorAll('.cdk-overlay-backdrop,tp-yt-iron-overlay-backdrop')"
            ".forEach(e => { e.style.pointerEvents='none'; })")
    except Exception:
        pass


async def _real_click(page, locator, timeout=4000):
    """Real (trusted) click with backdrop removal + retry — needed for the
    account card, whose polymer navigation does NOT fire on a synthetic click.

    Uses force=True so an invisible transparent overlay (common on the 2026
    Studio shell) cannot make Playwright's actionability check hang on the
    avatar/menu button; force still dispatches a real trusted pointer event, so
    polymer navigation fires. Falls back to a synthetic dispatch only if the
    trusted click never lands."""
    for _ in range(3):
        await _strip_backdrops(page)
        try:
            await locator.click(timeout=timeout, force=True)
            return True
        except Exception:
            await page.wait_for_timeout(800)
    try:
        await locator.first.evaluate("el => el.click()")
        return True
    except Exception:
        return False


async def _click_switch_card(page, needle):
    """Real Playwright click on the Accounts-panel card containing `needle`.
    A synthetic JS click does NOT fire YouTube's polymer navigation, so we click
    the card element for real. `needle` is a handle like '@marketmaker-cc' (unique
    enough not to also match '@marketmaker-school-ru')."""
    # The Accounts panel can take a few seconds to populate after "Switch
    # account"; wait for the target card to actually render before clicking.
    for sel in (f"ytd-account-item-renderer:has-text(\"{needle}\")",
                f"tp-yt-paper-item:has-text(\"{needle}\")",
                f"a#endpoint:has-text(\"{needle}\")"):
        loc = page.locator(sel)
        try:
            await loc.first.wait_for(state="visible", timeout=8000)
        except Exception:
            continue
        if await _real_click(page, loc.first):
            return True
    try:
        await page.get_by_text(needle).first.click(timeout=4000)
        return True
    except Exception:
        return False


async def select_channel(page, channel_id=None, handle=None):
    """Switch Studio's active channel to a brand channel via the account switcher.
    Deep-linking to /channel/<id> does NOT work for brand channels (permission
    error) — the account context must be switched via the Accounts panel."""
    target = channel_id
    if not target and handle:
        target = await resolve_channel_id(page, handle)
        if not target:
            raise RuntimeError(f"Could not resolve {handle} to a channel id")

    await page.goto(STUDIO, wait_until="domcontentloaded", timeout=60_000)
    active = await wait_for_channel_context(page)
    if target and active == target:
        log(f"  already on target channel: {target}")
        return target

    needle = normalize_handle(handle) if handle else (target or "")
    ok = False
    # The avatar menu and the Accounts panel each render asynchronously; fixed
    # sleeps raced them and the "Switch account" click landed on nothing, so the
    # card never appeared. Wait for each step's own evidence instead, and retry
    # the whole sequence — a single miss used to leave the wrong channel active.
    for attempt in range(3):
        for sel in ("button#avatar-btn", "ytcp-icon-button#avatar-btn",
                    "button[aria-label='Account']"):
            b = page.locator(sel)
            try:
                if await b.count() > 0 and await b.first.is_visible():
                    if await _real_click(page, b.first):
                        break
            except Exception:
                continue
        # Don't gate on the menu item's visibility: "Switch account" resolves to
        # a zero-size text node inside the paper-item, which never reports
        # visible even with the menu fully open. Click it and judge by whether
        # the Accounts panel populates.
        await page.wait_for_timeout(2500)
        await _strip_backdrops(page)
        sa = page.get_by_text("Switch account").first
        if not await _real_click(page, sa, timeout=8000):
            log(f"  'Switch account' not clickable (attempt {attempt + 1})")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1500)
            continue
        cards = page.locator("ytd-account-item-renderer")
        try:
            await cards.first.wait_for(state="visible", timeout=10_000)
        except Exception:
            log(f"  accounts panel did not populate (attempt {attempt + 1})")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1500)
            continue
        ok = await _click_switch_card(page, needle)
        if ok:
            break
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(1500)
    # Switching reloads Studio as the new channel — wait for that.
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    await page.wait_for_timeout(4000)

    active = channel_id_from_url(page.url)
    if target and active != target:
        log(f"  WARNING: wanted {target} but active is {active} (clicked={ok})")
    else:
        log(f"  switched; active channel: {active} (clicked={ok})")
    return active
