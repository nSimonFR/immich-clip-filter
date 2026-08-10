"""Fixtures shared by every suite. The fakes themselves live in `fakes.py`.

They are a module rather than more conftest, because there are two conftest files
(this one and tests/integration/) and `from conftest import ...` would resolve to
whichever directory pytest inserted into sys.path first — which is exactly the
kind of failure that only shows up when someone adds the second suite.
"""

import pytest

from fakes import ALBUM, ASSET, OTHER, FakeConn, FakeCursor, FakeOpener, FakeResponse  # noqa: F401
from fakes import json_reply, settings  # noqa: F401


@pytest.fixture
def opener():
    return FakeOpener


@pytest.fixture
def logged():
    """A `log` callable that appends to a list instead of writing to stdout."""
    lines = []
    return lines, lines.append


@pytest.fixture
def q(tmp_path):
    """A real SQLite state database (pending queue + exclusions)."""
    from immich_clip import queue

    return queue.connect(str(tmp_path / "pending.sqlite"))
