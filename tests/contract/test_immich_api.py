"""Contract tests: do the REST endpoints this project drives still exist?

The schema tests next door guard the half that reads Postgres. This guards the
half that writes through the API — and, just as importantly, the endpoints the
integration harness itself drives. A harness that 404s on `POST /api/api-keys`
after an upgrade fails in a way that looks like a broken feature.

    IMMICH_CLIP_CONTRACT_URL=http://127.0.0.1:2283 \
    IMMICH_CLIP_CONTRACT_KEY=... pytest tests/contract -v

**Nothing here creates, modifies or deletes anything.** Every probe sends a
deliberately empty body, so a route that exists answers "Validation failed" and a
route that is gone answers something else entirely — see `route_exists`. The two
GET probes that could return data are asserted on shape, not content.

Skipped when those variables are unset, so `pytest` with no arguments stays
offline.
"""

import json
import os
import urllib.error
import urllib.request

import pytest

URL = os.environ.get("IMMICH_CLIP_CONTRACT_URL", "").rstrip("/")
KEY = os.environ.get("IMMICH_CLIP_CONTRACT_KEY", "")

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(
        not (URL and KEY),
        reason="set IMMICH_CLIP_CONTRACT_URL and IMMICH_CLIP_CONTRACT_KEY to run",
    ),
]

#: (method, path, who needs it). The empty body is the point: it makes every
#: probe harmless while still distinguishing a live route from a dead one.
ENDPOINTS = [
    ("GET", "/api/server/ping", "harness: waits for Immich to boot"),
    ("GET", "/api/system-config", "clip_model, ml_url"),
    ("GET", "/api/users/me", "doctor: proves a key belongs to who it says"),
    ("PUT", "/api/jobs/smartSearch", "drain: kicks the embedding backlog"),
    ("POST", "/api/albums", "backfill --create-album"),
    ("PUT", "/api/albums/{album}/assets", "filing — the whole point"),
    ("GET", "/api/albums/{album}", "harness: reads membership back"),
    ("DELETE", "/api/albums/{album}/assets", "harness: stages a hand-removal"),
    ("POST", "/api/assets", "harness: uploads fixtures"),
    ("POST", "/api/auth/admin-sign-up", "harness: bootstraps the admin"),
    ("POST", "/api/auth/login", "harness: logs in as the second user"),
    ("GET", "/api/api-keys", "harness: mints a key per user"),
    ("GET", "/api/admin/users", "harness: creates the second user"),
]

# An id that cannot exist, so a route that IS there fails on the resource rather
# than doing anything.
NOWHERE = "00000000-0000-0000-0000-000000000000"


def call(method, path, timeout=60):
    """Returns (status, parsed-or-raw-body). Never raises for an HTTP error."""
    req = urllib.request.Request(
        f"{URL}{path.format(album=NOWHERE)}",
        data=b"{}",
        headers={"x-api-key": KEY, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read().decode(), resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(), e.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw


def route_exists(body):
    """Is this a missing ROUTE, or merely a missing/invalid resource?

    NestJS answers an unrouted request with `{"message": "Cannot GET /api/foo"}`.
    Anything else — a validation error, a permission error, a real payload, a
    JSON array — means the route is there and only the (deliberately empty)
    request was wrong. That distinction is what makes these probes both safe and
    meaningful.
    """
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message.startswith("Cannot "):
            return False
    return True


@pytest.mark.parametrize(
    "method,path,why", ENDPOINTS, ids=[f"{m} {p}" for m, p, _ in ENDPOINTS]
)
def test_the_endpoint_still_exists(method, path, why):
    status, body = call(method, path)
    assert route_exists(body), (
        f"{method} {path} is gone from this Immich — needed for {why}. "
        f"HTTP {status}: {str(body)[:200]}"
    )


def test_the_probe_would_notice_a_route_that_really_is_missing():
    # Guards the guard. A probe that passes vacuously is worse than none, and
    # `route_exists` returning True by default makes that the failure mode.
    status, body = call("GET", "/api/a-route-immich-will-never-have")
    assert status == 404
    assert route_exists(body) is False


# ── shapes we actually depend on ─────────────────────────────────────────────
def test_the_server_reports_a_version_we_can_put_in_the_matrix():
    _, body = call("GET", "/api/server/version")
    assert {"major", "minor"} <= set(body)
    print(f"\n  Immich {body['major']}.{body['minor']}.{body.get('patch')}")


def test_system_config_still_carries_the_clip_model_and_ml_urls():
    # Both are read rather than configured, so that a stored centroid and the
    # server it is compared against cannot drift apart.
    _, body = call("GET", "/api/system-config")
    ml = body["machineLearning"]
    assert isinstance(ml["clip"]["modelName"], str) and ml["clip"]["modelName"]
    assert isinstance(ml["urls"], list)


def test_users_me_still_carries_the_email_per_owner_keys_match_on():
    _, body = call("GET", "/api/users/me")
    assert "@" in body["email"]


def test_an_album_read_back_through_the_api_still_hides_its_assets():
    """The quirk that sends membership reads to Postgres instead.

    As of 3.1 `GET /api/albums/{id}` reports `assetCount` but hands back an empty
    `assets` list. If this ever starts failing, the API became usable for
    membership and `store.album_asset_ids` could be simplified — so it is worth
    knowing about, and it is not an error either way.
    """
    status, body = call("GET", "/api/albums")
    if status != 200 or not isinstance(body, list) or not body:
        pytest.skip("no albums in this library")
    album_id = body[0]["id"]
    _, one = call("GET", f"/api/albums/{album_id}")
    assert "assetCount" in one
    if one.get("assets"):
        pytest.skip(
            "this Immich DOES return album assets — store.album_asset_ids could "
            "now use the API; see docs/limitations.md"
        )
