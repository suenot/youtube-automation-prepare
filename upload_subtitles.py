#!/usr/bin/env python3
"""Upload a timed SRT caption track to an existing YouTube Studio video.

This is deliberately a separate publishing step: subtitles embedded in a
render or created by Whisper are not discoverable as a YouTube caption track
until they are uploaded in Studio.
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path

from camoufox_session import make_camoufox, prepare_page, logged_in_youtube, log, shot
from channel import resolve_channel_id, select_channel
import youtube_ui as ui

STUDIO = "https://studio.youtube.com"


def current_manual_upload_surface(text, language):
    low = (text or "").lower()
    return (f"video language: {language.lower()}" in low
            and "upload manual" in low)


async def deep_click_text(page, phrases, timeout_ms=8000):
    """Click a visible Studio control by text, including open shadow roots."""
    wanted = [phrase.lower() for phrase in phrases]
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    script = r"""wanted => {
      const seen = new Set();
      function walk(root) {
        const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
        for (const el of all) {
          if (seen.has(el)) continue; seen.add(el);
          const rect = el.getBoundingClientRect();
          if (!rect.width || !rect.height) { if (el.shadowRoot) walk(el.shadowRoot); continue; }
          const text = ((el.getAttribute('aria-label') || '') + ' ' +
            (el.innerText || el.textContent || '')).replace(/\s+/g, ' ').trim().toLowerCase();
          const clickable = el.matches('button, [role=button], [role=menuitem], [role=radio], tp-yt-paper-item, ytcp-button, a') ||
            el.closest('button, [role=button], [role=menuitem], [role=radio], tp-yt-paper-item, ytcp-button, a');
          if (clickable && wanted.some(word => text.includes(word))) {
            const target = el.closest('button, [role=button], [role=menuitem], [role=radio], tp-yt-paper-item, ytcp-button, a') || el;
            const box = target.getBoundingClientRect();
            return {x: box.x + box.width / 2, y: box.y + box.height / 2};
          }
          if (el.shadowRoot) { const hit = walk(el.shadowRoot); if (hit) return true; }
        }
        return false;
      }
      return walk(document);
    }"""
    while asyncio.get_running_loop().time() < deadline:
        try:
            hit = await page.evaluate(script, wanted)
            if hit:
                if isinstance(hit, dict):
                    await page.mouse.click(hit["x"], hit["y"])
                await page.wait_for_timeout(500)
                return True
        except Exception:
            pass
        await page.wait_for_timeout(350)
    return False


async def file_input(page, timeout_ms=8000):
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        handles = await page.query_selector_all("input[type='file']")
        if handles:
            return handles[-1]
        await page.wait_for_timeout(300)
    return None


async def click_smallest_text(page, phrase, timeout_ms=8000):
    """Click a visible label even when Studio wraps it in a non-button div.

    The label is found in page script because Studio keeps it behind shadow
    roots, and then clicked with the real mouse. This used to dispatch a
    MouseEvent sequence at the element, which arrives with isTrusted: false.
    """
    return await ui.click_text_in_shadow(page, phrase.lower(), timeout_ms)


async def open_video_language_editor(page, language, timeout_ms=8000, debug=False):
    """Open the pencil action on the `English (video language)` row.

    Studio labels this action inconsistently (its visible tooltip can be
    either "Edit" or "Add"), so a generic text click is unsafe: it can click
    the unrelated "Add language" button.  Find the language row first and
    click only a control contained by that row or its immediate table wrapper.
    """
    # The table itself is light-DOM and stable.  Its empty subtitle cell only
    # exposes the pencil after a pointer hover, so target the cell by the
    # table's "Subtitles" column instead of chasing the transient icon.
    cell = await page.evaluate(r"""() => {
      const table = document.querySelector('#ytgn-video-translations-list-table');
      if (!table) return null;
      const tableBox = table.getBoundingClientRect();
      const header = document.querySelector('#captions-header-name');
      const headerBox = header && header.getBoundingClientRect();
      const rows = [...table.querySelectorAll('tr')];
      const row = rows.find(el => /english\s*\(video language\)/i.test((el.innerText || el.textContent || '')));
      const rowBox = row && row.getBoundingClientRect();
      if (!tableBox.width || !tableBox.height) return null;
      return {
        x: headerBox && headerBox.width ? headerBox.x + headerBox.width / 2 : tableBox.x + tableBox.width * 0.46,
        y: rowBox && rowBox.height ? rowBox.y + rowBox.height / 2 : tableBox.y + tableBox.height * 0.46
      };
    }""")
    if cell:
        await page.mouse.move(cell["x"], cell["y"])
        await page.wait_for_timeout(650)
        await shot(page, "captions_00_hovered_dash", debug)
        await page.mouse.click(cell["x"], cell["y"])
        await page.wait_for_timeout(700)
        return True

    wanted = re.escape(language.lower())
    locate_script = rf"""() => {{
      const wanted = /{wanted}\s*\(video language\)/i;
      const seen = new Set(), elements = [];
      function walk(root) {{
        if (!root || seen.has(root)) return;
        seen.add(root);
        for (const el of root.querySelectorAll ? root.querySelectorAll('*') : []) {{
          if (seen.has(el)) continue;
          seen.add(el); elements.push(el);
          if (el.shadowRoot) walk(el.shadowRoot);
        }}
      }}
      function visible(el) {{
        const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0;
      }}
      function hostParent(el) {{
        return el.parentElement || (el.getRootNode && el.getRootNode().host) || null;
      }}
      walk(document);
      const labels = elements.filter(el => visible(el) && wanted.test((el.innerText || el.textContent || '').replace(/\s+/g, ' ')));
      // Containers inherit all descendant text; choose the smallest matching
      // node, not <html> or the entire Studio application shell.
      labels.sort((a, b) => (a.innerText || a.textContent || '').length - (b.innerText || b.textContent || '').length);
      const label = labels[0];
      if (!label) return {{ok:false, reason:'language row missing'}};
      const labelBox = label.getBoundingClientRect();
      const dash = elements.find(el => {{
        const text = (el.innerText || el.textContent || '').trim();
        if (!visible(el) || !/^[–—-]$/.test(text)) return false;
        const box = el.getBoundingClientRect();
        return Math.abs((box.y + box.height / 2) - (labelBox.y + labelBox.height / 2)) < 80 &&
          box.x > labelBox.x + labelBox.width;
      }});
      if (dash && !window.__captionDashHovered) {{
        const box = dash.getBoundingClientRect();
        return {{ok:false, hover:{{x:box.x + box.width / 2, y:box.y + box.height / 2}}, reason:'hover dash'}};
      }}
      // After hover, the row's pencil is mounted as an icon button. Check the
      // exact row first so no global "Add language" control can be selected.
      const rowAction = elements.find(el => {{
        if (!visible(el) || !el.matches('button, [role=button], ytcp-icon-button, ytcp-button')) return false;
        const box = el.getBoundingClientRect();
        if (Math.abs((box.y + box.height / 2) - (labelBox.y + labelBox.height / 2)) >= 80) return false;
        if (box.x <= labelBox.x + labelBox.width) return false;
        const text = ((el.getAttribute('aria-label') || '') + ' ' +
          (el.getAttribute('title') || '') + ' ' + (el.innerText || el.textContent || '')).toLowerCase();
        return /edit|add|subtitle|caption/.test(text) || el.tagName.toLowerCase() === 'ytcp-icon-button';
      }});
      if (rowAction) {{
        rowAction.click();
        return {{ok:true, control: rowAction.getAttribute('aria-label') || rowAction.getAttribute('title') || rowAction.tagName}};
      }}
      // Studio renders the empty caption cell as a literal dash. Its pencil
      // action is inserted only after a real pointer hover over that dash.
      if (dash && !window.__captionDashHovered) {{
        const box = dash.getBoundingClientRect();
        return {{ok:false, hover:{{x:box.x + box.width / 2, y:box.y + box.height / 2}}, reason:'hover dash'}};
      }}
      let row = label;
      for (let depth = 0; row && depth < 8; depth++, row = hostParent(row)) {{
        const controls = [];
        const rowRoot = row.shadowRoot || row;
        for (const el of rowRoot.querySelectorAll ? rowRoot.querySelectorAll('button, [role=button], ytcp-icon-button, ytcp-button') : []) {{
          if (!visible(el)) continue;
          const text = ((el.getAttribute('aria-label') || '') + ' ' +
            (el.getAttribute('title') || '') + ' ' + (el.innerText || el.textContent || '')).toLowerCase();
          if (/edit|add|subtitle|caption/.test(text) || el.tagName.toLowerCase() === 'ytcp-icon-button') controls.push(el);
        }}
        // The pencil control is the only icon button in this row.  Do not
        // climb past a compact table wrapper, where "Add language" appears.
        if (controls.length) {{
          const box = controls[0].getBoundingClientRect();
          return {{action:{{x:box.x + box.width / 2, y:box.y + box.height / 2}},
            control: controls[0].getAttribute('aria-label') || controls[0].getAttribute('title') || controls[0].tagName}};
        }}
        const txt = (row.innerText || row.textContent || '').replace(/\s+/g, ' ').toLowerCase();
        if (txt.includes('add language')) break;
      }}
      return {{ok:false, reason:'row action missing'}};
    }}"""
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    last = ""
    while asyncio.get_running_loop().time() < deadline:
        try:
            result = await page.evaluate(locate_script)
            if result.get("hover"):
                pos = result["hover"]
                await page.mouse.move(pos["x"], pos["y"])
                await page.wait_for_timeout(500)
                await page.evaluate("window.__captionDashHovered = true")
                # Re-run after hover: the pencil is dynamically inserted.
                result = await page.evaluate(locate_script)
            if result.get("action"):
                pos = result["action"]
                await shot(page, "captions_00_before_action", debug)
                await page.mouse.click(pos["x"], pos["y"])
                log(f"  opened caption editor via {result.get('control')} at {pos}")
                await page.wait_for_timeout(500)
                return True
            last = result.get("reason", "not found")
        except Exception as exc:
            last = str(exc)
        await page.wait_for_timeout(350)
    log(f"  caption editor unavailable: {last}")
    return False


async def upload_one(page, args):
    # Studio's current captions surface is `/translations`; `/subtitles` can
    # render a transient "Oops" page for newly uploaded videos.
    await page.goto(f"{STUDIO}/video/{args.video_id}/translations",
                    wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(6000)
    await ui.dismiss_overlays(page)
    log(f"  subtitle route: {page.url}")
    if f"/video/{args.video_id}" not in page.url:
        raise RuntimeError("subtitle page did not open")
    # Unlike the details editor, the current Studio subtitles route does not
    # expose the video title in its light DOM.  The immutable video ID in the
    # route is the reliable identity check here; requiring a missing title
    # would incorrectly reject every valid caption upload.
    # The grid is hydrated after the page shell. Do not mistake the shell's
    # navigation text for the fully rendered captions surface.
    deadline = asyncio.get_running_loop().time() + 25
    text = await ui.all_text(page)
    while ("video language" not in text and "set language" not in text
           and asyncio.get_running_loop().time() < deadline):
        await page.wait_for_timeout(500)
        text = await ui.all_text(page)
    log(f"  subtitle surface: {text[:240]!r}")

    # Add the requested language only when the language row is absent.
    manual_surface = current_manual_upload_surface(text, args.language)
    if (f"{args.language.lower()} (video language)" not in text
            and not manual_surface):
        await shot(page, "captions_00_language_missing", args.debug)
        if not await click_smallest_text(page, "set language"):
            raise RuntimeError("Set language control not found")
        await page.wait_for_timeout(500)
        await shot(page, "captions_00_language_dropdown", args.debug)
        if not await click_smallest_text(page, args.language):
            raise RuntimeError(f"language {args.language!r} not selectable")
        await page.wait_for_timeout(1200)
        if not await click_smallest_text(page, "confirm"):
            raise RuntimeError("language Confirm control not found")
        await page.wait_for_timeout(1600)

    # A channel video language creates a row before any manual caption track.
    # Its hover action is an Edit pencil (as opposed to the separate Add
    # language button). Enter that editor and choose Upload file there.
    text = await ui.all_text(page)
    manual_surface = current_manual_upload_surface(text, args.language)
    if manual_surface:
        if not await deep_click_text(page, ["upload manual"]):
            raise RuntimeError("Upload manual action not found")
        timing = page.locator(
            "tp-yt-paper-radio-button:has-text('With timing'), "
            "[role='radio']:has-text('With timing')"
        ).first
        try:
            await timing.wait_for(state="visible", timeout=5000)
            if await timing.get_attribute("aria-checked") != "true":
                await timing.click(force=True)
        except Exception as exc:
            raise RuntimeError("With timing option not found") from exc
        continue_button = page.get_by_role("button", name="Continue", exact=True).first
        try:
            await continue_button.wait_for(state="visible", timeout=5000)
        except Exception as exc:
            raise RuntimeError("caption Continue control not found") from exc
        if await continue_button.is_disabled():
            raise RuntimeError("caption Continue control is disabled")
        if not await deep_click_text(page, ["continue"]):
            raise RuntimeError("caption Continue control not found")
    else:
        if "automatic captions" in text and "published" in text:
            # Automatic captions are a distinct track and must not suppress the
            # user-provided SRT attached to the video-language row.
            pass
        if not await open_video_language_editor(page, args.language,
                                                debug=args.debug):
            raise RuntimeError("caption Edit control not found")
        await page.wait_for_timeout(700)
        if not await deep_click_text(page, ["upload file"]):
            raise RuntimeError("Upload file action not found")
        await page.wait_for_timeout(600)
        if not await deep_click_text(page, ["with timing"]):
            raise RuntimeError("With timing option not found")
    handle = await file_input(page)
    if handle is None:
        raise RuntimeError("caption file input not found")
    await handle.set_input_files(str(args.srt))
    await page.wait_for_timeout(2500)
    await shot(page, "captions_01_uploaded", args.debug)
    if not await deep_click_text(page, ["publish"]):
        raise RuntimeError("caption Publish control not found")
    await page.wait_for_timeout(1800)
    final_text = await ui.all_text(page)
    if "published" not in final_text:
        raise RuntimeError("Studio did not confirm the caption track as published")
    log(f"CAPTIONS_PUBLISHED {args.video_id}: {args.srt.name}")


async def run(args):
    async with make_camoufox(False) as context:
        page = await prepare_page(context)
        # Studio periodically keeps its global shell loading long after the
        # cookie-bearing navigation has committed. We only need the committed
        # origin to validate the session and select the channel below.
        await page.goto(STUDIO, wait_until="commit", timeout=30_000)
        await page.wait_for_timeout(2500)
        if not await logged_in_youtube(page):
            raise RuntimeError("not logged into YouTube")
        wanted = await resolve_channel_id(page, args.channel_handle)
        if not wanted:
            raise RuntimeError(f"could not resolve channel {args.channel_handle}")
        active = await select_channel(page, channel_id=wanted, handle=args.channel_handle)
        if active != wanted:
            raise RuntimeError(f"wrong active channel: wanted {wanted}, got {active}")
        await upload_one(page, args)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel-handle", required=True)
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--language", default="English")
    parser.add_argument("--title", default="")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    if not args.srt.is_file():
        parser.error(f"caption file does not exist: {args.srt}")
    if args.srt.suffix.lower() != ".srt":
        parser.error("only .srt tracks are supported")
    return args


def main(argv=None):
    try:
        asyncio.run(run(parse_args(argv)))
    except RuntimeError as exc:
        log(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
