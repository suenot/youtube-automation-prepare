from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import login


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
