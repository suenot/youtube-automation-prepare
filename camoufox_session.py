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


def verify_browser_build():
    from camoufox.browser_pin import load_pin
    from camoufox.multiversion import get_active_path

    executable = os.environ.get("CAMOUFOX_EXECUTABLE_PATH")
    if executable:
        metadata = next((p / "version.json" for p in Path(executable).resolve().parents
                         if (p / "version.json").is_file()), None)
    else:
        active = get_active_path()
        metadata = active / "version.json" if active else None
    if not metadata or not metadata.is_file():
        raise RuntimeError("Install the paired browser with python -m camoufox fetch")
    pin = load_pin()
    actual = json.loads(metadata.read_text(encoding="utf-8"))
    if not pin or (actual.get("version"), actual.get("build")) != (pin.version, pin.build):
        raise RuntimeError("Camoufox browser differs from the package's paired release")


@asynccontextmanager
async def make_camoufox(headless=False, profile_dir=PROFILE_DIR, profile_locked=False):
    from camoufox.async_api import AsyncCamoufox
    from camoufox.fingerprints import generate_fingerprint
    verify_browser_build()
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
        opts["fingerprint"] = fp
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
    await page.set_viewport_size({"width": 1920, "height": 1080})
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
