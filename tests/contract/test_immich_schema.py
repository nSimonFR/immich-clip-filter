"""Contract tests: does Immich's database still look the way the SQL assumes?

This project reads Immich's internal schema. That is not an API, it carries no
stability promise, and it is unavoidable — the REST smart-search endpoint returns
ranked assets *without distances*, so a threshold cannot be applied through it.

These tests are the early-warning system for that. They are fast, they need no ML
server and no fixtures, and they are meant to run against a **real Immich
database** — ideally the one you are about to upgrade. An upgrade that renames a
column should break this file, not somebody's album six weeks later.

    IMMICH_CLIP_CONTRACT_DSN=postgresql://immich@/immich pytest tests/contract -v

Skipped when that variable is unset, so `pytest` with no arguments stays offline.
Nothing here writes: every statement is a SELECT, and the whole run happens inside
a transaction that is rolled back.
"""

import os

import pytest

from immich_clip import backfill, exclusions, schema, store

DSN = os.environ.get("IMMICH_CLIP_CONTRACT_DSN", "")

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(not DSN, reason="set IMMICH_CLIP_CONTRACT_DSN to run"),
]

ZERO_UUID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(scope="module")
def conn():
    psycopg2 = pytest.importorskip("psycopg2")
    c = psycopg2.connect(DSN)
    yield c
    c.rollback()
    c.close()


@pytest.fixture
def cur(conn):
    """A cursor whose work is always rolled back."""
    c = conn.cursor()
    yield c
    conn.rollback()
    c.close()


# ── the probe itself ─────────────────────────────────────────────────────────
def test_every_table_and_column_the_probe_requires_exists(cur):
    # The same check the sidecar runs at startup. If this fails, the sidecar will
    # refuse to start — which is the point.
    schema.check(cur)


def test_pgvector_answers_the_two_operators_the_whole_project_rests_on(cur):
    assert schema.vector_ops_work(cur) is True


def test_the_probe_would_notice_a_renamed_column(cur):
    # Guards the guard: a probe that passes vacuously would be worse than none.
    with pytest.raises(schema.SchemaMismatch):
        schema.check(cur, {"smart_search": ["a_column_immich_will_never_have"]})


# ── the queries, run for real ────────────────────────────────────────────────
def test_an_absent_album_name_resolves_to_none(cur):
    assert store.album_id_by_name(cur, "an album nobody would ever create ‡") is None


def test_album_membership_reads_back_as_uuid_strings(cur):
    # `GET /api/albums/{id}` reports assetCount but hands back an empty `assets`
    # list, so membership has to come from here.
    cur.execute('SELECT "albumId" FROM album_asset LIMIT 1')
    row = cur.fetchone()
    if row is None:
        pytest.skip("no albums with assets in this library")
    ids = store.album_asset_ids(cur, str(row[0]))
    assert ids and all(isinstance(i, str) for i in ids)


def test_an_embedding_comes_back_as_a_pgvector_literal_string(cur):
    # No vector adapter is registered on our connections, so psycopg2 hands the
    # value back as text. `vectors.parse_vector` exists precisely for this, and a
    # future psycopg that returned a list would silently change the shape.
    from immich_clip.vectors import parse_vector

    cur.execute('SELECT embedding FROM smart_search LIMIT 1')
    row = cur.fetchone()
    if row is None:
        pytest.skip("no embeddings in this library")
    vec = parse_vector(row[0])
    assert len(vec) > 100 and all(isinstance(x, float) for x in vec)


def test_scoring_one_asset_against_a_vector_needs_no_probes_guc(cur):
    """The vchordrq index must not be involved.

    `smart_search.embedding` carries a vchordrq (ANN) index that raises
    `need 1 probes, but 0 probes provided` at PLAN time when the GUC is unset —
    `SET LOCAL enable_indexscan = off` does not avoid it, because the EXPLAIN
    itself still errors. A primary-key lookup sidesteps it entirely.
    """
    cur.execute('SELECT "assetId", embedding FROM smart_search LIMIT 1')
    row = cur.fetchone()
    if row is None:
        pytest.skip("no embeddings in this library")
    from immich_clip.vectors import parse_vector

    d = store.distance_to(cur, str(row[0]), parse_vector(row[1]))
    # An asset against its own embedding: exactly zero, and exact is the point.
    assert d == pytest.approx(0.0, abs=1e-6)


def test_the_backfill_scan_plans_as_a_seq_scan_not_an_index_scan(cur):
    """The MATERIALIZED CTE is load-bearing, not style.

    With an ORDER BY the planner reaches for the ANN index, which answers
    approximately (visibly out-of-order distances at probes=1) and errors outright
    when the GUC is unset. Scoring inside a CTE with no ORDER BY leaves the index
    nothing to serve.
    """
    cur.execute('SELECT embedding FROM smart_search LIMIT 1')
    row = cur.fetchone()
    if row is None:
        pytest.skip("no embeddings in this library")
    cur.execute("EXPLAIN " + backfill.SCAN_CENTROID_SQL, (row[0], [], ZERO_UUID))
    plan = "\n".join(r[0] for r in cur.fetchall())
    assert "Index Scan" not in plan, plan


