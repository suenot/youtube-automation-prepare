#!/usr/bin/env python3
"""Rewrite the title, description and tags of an already-published video.

Needed when an upload reaches YouTube but its metadata does not — the details
wizard can publish the file while dropping everything typed into it, leaving the
video public under its mp4 filename with an empty description. Re-uploading would
duplicate a video that is already live, so edit the existing one.

    python edit_details.py @marketmaker-zh VIDEO_ID --metadata meta.json
    python edit_details.py @marketmaker-cc --ids-file ids.txt --language English

Only works on published videos: a draft disables the whole edit page with
"Feature unavailable while video is in a draft state" — use finish_draft.py first.
"""
import argparse
import asyncio
import re
import sys

from camoufox_session import make_camoufox, prepare_page, log
from channel import resolve_channel_id, select_channel
from metadata import load_metadata
import youtube_ui as ui

STUDIO_VIDEO = "https://studio.youtube.com/video/{vid}/edit"


async def _try_click(page, loc):
    try:
        await loc.click(timeout=4000)
        return True
    except Exception:
        return await ui.mouse_click(page, loc)


async def _type_into(page, loc, text) -> bool:
    """Focus a contenteditable and type into it, confirming the text landed.

    Typing straight after a click is not enough on the edit page: an autosave
    toast can eat the click, focus stays on the body, and every keystroke goes
    nowhere while the code happily reports success. Check the focus, and check
    the field afterwards.
    """
    for attempt in range(3):
        await ui.dismiss_overlays(page)
        await loc.scroll_into_view_if_needed(timeout=5000)
        if not await _try_click(page, loc):
            continue
        await page.wait_for_timeout(400)
        for combo in ("Meta+A", "Control+A"):
            try:
                await page.keyboard.press(combo)
            except Exception:
                pass
        await page.keyboard.press("Delete")
        # Must be real key events. insert_text drops the characters into the
        # DOM without the input events the Angular component listens for, so
        # the field looks filled, Save posts an empty value, and the change is
        # silently lost -- the title, typed key by key, persists just fine.
        await page.keyboard.type(text, delay=0)
        await page.wait_for_timeout(600)
        # Commit by blurring. The field only pushes its value into the model on
        # blur, so a field still focused when Save is clicked is silently
        # dropped -- which is why the title (blurred by moving to the next
        # field) persisted while the description, typed last, never did.
        await page.evaluate("() => document.activeElement && document.activeElement.blur()")
        await page.wait_for_timeout(800)
        if (await loc.inner_text()).strip():
            return True
        log(f"  type attempt {attempt + 1} landed nothing; retrying")
    return False


