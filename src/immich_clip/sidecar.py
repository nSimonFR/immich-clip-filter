#!/usr/bin/env python3
"""The verdict service behind the workflow step.

    POST /classify {"assetId", "profile"|"seedAlbum", "threshold", "waitSec", "albumIds"}
      -> {"match": true,  "distance": 0.21, "filed": 1}
      -> {"match": false, "distance": 0.55, "reason": "over threshold"}
      -> {"match": false, "reason": "...", "undecided": true, "queued": true}

Why a sidecar exists at all: the WASM plugin's only escape hatch is Immich's
`httpRequest` host function, which returns `body: await res.text()`. No image
bytes can cross that, so the plugin cannot call CLIP and something outside it must
decide.

Why it runs no inference either: Immich's own SmartSearch job already embeds every
asset and writes `smart_search.embedding`. Reading that costs nothing, avoids a
second GPU pass, and sidesteps decoding HEIC originals.

THREE outcomes, not two. An asset whose embedding does not exist yet is not "not
food" — it is *undecided*. On a deployment whose ML host sleeps, that is the
common case rather than the edge case. Undecided assets go on the pending queue
and `immich-clip-drain` finishes them once Immich has embedded them. Only a
genuine over-threshold distance is a no.

Everything else fails closed: an unknown profile, an unreachable database, a
malformed request all answer `match: false` without queueing. A false negative
leaves one photo out of an album; a false positive files the whole camera roll.
"""

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import api, exclusions, queue, schema
from .config import load
from .logs import logger
from .store import ProfileError, asset_owner, connect_pg, resolve_rule, score_rule

UUID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")

log = logger("immich-clip-filter")


def no(reason, **extra):
    return dict({"match": False, "reason": reason}, **extra)


