"""Fakes for the injectable seams. No network, no database, no os.environ.

Every function in `immich_clip` that does I/O takes the doer as a parameter —
`connect=`, `opener=`, `log=`, `sleep=`, `now=`. That is what lets the whole unit
suite run in a few seconds with nothing installed but pytest, and it is the reason
these fakes are ~80 lines rather than a mocking framework.
"""

import io
import json

from immich_clip.config import ApiKey, Settings

ASSET = "0a342213-0aca-4f8a-abc1-7260fbff30a1"
OTHER = "fba7dd29-623c-47c7-92db-65fb252614a8"
ALBUM = "41a4a164-360a-40d0-88ff-a9a6436c992c"


def settings(api_key=None, keys=None, **kw):
    """Settings for a test, with the one-key shorthand spelled the short way.

    `api_key="k"` is by far the common case in these tests, and writing
    `keys=(ApiKey("*", "k"),)` sixty times would bury what each test is about.
    """
    if keys is None:
        keys = (ApiKey("*", api_key),) if api_key else ()
    return Settings(keys=tuple(keys), **kw)


class FakeResponse(io.BytesIO):
    """Enough of an http.client.HTTPResponse for httpjson to work with."""

    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeOpener:
    """Drop-in for `urllib.request.urlopen`.

    Records every Request it is handed and replies from a queue of responses (the
    last one repeats, so single-reply tests stay terse).
    """

    def __init__(self, replies=None):
        self.replies = list(replies or [FakeResponse(b"{}")])
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return reply() if callable(reply) else reply

    @property
    def last(self):
        return self.requests[-1]

    def body_of(self, index=-1):
        return json.loads(self.requests[index].data.decode())


def json_reply(obj, status=200):
    return lambda: FakeResponse(json.dumps(obj).encode(), status)


class FakeCursor:
    """`results` is a queue of result SETS, one per fetch — so a test can model
    "no embedding yet, no embedding yet, then one" as [[], [], [(0.2,)]]."""

    def __init__(self, results=()):
        self.results = [list(r) for r in results]
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((" ".join(sql.split()), params))

    def _next(self):
        return self.results.pop(0) if self.results else []

    def fetchone(self):
        rows = self._next()
        return rows[0] if rows else None

    def fetchall(self):
        return self._next()


class FakeConn:
    def __init__(self, cursor=None):
        self._cursor = cursor or FakeCursor()
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        pass

    def close(self):
        self.closed = True


