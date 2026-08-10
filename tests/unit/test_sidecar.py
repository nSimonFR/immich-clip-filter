"""The verdict service: sidecar, profile builder, backfill.

The sidecar is the interesting one. It is called by a WASM plugin running inside
Immich's workflow engine, on the hot path of every upload, and its contract is
that it must NEVER answer "match" by accident — an unknown profile, a dead
database, an embedding that never arrives all have to come back as a clean
`match: false`. Most of what follows is that contract, one failure mode per test.
"""

import json

import pytest
from fakes import ALBUM, ASSET, OTHER, FakeConn, FakeCursor, FakeOpener, json_reply, settings

from immich_clip import api, backfill, profile, queue, store, vectors
from immich_clip.config import load
from immich_clip import sidecar as clip_filter


def a_profile(tmp_path, name="food", model="ViT-H-14", vector=(0.6, 0.8)):
    store.save_profile(tmp_path, name, model, list(vector), {"kind": "test"}, now=0)
    return settings(profile_dir=str(tmp_path), model=model, poll_sec=0)


def classify(cfg, req, results=(), connect=None, sleeps=None):
    conn = FakeConn(FakeCursor(results))
    ticks = iter(range(0, 10_000))
    return clip_filter.classify(
        cfg,
        req,
        connect=connect or (lambda c: conn),
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        monotonic=lambda: next(ticks),
    ), conn


# ── nothing happens at import ────────────────────────────────────────────────
def test_the_modules_have_no_import_time_side_effects():
    # Built from an empty env: no secret read, no socket, no /var/lib touched —
    # and, the safety property, nothing that could write.
    cfg = load(env={}, read_file=lambda p: None)
    assert cfg.listen_port == 8351
    assert cfg.model == ""
    assert cfg.immich_url == "http://127.0.0.1:2283"
    assert cfg.ml_url == ""
    assert cfg.apply is False
    assert cfg.keys == ()


# ── vectors ──────────────────────────────────────────────────────────────────
def test_a_vector_survives_the_pgvector_text_round_trip():
    vec = [0.1, -0.25, 1e-7, 3.0]
    assert vectors.parse_vector(vectors.format_vector(vec)) == vec


def test_the_pgvector_literal_carries_no_spaces():
    # pgvector rejects "[0.1, 0.2]" — the space after the comma is not cosmetic.
    assert " " not in vectors.format_vector([0.1, 0.2])


def test_a_non_literal_is_rejected_rather_than_half_parsed():
    with pytest.raises(ValueError):
        vectors.parse_vector("0.1,0.2")
    with pytest.raises(ValueError):
        vectors.parse_vector("[]")


def test_normalising_gives_unit_length():
    out = vectors.l2_normalize([3.0, 4.0])
    assert out == pytest.approx([0.6, 0.8])


def test_a_zero_vector_cannot_be_normalised():
    with pytest.raises(ValueError):
        vectors.l2_normalize([0.0, 0.0])


def test_the_centroid_is_the_component_wise_mean():
    assert vectors.mean_vector([[0.0, 2.0], [2.0, 4.0]]) == [1.0, 3.0]


def test_seed_vectors_of_different_dimensions_are_refused():
    # Silently averaging a 512-dim and a 1024-dim embedding would produce a
    # plausible-looking centroid that matches nothing.
    with pytest.raises(ValueError):
        vectors.mean_vector([[1.0, 2.0], [1.0]])


# ── profiles on disk ─────────────────────────────────────────────────────────
def test_a_profile_round_trips(tmp_path):
    store.save_profile(tmp_path, "food", "ViT-H-14", [0.6, 0.8], {"kind": "seed"}, now=0)
    loaded = store.load_profile(tmp_path, "food", "ViT-H-14")
    assert loaded["vector"] == [0.6, 0.8]
    assert loaded["dim"] == 2
    assert loaded["built_from"] == {"kind": "seed"}


def test_a_profile_name_cannot_escape_the_profile_directory(tmp_path):
    # The name arrives from the workflow step's config box in the Immich UI.
    for bad in ("../../etc/passwd", "/etc/passwd", "food/../..", "", "Food", "a" * 65):
        with pytest.raises(store.ProfileError):
            store.profile_path(tmp_path, bad)


def test_a_missing_profile_is_a_profile_error_not_a_crash(tmp_path):
    with pytest.raises(store.ProfileError):
        store.load_profile(tmp_path, "nope")


