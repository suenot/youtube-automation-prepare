"""Import only YouTube/Google authentication cookies; values stay in memory."""

import sqlite3
import time
from pathlib import Path

COOKIE_HOSTS = {"youtube.com", "www.youtube.com", "studio.youtube.com",
                "google.com", "accounts.google.com"}
SCHEMA_VERSIONS = {15, 16, 17}
SAME_SITE = {0: "None", 1: "Lax", 2: "Strict"}
REQUIRED_COLUMNS = {"name", "value", "host", "path", "expiry", "isSecure",
                    "isHttpOnly", "sameSite", "originAttributes"}


def firefox_cookies(profile):
    path = Path(profile).expanduser().resolve() / "cookies.sqlite"
    if not path.is_file():
        raise ValueError("Firefox profile has no readable cookies.sqlite")
    source = snapshot = None
    try:
        source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        snapshot = sqlite3.connect(":memory:")
        deadline = time.monotonic() + 15

        def progress(_status, _remaining, _total):
            if time.monotonic() > deadline:
                raise TimeoutError()

        source.backup(snapshot, pages=128, progress=progress, sleep=0.05)
        snapshot.row_factory = sqlite3.Row
        version = snapshot.execute("PRAGMA user_version").fetchone()[0]
        columns = {r[1] for r in snapshot.execute("PRAGMA table_info(moz_cookies)")}
        if version not in SCHEMA_VERSIONS or not REQUIRED_COLUMNS <= columns:
            raise ValueError("Firefox cookie schema is unsupported")
        hosts = sorted(COOKIE_HOSTS | {"." + h for h in COOKIE_HOSTS})
        partitioned = "isPartitionedAttributeSet" if "isPartitionedAttributeSet" in columns else "0"
        rows = snapshot.execute(
            "SELECT name,value,host,path,expiry,isSecure,isHttpOnly,sameSite,"
            "originAttributes," + partitioned + " AS partitioned FROM moz_cookies WHERE host IN ("
            + ",".join("?" for _ in hosts) + ") LIMIT 4097", hosts).fetchall()
        if len(rows) > 4096:
            raise ValueError("Too many authentication cookies to import safely")
        result = []
        for row in rows:
            if row["originAttributes"] or row["partitioned"]:
                continue  # Never flatten container or partitioned cookie scope.
            expiry = row["expiry"] / (1000 if version >= 16 else 1)
            if expiry <= time.time():
                continue
            if row["sameSite"] not in (*SAME_SITE, 256):
                raise ValueError("Unsupported Firefox cookie SameSite attribute")
            if (not isinstance(row["name"], str) or not row["name"]
                    or not isinstance(row["value"], str)
                    or not isinstance(row["path"], str)
                    or not row["path"].startswith("/")):
                raise ValueError("Invalid Firefox cookie attributes")
            cookie = dict(name=row["name"], value=row["value"],
                          domain=row["host"], path=row["path"], expires=expiry,
                          secure=bool(row["isSecure"]), httpOnly=bool(row["isHttpOnly"]))
            if row["sameSite"] in SAME_SITE:
                cookie["sameSite"] = SAME_SITE[row["sameSite"]]
            result.append(cookie)
        if not result:
            raise ValueError("No live, unpartitioned YouTube/Google session cookies")
        return result
    except (sqlite3.Error, OSError, TimeoutError, TypeError):
        raise ValueError("Firefox cookie snapshot failed; check profile access") from None
    finally:
        if snapshot is not None:
            snapshot.close()
        if source is not None:
            source.close()