def classify(cfg, req, connect=None, sleep=None, monotonic=None):
    """Decide one asset. Pure except for the three injected seams.

    Returns `undecided: True` when the embedding simply is not there yet — the
    caller queues those rather than treating them as a no.
    """
    do_connect = connect or connect_pg
    do_sleep = sleep or time.sleep
    clock = monotonic or time.monotonic

    asset_id = str(req.get("assetId") or "")
    if not UUID_RE.match(asset_id):
        return no(f"bad assetId {asset_id[:40]!r}")

    try:
        threshold = float(req.get("threshold"))
    except (TypeError, ValueError):
        return no("bad threshold")

    wait_sec = min(max(int(req.get("waitSec") or 0), 0), cfg.max_wait_sec)

    try:
        conn = do_connect(cfg)
    except Exception as e:  # noqa: BLE001 - a DB outage must not 500 the workflow
        return no(f"database unreachable: {e}")

    started = clock()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        # Resolved against the DB because a rule may name a seed ALBUM, whose
        # membership is read live — that is what makes a rule definable purely in
        # the Immich UI.
        try:
            rule = resolve_rule(cur, cfg.profile_dir, cfg.model or None, req)
        except ProfileError as e:
            return no(str(e))
        while True:
            distance = score_rule(cur, asset_id, rule)
            waited = round(clock() - started, 1)
            if distance is not None:
                return {
                    "match": distance <= threshold,
                    "distance": round(distance, 4),
                    "waitedSec": waited,
                    "profile": rule["name"],
                    **({} if distance <= threshold else {"reason": "over threshold"}),
                }
            if waited >= wait_sec:
                return no(
                    "not embedded yet — queued until the ML server catches up",
                    undecided=True,
                    waitedSec=waited,
                    profile=rule["name"],
                )
            do_sleep(cfg.poll_sec)
    except Exception as e:  # noqa: BLE001
        return no(f"lookup failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def file_into_albums(cfg, album_ids, asset_id, add_assets=None, owner=None):
    """Add one asset to each configured album. Returns how many took it.

    Filing lives here, not in the WASM plugin, so the immediate path and the
    drained path share one implementation — and so it is testable in Python rather
    than only through a wasm harness.
    """
    do_add = add_assets or api.add_assets
    filed = 0
    for album_id in album_ids:
        filed += do_add(cfg, album_id, [asset_id], log=lambda m: None, owner=owner)
    return filed


def _owner_and_exclusions(cfg, state, asset_id, album_ids, now, connect=None, log=log):
    """One Postgres round trip that does two things, and never raises.

    * refreshes the exclusion table from Immich's audit log — see below;
    * resolves who owns the asset, so the right API key is used to file it.

    Learning removals HERE, not just on the drain tick, is load-bearing.
    `excluded` is populated by `sync_from_audit`, which used to run only in
    drain/backfill — so a photo taken out by hand could be refiled by the live
    workflow for up to a full drain interval afterwards. Immich's audit row is
    written synchronously with the removal, so reading it here closes that
    window. One extra connection per MATCH only, which is a few percent of
    uploads.

    A failure logs and proceeds: a stale exclusion set is a far smaller problem
    than a rule that stops filing.
    """
    try:
        pg = (connect or connect_pg)(cfg)
    except Exception as e:  # noqa: BLE001
        log(f"could not refresh exclusions: {e}")
        return None
    try:
        pg.autocommit = True
        cur = pg.cursor()
        exclusions.sync_from_audit(state, cur, album_ids, now)
        owner_id, email = asset_owner(cur, asset_id)
        return email or owner_id
    except Exception as e:  # noqa: BLE001
        log(f"could not refresh exclusions: {e}")
        return None
    finally:
        try:
            pg.close()
        except Exception:  # noqa: BLE001
            pass


def handle(cfg, req, classify_fn=None, queue_conn=None, add_assets=None, now=None,
           connect=None, log=log):
    """One /classify request: decide, then file or queue.

    `queue_conn` is for tests. In production each call opens its own SQLite
    connection: this runs under ThreadingHTTPServer, and a connection is bound to
    the thread that created it — sharing one raises "SQLite objects created in a
    thread can only be used in that same thread" and silently loses the queue
    write. Connections are cheap and enqueues are rare, so per-call it is.
    """
    result = (classify_fn or (lambda r: classify(cfg, r)))(req)
    album_ids = req.get("albumIds") or []

    if result.get("undecided"):
        conn, owned = (queue_conn, False)
        try:
            if conn is None:
                conn, owned = queue.connect(cfg.queue_db), True
            queue.enqueue(
                conn,
                req["assetId"],
                # The RULE identity, which for a seed-album rule is "album:<name>"
                # rather than a profile filename.
                result.get("profile") or req.get("profile") or "",
                req.get("threshold") or 0,
                album_ids,
                now if now is not None else time.time(),
                seed_album=req.get("seedAlbum") or "",
                scoring=req.get("scoring") or "",
            )
            return dict(result, queued=True)
        except Exception as e:  # noqa: BLE001 - failing to queue must not 500
            return dict(result, queued=False, queueError=str(e))
        finally:
            if owned and conn is not None:
                conn.close()

    if result.get("match") and album_ids:
        conn, owned = (queue_conn, False)
        try:
            if conn is None:
                conn, owned = queue.connect(cfg.queue_db), True
            owner = _owner_and_exclusions(
                cfg, conn, req["assetId"], album_ids, now, connect=connect, log=log
            )
            wanted = exclusions.allowed(conn, req["assetId"], album_ids)
            skipped = len(album_ids) - len(wanted)
            result = dict(result, filed=file_into_albums(
                cfg, wanted, req["assetId"], add_assets=add_assets, owner=owner))
            if skipped:
                result = dict(result, excluded=skipped)
        except api.NoKeyForOwner as e:
            # Distinct from a generic failure on purpose: this one is a
            # configuration gap, not a transient error, and it will not fix
            # itself on the next pass.
            result = dict(result, filed=0, ownerError=str(e))
        except Exception as e:  # noqa: BLE001
            # The verdict stands even if filing failed; immich-clip-backfill will
            # pick it up. Do not turn this into a false negative.
            result = dict(result, filed=0, fileError=str(e))
        finally:
            if owned and conn is not None:
                conn.close()
    return result


def make_handler(cfg, handle_fn=None, log=log):
    do_handle = handle_fn or (lambda req: handle(cfg, req))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)
            try:
                req = json.loads(raw or b"{}")
            except ValueError:
                return self._reply(200, no("request body was not JSON"))
            try:
                result = do_handle(req)
            except Exception as e:  # noqa: BLE001 - one bad asset must not kill the server
                result = no(f"unhandled: {e}")
            log(f"{req.get('assetId', '?')} -> {json.dumps(result)}")
            # Always 200: the plugin distinguishes on the body, and a non-2xx
            # would only turn a clean verdict into an opaque transport error.
            self._reply(200, result)

        def do_GET(self):
            self._reply(200, {"ok": True, "service": "immich-clip-filter"})

    return H


def serve(cfg, server_class=ThreadingHTTPServer):
    log(f"listening {cfg.listen_addr}:{cfg.listen_port} "
        f"(profiles {cfg.profile_dir}, queue {cfg.queue_db}, "
        f"model {cfg.model or 'unchecked'}, "
        f"keys for {', '.join(cfg.owners()) or 'nobody — filing will fail'})")
    server_class((cfg.listen_addr, cfg.listen_port), make_handler(cfg)).serve_forever()


def main(argv=None, env=None, serve_fn=None, queue_connect=None, check=None):
    cfg = load(env=env)
    if not cfg.model:
        log("WARNING: no CLIP model configured — profiles will not be checked "
            "against the live one (set clip_model, or let the tools ask Immich)")
    if cfg.check_schema:
        (check or schema.check_settings)(cfg)
    # Create the file and schema once up front, on the main thread, so a request
    # never races the first CREATE TABLE. Each request opens its own connection.
    (queue_connect or queue.connect)(cfg.queue_db).close()
    (serve_fn or serve)(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
