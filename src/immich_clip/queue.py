"""The pending queue — verdicts that could not be reached yet.

A photo uploaded while the ML server is offline has no CLIP embedding, so the
workflow step cannot decide anything about it. The obvious behaviour is to wait a
while and then answer "no match", but that is a lie: the answer is *unknown*, and
it gets thrown away. On a deployment whose GPU host is usually asleep, that loses
most of the decisions.

So an undecidable asset is recorded here instead, and `drain` finishes the job
once Immich has embedded it. Same shape as Immich's own ML offload: the work
queues, waits for the GPU host, and completes when it returns.

SQLite rather than a JSON file because two processes write it — the sidecar on
every undecidable asset, the drainer when it resolves one. WAL mode so a drain
pass never blocks a live classify.
"""

import json
import sqlite3

# The key is (assetId, profile), NOT assetId. More than one workflow can be
# watching the same library — a `food` rule and a `pets` rule, say — and both park
# the same undecidable asset. Under an assetId-only primary key the second enqueue
# silently overwrote the first, so one of the two verdicts was lost with nothing
# to show for it.
SCHEMA = """
CREATE TABLE IF NOT EXISTS pending (
    assetId    TEXT NOT NULL,
    profile    TEXT NOT NULL,
    threshold  REAL NOT NULL,
    albumIds   TEXT NOT NULL,
    enqueuedAt INTEGER NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    seedAlbum  TEXT NOT NULL DEFAULT '',
    scoring    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (assetId, profile)
);

-- Not the queue, but the same state database, and one place should own the
-- schema. See exclusions.py: (album, asset) pairs the user took OUT of an album
-- by hand, which nothing may ever file back in.
CREATE TABLE IF NOT EXISTS excluded (
    albumId TEXT NOT NULL,
    assetId TEXT NOT NULL,
    since   INTEGER NOT NULL,
    PRIMARY KEY (albumId, assetId)
);
"""


def _migrate(conn):
    """Move an assetId-keyed table onto the composite key, keeping its rows.

    SQLite cannot alter a primary key in place, so this is copy-and-swap. Rows are
    preserved rather than dropped: they are undecided verdicts, and throwing them
    away is exactly the failure the queue exists to prevent. Kept for anyone
    upgrading from a build that predates multi-rule support.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pending'"
    ).fetchone()
    if not row or "PRIMARY KEY (assetId, profile)" in row[0]:
        return False
    conn.executescript(
        """
        ALTER TABLE pending RENAME TO pending_old;
        """
        + SCHEMA
        + """
        INSERT OR IGNORE INTO pending
            (assetId, profile, threshold, albumIds, enqueuedAt, attempts)
        SELECT assetId, profile, threshold, albumIds, enqueuedAt, attempts
        FROM pending_old;
        DROP TABLE pending_old;
        """
    )
    conn.commit()
    return True


def _ensure_columns(conn):
    """Add the seedAlbum/scoring columns to a queue created before they existed.

    A parked verdict has to carry enough to be re-decided later, and a rule that
    names a seed album is not reconstructable from a profile name alone.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(pending)").fetchall()}
    for col in ("seedAlbum", "scoring"):
        if col not in have:
            conn.execute(f"ALTER TABLE pending ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    conn.commit()


def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    _ensure_columns(conn)
    return conn


def enqueue(conn, asset_id, profile, threshold, album_ids, now, seed_album="", scoring=""):
    """Record an undecidable asset. Idempotent on (assetId, profile).

    A re-trigger of the same asset (a metadata refresh, say) must not create a
    second row, but it should refresh the config — the workflow's threshold or
    target album may have been edited since. A DIFFERENT profile for the same
    asset is a separate row, not an overwrite.
    """
    conn.execute(
        """INSERT INTO pending
             (assetId, profile, threshold, albumIds, enqueuedAt, seedAlbum, scoring)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(assetId, profile) DO UPDATE SET
             threshold=excluded.threshold,
             albumIds=excluded.albumIds,
             seedAlbum=excluded.seedAlbum,
             scoring=excluded.scoring""",
        (asset_id, profile, float(threshold), json.dumps(list(album_ids)), int(now),
         seed_album, scoring),
    )
    conn.commit()


def pending(conn, limit=1000):
    rows = conn.execute(
        """SELECT assetId, profile, threshold, albumIds, enqueuedAt, attempts,
                  seedAlbum, scoring
           FROM pending ORDER BY enqueuedAt LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {
            "assetId": r[0],
            "profile": r[1],
            "threshold": r[2],
            "albumIds": json.loads(r[3]),
            "enqueuedAt": r[4],
            "attempts": r[5],
            "seedAlbum": r[6],
            "scoring": r[7],
        }
        for r in rows
    ]


def count(conn):
    return conn.execute("SELECT count(*) FROM pending").fetchone()[0]


def resolve(conn, asset_id, profile):
    """Drop one decided (asset, profile) pair.

    Called for match AND no-match alike — the queue holds undecided work, not
    unfiled work. Scoped to the profile so resolving a `food` verdict does not
    also discard a still-pending `pets` one for the same photo.
    """
    conn.execute(
        "DELETE FROM pending WHERE assetId = ? AND profile = ?", (asset_id, profile)
    )
    conn.commit()


def bump(conn, asset_id, profile):
    conn.execute(
        "UPDATE pending SET attempts = attempts + 1 WHERE assetId = ? AND profile = ?",
        (asset_id, profile),
    )
    conn.commit()


def expire(conn, max_age_days, now):
    """Drop entries older than the cut-off, returning what was dropped.

    Without this the queue grows forever on assets that will never be embedded
    (deleted originals, formats the ML server rejects). Callers log the return
    value — a silently shrinking queue would hide a real problem.
    """
    cutoff = int(now) - int(max_age_days) * 86400
    rows = conn.execute(
        "SELECT assetId FROM pending WHERE enqueuedAt < ?", (cutoff,)
    ).fetchall()
    conn.execute("DELETE FROM pending WHERE enqueuedAt < ?", (cutoff,))
    conn.commit()
    return [r[0] for r in rows]


def drop_missing(conn, known_asset_ids, candidates):
    """Drop queued assets that no longer exist (deleted since being queued)."""
    gone = [a for a in candidates if a not in known_asset_ids]
    if gone:
        conn.executemany("DELETE FROM pending WHERE assetId = ?", [(a,) for a in gone])
        conn.commit()
    return gone