def test_the_centroid_of_several_assets_is_a_real_aggregate(cur):
    # AVG(vector) in SQL is what lets a rule name a seed ALBUM and stay correct as
    # photos are added to it — nothing precomputed, no file, no shell.
    cur.execute('SELECT "assetId" FROM smart_search LIMIT 3')
    ids = [str(r[0]) for r in cur.fetchall()]
    if len(ids) < 3:
        pytest.skip("not enough embeddings in this library")
    d = store.centroid_distance(cur, ids[0], ids)
    assert d is not None and 0.0 <= d <= 2.0


def test_the_nearest_seed_of_a_set_including_the_asset_itself_is_zero(cur):
    cur.execute('SELECT "assetId" FROM smart_search LIMIT 3')
    ids = [str(r[0]) for r in cur.fetchall()]
    if len(ids) < 3:
        pytest.skip("not enough embeddings in this library")
    assert store.nearest_seed_distance(cur, ids[0], ids) == pytest.approx(0.0, abs=1e-6)


def test_the_embedding_column_is_fixed_width_and_says_how_wide(cur):
    """The integration harness inserts its own vectors, so this decides its shape.

    `smart_search.embedding` is `vector(N)`, not a free-dimension `vector`: an
    insert of the wrong width fails outright. N follows the configured CLIP model
    (512 for a ViT-B, 1024 for a ViT-H), so the harness discovers it from
    `atttypmod` — which pgvector stores as the dimension directly — rather than
    from a row, since a freshly-migrated database has none.
    """
    cur.execute(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = 'smart_search'::regclass AND attname = 'embedding'"
    )
    dim = cur.fetchone()[0]
    assert dim > 0, "embedding is not a fixed-width vector — the harness assumes it is"

    cur.execute('SELECT vector_dims(embedding) FROM smart_search LIMIT 1')
    row = cur.fetchone()
    if row is not None:
        assert row[0] == dim, "atttypmod disagrees with the stored vectors"


def test_zero_padding_a_short_vector_preserves_the_cosine_distance(cur):
    """Why the harness can reason in two dimensions against a 1024-wide column.

    The padding contributes nothing to either the dot product or the norms, so the
    distance between two padded vectors is exactly the distance between the
    originals. If that ever stopped holding, every threshold in the integration
    suite would be quietly wrong rather than obviously broken.
    """
    cur.execute(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = 'smart_search'::regclass AND attname = 'embedding'"
    )
    dim = cur.fetchone()[0]
    pad = ",".join(["0"] * (dim - 2))
    north, east = f"[0,1,{pad}]", f"[1,0,{pad}]"
    cur.execute("SELECT %s::vector <=> %s::vector, %s::vector <=> %s::vector",
                (north, east, north, north))
    orthogonal, identical = cur.fetchone()
    assert float(orthogonal) == pytest.approx(1.0, abs=1e-6)
    assert float(identical) == pytest.approx(0.0, abs=1e-6)


def test_an_unembedded_asset_scores_none_rather_than_raising(cur):
    # This is the whole three-outcome design: "no embedding yet" has to be
    # distinguishable from "too far away".
    assert store.distance_to(cur, ZERO_UUID, [0.0] * 8) is None


def test_the_audit_table_records_removals_with_the_columns_we_read(cur):
    # Immich prunes this after ~31 days, which is why exclusions.py copies out of
    # it rather than treating it as the memory.
    cur.execute('SELECT "albumId", "assetId" FROM album_asset_audit LIMIT 1')
    cur.fetchall()  # shape only; an empty audit window is perfectly normal


def test_reading_the_audit_through_our_own_helper_works_against_real_uuids(cur, tmp_path):
    from immich_clip import queue

    state = queue.connect(str(tmp_path / "state.sqlite"))
    try:
        # No albums named -> nothing learned, but the ::uuid[] casts and the
        # column quoting are exercised for real.
        assert exclusions.sync_from_audit(state, cur, [ZERO_UUID], now=0) == []
    finally:
        state.close()


def test_deleted_assets_are_excluded_from_the_liveness_check(cur):
    assert store.existing_asset_ids(cur, [ZERO_UUID]) == set()


def test_an_asset_owner_resolves_to_an_email(cur):
    # What per-owner API keys are matched on.
    cur.execute('SELECT id FROM asset WHERE "deletedAt" IS NULL LIMIT 1')
    row = cur.fetchone()
    if row is None:
        pytest.skip("empty library")
    owner_id, email = store.asset_owner(cur, str(row[0]))
    assert owner_id is not None
    assert email is None or "@" in email


# ── the quirk that decides the plugin's whole shape ──────────────────────────
def test_workflow_steps_are_still_returned_without_an_order_by(conn):
    """If upstream ever fixes this, the single-step design can be relaxed.

    `WorkflowRepository.getForWorkflowRun` selects workflow_step with no
    `ORDER BY "order"`, so Postgres returns the steps in whatever order it likes —
    the same query was observed returning [typeFilter, addToAlbums, clipFilter] on
    one call and the declared order on the next. When the add lands first, EVERY
    asset is filed regardless of the verdict.

    There is no way to assert Immich's JavaScript from here, so this asserts the
    thing that makes it dangerous: the table has an `order` column that nothing
    forces the read to use. Treat a failure as "go and re-read the repository".
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'workflow_step'"
    )
    columns = {r[0] for r in cur.fetchall()}
    if not columns:
        pytest.skip("this Immich has no workflow_step table")
    assert "order" in columns
    conn.rollback()
