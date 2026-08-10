"""The startup probe and the preflight command.

This project reads Immich's internal schema, which carries no stability promise.
An upgrade that renames a column would otherwise show up as: every asset fails
closed, every workflow quietly stops filing, and nobody notices until an album is
months stale. The probe turns that into a refusal at startup naming the column.

The doctor exists for the same reason at a different level — every check it makes
is one whose failure is invisible from the Immich UI.
"""

import pytest
from fakes import FakeConn, FakeCursor, FakeOpener, json_reply, settings

from immich_clip import doctor, schema
from immich_clip.config import ApiKey

# What information_schema returns when everything is present.
ALL_COLUMNS = [
    (table, column)
    for table, columns in schema.REQUIRED.items()
    for column in columns
]


def healthy_cursor(extra=()):
    """A cursor answering the probe's three queries in order."""
    return FakeCursor([ALL_COLUMNS, [(0.5,)], [("[1,0]",)], *extra])


# ── the probe ────────────────────────────────────────────────────────────────
def test_a_complete_schema_passes_quietly():
    schema.check(healthy_cursor())   # does not raise


def test_a_missing_column_is_named():
    without = [c for c in ALL_COLUMNS if c != ("smart_search", "embedding")]
    cur = FakeCursor([without, [(0.5,)], [("[1,0]",)]])
    with pytest.raises(schema.SchemaMismatch) as e:
        schema.check(cur)
    assert "smart_search.embedding" in str(e.value)


def test_a_missing_table_is_reported_as_a_whole_table():
    without = [c for c in ALL_COLUMNS if c[0] != "album_asset_audit"]
    cur = FakeCursor([without, [(0.5,)], [("[1,0]",)]])
    with pytest.raises(schema.SchemaMismatch) as e:
        schema.check(cur)
    assert "album_asset_audit (whole table)" in str(e.value)


def test_every_problem_is_reported_at_once():
    # Fixing a compatibility break one error message at a time, restarting
    # between each, is how a five-minute upgrade becomes an afternoon.
    without = [c for c in ALL_COLUMNS
               if c not in (("smart_search", "embedding"), ("album", "albumName"))]
    cur = FakeCursor([without, [(0.5,)], [("[1,0]",)]])
    with pytest.raises(schema.SchemaMismatch) as e:
        schema.check(cur)
    assert "smart_search.embedding" in str(e.value)
    assert "album.albumName" in str(e.value)


def test_a_pgvector_that_cannot_do_the_operators_we_need_is_a_mismatch():
    # `<=>` is every distance and AVG(vector) is what lets a rule name an album;
    # an install where those fail would break on the first rule, not at startup.
    class NoVectors(FakeCursor):
        def execute(self, sql, params=()):
            if "vector" in sql and "information_schema" not in sql:
                raise RuntimeError('operator does not exist: vector <=> vector')
            super().execute(sql, params)

    with pytest.raises(schema.SchemaMismatch) as e:
        schema.check(NoVectors([ALL_COLUMNS]))
    assert "pgvector" in str(e.value)


def test_the_error_points_at_the_compatibility_matrix():
    cur = FakeCursor([[], [(0.5,)], [("[1,0]",)]])
    with pytest.raises(schema.SchemaMismatch) as e:
        schema.check(cur)
    assert "docs/limitations.md" in str(e.value)


def test_the_required_set_covers_every_table_the_sql_actually_touches():
    """A column used in a query and absent here is one we would not catch.

    Cheap and blunt: grep the SQL in the modules that talk to Postgres for
    `FROM <table>` / `JOIN <table>` and assert the probe knows about each.
    """
    import re
    from pathlib import Path

    import immich_clip

    root = Path(immich_clip.__file__).parent
    found = set()
    for name in ("store.py", "exclusions.py", "backfill.py"):
        sql = (root / name).read_text()
        for m in re.finditer(r'(?:FROM|JOIN)\s+"?([a-z_]+)"?', sql):
            found.add(m.group(1))
    # Aliases and CTE names are not tables.
    found -= {"smart_search_a", "scored", "seeds", "c", "t"}
    # SQLite-side tables (our own state) and query aliases are not Immich's.
    known = set(schema.REQUIRED) | {"a", "b", "s", "sc", "aa", "excluded", "pending"}
    assert found <= known, f"SQL touches unprobed tables: {sorted(found - known)}"