async def expand_advanced(page) -> bool:
    """Open the "Show more" section that holds the language controls.

    The toggle sits far below the fold, and a mouse click at an off-screen
    bounding box lands nowhere, so scroll to it first and confirm the label
    flipped to "Show less" instead of assuming the click worked.
    """
    tog = page.locator("ytcp-button#toggle-button")
    if await tog.count() == 0:
        log("  ERROR: no advanced-settings toggle")
        return False
    try:
        await tog.first.scroll_into_view_if_needed(timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(600)
    label = (await tog.first.inner_text()).strip().lower()
    if "less" in label:
        return True
    if not await _try_click(page, tog.first):
        log("  ERROR: advanced-settings toggle is not clickable")
        return False
    await page.wait_for_timeout(2500)
    label = (await tog.first.inner_text()).strip().lower()
    if "less" not in label:
        log(f"  ERROR: advanced settings did not open (toggle reads {label!r})")
        return False
    return True


async def read_video_language(page) -> str:
    """Return the current 'Video language' value, or "" if the field is absent.

    Assumes expand_advanced() has already run.
    """
    sel = page.locator("ytcp-form-select").filter(has_text="Video language")
    if await sel.count() == 0:
        return ""
    text = (await sel.first.inner_text()).strip()
    # "Video language\n<value>" — the label and the value share one node.
    return text.split("\n")[-1].strip()


async def set_video_language(page, language: str) -> bool:
    """Set the 'Video language' dropdown to `language` (its English name).

    The menu renders all ~240 options at once, alphabetically, with no filter
    box. It is also how a stray "a" keystroke lands on Akkadian, the first A
    entry — so the value is read back and confirmed rather than assumed.
    """
    if not await expand_advanced(page):
        return False
    current = await read_video_language(page)
    if not current:
        log("  ERROR: no 'Video language' control")
        return False
    if current == language:
        log(f"  video language already {language}")
        return True

    sel = page.locator("ytcp-form-select").filter(has_text="Video language")
    trig = sel.first.locator("ytcp-dropdown-trigger").first
    try:
        await trig.scroll_into_view_if_needed(timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(600)
    await ui.mouse_click(page, trig)
    await page.wait_for_timeout(2500)

    # Match on the trimmed innerText. Playwright's has_text regex runs against
    # textContent, which carries the template's whitespace, so an anchored
    # pattern never matches a menu entry.
    opt = page.locator("tp-yt-paper-item")
    labels = await opt.evaluate_all(
        "els => els.map(e => (e.innerText || '').trim())")
    if language not in labels:
        near = [t for t in labels if t.startswith(language.split(" ")[0])]
        log(f"  ERROR: '{language}' not offered in the language list "
            f"({len(labels)} options; near matches: {near[:6]})")
        await page.keyboard.press("Escape")
        return False
    target = opt.nth(labels.index(language))
    try:
        await target.scroll_into_view_if_needed(timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(400)
    await ui.mouse_click(page, target)
    await page.wait_for_timeout(2000)

    shown = await read_video_language(page)
    if shown != language:
        log(f"  ERROR: language reads {shown!r} after selecting {language!r}")
        return False
    log(f"  video language: {current!r} -> {language!r}")
    return True


async def edit_one(page, vid: str, meta, language: str = "") -> bool:
    log(f"  opening {vid}")
    if not await ui.open_video_via_ui(page, vid, log):
        return False
    await page.wait_for_timeout(5000)
    await ui.dismiss_overlays(page)
    log("  page loaded")

    text = (await ui.all_text(page)).lower()
    if "draft state" in text:
        log(f"  {vid} is still a draft; run finish_draft.py first")
        return False

    tb = await ui.first_present(page, ["#title-textarea #textbox"], 20000)
    if tb is None:
        log(f"  {vid} no title field")
        return False
    if meta["title"]:
        if not await ui.fill_contenteditable(page, tb, meta["title"]):
            log("  ERROR: title field rejected input")
            return False
        # Leaving focus in the title is how a later Meta+A reached the language
        # dropdown and selected Akkadian; blur before touching anything else.
        await page.evaluate("() => document.activeElement && document.activeElement.blur()")
    db = await ui.first_present(page, ["#description-textarea #textbox"], 20000)
    if db is None and meta["description"]:
        log(f"  {vid} no description field")
        return False
    if db is not None and meta["description"]:
        if not await _type_into(page, db, meta["description"]):
            log("  ERROR: description field is still empty after typing")
            return False

    if meta["tags"]:
        await expand_advanced(page)
        await page.wait_for_timeout(1000)
        ti = await ui.first_present(page, ["input[aria-label='Tags']",
                                           "#tags-container input#text-input"], 5000)
        if ti is not None:
            if not await _try_click(page, ti):
                log("  ERROR: tags field is not clickable")
                return False
            # Replace existing chips instead of appending duplicates. When the
            # input is empty, Backspace removes the preceding chip in Studio.
            for _ in range(60):
                await page.keyboard.press("Backspace")
            # The tags field is a chip input with an autocomplete dropdown: it
            # rewrites its own value between keystrokes, so a zero delay races
            # it and two tags fuse into one ("terminal tooai agents explained").
            # Type one tag at a time and let the chip commit before the next.
            for tag in meta["tags"]:
                await page.keyboard.type(tag + ",", delay=6)
                await page.wait_for_timeout(120)
        else:
            log("  tags field not found; leaving tags unchanged")

    if language:
        log("  setting video language")
        if not await set_video_language(page, language):
            return False

    # Read the form back before saving: typing into a contenteditable can land
    # in the wrong node, and this is the failure this script exists to repair.
    # The screenshot is for human review; the assertions below are what
    # actually stops a bad save.
    await page.screenshot(path=f"debug/edit_{vid}_before_save.png")
    log(f"  review screenshot: debug/edit_{vid}_before_save.png")
    got = (await tb.inner_text()).strip()
    if meta["title"] and got != meta["title"].strip():
        log(f"  ERROR: title did not take (field reads {got[:60]!r})")
        return False

    saved = False
    for attempt in range(3):
        log(f"  save attempt {attempt + 1}")
        btn = page.locator(
            "button[aria-label='Save'], ytcp-button[aria-label='Save'], "
            "ytcp-button#save, #save-button, #submit-button"
        )
        if await btn.count() == 0:
            await page.wait_for_timeout(1000)
            continue
        await _try_click(page, btn.first)
        await page.wait_for_timeout(5000)
        # Reload and read the field back. The "Changes saved" toast is
        # short-lived and its wording varies, so it is not evidence.
        await page.reload(wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(6000)
        again = await ui.first_present(page, ["#title-textarea #textbox"], 20000)
        desc_el = await ui.first_present(page, ["#description-textarea #textbox"], 10000)
        title_ok = (not meta["title"]
                    or (again is not None
                        and (await again.inner_text()).strip() == meta["title"].strip()))
        # A saved title says nothing about the description: they persist
        # independently, and a run that only lands the title is not done.
        desc_ok = (not meta["description"]
                   or (desc_el is not None
                       and (await desc_el.inner_text()).strip()
                       == meta["description"].strip()))
        # Same for the language: it lives in its own request and can be the only
        # thing this run changed.
        lang_ok = True
        if language:
            lang_ok = (await expand_advanced(page)
                       and await read_video_language(page) == language)
        if title_ok and desc_ok and lang_ok:
            saved = True
            break
        log(f"  title_ok={title_ok} desc_ok={bool(desc_ok)} lang_ok={lang_ok}")
        log(f"  save attempt {attempt + 1} did not stick; retrying")
        tb = again
        if tb is not None and meta["title"]:
            await ui.fill_contenteditable(page, tb, meta["title"])
            await page.evaluate("() => document.activeElement && document.activeElement.blur()")
            db = await ui.first_present(page, ["#description-textarea #textbox"], 5000)
            if db is not None and meta["description"]:
                await ui.fill_contenteditable(page, db, meta["description"])
        if language and not lang_ok:
            await set_video_language(page, language)
    log(f"{vid} {'SAVED' if saved else 'NOT SAVED'}")
    return saved


async def main(args) -> int:
    meta = load_metadata(args.metadata, args.title, args.description, args.tags)
    has_meta = bool(meta["title"] or meta["description"] or meta["tags"])
    if not (has_meta or args.language):
        log("nothing to change: pass --metadata/--title/--description/--tags "
            "or --language")
        return 2

    ids = list(args.video_ids)
    if args.ids_file:
        with open(args.ids_file, encoding="utf-8") as f:
            ids += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not ids:
        log("no video ids given")
        return 2
    # Title/description/tags describe one specific video; only the language is
    # safe to apply across a batch.
    if has_meta and len(ids) > 1:
        log("metadata edits take exactly one video id")
        return 2

    failed = []
    async with make_camoufox(args.headless) as ctx:
        page = await prepare_page(ctx)
        active = await select_channel(page, handle=args.channel_handle)
        wanted = await resolve_channel_id(page, args.channel_handle)
        # Switching is flaky on this account: the Accounts panel often refuses to
        # open, and Studio then answers every other channel's video with "Oops,
        # something went wrong". Editing on the wrong channel accomplishes
        # nothing, so stop rather than grind through the whole list.
        if wanted and active != wanted:
            log(f"ABORT: on channel {active}, wanted {wanted} "
                f"({args.channel_handle}) — retry the switch later")
            return 3
        for vid in ids:
            # Studio sometimes stops servicing input events, and the mouse
            # clicks these edits rely on have no timeout of their own — one
            # such video used to hold the whole batch for as long as it was
            # left running.
            try:
                ok = await asyncio.wait_for(
                    edit_one(page, vid, meta, args.language),
                    timeout=args.per_video_timeout)
            except asyncio.TimeoutError:
                log(f"  {vid}: gave up after {args.per_video_timeout}s")
                ok = False
            if not ok:
                failed.append(vid)
    log(f"RESULT: {len(ids) - len(failed)}/{len(ids)} ok on {args.channel_handle}")
    for vid in failed:
        log(f"  FAILED {vid}")
    return 0 if not failed else 1


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("channel_handle")
    p.add_argument("video_ids", nargs="*")
    p.add_argument("--ids-file", default="",
                   help="file of video ids, one per line (language edits only)")
    p.add_argument("--metadata", default="")
    p.add_argument("--title", default="")
    p.add_argument("--description", default="")
    p.add_argument("--tags", default="")
    p.add_argument("--language", default="",
                   help="Video language, exactly as Studio lists it "
                        "(e.g. 'Chinese (Simplified)', 'Russian', 'English')")
    p.add_argument("--per-video-timeout", type=int, default=240,
                   help="seconds before abandoning one video and "
                        "moving to the next")
    p.add_argument("--headless", action="store_true")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
