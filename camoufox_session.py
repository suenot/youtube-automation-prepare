import fcntl
import json
import os
import pickle
import re
import time
from contextlib import asynccontextmanager, contextmanager, nullcontext
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / ".camoufox_profile"
FP_FILE = HERE / ".camoufox_fp.pkl"
DEBUG_DIR = HERE / "debug"
WINDOW_WIDTH, WINDOW_HEIGHT = 1700, 1060
VIEWPORT = {"width": 1700, "height": 948}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _geoip_available():
    try:
        import socket
        socket.create_connection(("api.ipify.org", 443), timeout=3).close()
        return True
    except Exception:
        return False


def _stable_fingerprint():
    # FP_FILE is a locally-generated, git-ignored fingerprint written only by this
    # project (never from an external/untrusted source), so unpickling it is safe.
    if FP_FILE.exists():
        try:
            return pickle.loads(FP_FILE.read_bytes())
        except Exception:
            return None
    return None


@contextmanager
def profile_lock(profile=PROFILE_DIR):
    lock = Path(str(profile) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        lock.chmod(0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Camoufox profile is busy; close the other session") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


# The owner pinned this build (158.0 beta.40) instead of the package's paired
# 156.0.1-beta.34. It lives in the project, so the pin check never depends on a
# cache or on another project's folder. Change both together.
BROWSER_DIR = HERE / ".camoufox-browser"
BROWSER_EXECUTABLE = BROWSER_DIR / "Camoufox.app" / "Contents" / "MacOS" / "camoufox"
PINNED_BROWSER = ("158.0", "beta.40")


def verify_browser_build():
    """Launch only the pinned build; return its Firefox version for the library."""
    metadata = BROWSER_DIR / "version.json"
    if not BROWSER_EXECUTABLE.is_file() or not metadata.is_file():
        raise RuntimeError(f"Pinned Camoufox {'-'.join(PINNED_BROWSER)} missing in {BROWSER_DIR}")
    actual = json.loads(metadata.read_text(encoding="utf-8"))
    found = (actual.get("version"), actual.get("build") or actual.get("release"))
    if found != PINNED_BROWSER:
        raise RuntimeError(f"Camoufox browser is {'-'.join(map(str, found))}, pinned {'-'.join(PINNED_BROWSER)}")
    os.environ["CAMOUFOX_EXECUTABLE_PATH"] = str(BROWSER_EXECUTABLE)
    return found[0]


def _normalize_window(fp):
    # The fingerprint pins the real window size. A stored or generated one can be
    # small or square; always use a normal landscape window that fits the screen.
    fp.setdefault("window", {}).update(
        innerWidth=VIEWPORT["width"], innerHeight=VIEWPORT["height"],
        outerWidth=WINDOW_WIDTH, outerHeight=WINDOW_HEIGHT, screenX=0, screenY=25)
    return fp


def _reset_window_size(profile_dir):
    # Firefox restores the last window size from xulstore.json; a small or square
    # leftover makes every run open a cramped window. Force a normal landscape one.
    store = Path(profile_dir) / "xulstore.json"
    try:
        data = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
    except (OSError, ValueError):
        data = {}
    win = data.setdefault("chrome://browser/content/browser.xhtml", {}).setdefault("main-window", {})
    win.update(width=str(WINDOW_WIDTH), height=str(WINDOW_HEIGHT), screenX="0", screenY="25", sizemode="normal")
    store.write_text(json.dumps(data), encoding="utf-8")


@asynccontextmanager
async def make_camoufox(headless=False, profile_dir=PROFILE_DIR, profile_locked=False):
    from camoufox.async_api import AsyncCamoufox
    from camoufox.fingerprints import generate_fingerprint
    browser_version = verify_browser_build()
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile_dir.chmod(0o700)
    # Camoufox 0.5.x treats bool as an int and serializes humanize=True as
    # `humanize:maxTime=true`; the browser requires a double and then stops
    # servicing trusted mouse input. Pass an explicit float instead.
    opts = dict(headless=headless, humanize=1.0, geoip=_geoip_available(),
                block_images=False, persistent_context=True,
                user_data_dir=str(profile_dir), window=(1920, 1080),
                locale="en-US", main_world_eval=True, i_know_what_im_doing=True)
    # The library cannot read the version of a build outside its own cache.
    opts["ff_version"] = int(browser_version.split(".")[0])
    # The refresh path holds profile_lock around its cookie backup, browser use,
    # and rollback; it must not try to acquire the same flock a second time.
    with nullcontext() if profile_locked else profile_lock(profile_dir):
        fp_path = profile_dir / "fingerprint.json"
        if fp_path.exists():
            fp = json.loads(fp_path.read_text(encoding="utf-8"))
        else:
            fp = _stable_fingerprint() if profile_dir == PROFILE_DIR else None
            if fp is None:
                fp = generate_fingerprint(os="macos")
                fp_path.write_text(json.dumps(fp), encoding="utf-8")
                fp_path.chmod(0o600)
        opts["fingerprint"] = _normalize_window(fp)
        _reset_window_size(profile_dir)
        try:
            async with AsyncCamoufox(**opts) as context:
                yield context
        finally:
            for parent, _dirs, files in os.walk(profile_dir):
                Path(parent).chmod(0o700)
                for name in files:
                    path = Path(parent) / name
                    if not path.is_symlink():
                        path.chmod(0o600)


async def prepare_page(context):
    page = context.pages[0] if context.pages else await context.new_page()
    await page.set_viewport_size(VIEWPORT)
    return page


async def logged_in_youtube(page):
    try:
        # A stale SID can survive after Google redirects Studio to its signed-out
        # account chooser. Require an authenticated Studio channel/video page and
        # its account control instead of inferring login from cookie presence.
        for _ in range(20):
            url = urlsplit(page.url)
            if url.hostname != "studio.youtube.com":
                return False
            if re.match(r"^/(?:channel/UC[\w-]+|video/[\w-]+)(?:/|$)", url.path):
                avatar = page.locator("button#avatar-btn, ytcp-icon-button#avatar-btn, "
                                      "button[aria-label='Account']").first
                if await avatar.is_visible():
                    return True
            await page.wait_for_timeout(500)
        return False
    except Exception:
        return False


async def shot(page, name, enabled=True):
    if not enabled:
        return
    DEBUG_DIR.mkdir(exist_ok=True)
    try:
        await page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=False)
        log(f"  screenshot -> debug/{name}.png")
    except Exception as e:
        log(f"  (screenshot {name} failed: {e})")
