"""The deferred half: pending queue + drainer.

CLIP inference runs on a GPU host that is often off, so "no embedding yet" is the
COMMON case, not the edge case. What these tests pin down is that an undecidable
asset is parked rather than answered "no" — and that a drain pass later finishes
it, files it, and asks Immich to embed the backlog it otherwise never retries.
"""

import json

from fakes import ALBUM, ASSET, OTHER, FakeConn, FakeCursor, FakeOpener, FakeResponse, json_reply, settings

from immich_clip import api, drain, exclusions, queue, store
from immich_clip import sidecar as clip_filter
from immich_clip.config import load


def a_profile(tmp_path, name="food", model="m", vector=(1.0, 0.0)):
    store.save_profile(tmp_path, name, model, list(vector), {"kind": "test"}, now=0)


# ── the queue ────────────────────────────────────────────────────────────────
def test_an_undecidable_asset_round_trips_through_the_queue(q):
    queue.enqueue(q, ASSET, "food", 0.3, [ALBUM], now=1000)
    assert queue.pending(q) == [{
        "assetId": ASSET, "profile": "food", "threshold": 0.3,
        "albumIds": [ALBUM], "enqueuedAt": 1000, "attempts": 0,
        "seedAlbum": "", "scoring": "",
    }]


def test_two_profiles_park_the_same_asset_independently(q):
    # A `food` rule and a `burgie` rule both watch the library. Under the old
    # assetId-only primary key the second enqueue overwrote the first and one
    # verdict vanished.
    queue.enqueue(q, ASSET, "food", 0.30, [ALBUM], now=1000)
    queue.enqueue(q, ASSET, "burgie", 0.22, ["burgie-album"], now=1000)
    rows = {r["profile"]: r for r in queue.pending(q)}
    assert set(rows) == {"food", "burgie"}
    assert rows["burgie"]["albumIds"] == ["burgie-album"]

    # Resolving one must not discard the other.
    queue.resolve(q, ASSET, "food")
    assert [r["profile"] for r in queue.pending(q)] == ["burgie"]


def test_a_legacy_assetid_keyed_queue_is_migrated_without_losing_rows(tmp_path):
    import sqlite3
    db = str(tmp_path / "legacy.sqlite")
    old = sqlite3.connect(db)
    old.executescript("""
        CREATE TABLE pending (
            assetId TEXT PRIMARY KEY, profile TEXT NOT NULL, threshold REAL NOT NULL,
            albumIds TEXT NOT NULL, enqueuedAt INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0);
        INSERT INTO pending VALUES ('a-1','food',0.3,'["x"]',1000,2);
    """)
    old.commit(); old.close()

    conn = queue.connect(db)
    assert "PRIMARY KEY (assetId, profile)" in conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='pending'").fetchone()[0]
    # The parked verdict survives the migration — dropping it is the exact
    # failure the queue exists to prevent.
    assert queue.pending(conn) == [{
        "assetId": "a-1", "profile": "food", "threshold": 0.3,
        "albumIds": ["x"], "enqueuedAt": 1000, "attempts": 2,
        "seedAlbum": "", "scoring": ""}]


def test_requeuing_the_same_asset_updates_it_rather_than_duplicating(q):
    # A metadata refresh re-fires the workflow; the row must not double, but an
    # edited threshold or target album should take effect.
    queue.enqueue(q, ASSET, "food", 0.3, [ALBUM], now=1000)
    queue.enqueue(q, ASSET, "food", 0.25, ["other-album"], now=2000)
    rows = queue.pending(q)
    assert len(rows) == 1
    assert rows[0]["threshold"] == 0.25
    assert rows[0]["albumIds"] == ["other-album"]


def test_the_queue_is_drained_oldest_first(q):
    queue.enqueue(q, OTHER, "food", 0.3, [], now=2000)
    queue.enqueue(q, ASSET, "food", 0.3, [], now=1000)
    assert [r["assetId"] for r in queue.pending(q)] == [ASSET, OTHER]


def test_resolving_removes_the_entry(q):
    queue.enqueue(q, ASSET, "food", 0.3, [], now=1000)
    queue.resolve(q, ASSET, "food")
    assert queue.count(q) == 0


def test_entries_that_will_never_embed_age_out_and_are_reported(q):
    queue.enqueue(q, ASSET, "food", 0.3, [], now=0)
    queue.enqueue(q, OTHER, "food", 0.3, [], now=30 * 86400)
    dropped = queue.expire(q, max_age_days=30, now=30 * 86400 + 1)
    assert dropped == [ASSET]
    assert [r["assetId"] for r in queue.pending(q)] == [OTHER]


