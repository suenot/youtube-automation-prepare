import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

from camoufox_session import (PROFILE_DIR, make_camoufox, prepare_page,
                             logged_in_youtube, log, shot)
from publication_state import RequestJournal, request_fingerprint
from metadata import load_metadata
from precheck import video_duration, check
from channel import (select_channel, resolve_channel_id,
                     channel_id_from_url, wait_for_channel_context,
                     _strip_backdrops)
import youtube_ui as ui

STUDIO = "https://studio.youtube.com"


async def _goto(page, url, tries=3):
    last = None
    for _ in range(tries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            return
        except Exception as e:
            last = e
            await page.wait_for_timeout(1500)
    if last:
        raise last


# Studio renders its content rows inside shadow roots, so document-level
# queries see nothing; walk the roots and collect each row's link and text.
CONTENT_ROWS_JS = r"""
() => {
  const out = []; const seen = new Set();
  function walk(root) {
    let els; try { els = root.querySelectorAll('*') } catch (e) { return }
    for (const el of els) {
      if (seen.has(el)) continue; seen.add(el);
      const href = el.getAttribute && el.getAttribute('href');
      if (href) {
        const m = href.match(/\/video\/([\w-]{11})\//);
        if (m) {
          const row = el.closest ? el.closest('ytcp-video-row') : null;
          out.push({id: m[1], text: ((row || el).innerText || '')});
        }
      }
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  }
  walk(document);
  return out;
}
"""


def _norm(s):
    """PURE: reduce text to comparable letters and digits."""
    return "".join(c for c in s.lower() if c.isalnum())


async def find_by_title(page, channel_id, title):
    """Return the id of a video already on the channel with this title, or "".

    A publish run can report failure after the video is already live — the
    wizard's later steps are what fail, not the upload — so a blind retry
    publishes a duplicate. Studio's own content list is the authority here: it
    shows drafts and brand-new uploads immediately, which the public channel
    page does not.
    """
    needle = _norm(title)
    if not needle:
        return ""
    for tab in ("short", "upload"):
        try:
            await page.goto(f"{STUDIO}/channel/{channel_id}/videos/{tab}",
                            wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(9000)
            await page.locator("ytcp-video-row, ytcp-video-section-content").first.wait_for(
                state="visible", timeout=30_000)
            if channel_id_from_url(page.url) != channel_id:
                raise RuntimeError("Studio changed channel during duplicate check")
            rows = await page.evaluate(CONTENT_ROWS_JS)
            if not rows:
                text = await ui.all_text(page)
                if not any(marker in text for marker in (
                        "no content available", "no videos found", "upload your first video")):
                    raise RuntimeError("No loaded video rows or confirmed empty content list")
        except Exception as e:
            raise RuntimeError(f"Duplicate check on /{tab} failed; refusing upload") from e
        for row in rows:
            if needle in _norm(row["text"]):
                return row["id"]
    return ""


OPEN_UPLOAD_LIMIT_S = 480
UPLOAD_LAUNCHERS = (
    ("ytcp-icon-button#upload-icon", True),
    ("button[aria-label='Upload videos']", True),
    ("button[aria-label='Create']", False),
)

# YouTube shows a transient notice when the channel hit its daily upload quota
# (common on new/low-subscriber channels after a burst of uploads). Retrying is
# futile — the quota resets ~24h later — so detect it fast, screenshot it, and
# signal the caller to stop the whole upload phase for the day.
UPLOAD_LIMIT_PHRASES = (
    "daily upload limit", "upload limit", "daily limit", "limit reached",
    "reached the limit", "reached your", "uploads are limited", "can't upload",
    "cannot upload", "try again in 24", "try again later", "upload more videos",
    "exceeded the", "exceeded your", "maximum number of uploads", "upload quota",
    "not eligible to upload", "verify your account", "verify your channel",
)


def _limit_hit(txt):
    """Return the matched limit-phrase (lowercased) if `txt` looks like an
    upload-limit/quota notice, else None."""
    low = (txt or "").lower()
    for p in UPLOAD_LIMIT_PHRASES:
        if p in low:
            return p
    return None


class UploadLimitReached(Exception):
    """Raised when YouTube reports the daily upload limit / a verify gate.
    Signals the orchestrator to STOP the upload phase (no retry)."""


async def _wait_for_upload_launcher(page, timeout_ms=20_000):
    """Return Studio's first visible upload launcher once its shell mounts."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for selector, opens_dialog in UPLOAD_LAUNCHERS:
            locator = page.locator(selector)
            try:
                if await locator.count() > 0 and await locator.first.is_visible():
                    return locator.first, opens_dialog
            except Exception:
                continue
        await page.wait_for_timeout(500)
    return None, False


def _upload_route(channel_id):
    base = f"{STUDIO}/channel/{channel_id}" if channel_id else STUDIO
    return f"{base}/videos/upload?d=ud"


async def open_upload(page, video, debug):
    """Open the upload dialog and hand it the file, under a hard time limit.

    page.mouse.click() has no timeout of its own, and Studio occasionally stops
    servicing input events entirely — the run then sits there silently until
    something outside kills it. Bound the whole step so a stuck dialog costs one
    upload instead of the queue behind it.
    """
    try:
        return await asyncio.wait_for(_open_upload(page, video, debug),
                                      timeout=OPEN_UPLOAD_LIMIT_S)
    except asyncio.TimeoutError:
        log(f"  ERROR: upload dialog did not come up within {OPEN_UPLOAD_LIMIT_S}s")
        try:
            await shot(page, "yt_xx_upload_timeout", debug)
            log(f"  current url: {page.url}")
            at = await ui.all_text(page)
            snippet = (at or "")[:400].replace("\n", " ")
            log(f"  page text snippet: {snippet}")
        except Exception as e:
            log(f"  (timeout screenshot failed: {e})")
        return False


async def _open_upload(page, video, debug):
    # The duplicate check leaves Studio on the target channel's Content page.
    # Returning through the root dashboard can hang before its shell mounts, so
    # ask Studio to open a fresh upload dialog on that channel directly. Keep
    # this to one bounded navigation: a missing input is evidence to stop, not
    # a reason to keep sending clicks to an unknown page.
    active = channel_id_from_url(page.url)
    route = _upload_route(active)
    log(f"  opening upload route: {route}")
    try:
        await _goto(page, route, tries=1)
        await wait_for_channel_context(page, timeout_ms=20_000)
    except Exception as e:
        log(f"  upload route did not load: {e}")

    if not active or channel_id_from_url(page.url) != active:
        log("ERROR: Studio channel changed before attaching the file")
        return False

    file_selectors = ["ytcp-uploads-dialog input[type='file']", "input[type='file']"]
    fi = await ui.first_present(page, file_selectors, 20_000)
    if fi is None:
        await ui.dismiss_overlays(page)
        await _strip_backdrops(page)
        launcher, opens_dialog = await _wait_for_upload_launcher(page)
        clicked = launcher is not None and await ui.mouse_click(page, launcher)
        if clicked and not opens_dialog:
            await page.wait_for_timeout(1200)
            await _strip_backdrops(page)
            await ui.click_text(page, ["Upload video", "Upload videos"], 5000)
        if clicked:
            fi = await ui.first_present(page, file_selectors, 15_000)

    await shot(page, "yt_02_upload_dialog", debug)
    if fi is None:
        # Studio's current upload dialog can render the file input only after
        # its visible "Select files" control is activated.  The modal is
        # already open in this state (the screenshot above is the evidence),
        # so click that control once before treating the upload as unavailable.
        selected = await ui.click_text(page, ["Select files", "Select file"], 5000)
        if selected:
            fi = await ui.first_present(page, file_selectors, 15_000)
    if fi is None:
        log("  no file input found")
        return False
    await fi.set_input_files(str(video))
    log(f"  selected: {video.name}")
    await page.wait_for_timeout(4000)
    # Big uploads keep the details dialog dimmed ("Creating link...") until the
    # video entity exists; editing before that silently fails. Wait until that
    # text clears AND the audience radio is actually clickable (the dialog is
    # only interactive then). YouTube can throttle this for minutes after many
    # uploads, so wait up to ~8 min.
    ready = False
    for _ in range(160):
        try:
            txt = (await ui.all_text(page))
        except Exception as e:
            # The dialog/page closed on us mid-upload — almost always means
            # YouTube rejected the upload (daily limit, quota, verify gate).
            # Treat a closed target as a hard reject, not a throttle wait.
            if "TargetClosedError" in type(e).__name__ or "closed" in str(e).lower():
                await shot(page, "yt_xx_dialog_closed", debug)
                log("  upload dialog closed mid-upload (rejected?)")
                raise UploadLimitReached("upload dialog closed mid-upload")
            txt = ""
        # Daily upload limit / quota notice — fail fast, screenshot the proof.
        hit = _limit_hit(txt)
        if hit:
            await shot(page, "yt_xx_upload_limit", debug)
            snippet = " ".join(txt.split())[:300]
            log(f"  UPLOAD LIMIT NOTICE ('{hit}'): {snippet}")
            raise UploadLimitReached(f"upload limit notice: '{hit}'")
        creating = "creating link" in txt
        r = page.locator("tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']")
        try:
            enabled = await r.count() > 0 and await r.first.is_enabled()
        except Exception as e:
            if "TargetClosedError" in type(e).__name__ or "closed" in str(e).lower():
                await shot(page, "yt_xx_dialog_closed", debug)
                log("  upload dialog closed mid-upload (rejected?)")
                raise UploadLimitReached("upload dialog closed mid-upload")
            enabled = False
        if not creating and enabled:
            ready = True
            log("  details dialog is interactive")
            break
        await page.wait_for_timeout(3000)
    if not ready:
        log("  WARNING: dialog still not interactive (YouTube throttling link "
            "creation?) — details may not stick")
    return True


def _same_field_text(actual, expected):
    return actual.replace("\r\n", "\n").strip() == expected.replace("\r\n", "\n").strip()


async def _fill_upload_contenteditable(page, loc, value, field_name="field"):
    """Fill a Studio upload field only after it owns keyboard focus.

    A forced click can look successful while focus remains on the dialog body.
    In that state the title stays as the filename and the description stays
    empty. Focus explicitly, verify the exact field, and retry with real key
    events if direct text insertion did not land.
    """
    for method in ("insert_text", "fill", "type", "exec_command"):
        try:
            await loc.scroll_into_view_if_needed(timeout=5000)
            if not await _try_click(page, loc):
                continue
            await loc.focus(timeout=5000)
            if method == "exec_command":
                # Studio's Polymer description editor can ignore Playwright's
                # fill/type APIs even though it is a normal contenteditable.
                # execCommand produces the input event Polymer listens for and
                # therefore updates the persisted model, not just textContent.
                result = await loc.evaluate(
                    """(el, replacement) => {
                      el.focus();
                      const selection = window.getSelection();
                      const range = document.createRange();
                      range.selectNodeContents(el);
                      selection.removeAllRanges();
                      selection.addRange(range);
                      return document.execCommand(
                        'insertText', false, replacement);
                    }""",
                    value,
                )
                if not result:
                    continue
            else:
                await page.keyboard.press("Meta+A")
                await page.keyboard.press("Delete")
                if method == "insert_text":
                    await page.keyboard.insert_text(value)
                elif method == "fill":
                    await loc.fill(value)
                else:
                    await page.keyboard.type(value, delay=0)
            await page.wait_for_timeout(600)
            actual = await loc.inner_text()
            if not _same_field_text(actual, value):
                log(f"  {field_name} {method} did not land "
                    f"(field reads {actual.strip()[:80]!r})")
                continue
            await page.evaluate("() => document.activeElement && document.activeElement.blur()")
            await page.wait_for_timeout(800)
            if _same_field_text(await loc.inner_text(), value):
                return True
        except Exception as e:
            log(f"  {field_name} {method} failed: "
                f"{type(e).__name__}: {str(e).splitlines()[0][:120]}")
            continue
    return False


async def fill_details(page, meta, thumbnail, made_for_kids, debug):
    tb = await ui.first_visible(
        page,
        ["ytcp-uploads-dialog #title-textarea #textbox",
         "#title-textarea #textbox"],
        30000)
    if tb is not None and meta["title"]:
        if not await _fill_upload_contenteditable(page, tb, meta["title"], "title"):
            log("  ERROR: title field rejected input")
            return False
        log("  title set and verified")
    elif meta["title"]:
        log("  ERROR: title field not found")
        return False
    db = await ui.first_visible(
        page,
        ["ytcp-uploads-dialog #description-textarea #textbox",
         "#description-textarea #textbox"],
        5000)
    if db is not None and meta["description"]:
        if not await _fill_upload_contenteditable(page, db, meta["description"],
                                                  "description"):
            log("  ERROR: description field rejected input")
            return False
        log("  description set and verified")
    elif meta["description"]:
        log("  ERROR: description field not found")
        return False
    if meta["title"] and not _same_field_text(await tb.inner_text(), meta["title"]):
        log("  ERROR: title changed while filling description")
        return False
    if (meta["description"]
            and not _same_field_text(await db.inner_text(), meta["description"])):
        log("  ERROR: description changed before validation")
        return False
    if thumbnail and Path(thumbnail).is_file():
        th = page.locator("ytcp-thumbnails-compact-editor-uploader input[type='file'], "
                          "#file-loader input[type='file']")
        try:
            if await th.count() > 0:
                await th.first.set_input_files(str(thumbnail))
                log("  thumbnail set")
                await page.wait_for_timeout(2000)
            else:
                log("  thumbnail input not ready; skipping")
        except Exception:
            log("  thumbnail failed; skipping")
    if meta["tags"]:
        await ui.click_text(page, ["Show more"], 4000)
        await page.wait_for_timeout(800)
        ti = await ui.first_present(page, ["input[aria-label='Tags']",
                                           "#tags-container input#text-input"], 4000)
        if ti is not None:
            await ti.click(timeout=3000)
            await page.keyboard.type(", ".join(meta["tags"]) + ",", delay=2)
            log("  tags set")
    ok = await set_audience(page, made_for_kids)
    if ok:
        ok = await validate_details_before_next(page, debug)
    await shot(page, "yt_05_details", debug)
    return ok


async def set_audience(page, made_for_kids):
    """Answer the required "made for kids" question.

    Leaving it unanswered is what silently blocks Next: the wizard sits on the
    details step forever and the upload ends its life as a draft. Studio renders
    the radio twice (once in a collapsed section), and the first copy is not
    always the live one, so try every match and trust only aria-checked.
    """
    name = ("VIDEO_MADE_FOR_KIDS_MFK" if made_for_kids
            else "VIDEO_MADE_FOR_KIDS_NOT_MFK")
    r = page.locator(f"tp-yt-paper-radio-button[name='{name}']")
    for attempt in range(3):
        n = await r.count()
        for i in range(n):
            el = r.nth(i)
            if (await el.get_attribute("aria-checked")) == "true":
                log("  audience set")
                return True
            await _try_click(page, el)
            await page.wait_for_timeout(500)
            if (await el.get_attribute("aria-checked")) == "true":
                log("  audience set")
                return True
        await page.wait_for_timeout(1000)
    log("  ERROR: 'made for kids' left unanswered; the wizard will not advance")
    return False


VISIBILITY_RADIO = "tp-yt-paper-radio-button[name='{}']"


# Studio surfaces validation in several ways depending on the current Polymer
# component: aria-invalid, an `invalid` property/attribute, an error class, or a
# red field/container. Walk open shadow roots so upload-dialog fields are not
# missed by document-level selectors.
DETAILS_VALIDATION_JS = r"""
() => {
  const out = []; const seen = new Set(); const emitted = new Set();
  const controls = [
    'input', 'textarea', 'select', '[contenteditable="true"]',
    'ytcp-dropdown-trigger', 'ytcp-text-dropdown-trigger',
    'ytcp-form-input-container', 'tp-yt-paper-input'
  ].join(',');

  function isRed(value) {
    const m = String(value || '').match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/i);
    if (!m) return false;
    const r = Number(m[1]), g = Number(m[2]), b = Number(m[3]);
    return r >= 170 && r >= g * 1.35 && r >= b * 1.2;
  }

  function visible(el) {
    try {
      const r = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return r.width > 0 && r.height > 0
        && r.bottom > 0 && r.right > 0
        && r.top < window.innerHeight && r.left < window.innerWidth
        && style.display !== 'none' && style.visibility !== 'hidden';
    } catch (_) { return false; }
  }

  function add(el, reason) {
    const aria = el.getAttribute && el.getAttribute('aria-label');
    const name = el.getAttribute && el.getAttribute('name');
    const id = el.id || '';
    let field = aria || name || id || el.tagName.toLowerCase();
    let text = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
    for (let p = el.parentElement, depth = 0; p && depth < 4; p = p.parentElement, depth++) {
      const candidate = (p.innerText || p.textContent || '').trim().replace(/\s+/g, ' ');
      if (candidate && candidate.length <= 180) text = candidate;
      const label = p.getAttribute && p.getAttribute('aria-label');
      if (label) field = label;
    }
    const key = `${field}|${reason}|${text.slice(0, 180)}`;
    if (!emitted.has(key)) {
      emitted.add(key);
      out.push({field: field.slice(0, 100), reason, text: text.slice(0, 180)});
    }
  }

  function inspect(el) {
    if (!visible(el)) return;
    const cls = String(el.className || '').toLowerCase();
    const attrInvalid = (el.getAttribute && el.getAttribute('aria-invalid') === 'true')
      || (el.hasAttribute && el.hasAttribute('invalid'))
      || (el.hasAttribute && el.hasAttribute('has-error'))
      || el.invalid === true
      || (typeof el.checkValidity === 'function' && !el.checkValidity())
      || /(^|[\s_-])(error|invalid)([\s_-]|$)/.test(cls);
    if (attrInvalid) {
      add(el, 'invalid');
      return;
    }
    let node = el;
    for (let depth = 0; node && depth < 4; node = node.parentElement, depth++) {
      const style = getComputedStyle(node);
      if ([style.borderTopColor, style.borderRightColor, style.borderBottomColor,
           style.borderLeftColor, style.outlineColor].some(isRed)) {
        add(el, 'visual-red');
        return;
      }
    }
  }

  function walk(root) {
    let elements; try { elements = root.querySelectorAll('*'); } catch (_) { return; }
    for (const el of elements) {
      if (seen.has(el)) continue;
      seen.add(el);
      try { if (el.matches && el.matches(controls)) inspect(el); } catch (_) {}
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  }
  // The draft page remains mounted behind the upload wizard and contains
  // duplicate, off-screen controls. Inspect the active wizard when present so
  // a stale editor field cannot create a false red-field failure.
  const dialogs = Array.from(document.querySelectorAll('ytcp-uploads-dialog'))
    .filter(visible);
  walk(dialogs.length ? dialogs[dialogs.length - 1] : document);
  return out;
}
"""


async def details_validation_issues(page):
    """Return visible invalid/red upload fields, including shadow DOM."""
    try:
        issues = await page.evaluate(DETAILS_VALIDATION_JS)
    except Exception as e:
        return [{"field": "details form", "reason": "inspection-failed",
                 "text": str(e).splitlines()[0][:180]}]
    return issues if isinstance(issues, list) else []


def format_validation_issues(issues):
    parts = []
    for issue in issues[:8]:
        field = str(issue.get("field") or "field")
        reason = str(issue.get("reason") or "invalid")
        text = str(issue.get("text") or "").strip()
        parts.append(f"{field} [{reason}]" + (f": {text}" if text else ""))
    return "; ".join(parts)


async def validate_details_before_action(page, debug, action):
    issues = await details_validation_issues(page)
    if not issues:
        return True
    log(f"  ERROR: invalid details before {action}: "
        + format_validation_issues(issues))
    await shot(page, "yt_xx_details_invalid", debug)
    return False


async def validate_details_before_next(page, debug):
    return await validate_details_before_action(page, debug, "Next")


async def _try_click(page, loc):
    """Locator.click() first, mouse click as the fallback."""
    try:
        await loc.click(timeout=4000)
        return True
    except Exception:
        return await ui.mouse_click(page, loc)


async def click_next(page, times, debug):
    """Advance the details wizard until the visibility step is on screen.

    Every step of this used to swallow its failures, so a wizard that never
    advanced still reported success and left the upload sitting as a private
    draft with the filename as its title. Now the caller is told.
    """
    for _ in range(times + 5):
        if await page.locator(VISIBILITY_RADIO.format("PUBLIC")).count() > 0:
            return True
        # Studio autosaves the details as they are typed and disables Next while
        # the "Saving..." chip is up; clicking through it does nothing.
        for _ in range(10):
            if "saving" not in (await ui.all_text(page)).lower():
                break
            await page.wait_for_timeout(1000)
        if not await validate_details_before_next(page, debug):
            return False
        nx = page.locator("ytcp-button#next-button, #next-button")
        clicked = False
        try:
            if await nx.count() > 0 and await nx.first.is_visible():
                if not await nx.first.is_enabled():
                    log("  ERROR: Next is disabled after details validation")
                    await shot(page, "yt_xx_next_disabled", debug)
                    return False
                clicked = await _try_click(page, nx.first)
        except Exception:
            clicked = False
        if not clicked:
            # The draft wizard reached via "Edit draft" renders a plain Next
            # button without the #next-button id the upload dialog uses.
            clicked = await ui.click_text(page, ["Next"], 4000)
        await page.wait_for_timeout(1800)
    reached = await page.locator(VISIBILITY_RADIO.format("PUBLIC")).count() > 0
    if not reached:
        log("  ERROR: never reached the visibility step")
    return reached


async def set_visibility(page, visibility, debug):
    name = {"private": "PRIVATE", "unlisted": "UNLISTED",
            "public": "PUBLIC"}.get(visibility, "PRIVATE")
    r = page.locator(VISIBILITY_RADIO.format(name))
    ok = False
    for attempt in range(3):
        if await r.count() == 0:
            await page.wait_for_timeout(1000)
            continue
        await _try_click(page, r.first)
        await page.wait_for_timeout(600)
        # aria-checked is the only trustworthy signal: the click can land on the
        # row without selecting the radio underneath it.
        if (await r.first.get_attribute("aria-checked")) == "true":
            ok = True
            break
    await shot(page, "yt_07_visibility", debug)
    if ok:
        log(f"  visibility: {visibility}")
    else:
        log(f"  ERROR: could not select visibility '{visibility}'")
    return ok


# Studio replaces the details wizard with a confirmation dialog once the save
# lands ("Video processing" / "Upload complete ... Processing will begin
# shortly"). That dialog carries its own done-button, so a save that worked
# perfectly still looks like a wizard that never closed.
SAVED_MARKERS = ("processing will begin", "upload complete", "video processing",
                 "your video is now", "share your video",
                 # Save can land while the file is still going up; the same
                 # dialog then reports progress instead of completion.
                 "video uploading", "still uploading", "keep this browser tab open")
# The browser must outlive the file transfer: closing it at 70% throws the
# upload away, and the save that preceded it counts for nothing.
UPLOAD_DONE_MARKERS = ("processing will begin", "upload complete",
                       "video processing", "checks complete", "your video is now")
UPLOAD_WAIT_S = 900

# Completion evidence belongs to the active upload/confirmation dialog. The
# user's metadata and the Content list behind it can contain the same words.
UPLOAD_STATUS_JS = r"""
() => {
  const parts = [], seen = new Set();
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  function walk(root) {
    if (!root || seen.has(root)) return;
    seen.add(root);
    if (root.nodeType === 1) {
      if (root.matches('input,textarea,[contenteditable],#title-textarea,#description-textarea,ytcp-video-row')) return;
      if (getComputedStyle(root).display === 'none' || getComputedStyle(root).visibility === 'hidden') return;
      if (root.shadowRoot) walk(root.shadowRoot);
    }
    if (root.nodeType === 3) {
      const text = (root.textContent || '').trim();
      if (text) parts.push(text);
    }
    for (const node of root.childNodes || []) walk(node);
  }
  const dialogs = Array.from(document.querySelectorAll('ytcp-uploads-dialog,ytcp-dialog,[role="dialog"]')).filter(visible);
  for (const dialog of dialogs) walk(dialog);
  return parts.join(' ').toLowerCase();
}
"""


def upload_completion_confirmed(text):
    if "keep this browser tab open" in text or "still uploading" in text:
        return False
    for progress in re.finditer(r"\buploading(?:\s+(\d+)%)?", text):
        if progress.group(1) is None or int(progress.group(1)) < 100:
            return False
    return any(marker in text for marker in UPLOAD_DONE_MARKERS)


async def _save_landed(page, done_locator):
    try:
        if await done_locator.count() == 0 or not await done_locator.first.is_visible():
            return True
    except Exception:
        return True
    text = await ui.all_text(page)
    return any(m in text for m in SAVED_MARKERS)


async def save(page, debug):
    if not await validate_details_before_action(page, debug, "Save"):
        log("  ERROR: refusing Save while a visible form field is invalid")
        return False
    d = page.locator("ytcp-button#done-button, #done-button")
    clicked = False
    for attempt in range(3):
        try:
            if await d.count() == 0 or not await d.first.is_visible():
                await page.wait_for_timeout(1000)
                continue
        except Exception:
            await page.wait_for_timeout(1000)
            continue
        await _try_click(page, d.first)
        await page.wait_for_timeout(3000)
        # Save is confirmed by the wizard giving way, not by the click itself.
        if await _save_landed(page, d):
            clicked = True
            break
    await page.wait_for_timeout(1000)
    await shot(page, "yt_08_saved", debug)
    if not clicked:
        log("  ERROR: Save did not take effect (details dialog still open)")
        return False
    log("  clicked Save")
    return await wait_for_upload(page, debug)


async def wait_for_upload(page, debug):
    """Hold the browser open until YouTube has the whole file.

    Saving only commits the metadata — the transfer can still be at 70%, and
    quitting then discards the video. Poll the dialog until it stops reporting
    progress.
    """
    deadline = time.time() + UPLOAD_WAIT_S
    last = ""
    while time.time() < deadline:
        text = await page.evaluate(UPLOAD_STATUS_JS)
        if not isinstance(text, str):
            raise RuntimeError("Upload status could not be inspected")
        if upload_completion_confirmed(text):
            log("  upload complete")
            return True
        m = re.search(r"uploading (\d+)%", text)
        if m and m.group(1) != last:
            last = m.group(1)
            log(f"  uploading {last}%")
        await page.wait_for_timeout(5000)
    log(f"  ERROR: transfer not confirmed after {UPLOAD_WAIT_S}s")
    await shot(page, "yt_09_upload_timeout", debug)
    return False


async def verify_before_save(page, channel_id, visibility):
    if channel_id_from_url(page.url) != channel_id:
        log("ERROR: Studio channel changed before Save")
        return False
    name = {"private": "PRIVATE", "unlisted": "UNLISTED", "public": "PUBLIC"}[visibility]
    radio = await ui.first_visible(page, [VISIBILITY_RADIO.format(name)], 5000)
    if radio is None or await radio.get_attribute("aria-checked") != "true":
        log(f"ERROR: expected {visibility} is not selected before Save")
        return False
    return True


async def verify_saved_video(page, video_id, meta, visibility, channel_id=None):
    """Read persisted details for this exact video, never an unrelated Public label."""
    if not video_id or not re.fullmatch(r"[\w-]{11}", video_id):
        return False
    url = f"{STUDIO}/video/{video_id}/edit"
    await _goto(page, url)
    if page.url.rstrip("/") != url:
        return False
    title = await ui.first_visible(page, ["#title-textarea #textbox"], 30_000)
    description = await ui.first_visible(page, ["#description-textarea #textbox"], 5000)
    if title is None or description is None:
        return False
    text = await ui.all_text(page)
    if any(reason in text for reason in ("processing abandoned", "too long", "upload failed")):
        return False
    if (not _same_field_text(await title.inner_text(), meta["title"])
            or not _same_field_text(await description.inner_text(), meta["description"])):
        return False
    if await page.locator("ytcp-video-visibility").count():
        control = await ui.first_visible(page, ["ytcp-video-visibility"], 5000)
        if control is not None:
            text = (await control.inner_text()).strip().lower()
            if re.search(r"\b" + re.escape(visibility) + r"\b", text):
                return True
    # The current details editor omits visibility. Read the dedicated cell of
    # this exact video in the bound channel's Content list instead.
    if channel_id:
        for tab in ("short", "upload"):
            await _goto(page, f"{STUDIO}/channel/{channel_id}/videos/{tab}")
            if await wait_for_channel_context(page) != channel_id:
                return False
            row = page.locator("ytcp-video-row").filter(
                has=page.locator(f'a[href*="/video/{video_id}/"]'))
            try:
                await row.first.wait_for(state="visible", timeout=15_000)
            except Exception:
                continue
            if await row.count() != 1:
                return False
            text = (await row.inner_text()).lower()
            if any(reason in text for reason in ("processing abandoned", "too long", "upload failed")):
                return False
            cell = row.locator("[class*='visibility']")
            if await cell.count() == 1 and await cell.is_visible():
                return (await cell.inner_text()).strip().lower() == visibility
            return False
    log("ERROR: saved visibility could not be read for the exact video")
    return False


async def clear_verify_gate(page, args, reload_after=False):
    """Google's 'Verify it's you' gate can appear at load OR mid-upload (right
    after the sensitive upload action). Returns True if clear to proceed, False
    if still gated. Never navigates away unless reload_after=True (safe only at
    load time — never mid-upload, which would discard the upload dialog)."""
    if not await ui.verify_gate_present(page):
        return True
    log("BLOCKED: 'Verify it's you' challenge.")
    await shot(page, "yt_verify_gate", True)
    if not args.keep_open:
        log("  Re-run with --keep-open and clear it in the window.")
        return False
    log(f"  Complete it in the window; waiting up to {args.verify_wait}s...")
    await ui.click_text(page, ["Next", "Continue"], 4000)
    end = time.time() + args.verify_wait
    while time.time() < end and await ui.verify_gate_present(page):
        await page.wait_for_timeout(3000)
    if await ui.verify_gate_present(page):
        log("  still gated; aborting.")
        return False
    log("  verification cleared.")
    if reload_after:
        await _goto(page, STUDIO)
        await page.wait_for_timeout(3000)
    return True


async def run(args):
    video = Path(args.video).expanduser()
    if not video.is_file():
        log(f"ERROR: --video not found: {video}")
        return 2
    if args.metadata and not Path(args.metadata).expanduser().is_file():
        log("ERROR: --metadata file was not found")
        return 2
    meta = load_metadata(args.metadata, args.title, args.description, args.tags)
    if not meta["title"]:
        meta["title"] = video.stem.replace("-", " ").replace("_", " ").title()

    if args.require_public and args.visibility != "public":
        log("ERROR: --require-public requires explicit --visibility public")
        return 2
    binding_file = PROFILE_DIR / "channel.json"
    binding = json.loads(binding_file.read_text()) if binding_file.exists() else {}
    if binding:
        if (args.channel_id and args.channel_id != binding["channel_id"]
                or args.channel_handle and args.channel_handle.lower() != binding["handle"].lower()):
            log("ERROR: requested channel differs from the persistent session binding")
            return 5
        args.channel_id = binding["channel_id"]
        args.channel_handle = binding["handle"]
    journal = RequestJournal(args.request_id, request_fingerprint(args, meta)) if args.request_id else None
    previous = journal.lookup() if journal else None
    if previous and not args.preflight:
        status, vid = previous
        log(f"REQUEST: status={status} video_id={vid or 'unknown'} — refusing another upload")
        return 0 if status == "published" else 11
    dur = video_duration(str(video))
    ok, reason = check(dur, verified=False, allow_long=args.allow_long)
    if not ok:
        log(f"PRECHECK FAILED: {reason}")
        return 8
    log(f"  duration {int(dur)}s ok")

    async with make_camoufox(args.headless) as ctx:
        page = await prepare_page(ctx)
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))
        await _goto(page, STUDIO)
        await page.wait_for_timeout(4000)
        await shot(page, "yt_01_studio", args.debug)
        if not await logged_in_youtube(page):
            log("ERROR: not logged in. Run login.py first.")
            return 3

        if not await clear_verify_gate(page, args, reload_after=True):
            return 7

        cid = await wait_for_channel_context(page)
        if args.channel_id or args.channel_handle:
            wanted = args.channel_id or await resolve_channel_id(
                page, args.channel_handle)
            if not wanted:
                log("ABORT: expected channel could not be resolved")
                return 5
            cid = await select_channel(page, wanted, args.channel_handle)
            # The Accounts panel regularly refuses to open. Uploading anyway
            # publishes to whichever channel happens to be active — a Chinese
            # Short landing on the Russian channel is not recoverable by
            # editing, only by deleting and re-uploading.
            if cid != wanted:
                log(f"ABORT: on channel {cid}, wanted {wanted} "
                    f"({args.channel_handle or args.channel_id}) — "
                    f"retry once the switch lands")
                return 5

        if not cid:
            log("ABORT: active Studio channel could not be verified")
            return 5

        if args.preflight and previous and previous[1]:
            verified = await verify_saved_video(page, previous[1], meta, args.visibility, cid)
            log(f"PREFLIGHT: stored_video={previous[1]} persisted_metadata_visibility={'ok' if verified else 'failed'}; no upload")
            if verified:
                journal.published(previous[1])
            return 0 if verified else 6

        if cid and not args.allow_duplicate:
            dup = await find_by_title(page, cid, meta["title"])
            if dup:
                log(f"ALREADY ON THE CHANNEL as {dup} — refusing to upload a "
                    f"second copy (pass --allow-duplicate to override)")
                if args.preflight:
                    verified = await verify_saved_video(page, dup, meta, args.visibility, cid)
                    log(f"PREFLIGHT: existing_video={dup} persisted_metadata_visibility={'ok' if verified else 'failed'}; no upload")
                    return 0 if verified else 6
                return 10

        if args.preflight:
            log(f"PREFLIGHT: channel={cid} visibility={args.visibility} metadata=loaded duplicate=absent; no upload")
            return 0
        if channel_id_from_url(page.url) != cid:
            log("ABORT: Studio channel changed before upload")
            return 5
        if journal:
            journal.start()  # Commit before a file can be attached, including crash recovery.

        try:
            if not await open_upload(page, video, args.debug):
                return 4
        except UploadLimitReached as e:
            # Do NOT close the browser. The daily limit on new channels is lifted
            # by account verification (phone) — the human needs the window open to
            # complete it. Log the sentinel for the orchestrator, screenshot the
            # proof, then hold the session open until the human is done.
            log(f"DAILY_LIMIT_REACHED — browser held open for verification. "
                f"Complete verification in the window, then Ctrl+C to stop. Reason: {e}")
            try:
                while True:
                    await page.wait_for_timeout(60000)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            return 2
        # The gate most often fires here — on the sensitive upload action. Clear
        # it BEFORE filling details, so title/description/tags/audience/Save are
        # not silently blocked by the modal. No reload (would drop the dialog).
        if not await clear_verify_gate(page, args):
            return 7
        async def landed_anyway(stage):
            """Did the video make it onto the channel despite `stage` failing?

            Studio autosaves the details as they are typed, so a wizard that
            stalls half-way can still leave a complete, published video. Saying
            "failed" then is worse than useless: the caller retries and
            publishes a duplicate.
            """
            log(f"  {stage} failed; checking whether the video landed anyway")
            if not await wait_for_upload(page, args.debug):
                return ""
            if not cid:
                return ""
            return await find_by_title(page, cid, meta["title"])

        for stage, step in (
                ("details", lambda: fill_details(page, meta, args.thumbnail,
                                                 args.made_for_kids, args.debug)),
                ("visibility step", lambda: click_next(page, 3, args.debug)),
                ("visibility", lambda: set_visibility(page, args.visibility,
                                                      args.debug)),
                ("pre-submit verification", lambda: verify_before_save(page, cid, args.visibility)),
                ("save", lambda: save(page, args.debug))):
            if await step():
                continue
            found = await landed_anyway(stage)
            if found:
                if journal:
                    journal.remember_video(found)
                log(f"  the {stage} step failed, but the video is on the channel "
                    f"as {found} — not retrying")
                log(f"VIDEO_ID: {found}")
                log("RESULT: status=uncertain note=wizard-incomplete; finish the existing video")
                return 9
            log(f"PUBLISH FAILED: {stage} did not take effect; "
                f"the upload is left as a draft")
            return 9

        # Capture the watch id from the save dialog (for blog embedding).
        # Only trust the short youtu.be/<id> share link the save dialog renders —
        # a plain watch?v= match can come from the Content list sitting behind
        # the dialog and yields a DIFFERENT (older) video's id. Poll for it.
        # A Short's share link is youtube.com/shorts/<id> rather than youtu.be/<id>.
        import re as _re
        vid = None
        for _ in range(12):
            try:
                html = await page.content()
                m = (_re.search(r"youtu\.be/([\w-]{6,})", html)
                     or _re.search(r"youtube\.com/shorts/([\w-]{6,})", html))
                if m:
                    vid = m.group(1)
                    break
            except Exception:
                pass
            await page.wait_for_timeout(1000)
        if vid:
            log(f"VIDEO_ID: {vid}")
        else:
            # Fallback: the save-dialog share-link scrape is flaky (the dialog
            # sometimes closes before youtu.be/<id> renders), but at this point
            # the upload IS saved. Recover the id from Studio's own content list
            # via find_by_title — built for exactly this case (its docstring).
            try:
                vid = await find_by_title(page, cid, meta["title"])
            except Exception as e:
                log(f"  find_by_title fallback failed: {e}")
            log(f"VIDEO_ID: {vid}" if vid
                else "VIDEO_ID: not found in save dialog (find_by_title fallback empty)")
        if journal:
            journal.remember_video(vid)

        # YouTube may still be running content checks (especially on Shorts,
        # whose NotebookLM background music gets Content-ID-flagged) and then
        # recommends keeping the video private with a "Publish anyway" option.
        # Click it to honor the --visibility we asked for. No-op when the button
        # isn't present (desktop videos pass checks cleanly).
        try:
            for _ in range(3):
                if args.visibility != "public":
                    break
                if await ui.click_text(page, ["Publish anyway",
                                              "Опубликовать в любом случае"], 3000):
                    log("  clicked 'Publish anyway' (override checks-recommend-private)")
                    await page.wait_for_timeout(3000)
                    break
                await page.wait_for_timeout(2000)
        except Exception as e:
            log(f"  Publish-anyway note: {str(e).splitlines()[0][:50]}")

        verified = await verify_saved_video(page, vid, meta, args.visibility, cid)
        if verified and journal:
            journal.published(vid)
        log(f"RESULT: status={'published' if verified else 'uncertain'} visibility={args.visibility}")
        if args.keep_open:
            log("--keep-open: browser stays open. Ctrl+C to quit.")
            while True:
                await page.wait_for_timeout(3600_000)
        return 0 if verified else 6


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Publish a video to YouTube via Camoufox.")
    p.add_argument("--video", required=True)
    p.add_argument("--metadata", default="")
    p.add_argument("--thumbnail", default="")
    p.add_argument("--title", default="")
    p.add_argument("--description", default="")
    p.add_argument("--tags", default="")
    p.add_argument("--channel-id", default="")
    p.add_argument("--channel-handle", default="")
    p.add_argument("--visibility", default="private",
                   choices=["private", "unlisted", "public"])
    p.add_argument("--made-for-kids", action="store_true")
    p.add_argument("--allow-long", action="store_true")
    p.add_argument("--allow-duplicate", action="store_true",
                   help="upload even if the channel already has this title")
    p.add_argument("--request-id", default="", help="Durable request key; never repeat a started upload")
    p.add_argument("--preflight", action="store_true", help="Verify media/session/channel/duplicates without uploading")
    p.add_argument("--require-public", action="store_true", help="Reject any visibility other than explicit public")
    p.add_argument("--verify-wait", type=int, default=600)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--keep-open", action="store_true")
    p.add_argument("--debug", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except UploadLimitReached as e:
        # Sentinel the orchestrator greps to STOP the upload phase for the day.
        # Exit code 2 = daily-limit/verify-gate; retrying is futile (~24h reset).
        log(f"DAILY_LIMIT_REACHED — stopping upload phase. Reason: {e}")
        return 2
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError) as exc:
        log(f"ERROR: {exc}")
        return 9


if __name__ == "__main__":
    sys.exit(main())
