"""Remembering "I took that out of the album".

Everything here is append-only: the workflow files photos and never removes one.
That is deliberate — a bad threshold should be a cleanup job, not data loss. But
it means a photo you delete from an album by hand comes back the next time
`immich-clip-backfill --apply` runs, because that scan excludes only CURRENT
members and a removed match is a fresh candidate again.

So a removal is treated as a decision. Immich already records every one in
`album_asset_audit(albumId, assetId, deletedAt)` — but prunes it after 31 days
(`MAX_DAYS = 30` in sync.service.js), so it cannot be the memory itself. Each pass
copies anything new out of that window into our own table, which is never pruned,
and every filing path skips the pairs it holds.

Net effect: taking a photo out of the album in the Immich UI means "never file
this here again", learned from what you already do, with nothing extra to run.
"""


def sync_from_audit(state, cur, album_ids, now=None):
    """Copy new removals out of Immich's audit window into our own table.

    Returns the pairs newly learned, so a caller can log them — a silently growing
    exclusion set would be as opaque as the problem it fixes.
    """
    if not album_ids:
        return []
    cur.execute(
        'SELECT "albumId", "assetId" FROM album_asset_audit '
        'WHERE "albumId" = ANY(%s::uuid[])',
        (list(album_ids),),
    )
    seen = [(str(a), str(b)) for a, b in cur.fetchall()]
    if not seen:
        return []

    have = {
        (r[0], r[1])
        for r in state.execute(
            "SELECT albumId, assetId FROM excluded WHERE albumId IN (%s)"
            % ",".join("?" * len(album_ids)),
            list(album_ids),
        ).fetchall()
    }
    fresh = [p for p in seen if p not in have]
    if fresh:
        state.executemany(
            "INSERT OR IGNORE INTO excluded (albumId, assetId, since) VALUES (?, ?, ?)",
            [(a, b, int(now or 0)) for a, b in fresh],
        )
        state.commit()

    # Symmetry: putting a photo BACK in by hand is just as clear a signal as
    # taking it out, so it clears the exclusion. Without this, one accidental
    # removal would ban the photo from the album for good, and the only cure would
    # be editing a SQLite file.
    clear_readded(state, cur, album_ids)
    return fresh


def clear_readded(state, cur, album_ids):
    """Forget exclusions for pairs that are currently in the album again."""
    if not album_ids:
        return []
    cur.execute(
        'SELECT "albumId", "assetId" FROM album_asset WHERE "albumId" = ANY(%s::uuid[])',
        (list(album_ids),),
    )
    present = [(str(a), str(b)) for a, b in cur.fetchall()]
    if not present:
        return []
    state.executemany("DELETE FROM excluded WHERE albumId = ? AND assetId = ?", present)
    state.commit()
    return present


def for_album(state, album_id):
    """Asset ids that must never be filed into this album again."""
    return {
        r[0]
        for r in state.execute(
            "SELECT assetId FROM excluded WHERE albumId = ?", (album_id,)
        ).fetchall()
    }


def albums_for_asset(state, asset_id):
    """Albums this asset was taken out of."""
    return {
        r[0]
        for r in state.execute(
            "SELECT albumId FROM excluded WHERE assetId = ?", (asset_id,)
        ).fetchall()
    }


def allowed(state, asset_id, album_ids):
    """`album_ids` minus the ones this asset was removed from."""
    if state is None:
        return list(album_ids)
    blocked = albums_for_asset(state, asset_id)
    return [a for a in album_ids if a not in blocked]
