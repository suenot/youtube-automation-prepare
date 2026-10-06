import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from camoufox_session import (HERE, PROFILE_DIR, make_camoufox, prepare_page,
                             logged_in_youtube, log, profile_lock)
from channel import select_channel, resolve_channel_id, normalize_handle, STUDIO
from firefox_cookies import firefox_cookies


async def verify_channel(page, handle):
    wanted = await resolve_channel_id(page, handle)
    if not wanted:
        raise RuntimeError("Could not resolve the expected YouTube channel")
    active = await select_channel(page, channel_id=wanted, handle=handle)
    if active != wanted:
        raise RuntimeError("Authenticated Studio channel differs from the expected channel")
    return {"channel_id": wanted, "handle": normalize_handle(handle)}


def save_binding(profile, binding):
    path = profile / "channel.json"
    path.write_text(json.dumps(binding), encoding="utf-8")
    path.chmod(0o600)


async def import_firefox(args):
    cookies = firefox_cookies(args.firefox_profile)
    private = HERE / ".local"
    private.mkdir(exist_ok=True, mode=0o700)
    private.chmod(0o700)
    with profile_lock(PROFILE_DIR), tempfile.TemporaryDirectory(prefix="youtube-import-", dir=private) as tmp:
        if PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir()):
            raise RuntimeError("Camoufox profile already exists; use --status to verify it")
        profile = Path(tmp) / "profile"
        async with make_camoufox(args.headless, profile_dir=profile) as ctx:
            try:
                await ctx.add_cookies(cookies)
            except Exception:
                raise RuntimeError("Camoufox could not import Firefox authentication cookies") from None
            page = await prepare_page(ctx)
            await page.goto(STUDIO, wait_until="domcontentloaded", timeout=60_000)
            if not await logged_in_youtube(page):
                raise RuntimeError("Imported Firefox session is not authenticated in Camoufox")
            binding = await verify_channel(page, args.channel_handle)
            save_binding(profile, binding)
        if PROFILE_DIR.exists():
            PROFILE_DIR.rmdir()
        shutil.move(str(profile), PROFILE_DIR)
        log(f"Imported Firefox session; verified channel {binding['handle']} ({binding['channel_id']})")
    return 0


async def run(args):
    if args.firefox_profile:
        return await import_firefox(args)
    if args.status and not PROFILE_DIR.exists():
        log("ERROR: no persistent Camoufox session; import Firefox or sign in")
        return 3
    async with make_camoufox(args.headless) as ctx:
        page = await prepare_page(ctx)
        await page.goto("https://studio.youtube.com",
                        wait_until="domcontentloaded", timeout=60_000)
        if not args.status:
            log("Sign into the TARGET YouTube account in this window.")
        for _ in range(1 if args.status else 3600):
            if await logged_in_youtube(page):
                binding_file = PROFILE_DIR / "channel.json"
                binding = json.loads(binding_file.read_text()) if binding_file.exists() else {}
                handle = args.channel_handle or binding.get("handle")
                if handle:
                    verified = await verify_channel(page, handle)
                    if binding and verified["channel_id"] != binding["channel_id"]:
                        raise RuntimeError("Authenticated channel differs from the saved binding")
                    save_binding(PROFILE_DIR, verified)
                    log(f"Verified channel {verified['handle']} ({verified['channel_id']})")
                log("Logged in — session saved to .camoufox_profile/. You can close now.")
                await page.wait_for_timeout(2000)
                return 0
            await page.wait_for_timeout(1000)
        log("ERROR: not logged in" if args.status else "Timed out waiting for sign-in.")
        return 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--firefox-profile", default="", help="Read-only Firefox cookie source; requires --channel-handle")
    ap.add_argument("--channel-handle", default="", help="Expected authenticated YouTube channel")
    ap.add_argument("--status", action="store_true", help="Verify saved session and channel without waiting for login")
    a = ap.parse_args()
    if a.firefox_profile and (not a.channel_handle or a.status):
        ap.error("--firefox-profile requires --channel-handle and cannot be combined with --status")
    try:
        return asyncio.run(run(a))
    except (ValueError, RuntimeError) as exc:
        log(f"ERROR: {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