def test_assets_deleted_since_being_queued_are_dropped(q):
    queue.enqueue(q, ASSET, "food", 0.3, [], now=0)
    queue.enqueue(q, OTHER, "food", 0.3, [], now=0)
    gone = queue.drop_missing(q, {ASSET}, [ASSET, OTHER])
    assert gone == [OTHER]
    assert queue.count(q) == 1


# ── the sidecar's three-way outcome ──────────────────────────────────────────
def test_an_unembedded_asset_is_queued_not_answered_no(tmp_path, q):
    a_profile(tmp_path)
    cfg = settings(profile_dir=str(tmp_path), model="m", poll_sec=0)
    req = {"assetId": ASSET, "profile": "food", "threshold": 0.3,
           "waitSec": 0, "albumIds": [ALBUM]}

    result = clip_filter.handle(
        cfg, req,
        classify_fn=lambda r: clip_filter.classify(
            cfg, r, connect=lambda c: FakeConn(FakeCursor([])),
            sleep=lambda s: None, monotonic=lambda: 0),
        queue_conn=q, now=1000)

    assert result["match"] is False
    assert result["undecided"] is True
    assert result["queued"] is True
    row = queue.pending(q)[0]
    assert row["assetId"] == ASSET and row["albumIds"] == [ALBUM]


def test_queueing_works_from_a_different_thread_than_opened_the_db(tmp_path):
    """Regression: the sidecar runs under ThreadingHTTPServer.

    The first version opened ONE sqlite connection at startup and shared it, so
    every real enqueue died with "SQLite objects created in a thread can only be
    used in that same thread" — and because queueing failures are swallowed to
    keep the workflow alive, it failed silently. Every other test here injects a
    connection, so none of them saw it. This one lets handle() open its own,
    from a worker thread, exactly like production.
    """
    import threading

    a_profile(tmp_path)
    db = str(tmp_path / "pending.sqlite")
    queue.connect(db).close()  # schema created on THIS thread, as main() does
    cfg = settings(profile_dir=str(tmp_path), model="m", queue_db=db)

    out = {}

    def worker():
        out["result"] = clip_filter.handle(
            cfg,
            {"assetId": ASSET, "profile": "food", "threshold": 0.3, "albumIds": [ALBUM]},
            classify_fn=lambda r: {"match": False, "undecided": True, "reason": "x"},
            now=1000)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert out["result"]["queued"] is True, out["result"].get("queueError")
    conn = queue.connect(db)
    assert [r["assetId"] for r in queue.pending(conn)] == [ASSET]


def test_a_genuine_over_threshold_is_a_no_and_is_NOT_queued(tmp_path, q):
    # The distinction that matters: "too far away" is decided, "not embedded" is
    # not. Queueing a decided no would refile it forever.
    a_profile(tmp_path)
    cfg = settings(profile_dir=str(tmp_path), model="m", poll_sec=0)
    req = {"assetId": ASSET, "profile": "food", "threshold": 0.3, "albumIds": [ALBUM]}

    result = clip_filter.handle(
        cfg, req,
        classify_fn=lambda r: {"match": False, "distance": 0.9, "reason": "over threshold"},
        queue_conn=q, now=1000)

    assert result["match"] is False
    assert "queued" not in result
    assert queue.count(q) == 0


def test_a_match_is_filed_immediately_and_reports_how_many_albums(tmp_path, q):
    cfg = settings(profile_dir=str(tmp_path), model="m", api_key="k")
    calls = []

    def fake_add(c, album_id, ids, opener=None, log=None, owner=None):
        calls.append((album_id, ids))
        return 1

    result = clip_filter.handle(
        cfg, {"assetId": ASSET, "albumIds": [ALBUM, "second"]},
        classify_fn=lambda r: {"match": True, "distance": 0.1},
        queue_conn=q, add_assets=fake_add)

    assert result["filed"] == 2
    assert calls == [(ALBUM, [ASSET]), ("second", [ASSET])]


def test_a_failed_album_add_does_not_turn_a_match_into_a_no(tmp_path, q):
    cfg = settings(profile_dir=str(tmp_path), model="m", api_key="k")

    def boom(*a, **k):
        raise OSError("immich is asleep")

    result = clip_filter.handle(
        cfg, {"assetId": ASSET, "albumIds": [ALBUM]},
        classify_fn=lambda r: {"match": True, "distance": 0.1},
        queue_conn=q, add_assets=boom)

    assert result["match"] is True
    assert result["filed"] == 0
    assert "immich is asleep" in result["fileError"]


