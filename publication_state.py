"""A durable stop guard: a started request is never uploaded again."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from camoufox_session import HERE

STATE_FILE = HERE / ".local" / "publications.sqlite"


def request_fingerprint(args, meta):
    def digest(path):
        hasher = hashlib.sha256()
        with Path(path).expanduser().open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    data = dict(video=digest(args.video), metadata=meta,
                thumbnail=digest(args.thumbnail) if args.thumbnail else "",
                channel_id=args.channel_id, channel_handle=args.channel_handle.lower(),
                visibility=args.visibility, made_for_kids=args.made_for_kids)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class RequestJournal:
    def __init__(self, request_id, fingerprint, path=STATE_FILE):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", request_id):
            raise ValueError("--request-id must contain 1–100 letters, digits, dots, dashes or underscores")
        self.request_id, self.fingerprint, self.path = request_id, fingerprint, Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        db = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        db.execute("CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, "
                   "fingerprint TEXT NOT NULL, status TEXT NOT NULL, video_id TEXT NOT NULL)")
        return db

    def lookup(self):
        if not self.path.exists():
            return None
        with closing(self._connect()) as db:
            row = db.execute("SELECT fingerprint,status,video_id FROM requests WHERE request_id=?",
                             (self.request_id,)).fetchone()
        if row and row[0] != self.fingerprint:
            raise ValueError("--request-id already belongs to different media, metadata or publication options")
        return row[1:] if row else None

    def start(self):
        with closing(self._connect()) as db, db:
            try:
                db.execute("INSERT INTO requests VALUES (?, ?, 'uncertain', '')",
                           (self.request_id, self.fingerprint))
            except sqlite3.IntegrityError:
                raise RuntimeError("Request already started; refusing another upload") from None

    def published(self, video_id):
        with closing(self._connect()) as db, db:
            db.execute("UPDATE requests SET status='published',video_id=? WHERE request_id=?",
                       (video_id, self.request_id))

    def remember_video(self, video_id):
        if video_id and re.fullmatch(r"[\w-]{11}", video_id):
            with closing(self._connect()) as db, db:
                db.execute("UPDATE requests SET video_id=? WHERE request_id=?",
                           (video_id, self.request_id))
