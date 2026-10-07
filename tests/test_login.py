from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import login
import camoufox_session


@pytest.mark.asyncio
async def test_failed_identity_discards_imported_profile(tmp_path, monkeypatch):
    profile = tmp_path / "saved-profile"
    monkeypatch.setattr(login, "HERE", tmp_path)
    monkeypatch.setattr(login, "PROFILE_DIR", profile)
    monkeypatch.setattr(login, "firefox_cookies", lambda _p: [{"name": "SID", "value": "private"}])

    class Context:
        async def add_cookies(self, _cookies):
            pass

    class Page:
        async def goto(self, *_args, **_kwargs):
            pass

    @asynccontextmanager
    async def session(_headless, profile_dir):
        profile_dir.mkdir()
        (profile_dir / "private-cookie-file").write_text("private")
        yield Context()

    async def page(_ctx):
        return Page()

    async def authenticated(_page):
        return True

    async def mismatch(_page, _handle):
        raise RuntimeError("channel differs")

    monkeypatch.setattr(login, "make_camoufox", session)
    monkeypatch.setattr(login, "prepare_page", page)
    monkeypatch.setattr(login, "logged_in_youtube", authenticated)
    monkeypatch.setattr(login, "verify_channel", mismatch)

    with pytest.raises(RuntimeError, match="channel differs"):
        await login.import_firefox(SimpleNamespace(firefox_profile="source", headless=True,
                                                  channel_handle="@expected"))
    assert not profile.exists()
    assert not list((tmp_path / ".local").iterdir())


@pytest.mark.asyncio
async def test_signed_out_google_chooser_is_not_authenticated():
    class Page:
        url = "https://accounts.google.com/v3/signin/identifier?continue=https://studio.youtube.com"

        @property
        def context(self):
            raise AssertionError("Cookie presence is not login evidence")

    assert not await camoufox_session.logged_in_youtube(Page())


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/channel/UCexpected/videos", "/video/abcdefghijk/edit", "/video/abcdefghijk/translations"])
async def test_authenticated_studio_requires_visible_account_control(route):
    class Control:
        def __init__(self, visible):
            self.visible = visible

        @property
        def first(self):
            return self

        async def is_visible(self):
            return self.visible

    class Page:
        url = "https://studio.youtube.com" + route

        def __init__(self, visible):
            self.control = Control(visible)

        def locator(self, _selector):
            return self.control

        async def wait_for_timeout(self, _ms):
            pass

    assert not await camoufox_session.logged_in_youtube(Page(False))
    assert await camoufox_session.logged_in_youtube(Page(True))


@pytest.mark.asyncio
@pytest.mark.parametrize("verified_id,expected_cookie", [
    ("UCexpected", b"new cookies"),
    ("UCwrong", b"old cookies"),
    (None, b"old cookies"),
])
async def test_refresh_keeps_binding_and_restores_cookies_on_mismatch(
        tmp_path, monkeypatch, verified_id, expected_cookie):
    profile = tmp_path / "saved-profile"
    profile.mkdir()
    (profile / "cookies.sqlite").write_bytes(b"old cookies")
    (profile / "cookies.sqlite-wal").write_bytes(b"old wal")
    binding = b'{"channel_id":"UCexpected","handle":"@expected"}'
    (profile / "channel.json").write_bytes(binding)
    (profile / "fingerprint.json").write_bytes(b"stable fingerprint")
    private = tmp_path / ".local"
    private.mkdir()
    (private / "publications.sqlite").write_bytes(b"journal")
    monkeypatch.setattr(login, "HERE", tmp_path)
    monkeypatch.setattr(login, "PROFILE_DIR", profile)
    monkeypatch.setattr(login, "firefox_cookies", lambda _p: [{"name": "SID", "value": "private"}])

    class Context:
        async def clear_cookies(self, *, domain):
            assert domain.search(".youtube.com")
            assert domain.search("accounts.google.com")
            (profile / "cookies.sqlite").write_bytes(b"new cookies")
            (profile / "cookies.sqlite-wal").unlink()

        async def add_cookies(self, _cookies):
            pass

    @asynccontextmanager
    async def session(_headless, *, profile_dir, profile_locked):
        assert profile_dir == profile
        assert profile_locked is True
        yield Context()

    class Page:
        async def goto(self, *_args, **_kwargs):
            pass

    async def page(_ctx):
        return Page()

    async def authenticated(_page):
        return verified_id is not None

    async def verify(_page, _handle):
        return {"channel_id": verified_id, "handle": "@expected"}

    monkeypatch.setattr(login, "make_camoufox", session)
    monkeypatch.setattr(login, "prepare_page", page)
    monkeypatch.setattr(login, "logged_in_youtube", authenticated)
    monkeypatch.setattr(login, "verify_channel", verify)
    args = SimpleNamespace(firefox_profile="source", headless=True,
                           channel_handle="@expected", refresh=True)

    if verified_id == "UCexpected":
        assert await login.import_firefox(args) == 0
    else:
        message = "not authenticated" if verified_id is None else "saved binding"
        with pytest.raises(RuntimeError, match=message):
            await login.import_firefox(args)
    assert (profile / "cookies.sqlite").read_bytes() == expected_cookie
    if verified_id != "UCexpected":
        assert (profile / "cookies.sqlite-wal").read_bytes() == b"old wal"
    assert (profile / "channel.json").read_bytes() == binding
    assert (profile / "fingerprint.json").read_bytes() == b"stable fingerprint"
    assert (private / "publications.sqlite").read_bytes() == b"journal"
    assert sorted(x.name for x in private.iterdir()) == ["publications.sqlite"]


@pytest.mark.asyncio
async def test_failed_rollback_retains_private_recovery_copy(tmp_path, monkeypatch):
    profile = tmp_path / "saved-profile"
    profile.mkdir()
    (profile / "cookies.sqlite").write_bytes(b"original cookies")
    (profile / "channel.json").write_text('{"channel_id":"UCexpected","handle":"@expected"}')
    monkeypatch.setattr(login, "HERE", tmp_path)
    monkeypatch.setattr(login, "PROFILE_DIR", profile)

    @asynccontextmanager
    async def session(*_args, **_kwargs):
        (profile / "cookies.sqlite").write_bytes(b"changed cookies")
        raise RuntimeError("Browser failed")
        yield

    def failed_restore(*_args):
        raise OSError("Disk error")

    monkeypatch.setattr(login, "make_camoufox", session)
    monkeypatch.setattr(login, "restore_cookies", failed_restore)
    args = SimpleNamespace(headless=True, channel_handle="@expected")
    with pytest.raises(RuntimeError, match="private recovery files retained") as error:
        await login.refresh_firefox(args, [])
    backups = list((tmp_path / ".local").glob("youtube-refresh-*"))
    assert len(backups) == 1 and str(backups[0]) in str(error.value)
    assert backups[0].stat().st_mode & 0o777 == 0o700
    assert (backups[0] / "cookies.sqlite").read_bytes() == b"original cookies"