def test_a_profile_built_for_another_clip_model_is_refused(tmp_path):
    # Changing clip.modelName re-embeds the library; the old centroid now points
    # into a different vector space and would match essentially at random.
    store.save_profile(tmp_path, "food", "old-model", [1.0], {}, now=0)
    with pytest.raises(store.ProfileError) as e:
        store.load_profile(tmp_path, "food", "new-model")
    assert "rebuild" in str(e.value)


def test_a_profile_with_no_vector_is_refused(tmp_path):
    (tmp_path / "food.json").write_text(json.dumps({"name": "food", "vector": []}))
    with pytest.raises(store.ProfileError):
        store.load_profile(tmp_path, "food")


def test_writing_a_profile_leaves_no_partial_file_behind(tmp_path):
    store.save_profile(tmp_path, "food", "m", [1.0], {}, now=0)
    assert [p.name for p in tmp_path.iterdir()] == ["food.json"]


# ── the sidecar's verdict ────────────────────────────────────────────────────
def test_an_asset_under_the_threshold_matches(tmp_path):
    cfg = a_profile(tmp_path)
    result, _ = classify(cfg, {"assetId": ASSET, "profile": "food",
                               "threshold": 0.28, "waitSec": 10}, results=[[(0.21,)]])
    assert result["match"] is True
    assert result["distance"] == 0.21
    assert result["profile"] == "food"


def test_an_asset_over_the_threshold_does_not_match(tmp_path):
    cfg = a_profile(tmp_path)
    result, _ = classify(cfg, {"assetId": ASSET, "profile": "food",
                               "threshold": 0.28, "waitSec": 10}, results=[[(0.55,)]])
    assert result["match"] is False
    assert result["distance"] == 0.55
    assert result["reason"] == "over threshold"


def test_the_threshold_boundary_is_inclusive(tmp_path):
    cfg = a_profile(tmp_path)
    result, _ = classify(cfg, {"assetId": ASSET, "profile": "food",
                               "threshold": 0.3, "waitSec": 10}, results=[[(0.3,)]])
    assert result["match"] is True


def test_the_sidecar_waits_for_an_embedding_that_is_not_ready_yet(tmp_path):
    # The normal case for a fresh upload: the SmartSearch job has not run yet.
    cfg = a_profile(tmp_path)
    sleeps = []
    result, _ = classify(cfg, {"assetId": ASSET, "profile": "food",
                               "threshold": 0.28, "waitSec": 30},
                         results=[[], [], [(0.2,)]], sleeps=sleeps)
    assert result["match"] is True
    assert len(sleeps) == 2


def test_an_embedding_that_never_arrives_is_undecided_not_a_no(tmp_path):
    # NOT the same as "too far away". The ML host is usually
    # off, so this is the common case; the caller queues it (see
    # test_drain.py) instead of losing the decision.
    cfg = a_profile(tmp_path)
    result, _ = classify(cfg, {"assetId": ASSET, "profile": "food",
                               "threshold": 0.28, "waitSec": 3}, results=[])
    assert result["match"] is False
    assert result["undecided"] is True


def test_a_step_cannot_ask_to_wait_longer_than_the_server_cap(tmp_path):
    # Otherwise one config box could pin a workflow-queue slot indefinitely.
    cfg = a_profile(tmp_path)
    cfg = cfg.with_(max_wait_sec=5)
    sleeps = []
    result, _ = classify(cfg, {"assetId": ASSET, "profile": "food",
                               "threshold": 0.28, "waitSec": 99999},
                         results=[], sleeps=sleeps)
    assert result["match"] is False
    assert result["waitedSec"] <= 6


def test_an_unknown_rule_fails_closed(tmp_path):
    # The rule is resolved AGAINST the database now (a rule may name a seed
    # album, whose members are read live), so unlike the malformed-input checks
    # below this one does open a connection first. It must still fail closed.
    cfg = a_profile(tmp_path)
    result, _ = classify(cfg, {"assetId": ASSET, "profile": "nope",
                               "threshold": 0.28, "waitSec": 1}, results=[])
    assert result["match"] is False
    assert "no profile" in result["reason"]