def test_a_database_that_is_merely_down_does_not_stop_startup(logged):
    # Ordering, not compatibility: the sidecar already fails closed per request,
    # and refusing to boot because Postgres is slow would be worse.
    lines, log = logged

    def refuse(_cfg):
        raise OSError("connection refused")

    assert schema.check_settings(settings(), connect=refuse, log=log) is True
    assert any("starting anyway" in line for line in lines)


def test_a_real_mismatch_does_stop_startup():
    conn = FakeConn(FakeCursor([[], [(0.5,)], [("[1,0]",)]]))
    with pytest.raises(schema.SchemaMismatch):
        schema.check_settings(settings(), connect=lambda c: conn, log=lambda m: None)


# ── the doctor ───────────────────────────────────────────────────────────────
def a_healthy_immich():
    return FakeOpener([
        json_reply({"email": "nico@example.com"}),                       # /users/me
        json_reply({"machineLearning": {"clip": {"modelName": "ViT-H-14"},
                                        "urls": ["http://ml:3003"]}}),   # clip_model
        json_reply({"machineLearning": {"urls": ["http://ml:3003"]}}),   # ml_url
        json_reply({}),                                                  # /ping
    ])


def a_healthy_db():
    return FakeConn(FakeCursor([ALL_COLUMNS, [(0.5,)], [("[1,0]",)], [(100,)], [(100,)]]))


def test_a_healthy_install_reports_no_problems(tmp_path):
    cfg = settings(api_key="k", state_dir=str(tmp_path))
    out = []
    rc = doctor.run(cfg, connect=lambda c: a_healthy_db(), opener=a_healthy_immich(),
                    out=out.append)
    assert rc == 0
    assert "0 to look at" in "\n".join(out)


def test_no_api_key_is_flagged_because_it_decides_but_cannot_file(tmp_path):
    cfg = settings(state_dir=str(tmp_path))
    out = []
    rc = doctor.run(cfg, connect=lambda c: a_healthy_db(),
                    opener=FakeOpener([json_reply({"machineLearning": {}})]), out=out.append)
    assert rc == 1
    assert any("cannot file" in line for line in out)


def test_a_key_that_belongs_to_someone_else_is_caught(tmp_path):
    # The quiet killer: a key labelled for one user but minted by another files
    # nothing for them and reports `no_permission`.
    cfg = settings(keys=[ApiKey("alfie@example.com", "k")], state_dir=str(tmp_path))
    op = FakeOpener([
        json_reply({"email": "nico@example.com"}),
        json_reply({"machineLearning": {"clip": {"modelName": "m"}, "urls": ["http://ml"]}}),
        json_reply({"machineLearning": {"urls": ["http://ml"]}}),
        json_reply({}),
    ])
    out = []
    assert doctor.run(cfg, connect=lambda c: a_healthy_db(), opener=op, out=out.append) == 1
    assert any("owner mismatch" in line for line in out)


def test_an_unembedded_tail_is_reported_because_it_can_never_match(tmp_path):
    # Immich never re-queues missing CLIP embeddings on its own, so a library can
    # sit with a large unembedded tail indefinitely and no rule will ever fire.
    cfg = settings(api_key="k", state_dir=str(tmp_path))
    db = FakeConn(FakeCursor([ALL_COLUMNS, [(0.5,)], [("[1,0]",)], [(50,)], [(2125,)]]))
    out = []
    assert doctor.run(cfg, connect=lambda c: db, opener=a_healthy_immich(), out=out.append) == 1
    assert any("2075 images have no embedding" in line for line in out)


def test_an_unreachable_database_is_reported_not_raised(tmp_path):
    cfg = settings(api_key="k", state_dir=str(tmp_path))

    def refuse(_cfg):
        raise OSError("connection refused")

    out = []
    assert doctor.run(cfg, connect=refuse, opener=a_healthy_immich(), out=out.append) == 1
    assert any("cannot connect" in line for line in out)
