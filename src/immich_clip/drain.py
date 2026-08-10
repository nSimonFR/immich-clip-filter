#!/usr/bin/env python3
"""Finish the verdicts the workflow could not reach — the ML-offline half.

CLIP inference usually runs on a separate GPU host, and that host is often off. A
photo uploaded in that window gets no embedding, so the workflow step cannot
decide anything about it and parks it on the pending queue instead of guessing.
This is what finishes the job; run it on a timer.

Each pass:

  1. retire entries that aged out, or whose asset was deleted since;
  2. for everything now embedded, compute the distance and file the matches;
  3. if anything is STILL unembedded and the ML server is reachable, kick Immich's
     `smartSearch` queue with force=false.

Step 3 is the part that is easy to miss. Immich does not re-queue missing
embeddings on its own: `handleNightlyJobs` covers missing thumbnails and face
clustering, and nothing else. A SmartSearch job that failed while the ML host was
down stays failed, so the asset would wait here forever no matter how patient this
drainer is. Hence the kick — rate-limited, because it queues every unembedded
asset in the library, not just ours.

Safe by default: `apply` is False unless configuration says otherwise, so a
Settings built from an empty environment reports and writes nothing.
"""

import argparse
import sys
import time

from . import api, exclusions, queue
from .config import load
from .logs import logger
from .sidecar import file_into_albums
from .state import load_json, save_json
from .store import (
    ProfileError,
    asset_owner,
    connect_pg,
    existing_asset_ids,
    resolve_rule,
    score_rule,
)

log = logger("immich-clip-drain")


def maybe_requeue_embeddings(cfg, waiting, now, opener=None, save=None, load_fn=None):
    """Ask Immich to embed what it never retried — at most once per interval.

    Returns a short reason string for the log, so a quiet pass still says WHY it
    was quiet.
    """
    if not waiting:
        return "nothing waiting on embeddings"
    stamp = (load_fn or load_json)(cfg.stamp_path, {})
    last = stamp.get("lastRequeue", 0)
    if now - last < cfg.requeue_every:
        return f"requeued {int(now - last)}s ago, holding off"
    if not api.ml_healthy(cfg, opener=opener):
        return "ML server unreachable — will retry next pass"
    if not cfg.apply:
        return f"would requeue smartSearch for {len(waiting)} waiting assets (dry run)"
    api.start_job(cfg, "smartSearch", force=False, opener=opener)
    (save or save_json)(cfg.stamp_path, dict(stamp, lastRequeue=int(now)))
    return f"requeued Immich smartSearch (missing) — {len(waiting)} assets waiting"


def run(cfg, connect=None, opener=None, queue_conn=None, now=None, log=log):
    now = time.time() if now is None else now
    conn_q = queue_conn if queue_conn is not None else queue.connect(cfg.queue_db)

    aged = queue.expire(conn_q, cfg.max_age_days, now)
    if aged:
        log(f"retired {len(aged)} entries older than {cfg.max_age_days}d "
            f"(never embedded): {aged[:3]}{' …' if len(aged) > 3 else ''}")

    items = queue.pending(conn_q)
    if not items:
        log("queue empty")
        return 0

    conn = (connect or connect_pg)(cfg)
    profiles, filed, decided, waiting = {}, 0, 0, []
    try:
        conn.autocommit = True
        cur = conn.cursor()

        # Learn any hand-removals before filing anything, so a photo taken out of
        # the album since it was queued is not put straight back.
        learned = exclusions.sync_from_audit(
            conn_q, cur, sorted({a for i in items for a in i["albumIds"]}), now)
        if learned:
            log(f"learned {len(learned)} new hand-removals — they will not be refiled")

        alive = existing_asset_ids(cur, [i["assetId"] for i in items])
        gone = queue.drop_missing(conn_q, alive, [i["assetId"] for i in items])
        if gone:
            log(f"dropped {len(gone)} entries whose asset was deleted")
        items = [i for i in items if i["assetId"] in alive]

        for item in items:
            name = item["profile"]
            if name not in profiles:
                try:
                    # Rebuilt from what was parked with the verdict: a seed-album
                    # rule is not reconstructable from a profile name alone.
                    profiles[name] = resolve_rule(cur, cfg.profile_dir, cfg.model or None, {
                        "profile": name,
                        "seedAlbum": item.get("seedAlbum") or "",
                        "scoring": item.get("scoring") or "",
                    })
                except ProfileError as e:
                    profiles[name] = None
                    log(f"rule {name!r} unusable, leaving its entries queued: {e}")
            profile = profiles[name]
            if profile is None:
                continue

            distance = score_rule(cur, item["assetId"], profile)
            if distance is None:
                waiting.append(item["assetId"])
                queue.bump(conn_q, item["assetId"], item["profile"])
                continue

            decided += 1
            match = distance <= item["threshold"]
            wanted = exclusions.allowed(conn_q, item["assetId"], item["albumIds"])
            note = ""
            if match and wanted:
                if cfg.apply:
                    owner_id, email = asset_owner(cur, item["assetId"])
                    try:
                        filed += file_into_albums(
                            cfg, wanted, item["assetId"], owner=email or owner_id)
                    except api.NoKeyForOwner as e:
                        # Leave it queued: the next pass will retry, and once a
                        # key for that owner is configured it files itself.
                        note = f" — NOT filed: {e}"
                        log(f"{item['assetId']}{note}")
                        continue
                else:
                    filed += 1  # counted, not written
            log(f"{item['assetId']} d={distance:.4f} "
                f"{'MATCH' if match else 'no'}{'' if cfg.apply else ' (dry run)'}")
            if cfg.apply:
                queue.resolve(conn_q, item["assetId"], item["profile"])
    finally:
        conn.close()

    log(f"decided {decided}, filed {filed}, still waiting {len(waiting)}"
        f"{'' if cfg.apply else ' — DRY RUN, nothing written'}")
    log(maybe_requeue_embeddings(cfg, waiting, now, opener=opener))
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="immich-clip-drain")
    ap.add_argument("--config", default=None, help="path to config.toml")
    # Both directions available, because the safe default is dry-run and the
    # timer needs to opt out of it, while a human debugging a live timer needs to
    # opt back in.
    ap.add_argument("--apply", action="store_true", help="actually file the matches")
    ap.add_argument("--dry-run", action="store_true", help="report only (the default)")
    return ap.parse_args(argv)


def main(argv=None, env=None, connect=None, opener=None, queue_conn=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cfg = load(args.config, env=env)
    if args.apply:
        cfg = cfg.with_(apply=True)
    if args.dry_run:
        cfg = cfg.with_(apply=False)
    return run(cfg, connect=connect, opener=opener, queue_conn=queue_conn)


if __name__ == "__main__":
    sys.exit(main())