def test_a_traversing_profile_name_fails_closed(tmp_path):
    cfg = a_profile(tmp_path)
    result, _ = classify(cfg, {"assetId": ASSET, "profile": "../../etc/passwd",
                               "threshold": 0.28, "waitSec": 1})
    assert result["match"] is False
    assert "invalid profile name" in result["reason"]


def test_a_malformed_asset_id_fails_closed_without_touching_the_database(tmp_path):
    cfg = a_profile(tmp_path)

    def explode(_cfg):
        raise AssertionError("must not connect for a bad asset id")

    for bad in ("", "not-a-uuid", "'; DROP TABLE asset; --"):
        result, _ = classify(cfg, {"assetId": bad, "profile": "food",
                                   "threshold": 0.28}, connect=explode)
        assert result["match"] is False


def test_a_missing_threshold_fails_closed(tmp_path):
    cfg = a_profile(tmp_path)
    result, _ = classify(cfg, {"assetId": ASSET, "profile": "food"})
    assert result == {"match": False, "reason": "bad threshold"}


def test_a_dead_database_fails_closed(tmp_path):
    cfg = a_profile(tmp_path)

    def refuse(_cfg):
        raise OSError("could not connect to server")

    result, _ = classify(cfg, {"assetId": ASSET, "profile": "food",
                               "threshold": 0.28, "waitSec": 1}, connect=refuse)
    assert result["match"] is False
    assert "database unreachable" in result["reason"]


def test_the_connection_is_closed_even_when_the_lookup_explodes(tmp_path):
    cfg = a_profile(tmp_path)

    class Exploding(FakeCursor):
        def execute(self, sql, params=()):
            raise RuntimeError("server closed the connection unexpectedly")

    conn = FakeConn(Exploding())
    result = clip_filter.classify(cfg, {"assetId": ASSET, "profile": "food",
                                        "threshold": 0.28, "waitSec": 1},
                                  connect=lambda c: conn, sleep=lambda s: None)
    assert result["match"] is False
    assert conn.closed is True


def test_the_lookup_is_a_primary_key_query_so_it_needs_no_probes_guc(tmp_path):
    # A vchordrq index scan without `vchordrq.probes` set errors outright; the
    # sidecar avoids the index entirely by selecting on the primary key.
    cfg = a_profile(tmp_path)
    _, conn = classify(cfg, {"assetId": ASSET, "profile": "food",
                             "threshold": 0.28, "waitSec": 1}, results=[[(0.1,)]])
    sql, params = conn.cursor().sql[0]
    assert 'WHERE "assetId" = %s' in sql
    assert "ORDER BY" not in sql
    assert params[1] == ASSET


# ── profile building ─────────────────────────────────────────────────────────
def test_seed_assets_with_no_embedding_are_reported_not_silently_dropped():
    cur = FakeCursor([[(ASSET, "[1.0,0.0]")]])
    found, missing = profile.seed_embeddings(cur, [ASSET, OTHER])
    assert found == [[1.0, 0.0]]
    assert missing == [OTHER]


def test_the_seed_lookup_casts_the_id_list_to_uuid():
    # psycopg2 adapts a list of str to text[], and Postgres has no uuid = text
    # operator — without the cast this fails with UndefinedFunction at runtime.
    cur = FakeCursor([[]])
    profile.seed_embeddings(cur, [ASSET])
    assert "ANY(%s::uuid[])" in cur.sql[0][0]


def test_a_seed_profile_is_the_normalised_centroid_of_its_seeds(tmp_path):
    cfg = settings(profile_dir=str(tmp_path), model="ViT-H-14")
    args = profile.parse_args(["--name", "food", "--seed-asset", ASSET,
                               "--seed-asset", OTHER])
    conn = FakeConn(FakeCursor([[(ASSET, "[3.0,0.0]"), (OTHER, "[0.0,3.0]")]]))
    payload = profile.build(cfg, args, connect=lambda c: conn)
    # mean([3,0],[0,3]) = [1.5,1.5] -> normalised
    assert payload["vector"] == pytest.approx([0.7071067811865475] * 2)
    assert payload["built_from"]["assets"] == 2
    assert payload["model"] == "ViT-H-14"