# ── the drainer ──────────────────────────────────────────────────────────────
def drain_cfg(tmp_path, **kw):
    return settings(queue_db=str(tmp_path / "pending.sqlite"),
                    profile_dir=str(tmp_path), state_dir=str(tmp_path),
                    model="m", api_key="k", ml_url="http://ml-host:3003", **kw)


def test_an_empty_queue_short_circuits_without_touching_the_database(tmp_path, q):
    def explode(_cfg):
        raise AssertionError("must not open Postgres for an empty queue")

    assert drain.run(drain_cfg(tmp_path), connect=explode, queue_conn=q) == 0


def test_a_now_embedded_match_is_filed_and_leaves_the_queue(tmp_path, q, monkeypatch):
    a_profile(tmp_path)
    queue.enqueue(q, ASSET, "food", 0.3, [ALBUM], now=1000)
    conn = FakeConn(FakeCursor([
        [],           # exclusions.sync_from_audit
        [(ASSET,)],   # existing_asset_ids
        [(0.12,)],    # score
    ]))
    filed = []
    monkeypatch.setattr(drain, "file_into_albums",
                        lambda cfg, albums, aid, owner=None: filed.append((albums, aid)) or 1)

    drain.run(drain_cfg(tmp_path, apply=True), connect=lambda c: conn,
              opener=FakeOpener([json_reply({})]), queue_conn=q, now=2000)

    assert filed == [([ALBUM], ASSET)]
    assert queue.count(q) == 0


def test_a_now_embedded_no_match_also_leaves_the_queue(tmp_path, q):
    # Decided is decided — leaving it queued would re-check it forever.
    a_profile(tmp_path)
    queue.enqueue(q, ASSET, "food", 0.3, [ALBUM], now=1000)
    conn = FakeConn(FakeCursor([[], [(ASSET,)], [(0.9,)]]))

    drain.run(drain_cfg(tmp_path, apply=True), connect=lambda c: conn,
              opener=FakeOpener([json_reply({})]), queue_conn=q, now=2000)

    assert queue.count(q) == 0


def test_a_still_unembedded_asset_stays_queued_and_counts_an_attempt(tmp_path, q):
    a_profile(tmp_path)
    queue.enqueue(q, ASSET, "food", 0.3, [ALBUM], now=1000)
    conn = FakeConn(FakeCursor([[], [(ASSET,)], []]))

    drain.run(drain_cfg(tmp_path, apply=True), connect=lambda c: conn,
              opener=FakeOpener([json_reply({})]), queue_conn=q, now=2000)

    rows = queue.pending(q)
    assert len(rows) == 1 and rows[0]["attempts"] == 1


def test_a_dry_run_writes_nothing_and_says_so(tmp_path, q, capsys):
    a_profile(tmp_path)
    queue.enqueue(q, ASSET, "food", 0.3, [ALBUM], now=1000)
    conn = FakeConn(FakeCursor([[], [(ASSET,)], [(0.12,)]]))

    drain.run(drain_cfg(tmp_path), connect=lambda c: conn,
              opener=FakeOpener([json_reply({})]), queue_conn=q, now=2000)

    assert "DRY RUN" in capsys.readouterr().out
    assert queue.count(q) == 1  # not resolved, so a real run still handles it


def test_an_unusable_profile_leaves_its_entries_queued(tmp_path, q, capsys):
    # Built for a different CLIP model: every distance would be meaningless, so
    # park the work rather than filing noise or discarding it.
    store.save_profile(tmp_path, "food", "old-model", [1.0, 0.0], {}, now=0)
    queue.enqueue(q, ASSET, "food", 0.3, [ALBUM], now=1000)
    conn = FakeConn(FakeCursor([[], [(ASSET,)]]))

    drain.run(drain_cfg(tmp_path, apply=True), connect=lambda c: conn,
              opener=FakeOpener([json_reply({})]), queue_conn=q, now=2000)

    assert "unusable" in capsys.readouterr().out
    assert queue.count(q) == 1


# ── kicking Immich's embedding backlog ───────────────────────────────────────
def test_nothing_waiting_means_no_requeue(tmp_path):
    msg = drain.maybe_requeue_embeddings(drain_cfg(tmp_path, apply=True), [], now=0,
                                         opener=FakeOpener([json_reply({})]))
    assert "nothing waiting" in msg


