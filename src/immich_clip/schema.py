"""The startup schema probe — the price of reading Immich's internal tables.

This project depends on Immich's database schema, which is not an API and carries
no stability promise. That dependency is unavoidable (the REST smart-search
endpoint returns ranked assets *without distances*, so a threshold cannot be
applied through it), so the answer is not to hide it but to check it, loudly, at
startup.

Without this, an Immich upgrade that renames a column shows up as: every asset
fails closed, every workflow quietly stops filing, and nobody notices until an
album is months stale. With it, the sidecar refuses to start and names the table
and column it could not find.

`contract` tests in tests/contract/ run this same probe against a real Immich, so
an upgrade breaks CI rather than somebody's album.
"""

from .logs import logger

log = logger("immich-clip-schema")

#: Every table and column the SQL in store.py, exclusions.py and backfill.py
#: touches. Keep this in step with the queries — a column added there and not
#: here is a column whose disappearance we would not catch.
REQUIRED = {
    "smart_search": ["assetId", "embedding"],
    "album": ["id", "albumName", "deletedAt"],
    "album_asset": ["albumId", "assetId"],
    # Immich's own removal log. Pruned after ~31 days, which is why exclusions.py
    # copies out of it rather than reading it as the source of truth.
    "album_asset_audit": ["albumId", "assetId"],
    "asset": ["id", "ownerId", "deletedAt", "type", "visibility", "originalFileName"],
    "user": ["id", "email"],
}


class SchemaMismatch(Exception):
    """Immich's schema is not the shape the SQL expects."""


def missing(cur, required=None):
    """Which (table, column) pairs are absent. Empty list means all present."""
    required = REQUIRED if required is None else required
    cur.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = ANY(%s)",
        (list(required),),
    )
    have = {(t, c) for t, c in cur.fetchall()}
    tables = {t for t, _ in have}
    problems = []
    for table, columns in required.items():
        if table not in tables:
            problems.append((table, "*"))
            continue
        problems.extend((table, c) for c in columns if (table, c) not in have)
    return problems


def vector_ops_work(cur):
    """Does pgvector answer `<=>` and `AVG(vector)`?

    Both are load-bearing: `<=>` is every distance, and `AVG` is what lets a rule
    name a seed album without precomputing anything. A pgvector that is installed
    but too old for the aggregate would otherwise fail on the first centroid rule
    rather than at startup.
    """
    try:
        cur.execute("SELECT '[1,0]'::vector <=> '[0,1]'::vector")
        cur.fetchone()
        cur.execute("SELECT AVG(v) FROM (SELECT '[1,0]'::vector AS v) t")
        cur.fetchone()
        return True
    except Exception:  # noqa: BLE001 - any failure means "cannot rely on it"
        return False


def check(cur, required=None):
    """Raise SchemaMismatch listing everything wrong, or return quietly.

    Everything at once, deliberately: fixing a compatibility break one error
    message at a time, restarting between each, is how a five-minute upgrade
    becomes an afternoon.
    """
    problems = missing(cur, required)
    notes = [
        f"  {table}.{column}" if column != "*" else f"  {table} (whole table)"
        for table, column in problems
    ]
    if not vector_ops_work(cur):
        notes.append("  pgvector: `<=>` or AVG(vector) did not work")
    if notes:
        raise SchemaMismatch(
            "Immich's database is not the shape this version expects. Missing:\n"
            + "\n".join(notes)
            + "\n\nThis usually means Immich was upgraded past a tested version. "
            "See docs/limitations.md for the compatibility matrix; set "
            "IMMICH_CLIP_CHECK_SCHEMA=0 to start anyway (filing will likely fail)."
        )


def check_settings(cfg, connect=None, log=log):
    """Probe using cfg's connection. Returns True when the schema is usable.

    Never raises on a *connection* failure: the database being down at boot is an
    ordering problem, not a compatibility one, and the sidecar already fails
    closed per request. Only an actual mismatch stops startup.
    """
    from .store import connect_pg

    try:
        conn = (connect or connect_pg)(cfg)
    except Exception as e:  # noqa: BLE001
        log(f"could not reach Postgres to check the schema ({e}) — starting anyway")
        return True
    try:
        conn.autocommit = True
        check(conn.cursor())
        log("schema OK")
        return True
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
