"""End-to-end against a real Immich: real uploads, real albums, real pgvector.

Every case below is a bug that was already paid for once on a live library. The
unit suite pins the logic; this pins the *assumptions* — that `album_asset` is
where membership lives, that a removal really does land in `album_asset_audit`
synchronously, that an API key really cannot touch another user's assets, and that
a rebuilt plugin really does need a changed manifest.

No ML server runs. The harness inserts the embeddings itself (see conftest), so
distances are exact and every threshold in this file is a number the test chose.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from immich_clip import backfill, drain, exclusions, queue, store
from immich_clip.config import ApiKey

# Unit vectors on a circle. cos distance = 1 - cos(angle), so:
#   NORTH vs NORTH   -> 0.0     (identical)
#   NORTH vs NE      -> ~0.293  (45°)
#   NORTH vs EAST    -> 1.0     (90°)
NORTH = [0.0, 1.0]
NORTHEAST = [0.7071067811865476, 0.7071067811865476]
EAST = [1.0, 0.0]


@pytest.fixture
def seeds(admin, image, embed):
    """A seed album of two photos that both point NORTH."""
    ids = [admin.upload(image("seed")) for _ in range(2)]
    for asset_id in ids:
        embed(asset_id, NORTH)
    album_id = admin.create_album("IT seeds", ids)
    return {"album": "IT seeds", "albumId": album_id, "assetIds": ids}


@pytest.fixture
def target(admin):
    return admin.create_album("IT target")


def a_request(asset_id, album_id, seed_album, threshold=0.1, wait=0):
    """The payload the WASM step sends, verbatim."""
    return {
        "assetId": asset_id,
        "seedAlbum": seed_album,
        "scoring": "nearest",
        "threshold": threshold,
        "waitSec": wait,
        "albumIds": [album_id],
    }


def members(cur, album_id):
    cur.execute('SELECT "assetId" FROM album_asset WHERE "albumId" = %s', (album_id,))
    return {str(r[0]) for r in cur.fetchall()}


# ── 1. a match is filed, exactly once ────────────────────────────────────────
def test_a_matching_photo_is_filed_into_the_target_album(
        admin, image, embed, seeds, target, sidecar, cur):
    asset = admin.upload(image("match"))
    embed(asset, NORTH)   # identical to the seeds -> distance 0

    result = sidecar(a_request(asset, target, seeds["album"]))

    assert result["match"] is True
    assert result["distance"] == 0.0
    assert result["filed"] == 1
    assert asset in members(cur, target)


def test_filing_the_same_photo_twice_does_not_duplicate_it(
        admin, image, embed, seeds, target, sidecar, cur):
    asset = admin.upload(image("twice"))
    embed(asset, NORTH)

    first = sidecar(a_request(asset, target, seeds["album"]))
    second = sidecar(a_request(asset, target, seeds["album"]))

    assert first["filed"] == 1
    # Immich reports an id already in the album as success:false, so the count is
    # "newly added" — the album is what matters, and it holds one row.
    assert second["filed"] == 0
    assert len(members(cur, target)) == 1


# ── 2. over threshold is not filed ───────────────────────────────────────────
def test_a_photo_past_the_threshold_is_not_filed(
        admin, image, embed, seeds, target, sidecar, cur):
    asset = admin.upload(image("far"))
    embed(asset, EAST)   # 90° away -> distance 1.0

    result = sidecar(a_request(asset, target, seeds["album"], threshold=0.1))

    assert result["match"] is False
    assert result["distance"] == pytest.approx(1.0, abs=1e-6)
    assert result["reason"] == "over threshold"
    assert asset not in members(cur, target)


def test_the_threshold_is_read_against_the_nearest_seed_not_their_average(
        admin, image, embed, seeds, target, sidecar):
    """The Eiffel-Tower case, staged exactly.

    Two seeds 90° apart average to a vector between them. A photo sitting ON that
    average is 0 from the centroid and 0.293 from either seed — so centroid
    scoring matches and nearest scoring does not. That is the whole reason the two
    modes exist, and it is why a threshold does not carry between them.
    """
    from immich_clip.config import Settings

    a, b = seeds["assetIds"]
    embed(a, NORTH)
    embed(b, EAST)
    asset = admin.upload(image("between"))
    embed(asset, NORTHEAST)

    # albumIds empty: this test is about the distance, not about filing.
    ask = dict(a_request(asset, None, seeds["album"], threshold=0.1), albumIds=[])
    nearest = sidecar(ask)
    centroid = sidecar(dict(ask, scoring="centroid"))

    assert nearest["match"] is False
    assert nearest["distance"] == pytest.approx(0.2929, abs=1e-3)
    assert centroid["match"] is True
    assert centroid["distance"] == pytest.approx(0.0, abs=1e-6)


# ── 3. no embedding yet is queued, not a "no" ────────────────────────────────
def test_an_unembedded_photo_is_queued_rather_than_answered_no(
        admin, image, unembed, seeds, target, sidecar, settings_for, cur):
    asset = admin.upload(image("unembedded"))
    unembed(asset)   # Immich has not run its SmartSearch job for this one
    cfg = settings_for()

    result = sidecar(a_request(asset, target, seeds["album"], wait=1), cfg=cfg)

    assert result["match"] is False
    assert result["undecided"] is True
    assert result["queued"] is True
    assert asset not in members(cur, target)

    conn = queue.connect(cfg.queue_db)
    try:
        assert [r["assetId"] for r in queue.pending(conn)] == [asset]
    finally:
        conn.close()


# ── 4. the embedding turns up, and the drainer finishes the job ──────────────
def test_a_queued_photo_is_filed_once_immich_catches_up(
        admin, image, embed, unembed, seeds, target, sidecar, settings_for, cur):
    asset = admin.upload(image("late"))
    unembed(asset)
    cfg = settings_for(apply=True)

    queued = sidecar(a_request(asset, target, seeds["album"], wait=1), cfg=cfg)
    assert queued["queued"] is True
    assert asset not in members(cur, target)

    # The GPU host wakes up and Immich embeds it.
    embed(asset, NORTH)
    drain.run(cfg, now=1_000_000)

    assert asset in members(cur, target)
    conn = queue.connect(cfg.queue_db)
    try:
        assert queue.count(conn) == 0   # decided is decided
    finally:
        conn.close()


# ── 5. fails closed when it cannot reach what it needs ───────────────────────
def test_an_unreachable_database_is_a_clean_no_not_an_exception(
        admin, image, embed, seeds, target, sidecar, settings_for, cur):
    """A false negative loses one photo; a false positive files the camera roll.

    The plugin turns any non-`match: true` body into `workflow.continue: false`,
    so what matters here is that the sidecar answers at all, with `match` false,
    rather than raising and giving the plugin a transport error to interpret.
    """
    asset = admin.upload(image("closed"))
    embed(asset, NORTH)
    broken = settings_for().with_(db_url="postgresql://nobody@127.0.0.1:1/nothing")

    result = sidecar(a_request(asset, target, seeds["album"]), cfg=broken)

    assert result["match"] is False
    assert "database unreachable" in result["reason"]
    assert "queued" not in result       # not queued either: this is not "undecided"
    assert asset not in members(cur, target)


# ── 6. a removal sticks, live and through the sweep ──────────────────────────
def test_a_photo_taken_out_by_hand_is_not_refiled_by_the_live_path(
        admin, image, embed, seeds, target, sidecar, settings_for, cur):
    """The regression that closed a 15-minute window.

    `sync_from_audit` used to run only on the drain tick, so a photo removed by
    hand could be refiled by the next trigger before the drainer learned about it.
    Immich writes the audit row synchronously with the removal, so the live path
    reads it too — this is that sequence, at full speed.
    """
    asset = admin.upload(image("removed"))
    embed(asset, NORTH)
    cfg = settings_for()

    sidecar(a_request(asset, target, seeds["album"]), cfg=cfg)
    assert asset in members(cur, target)

    admin.remove_from_album(target, [asset])
    assert asset not in members(cur, target)

    again = sidecar(a_request(asset, target, seeds["album"]), cfg=cfg)

    assert again["match"] is True        # the verdict is unchanged...
    assert again["filed"] == 0           # ...but it is not put back
    assert again["excluded"] == 1
    assert asset not in members(cur, target)


def test_a_removal_also_survives_the_full_backfill_sweep(
        admin, image, embed, seeds, target, sidecar, settings_for, cur):
    # The sweep excludes only CURRENT members, so without the exclusion table a
    # removed match is simply a fresh candidate again.
    asset = admin.upload(image("swept"))
    embed(asset, NORTH)
    cfg = settings_for()

    sidecar(a_request(asset, target, seeds["album"]), cfg=cfg)
    admin.remove_from_album(target, [asset])

    args = backfill.parse_args([
        "--seed-album", seeds["album"], "--album", "IT target",
        "--threshold", "0.1", "--apply"])
    backfill.run(cfg, args)

    assert asset not in members(cur, target)


# ── 7. putting it back clears the exclusion ──────────────────────────────────
def test_re_adding_a_photo_by_hand_forgets_the_exclusion(
        admin, image, embed, seeds, target, sidecar, settings_for, cur):
    # Otherwise one accidental removal bans the photo for good and the only cure
    # is editing a SQLite file.
    asset = admin.upload(image("readded"))
    embed(asset, NORTH)
    cfg = settings_for()

    sidecar(a_request(asset, target, seeds["album"]), cfg=cfg)
    admin.remove_from_album(target, [asset])
    sidecar(a_request(asset, target, seeds["album"]), cfg=cfg)   # learns the removal

    state = queue.connect(cfg.queue_db)
    try:
        assert exclusions.for_album(state, target) == {asset}
        admin.add_to_album(target, [asset])
        exclusions.sync_from_audit(state, cur, [target], now=0)
        assert exclusions.for_album(state, target) == set()
    finally:
        state.close()


# ── 8. two rules, one asset, decided independently ───────────────────────────
def test_two_rules_watching_the_same_library_do_not_clobber_each_other(
        admin, image, embed, unembed, seeds, target, sidecar, settings_for):
    """Both park the same undecidable asset; neither may lose its verdict.

    Under the original assetId-only primary key the second enqueue silently
    overwrote the first.
    """
    other_ids = [admin.upload(image("otherseed"))]
    embed(other_ids[0], EAST)
    admin.create_album("IT seeds east", other_ids)
    second_target = admin.create_album("IT target east")

    asset = admin.upload(image("both"))
    unembed(asset)
    cfg = settings_for()

    sidecar(a_request(asset, target, seeds["album"], wait=1), cfg=cfg)
    sidecar(a_request(asset, second_target, "IT seeds east", wait=1), cfg=cfg)

    conn = queue.connect(cfg.queue_db)
    try:
        parked = {r["profile"]: r for r in queue.pending(conn)}
        assert set(parked) == {"album:IT seeds", "album:IT seeds east"}
        assert parked["album:IT seeds east"]["albumIds"] == [second_target]
    finally:
        conn.close()


# ── 9. another user's asset ──────────────────────────────────────────────────
def test_a_key_cannot_file_another_users_photo_and_says_which_user(
        admin, second_user, image, embed, seeds, target, sidecar, settings_for, cur):
    """The multi-user gap, reproduced against a real permission boundary.

    A workflow is owned by whoever created it, and a key can only act on its own
    owner's assets. With one key this looked like `match: true, filed: 0`.
    """
    asset = second_user.upload(image("theirs"))
    embed(asset, NORTH)
    only_admin = settings_for(keys=(ApiKey(admin.me()["email"], admin.api_key),))

    result = sidecar(a_request(asset, target, seeds["album"]), cfg=only_admin)

    assert result["match"] is True
    assert result["filed"] == 0
    assert second_user.me()["email"] in result["ownerError"]
    assert asset not in members(cur, target)


def test_with_that_users_key_configured_their_photo_files_into_their_album(
        admin, second_user, image, embed, seeds, sidecar, settings_for, cur):
    their_target = second_user.create_album("IT target theirs")
    asset = second_user.upload(image("theirs-ok"))
    embed(asset, NORTH)
    both = settings_for(keys=(
        ApiKey(admin.me()["email"], admin.api_key),
        ApiKey(second_user.me()["email"], second_user.api_key),
    ))

    result = sidecar(a_request(asset, their_target, seeds["album"]), cfg=both)

    assert result["filed"] == 1
    assert asset in members(cur, their_target)


# ── 10. the manifest-hash trap ───────────────────────────────────────────────
def test_a_rebuilt_plugin_produces_a_different_manifest(built_plugin, tmp_path):
    """Immich keys the import on the SHA-256 of manifest.json's CONTENTS.

    A rebuilt wasm under an unchanged manifest is silently ignored and the old
    bytes keep running — which is a whole afternoon of "my fix did nothing". The
    build embeds a source hash in `description` so the manifest always moves with
    the sources; if that stops being true, this fails.
    """
    first = json.loads((built_plugin / "clip-filter" / "manifest.json").read_text())

    second_out = tmp_path / "plugins2"
    build = Path(__file__).resolve().parents[2] / "plugin" / "build.sh"
    subprocess.run(
        [str(build), str(second_out)], check=True, capture_output=True,
        env={**os.environ, "SIDECAR_URL": "http://somewhere-else:9999/classify"},
    )
    second = json.loads((second_out / "clip-filter" / "manifest.json").read_text())

    assert first["version"] == second["version"], "the version must NOT move — see manifest.py"
    assert first["description"] != second["description"], "the manifest must move with the build"
    assert second["methods"][0]["allowedHosts"] == ["somewhere-else"]


def test_the_manifest_validates_against_immichs_own_rules(built_plugin):
    """Nested schema properties REQUIRE title and description.

    In `dtos/json-schema.dto.js` only the top level makes them optional, so a
    property without them fails zod validation and Immich skips the plugin with a
    bare `Invalid plugin manifest` warning rather than an error.
    """
    manifest = json.loads((built_plugin / "clip-filter" / "manifest.json").read_text())
    for method in manifest["methods"]:
        for name, prop in method["schema"]["properties"].items():
            assert prop.get("title"), f"{name} has no title"
            assert prop.get("description"), f"{name} has no description"
        assert method["hostFunctions"] is True   # else httpRequest is a stub that throws
        assert method["allowedHosts"], "the host checks this before calling out"