def test_the_backlog_job_is_started_when_assets_are_waiting(tmp_path):
    cfg = drain_cfg(tmp_path, apply=True)
    op = FakeOpener([json_reply({}), json_reply({})])  # /ping, then PUT /api/jobs
    saved = {}
    msg = drain.maybe_requeue_embeddings(cfg, [ASSET], now=10_000, opener=op,
                                         save=lambda p, d: saved.update(d),
                                         load_fn=lambda p, d: {})
    put = op.requests[-1]
    assert put.get_method() == "PUT"
    assert put.full_url.endswith("/api/jobs/smartSearch")
    # force=false is "embed what has no embedding", not "re-embed everything".
    assert json.loads(put.data.decode()) == {"command": "start", "force": False}
    assert saved["lastRequeue"] == 10_000
    assert "requeued" in msg


def test_the_backlog_job_is_rate_limited(tmp_path):
    cfg = drain_cfg(tmp_path, apply=True, requeue_every=3600)

    def explode(*a, **k):
        raise AssertionError("must not call Immich inside the rate-limit window")

    msg = drain.maybe_requeue_embeddings(cfg, [ASSET], now=1000, opener=explode,
                                         load_fn=lambda p, d: {"lastRequeue": 900})
    assert "holding off" in msg


def test_an_unreachable_ml_server_defers_rather_than_failing(tmp_path):
    cfg = drain_cfg(tmp_path, apply=True)

    def refuse(req, timeout=None):
        raise OSError("connection refused")

    msg = drain.maybe_requeue_embeddings(cfg, [ASSET], now=10_000, opener=refuse,
                                         load_fn=lambda p, d: {})
    assert "unreachable" in msg


def test_a_dry_run_probes_health_but_does_not_start_the_backlog_job(tmp_path):
    # Probing /ping is read-only and keeps the dry-run report honest about what
    # WOULD happen; only the PUT is withheld.
    cfg = drain_cfg(tmp_path)  # apply=False
    op = FakeOpener([json_reply({})])
    msg = drain.maybe_requeue_embeddings(cfg, [ASSET], now=10_000, opener=op,
                                         load_fn=lambda p, d: {})
    assert "would requeue" in msg and "dry run" in msg
    assert all(r.get_method() == "GET" for r in op.requests)


def test_ml_health_uses_the_same_ping_immich_does():
    cfg = settings(ml_url="http://ml-host:3003")
    op = FakeOpener([json_reply({})])
    assert api.ml_healthy(cfg, opener=op) is True
    assert op.last.full_url == "http://ml-host:3003/ping"


def test_a_healthy_ml_server_is_recognised_from_its_non_json_pong():
    # /ping answers with the bare string `pong`. Parsing that as JSON raises, and
    # because any failure means "unreachable", a perfectly healthy ML host was
    # reported as down — the drainer then never kicked the embedding backlog.
    cfg = settings(ml_url="http://ml-host:3003")
    op = FakeOpener([lambda: FakeResponse(b"pong")])
    assert api.ml_healthy(cfg, opener=op) is True


def test_a_non_2xx_ping_is_not_healthy():
    cfg = settings(ml_url="http://ml-host:3003")
    op = FakeOpener([lambda: FakeResponse(b"nope", status=503)])
    assert api.ml_healthy(cfg, opener=op) is False


def test_the_drain_config_cannot_write_without_being_told_to():
    # Same rule as every other destructive tool here: settings built from an
    # empty environment are inert.
    assert load(env={}, read_file=lambda p: None).apply is False
    assert load(env={"IMMICH_CLIP_DRAIN_APPLY": "true"},
                read_file=lambda p: None).apply is True


# ── hand-removals are remembered ─────────────────────────────────────────────
def test_a_removal_learned_from_immichs_audit_is_never_refiled(q):
    # Immich records every album removal in album_asset_audit but prunes it after
    # 31 days, so it is copied into our own table, which is not pruned.
    # (second result set = current album membership, for the re-add check)
    cur = FakeCursor([[(ALBUM, ASSET)], []])
    learned = exclusions.sync_from_audit(q, cur, [ALBUM], now=1000)
    assert learned == [(ALBUM, ASSET)]
    assert exclusions.for_album(q, ALBUM) == {ASSET}
    assert exclusions.allowed(q, ASSET, [ALBUM, "other"]) == ["other"]


def test_learning_the_same_removal_twice_reports_it_once(q):
    exclusions.sync_from_audit(q, FakeCursor([[(ALBUM, ASSET)], []]), [ALBUM], now=1000)
    again = exclusions.sync_from_audit(q, FakeCursor([[(ALBUM, ASSET)], []]), [ALBUM], now=2000)
    assert again == []


