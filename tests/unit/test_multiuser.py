"""Filing on a shared library — the gap that made rules match and file nothing.

An Immich workflow is always owned by whoever created it (`ownerId: auth.user.id`,
no impersonation), and an API key can only put its own owner's assets into its own
owner's album. With one key, a second user's photos score correctly, return
`match: true`, and then file nothing: Immich answers `no_permission` per id, which
surfaces as a counter and not as an error.

That was a real deployment where one user's rules worked and another's silently
did not. These tests pin the fix: resolve the asset's owner, use that owner's key,
and when there is no key for them say so instead of returning zero.
"""

import pytest
from fakes import ALBUM, ASSET, OTHER, FakeConn, FakeCursor, FakeOpener, json_reply, settings

from immich_clip import api, backfill, queue, store
from immich_clip import sidecar as clip_filter
from immich_clip.config import ApiKey

NICO = "nico@example.com"
ALFIE = "alfie@example.com"


def two_users():
    return settings(keys=[ApiKey(NICO, "nico-key"), ApiKey(ALFIE, "alfie-key")])


# ── resolving the owner ──────────────────────────────────────────────────────
def test_the_owners_email_is_preferred_over_the_raw_id():
    # A human writing a config file knows the email, not the UUID.
    cur = FakeCursor([[("owner-1", NICO)]])
    assert store.asset_owner(cur, ASSET) == ("owner-1", NICO)


def test_an_owner_with_no_user_row_still_yields_an_id():
    cur = FakeCursor([[("owner-1", None)]])
    assert store.asset_owner(cur, ASSET) == ("owner-1", None)


def test_an_unknown_asset_has_no_owner():
    assert store.asset_owner(FakeCursor([[]]), ASSET) == (None, None)


def test_owners_are_looked_up_in_one_query_for_a_batch():
    cur = FakeCursor([[(ASSET, "o1", NICO), (OTHER, "o2", ALFIE)]])
    assert store.asset_owners(cur, [ASSET, OTHER]) == {ASSET: NICO, OTHER: ALFIE}
    assert "ANY(%s::uuid[])" in cur.sql[0][0]


def test_no_assets_means_no_query_at_all():
    cur = FakeCursor([])
    assert store.asset_owners(cur, []) == {}
    assert cur.sql == []


# ── choosing the key ─────────────────────────────────────────────────────────
def test_each_owners_assets_are_filed_with_that_owners_key():
    cfg = two_users()
    assert api.headers(cfg, NICO) == {"x-api-key": "nico-key"}
    assert api.headers(cfg, ALFIE) == {"x-api-key": "alfie-key"}


def test_an_owner_with_no_key_is_named_rather_than_silently_skipped():
    with pytest.raises(api.NoKeyForOwner) as e:
        api.headers(two_users(), "stranger@example.com")
    assert "stranger@example.com" in str(e.value)


def test_a_call_with_no_owner_does_not_raise_even_with_no_keys():
    # The CLI tools and system-config reads pass no owner; an empty key gets a
    # 401 from Immich, which is a clearer failure than a different exception for
    # the same thing.
    assert api.headers(settings()) == {"x-api-key": ""}


# ── the live path ────────────────────────────────────────────────────────────
def test_a_match_is_filed_with_the_key_of_whoever_owns_the_photo(tmp_path, q):
    cfg = two_users()
    # Postgres: no removals recorded, album membership empty, and the asset
    # belongs to Alfie.
    pg = FakeConn(FakeCursor([[], [("alfie-id", ALFIE)]]))
    seen = []

    result = clip_filter.handle(
        cfg, {"assetId": ASSET, "albumIds": [ALBUM]},
        classify_fn=lambda r: {"match": True, "distance": 0.1},
        queue_conn=q, connect=lambda c: pg,
        add_assets=lambda c, album, ids, opener=None, log=None, owner=None: (
            seen.append(owner) or 1),
        now=1000)

    assert result["filed"] == 1
    assert seen == [ALFIE]