def test_a_profile_records_the_seed_ids_so_it_can_be_rebuilt(tmp_path):
    # Recording only the COUNT made a hand-picked profile unreproducible, which
    # would have made the "this directory is only a cache" backup note false.
    cfg = settings(profile_dir=str(tmp_path), model="m")
    args = profile.parse_args(["--name", "food", "--seed-asset", ASSET,
                               "--seed-asset", OTHER])
    conn = FakeConn(FakeCursor([[(ASSET, "[1.0,0.0]"), (OTHER, "[0.0,1.0]")]]))
    payload = profile.build(cfg, args, connect=lambda c: conn)
    assert payload["built_from"]["assetIds"] == [ASSET, OTHER]


def test_a_seed_album_is_resolved_by_name_then_expanded_to_its_assets(tmp_path):
    cfg = settings(profile_dir=str(tmp_path), model="ViT-H-14")
    args = profile.parse_args(["--name", "food", "--seed-album", "Burgiiiiiiie"])
    # Album membership comes from the database: as of 3.1 GET /api/albums/{id}
    # reports assetCount but hands back an empty `assets` list.
    conn = FakeConn(FakeCursor([
        [("album-9",)],                                # album_id_by_name
        [(ASSET,), (OTHER,)],                          # album_asset_ids
        [(ASSET, "[1.0,0.0]"), (OTHER, "[1.0,0.0]")],  # seed_embeddings
    ]))
    payload = profile.build(cfg, args, connect=lambda c: conn)
    assert payload["built_from"] == {"kind": "seed", "album": "Burgiiiiiiie",
                                     "assets": 2, "requested": 2,
                                     "assetIds": [ASSET, OTHER]}


def test_a_seed_album_that_does_not_exist_is_an_error(tmp_path):
    cfg = settings(profile_dir=str(tmp_path), model="m")
    args = profile.parse_args(["--name", "food", "--seed-album", "Nope"])
    conn = FakeConn(FakeCursor([[]]))
    with pytest.raises(SystemExit):
        profile.build(cfg, args, connect=lambda c: conn)


def test_a_seed_set_with_no_embeddings_at_all_is_an_error(tmp_path):
    cfg = settings(profile_dir=str(tmp_path), model="m")
    args = profile.parse_args(["--name", "food", "--seed-asset", ASSET])
    conn = FakeConn(FakeCursor([]))
    with pytest.raises(SystemExit):
        profile.build(cfg, args, connect=lambda c: conn)


def test_text_mode_sends_immichs_own_ml_request_shape(tmp_path):
    cfg = settings(profile_dir=str(tmp_path), model="ViT-H-14",
                         ml_url="http://beast:3003")
    args = profile.parse_args(["--name", "food", "--text", "a plate of food"])
    op = FakeOpener([json_reply({"clip": [3.0, 4.0]})])
    payload = profile.build(cfg, args, opener=op)
    body = op.last.data.decode()
    assert op.last.full_url == "http://beast:3003/predict"
    assert 'name="entries"' in body and 'name="text"' in body
    assert '{"clip": {"textual": {"modelName": "ViT-H-14", "options": {}}}}' in body
    assert "a plate of food" in body
    assert payload["vector"] == pytest.approx([0.6, 0.8])


def test_text_mode_accepts_the_pgvector_string_the_ml_server_actually_returns(tmp_path):
    # Not hypothetical: beast returns {"clip": "[-0.0327,0.0078,…]"} — a pgvector
    # literal string, not a JSON array. Immich never notices because it passes
    # the value straight into an INSERT. An isinstance(list) check rejected it.
    cfg = settings(profile_dir=str(tmp_path), model="ViT-H-14",
                         ml_url="http://beast:3003")
    args = profile.parse_args(["--name", "food", "--text", "a plate of food"])
    op = FakeOpener([json_reply({"clip": "[3.0,4.0]"})])
    payload = profile.build(cfg, args, opener=op)
    assert payload["vector"] == pytest.approx([0.6, 0.8])


def test_text_mode_refuses_when_immich_has_no_ml_server_configured(tmp_path):
    cfg = settings(profile_dir=str(tmp_path), model="m")
    args = profile.parse_args(["--name", "food", "--text", "food"])
    op = FakeOpener([json_reply({"machineLearning": {"urls": []}})])
    with pytest.raises(SystemExit):
        profile.build(cfg, args, opener=op)


