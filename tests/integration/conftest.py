"""Fixtures for the end-to-end suite: a real Immich, a real Postgres, real albums.

**The insight that makes this tractable: no ML server is needed.** Immich computes
embeddings on a GPU host, which cannot run in CI — but the sidecar only ever
*reads* `smart_search`. So the harness INSERTS the vectors itself, which means the
tests control them exactly and can assert exact distances instead of "roughly
similar". A 2-dimensional unit vector is enough to place a photo anywhere on the
circle; cosine distance does not care about the dimensionality.

Everything is skipped unless `IMMICH_CLIP_IT_URL` is set, so a plain `pytest`
stays offline. See README.md in this directory for the compose harness.
"""

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from immich_client import Immich

URL = os.environ.get("IMMICH_CLIP_IT_URL", "")
DSN = os.environ.get("IMMICH_CLIP_IT_DSN", "")
ADMIN_EMAIL = os.environ.get("IMMICH_CLIP_IT_EMAIL", "admin@integration.test")
ADMIN_PASSWORD = os.environ.get("IMMICH_CLIP_IT_PASSWORD", "integration-password")

pytestmark = pytest.mark.integration

# A 1x1 PNG. The content is irrelevant — no inference runs, and the embeddings
# are inserted by hand — but Immich insists on a decodable image.
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100ffff0300000600"
    "05570b6b0000000049454e44ae426082"
)


HERE = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    """Skip this directory's tests when there is nothing to talk to.

    ⚠️ pytest hands this hook EVERY collected item, not just the ones under this
    conftest — so it has to filter by path. Without that, `pytest` at the repo
    root skipped all 198 tests and reported success.
    """
    if URL and DSN:
        return
    skip = pytest.mark.skip(
        reason="set IMMICH_CLIP_IT_URL and IMMICH_CLIP_IT_DSN (see tests/integration/README.md)")
    for item in items:
        if HERE in Path(str(item.fspath)).parents:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def admin():
    """An Immich logged in as the admin, with an API key minted for them."""
    client = Immich(URL)
    client.wait_until_up()
    client.admin_signup(ADMIN_EMAIL, ADMIN_PASSWORD)
    client.login(ADMIN_EMAIL, ADMIN_PASSWORD)
    client.api_key = client.create_api_key(f"it-{uuid.uuid4().hex[:8]}")
    return client


@pytest.fixture(scope="session")
def second_user():
    """A second Immich user with their own key.

    Minted by logging in AS them, because there is no admin path to create a key
    for someone else — the constraint that makes per-owner keys a configuration
    problem rather than something the sidecar could work out for itself.
    """
    email = "second@integration.test"
    password = "second-password"
    admin_client = Immich(URL)
    admin_client.wait_until_up()
    admin_client.admin_signup(ADMIN_EMAIL, ADMIN_PASSWORD)
    admin_client.login(ADMIN_EMAIL, ADMIN_PASSWORD)
    status, _ = admin_client.post(
        "/api/admin/users", {"email": email, "password": password, "name": "Second"})
    assert status in (200, 201, 400), status  # 400 = already exists, fine on re-run
    client = Immich(URL)
    client.login(email, password)
    client.api_key = client.create_api_key("it-second")
    return client


@pytest.fixture(scope="session")
def db():
    psycopg2 = pytest.importorskip("psycopg2")
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture
def cur(db):
    c = db.cursor()
    yield c
    c.close()


@pytest.fixture
def image(tmp_path):
    """A factory for uploadable 1x1 PNGs with distinct names."""
    def make(name="fixture"):
        p = tmp_path / f"{name}-{uuid.uuid4().hex[:8]}.png"
        p.write_bytes(PNG_1X1)
        return str(p)
    return make


@pytest.fixture
def embed(cur):
    """Give an asset an exact embedding — the trick that removes the ML server.

    Immich creates the `smart_search` row only when its own job runs, so this both
    inserts and updates. Vectors are unit-length 2-D, so cosine distance between
    two of them is a number the test picked.
    """
    def set_vector(asset_id, vector):
        literal = "[" + ",".join(repr(float(x)) for x in vector) + "]"
        cur.execute(
            'INSERT INTO smart_search ("assetId", embedding) VALUES (%s, %s::vector) '
            'ON CONFLICT ("assetId") DO UPDATE SET embedding = EXCLUDED.embedding',
            (asset_id, literal),
        )
    return set_vector


@pytest.fixture
def unembed(cur):
    """Take an embedding away — how "not embedded yet" is staged."""
    def clear(asset_id):
        cur.execute('DELETE FROM smart_search WHERE "assetId" = %s', (asset_id,))
    return clear


@pytest.fixture
def settings_for(tmp_path, admin):
    """Settings pointed at the live Immich, with per-test state."""
    from immich_clip.config import ApiKey, Settings

    def make(**kw):
        keys = kw.pop("keys", (ApiKey("*", admin.api_key),))
        return Settings(
            immich_url=URL, db_url=DSN, keys=tuple(keys),
            state_dir=str(tmp_path), poll_sec=0, **kw)
    return make


@pytest.fixture
def sidecar(settings_for):
    """The sidecar, in-process, driven exactly as the plugin drives it.

    In-process rather than over HTTP on purpose: the HTTP layer is 30 lines of
    `http.server` covered by unit tests, while everything interesting — the SQL,
    the album writes, the audit reads — is what these tests are here to exercise
    against a real server.
    """
    from immich_clip import sidecar as mod

    def classify(request, cfg=None):
        return mod.handle(cfg or settings_for(), request)
    return classify


@pytest.fixture(scope="session")
def built_plugin(tmp_path_factory):
    """Build the wasm plugin, or skip when the toolchain is absent.

    `extism-js` is a Rust/Wasm toolchain that not every contributor will have;
    the tests that need it say so rather than failing.
    """
    import shutil

    if not shutil.which("extism-js"):
        pytest.skip("extism-js not on PATH — cannot build the plugin here")
    out = tmp_path_factory.mktemp("plugins")
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [str(root / "plugin" / "build.sh"), str(out)],
        check=True, capture_output=True,
        env={**os.environ, "SIDECAR_URL": os.environ.get(
            "IMMICH_CLIP_IT_SIDECAR", "http://immich-clip-filter:8351/classify")},
    )
    return out


def wait_for(predicate, timeout=30, interval=0.5):
    """Poll until true — Immich does plenty of its work asynchronously."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


sys.path.insert(0, str(Path(__file__).parent))