def test_an_asset_removed_from_one_album_is_still_filed_into_another(q):
    exclusions.sync_from_audit(q, FakeCursor([[(ALBUM, ASSET)], []]), [ALBUM], now=1000)
    assert exclusions.allowed(q, OTHER, [ALBUM]) == [ALBUM]


def test_putting_a_photo_back_by_hand_clears_its_exclusion(q):
    # Otherwise one accidental removal bans the photo for good and the only cure
    # is editing a SQLite file.
    exclusions.sync_from_audit(q, FakeCursor([[(ALBUM, ASSET)], []]), [ALBUM], now=1000)
    assert exclusions.for_album(q, ALBUM) == {ASSET}
    # now it is back in the album
    exclusions.sync_from_audit(q, FakeCursor([[(ALBUM, ASSET)], [(ALBUM, ASSET)]]),
                               [ALBUM], now=2000)
    assert exclusions.for_album(q, ALBUM) == set()


def test_with_no_state_nothing_is_excluded():
    assert exclusions.allowed(None, ASSET, [ALBUM]) == [ALBUM]


def test_the_drainer_does_not_refile_a_photo_taken_out_by_hand(tmp_path, q, monkeypatch):
    a_profile(tmp_path)
    queue.enqueue(q, ASSET, "food", 0.3, [ALBUM], now=1000)
    conn = FakeConn(FakeCursor([
        [(ALBUM, ASSET)],   # audit: it was removed from this album by hand
        [],                 # and is not back in the album
        [(ASSET,)],         # still exists
        [(0.12,)],          # and still matches
    ]))
    filed = []
    monkeypatch.setattr(drain, "file_into_albums",
                        lambda cfg, albums, aid, owner=None: filed.append(aid) or 1)

    drain.run(drain_cfg(tmp_path, apply=True), connect=lambda c: conn,
              opener=FakeOpener([json_reply({})]), queue_conn=q, now=2000)

    assert filed == []          # matched, but not put back
    assert queue.count(q) == 0  # and still resolved, not left to retry forever


def test_a_seed_album_rule_parks_enough_to_be_re_decided_later(q):
    # A rule that learns from an album is not reconstructable from a profile
    # name, so the album and scoring ride along with the parked verdict.
    queue.enqueue(q, ASSET, "album:Cats", 0.3, [ALBUM], now=1000,
                  seed_album="Cats", scoring="centroid")
    row = queue.pending(q)[0]
    assert row["profile"] == "album:Cats"
    assert row["seedAlbum"] == "Cats"
    assert row["scoring"] == "centroid"


def test_a_removal_is_honoured_on_the_LIVE_path_not_just_the_drain_tick(tmp_path, q):
    """Regression: exclusions were learned only by drain/backfill.

    `sync_from_audit` ran on the 15-minute drain tick, so a photo taken out of an
    album by hand could be refiled by the workflow in the meantime — observed
    while verifying the 1.0.0 bump: a gelato removed and immediately re-triggered
    came straight back. Immich writes the audit row synchronously with the
    removal, so the live path reads it too.
    """
    cfg = settings(profile_dir=str(tmp_path), model="m", api_key="k")
    # Postgres says: this asset was removed from this album by hand, and is not
    # currently in it.
    pg = FakeConn(FakeCursor([[(ALBUM, ASSET)], []]))
    filed = []

    result = clip_filter.handle(
        cfg, {"assetId": ASSET, "albumIds": [ALBUM]},
        classify_fn=lambda r: {"match": True, "distance": 0.1},
        queue_conn=q,
        connect=lambda c: pg,
        add_assets=lambda *a, **k: filed.append(a) or 1,
        now=1000)

    assert result["match"] is True      # the verdict is unchanged
    assert filed == []                  # but it is not put back
    assert result.get("excluded") == 1
    assert exclusions.for_album(q, ALBUM) == {ASSET}   # and it was learned


def test_a_failure_to_refresh_exclusions_does_not_block_filing(tmp_path, q):
    # A stale exclusion set is a much smaller problem than a rule that stops
    # filing; the drain pass catches up either way.
    cfg = settings(profile_dir=str(tmp_path), model="m", api_key="k")
    filed = []

    def refuse(_cfg):
        raise OSError("postgres is down")

    result = clip_filter.handle(
        cfg, {"assetId": ASSET, "albumIds": [ALBUM]},
        classify_fn=lambda r: {"match": True, "distance": 0.1},
        queue_conn=q, connect=refuse,
        add_assets=lambda *a, **k: filed.append(a) or 1, now=1000)

    assert result["filed"] == 1
    assert len(filed) == 1