# ── talking to Immich ────────────────────────────────────────────────────────
def test_the_clip_model_is_read_from_immich_when_not_overridden():
    # The model name decides whether a stored centroid is valid; asking the
    # server that will be compared against removes the chance of drift.
    cfg = settings(model="")
    op = FakeOpener([json_reply({"machineLearning": {"clip": {"modelName": "ViT-H-14"}}})])
    assert api.clip_model(cfg, opener=op) == "ViT-H-14"
    assert op.last.full_url.endswith("/api/system-config")


def test_an_explicit_model_short_circuits_the_api_call():
    cfg = settings(model="pinned")

    def explode(*a, **k):
        raise AssertionError("should not call the API when the model is pinned")

    assert api.clip_model(cfg, opener=explode) == "pinned"


def test_the_ml_url_is_read_from_immich_when_not_overridden():
    cfg = settings(ml_url="")
    op = FakeOpener([json_reply({"machineLearning": {"urls": ["http://beast:3003/"]}})])
    assert api.ml_url(cfg, opener=op) == "http://beast:3003"


def test_an_ambiguous_album_name_is_an_error_rather_than_a_guess():
    with pytest.raises(store.AlbumError):
        store.album_id_by_name(FakeCursor([[("1",), ("2",)]]), "Food")


def test_an_absent_album_resolves_to_none_rather_than_raising():
    assert store.album_id_by_name(FakeCursor([[]]), "Food") is None


def test_a_deleted_album_does_not_resolve():
    cur = FakeCursor([[("1",)]])
    store.album_id_by_name(cur, "Food")
    assert '"deletedAt" IS NULL' in cur.sql[0][0]


def test_a_refused_add_is_reported_with_its_reason():
    # In a shared library the API key can only file its OWNER's assets. A bare
    # "53/78 added" read as a failure; it is a permission boundary.
    cfg = settings(immich_url="http://immich", api_key="k")
    op = FakeOpener([json_reply([{"id": ASSET, "success": True},
                                 {"id": OTHER, "success": False, "error": "no_permission"}])])
    lines = []
    api.add_assets(cfg, "album-1", [ASSET, OTHER], opener=op, log=lines.append)
    assert "1/2 added, 1 no_permission" in lines[0]


def test_assets_are_added_in_batches_and_only_successes_counted():
    cfg = settings(immich_url="http://immich", api_key="k")
    op = FakeOpener([json_reply([{"id": ASSET, "success": True},
                                 {"id": OTHER, "success": False}])])
    added = api.add_assets(cfg, "album-1", [ASSET, OTHER], opener=op, log=lambda m: None)
    # Immich reports an id already in the album as success:false, so this counts
    # newly added rather than present.
    assert added == 1


def test_an_existing_profile_is_not_overwritten_without_force(tmp_path, capsys):
    store.save_profile(tmp_path, "food", "m", [1.0], {}, now=0)
    env = {"IMMICH_CLIP_PROFILE_DIR": str(tmp_path)}
    rc = profile.main(["--name", "food", "--seed-asset", ASSET], env=env)
    assert rc == 1
    assert "--force" in capsys.readouterr().out


def test_seeds_and_text_cannot_be_combined():
    with pytest.raises(SystemExit):
        profile.parse_args(["--name", "food", "--text", "x", "--seed-asset", ASSET])


def test_a_profile_needs_some_source():
    with pytest.raises(SystemExit):
        profile.parse_args(["--name", "food"])


# ── backfill ─────────────────────────────────────────────────────────────────
def st(tmp_path):
    """A throwaway state DB (pending queue + exclusions) for a backfill run."""
    return queue.connect(str(tmp_path / "state.sqlite"))


def a_seed_profile(ids=("s1",)):
    return {"name": "food", "vector": [1.0, 0.0], "scoring": "nearest",
            "seedIds": list(ids)}


def an_album_rule(ids=("s1", "s2")):
    return {"name": "album:Cats", "seedIds": list(ids), "scoring": "centroid"}


def a_text_profile():
    return {"name": "prompt", "vector": [1.0, 0.0], "seedIds": []}


def test_scoring_mode_is_per_profile_not_global():
    # The two live profiles want opposite answers. `food` is 19 photos of
    # different subjects: averaging leaves only the shared context, which is how
    # an Eiffel Tower scored 0.277. `burgie` is 118 photos of ONE toy in many
    # places: there the average isolates the toy, while nearest-seed matched the
    # SCENE of whichever seed was closest (river selfies, interiors, a fan).
    diverse = {"vector": [1.0, 0.0],
               "built_from": {"kind": "seed", "assetIds": ["s1"]}, "scoring": "nearest"}
    one_subject = {"vector": [1.0, 0.0],
                   "built_from": {"kind": "seed", "assetIds": ["s1"]}, "scoring": "centroid"}
    assert store.seed_ids(diverse) == ["s1"]
    assert store.seed_ids(one_subject) == []   # -> centroid path