def test_a_photo_owned_by_someone_with_no_key_says_so_instead_of_filing_nothing(tmp_path, q):
    # The exact shape of the original bug, now visible in the verdict rather than
    # only as `filed: 0` next to `match: true`.
    cfg = settings(keys=[ApiKey(NICO, "nico-key")])
    pg = FakeConn(FakeCursor([[], [("stranger-id", "stranger@example.com")]]))

    result = clip_filter.handle(
        cfg, {"assetId": ASSET, "albumIds": [ALBUM]},
        classify_fn=lambda r: {"match": True, "distance": 0.1},
        queue_conn=q, connect=lambda c: pg, now=1000)

    assert result["match"] is True       # the verdict is still correct
    assert result["filed"] == 0
    assert "stranger@example.com" in result["ownerError"]


def test_when_the_owner_cannot_be_resolved_filing_still_uses_the_default_key(tmp_path, q):
    # Postgres being unreachable must not stop a single-key install from working;
    # the drain pass would only hit the same problem.
    cfg = settings(api_key="k")
    seen = []

    def refuse(_cfg):
        raise OSError("postgres is down")

    result = clip_filter.handle(
        cfg, {"assetId": ASSET, "albumIds": [ALBUM]},
        classify_fn=lambda r: {"match": True, "distance": 0.1},
        queue_conn=q, connect=refuse,
        add_assets=lambda c, album, ids, opener=None, log=None, owner=None: (
            seen.append(owner) or 1),
        now=1000)

    assert result["filed"] == 1
    assert seen == [None]


# ── the deferred path ────────────────────────────────────────────────────────
def test_a_queued_verdict_for_an_unkeyed_owner_stays_queued(tmp_path, q):
    """It is not resolved, because it is not done.

    Dropping it would mean the photo is never filed even after the missing key is
    configured. Leaving it means the next pass files it, with nothing to re-run by
    hand.
    """
    from immich_clip import drain

    store.save_profile(tmp_path, "food", "m", [1.0, 0.0], {"kind": "test"}, now=0)
    queue.enqueue(q, ASSET, "food", 0.3, [ALBUM], now=1000)
    cfg = settings(queue_db=str(tmp_path / "pending.sqlite"), profile_dir=str(tmp_path),
                   state_dir=str(tmp_path), model="m", ml_url="http://ml:3003",
                   apply=True, keys=[ApiKey(NICO, "nico-key")])
    conn = FakeConn(FakeCursor([
        [],                                  # audit: nothing removed
        [(ASSET,)],                          # the asset still exists
        [(0.12,)],                           # and it matches
        [("stranger-id", "stranger@example.com")],   # but is not Nico's
    ]))

    drain.run(cfg, connect=lambda c: conn, opener=FakeOpener([json_reply({})]),
              queue_conn=q, now=2000)

    assert queue.count(q) == 1


# ── the sweep ────────────────────────────────────────────────────────────────
def test_the_backfill_groups_its_hits_by_owner(monkeypatch):
    cfg = two_users()
    cur = FakeCursor([[(ASSET, "o1", NICO), (OTHER, "o2", ALFIE)]])
    calls = []

    def fake_add(c, album, ids, opener=None, log=None, owner=None):
        calls.append((owner, ids))
        return len(ids)

    monkeypatch.setattr(api, "add_assets", fake_add)
    added = backfill.file_by_owner(
        cfg, cur, "album-1", [ASSET, OTHER], log=lambda m: None)

    # One request per owner, each with that owner's key — not one request that
    # half fails.
    assert added == 2
    assert sorted(calls) == [(ALFIE, [OTHER]), (NICO, [ASSET])]


def test_the_backfill_reports_the_owners_it_had_to_skip(monkeypatch):
    cfg = settings(keys=[ApiKey(NICO, "nico-key")])
    cur = FakeCursor([[(ASSET, "o1", NICO), (OTHER, "o2", "stranger@example.com")]])
    lines = []

    def fake_add(c, album, ids, opener=None, log=None, owner=None):
        if owner == "stranger@example.com":
            raise api.NoKeyForOwner("no key for stranger@example.com")
        return len(ids)

    monkeypatch.setattr(api, "add_assets", fake_add)
    added = backfill.file_by_owner(cfg, cur, "album-1", [ASSET, OTHER], log=lines.append)

    assert added == 1
    assert any("stranger@example.com" in line for line in lines)
