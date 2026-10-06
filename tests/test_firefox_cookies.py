import sqlite3
import time

import pytest

from firefox_cookies import firefox_cookies


@pytest.mark.parametrize("version,scale", [(15, 1), (16, 1000), (17, 1000)])
def test_import_preserves_auth_scope_and_excludes_other_sites(tmp_path, version, scale):
    db = sqlite3.connect(tmp_path / "cookies.sqlite")
    db.execute(f"PRAGMA user_version={version}")
    db.execute("CREATE TABLE moz_cookies (name,value,host,path,expiry,isSecure,isHttpOnly,sameSite,originAttributes)")
    future = int(time.time() + 3600) * scale
    rows = [
        ("SID", "private", ".youtube.com", "/", future, 1, 1, 0, ""),
        ("token", "private", "accounts.google.com", "/signin", future, 1, 1, 1, ""),
        ("other", "secret", "mail.google.com", "/", future, 1, 1, 0, ""),
        ("other", "secret", "evil-youtube.com", "/", future, 1, 1, 0, ""),
        ("partition", "secret", ".youtube.com", "/", future, 1, 1, 0, "^partitionKey=x"),
        ("expired", "secret", ".youtube.com", "/", 1, 1, 1, 0, ""),
    ]
    db.executemany("INSERT INTO moz_cookies VALUES (?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()

    cookies = firefox_cookies(tmp_path)

    assert [c["name"] for c in cookies] == ["SID", "token"]
    assert cookies[0]["domain"] == ".youtube.com"
    assert cookies[1]["domain"] == "accounts.google.com"
    assert cookies[1]["path"] == "/signin"
    assert cookies[1]["sameSite"] == "Lax"
    assert cookies[1]["expires"] == future / scale
    with sqlite3.connect(tmp_path / "cookies.sqlite") as original:
        assert original.execute("SELECT COUNT(*) FROM moz_cookies").fetchone()[0] == len(rows)


def test_rejects_unreviewed_schema_without_values(tmp_path):
    with sqlite3.connect(tmp_path / "cookies.sqlite") as db:
        db.execute("PRAGMA user_version=999")
        db.execute("CREATE TABLE moz_cookies (secret)")
    with pytest.raises(ValueError, match="schema is unsupported"):
        firefox_cookies(tmp_path)