def test_a_profile_records_its_scoring_mode(tmp_path):
    p = store.save_profile(tmp_path, "burgie", "m", [1.0], {"kind": "seed"},
                           now=0, scoring="centroid")
    assert p["scoring"] == "centroid"
    assert store.load_profile(tmp_path, "burgie", "m")["scoring"] == "centroid"


def test_a_seed_profile_scores_against_the_nearest_seed_not_the_mean():
    # The centroid of a diverse seed set drifts toward the middle of the library
    # and drags ordinary photos in — an Eiffel Tower measured 0.277 from the mean
    # while sitting 0.367 from the nearest actual food photo.
    cur = FakeCursor([])
    backfill.scan(cur, a_seed_profile(["s1", "s2"]), "album-1")
    sql, params = cur.sql[0]
    assert "MIN(s.embedding <=> seeds.e)" in sql
    assert params[0] == ["s1", "s2"]


def test_a_text_profile_still_scores_against_its_single_vector():
    cur = FakeCursor([])
    backfill.scan(cur, a_text_profile(), "album-1")
    assert "<=> %s::vector" in cur.sql[0][0]


def test_an_album_rule_averages_its_members_live():
    # AVG(vector) in SQL is what lets a rule name a seed ALBUM and stay correct
    # as photos are added to it — nothing precomputed, no file, no shell.
    cur = FakeCursor([])
    backfill.scan(cur, an_album_rule(), "album-1")
    sql, params = cur.sql[0]
    assert "AVG(embedding)" in sql
    assert params[0] == ["s1", "s2"]


def test_hand_removals_are_excluded_from_the_scan():
    cur = FakeCursor([])
    backfill.scan(cur, a_seed_profile(), "album-1", excluded=["gone-1"])
    sql, params = cur.sql[0]
    assert "sc.asset_id <> ALL(%s::uuid[])" in sql
    assert params[1] == ["gone-1"]


def test_the_backfill_scan_is_exact_not_approximate():
    # smart_search.embedding carries a vchordrq (ANN) index which answers
    # `ORDER BY embedding <=> const` approximately, and errors with
    # "need 1 probes" at plan time when the GUC is unset. Scoring inside a
    # MATERIALIZED CTE with no ORDER BY leaves the index nothing to serve, so the
    # sort happens outside on a plain float. enable_indexscan=off does NOT work
    # here — the EXPLAIN itself still errors.
    cur = FakeCursor([])
    backfill.scan(cur, a_text_profile(), "album-1")
    sql = cur.sql[0][0]
    assert "AS MATERIALIZED" in sql
    assert sql.index("<=>") < sql.index("ORDER BY")
    assert "ORDER BY sc.d" in sql


def test_the_backfill_scan_skips_assets_already_in_the_album():
    cur = FakeCursor([])
    backfill.scan(cur, a_text_profile(), "album-1")
    sql, params = cur.sql[0]
    assert "NOT EXISTS" in sql and "album_asset" in sql
    assert "a.type = 'IMAGE'" in sql
    assert "NOT IN ('hidden', 'locked')" in sql
    assert params[2] == "album-1"


def test_the_histogram_buckets_the_distances():
    rows = [("a", "a.jpg", 0.21), ("b", "b.jpg", 0.22), ("c", "c.jpg", 0.55)]
    out = "\n".join(backfill.histogram(rows))
    assert "0.20–0.25" in out and "0.55–0.60" in out


def test_a_dry_run_writes_nothing(tmp_path, capsys):
    cfg = settings(profile_dir=str(tmp_path), model="m", api_key="k")
    store.save_profile(tmp_path, "food", "m", [1.0, 0.0], {}, now=0)
    op = FakeOpener([json_reply([{"id": "album-1", "albumName": "Food"}])])
    conn = FakeConn(FakeCursor([[("album-1",)], [], [(ASSET, "a.jpg", 0.1), (OTHER, "b.jpg", 0.9)]]))
    args = backfill.parse_args(["--profile", "food", "--album", "Food"])

    backfill.run(cfg, args, connect=lambda c: conn, opener=op, state=st(tmp_path))

    assert all(r.get_method() == "GET" for r in op.requests)
    assert "1 of 2 at or under threshold" in capsys.readouterr().out


def test_apply_adds_only_the_assets_under_the_threshold(tmp_path):
    cfg = settings(profile_dir=str(tmp_path), model="m", api_key="k")
    store.save_profile(tmp_path, "food", "m", [1.0, 0.0], {}, now=0)
    op = FakeOpener([
        json_reply([{"id": "album-1", "albumName": "Food"}]),
        json_reply([{"id": ASSET, "success": True}]),
    ])
    conn = FakeConn(FakeCursor([
        [("album-1",)],                                       # album_id_by_name
        [],                                                   # audit: no removals
        [(ASSET, "a.jpg", 0.1), (OTHER, "b.jpg", 0.9)],       # the scan
        [(ASSET, "owner-1", "nico@example.com")],             # asset_owners
    ]))
    args = backfill.parse_args(["--profile", "food", "--album", "Food", "--apply"])

    backfill.run(cfg, args, connect=lambda c: conn, opener=op, state=st(tmp_path))

    put = op.requests[-1]
    assert put.get_method() == "PUT"
    assert put.full_url.endswith("/api/albums/album-1/assets")
    assert json.loads(put.data.decode()) == {"ids": [ASSET]}


def test_a_missing_album_is_refused_unless_creation_was_asked_for(tmp_path):
    cfg = settings(profile_dir=str(tmp_path), model="m", api_key="k")
    store.save_profile(tmp_path, "food", "m", [1.0], {}, now=0)
    op = FakeOpener([json_reply([])])
    args = backfill.parse_args(["--profile", "food", "--album", "Food"])
    with pytest.raises(SystemExit) as e:
        backfill.run(cfg, args, connect=lambda c: FakeConn(), opener=op, state=st(tmp_path))
    assert "--create-album" in str(e.value)


def test_a_dry_run_does_not_create_the_album_even_when_asked_to(tmp_path, capsys):
    # --create-album is about the apply path; a dry run must stay read-only.
    cfg = settings(profile_dir=str(tmp_path), model="m", api_key="k")
    store.save_profile(tmp_path, "food", "m", [1.0, 0.0], {}, now=0)
    op = FakeOpener([json_reply([])])
    conn = FakeConn(FakeCursor([[], [], [(ASSET, "a.jpg", 0.1)]]))
    args = backfill.parse_args(["--profile", "food", "--album", "Food", "--create-album"])

    backfill.run(cfg, args, connect=lambda c: conn, opener=op, state=st(tmp_path))

    assert op.requests == []
    assert "--apply would create it" in capsys.readouterr().out


def test_creating_the_album_posts_it_once(tmp_path):
    cfg = settings(immich_url="http://immich", api_key="k")
    op = FakeOpener([json_reply({"id": "new-album"}, status=201)])
    assert api.create_album(cfg, "Food", "desc", opener=op) == "new-album"
    assert op.last.get_method() == "POST"
    assert json.loads(op.last.data.decode())["albumName"] == "Food"


def test_a_backfill_against_a_stale_profile_refuses_before_scanning(tmp_path):
    # The centroid was built for another CLIP model, so every distance would be
    # meaningless — better to stop than to fill an album with noise.
    cfg = settings(profile_dir=str(tmp_path), model="new-model", api_key="k")
    store.save_profile(tmp_path, "food", "old-model", [1.0], {}, now=0)
    args = backfill.parse_args(["--profile", "food", "--album", "Food"])
    with pytest.raises(SystemExit) as e:
        backfill.run(cfg, args, connect=lambda c: FakeConn(), opener=FakeOpener([json_reply([])]),
                     state=st(tmp_path))
    assert "rebuild" in str(e.value)


def test_apply_without_an_api_key_is_refused(tmp_path):
    store.save_profile(tmp_path, "food", "m", [1.0], {}, now=0)
    env = {"IMMICH_CLIP_PROFILE_DIR": str(tmp_path), "IMMICH_API_KEY_FILE": "/nonexistent"}
    with pytest.raises(SystemExit):
        backfill.main(["--profile", "food", "--album", "Food", "--apply"], env=env)
