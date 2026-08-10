#!/usr/bin/env python3
"""`immich-clip-doctor` — check an install before trusting it with a workflow.

Every check here exists because the failure it catches is invisible from the
Immich UI. A rule that matches and files nothing looks identical to a rule that
never matched; a plugin that failed to import looks identical to one whose step
was never added; a key belonging to the wrong user looks like nothing at all.

Nothing here writes. Run it after installing, and after upgrading Immich.
"""

import argparse
import sys

from . import api, queue, schema
from .config import ConfigError, config_path, load
from .logs import logger
from .store import connect_pg

log = logger("immich-clip-doctor")


def _ok(name, detail=""):
    return (True, name, detail)


def _bad(name, detail):
    return (False, name, detail)


def check_config(cfg, env=None):
    path = config_path(env)
    yield _ok("config file", path or "none — using defaults + environment")
    if not cfg.keys:
        yield _bad(
            "api keys",
            "none configured; the sidecar can decide but cannot file anything",
        )
    else:
        yield _ok("api keys", ", ".join(k.owner for k in cfg.keys))
    yield _ok("immich url", cfg.immich_url)
    yield _ok("state", f"{cfg.profile_dir} + {cfg.queue_db}")


def check_database(cfg, connect=None):
    try:
        conn = (connect or connect_pg)(cfg)
    except Exception as e:  # noqa: BLE001
        yield _bad("postgres", f"cannot connect: {e}")
        return
    try:
        conn.autocommit = True
        cur = conn.cursor()
        try:
            schema.check(cur)
            yield _ok("schema", f"all {len(schema.REQUIRED)} tables present, pgvector OK")
        except schema.SchemaMismatch as e:
            yield _bad("schema", str(e).splitlines()[0])
        # The one number that predicts "my rule matches nothing": Immich never
        # re-queues missing CLIP embeddings on its own, so a library can sit with
        # a large unembedded tail indefinitely.
        #
        # Counted as an anti-join rather than `count(smart_search)` vs
        # `count(images)`. Immich embeds videos too, so on a real library the
        # naive comparison reported "6113 of 5535 images embedded" — two
        # populations subtracted from each other, and a gap that could go
        # negative and hide a genuine backlog.
        cur.execute(
            'SELECT count(*) FROM asset a WHERE a."deletedAt" IS NULL '
            "AND a.type = 'IMAGE' AND a.visibility NOT IN ('hidden', 'locked') "
            'AND NOT EXISTS (SELECT 1 FROM smart_search s WHERE s."assetId" = a.id)'
        )
        missing_embeddings = cur.fetchone()[0]
        cur.execute(
            'SELECT count(*) FROM asset WHERE "deletedAt" IS NULL AND type = \'IMAGE\' '
            "AND visibility NOT IN ('hidden', 'locked')"
        )
        images = cur.fetchone()[0]
        if missing_embeddings == 0:
            yield _ok("embeddings", f"all {images} images embedded")
        else:
            yield _bad(
                "embeddings",
                f"{missing_embeddings} of {images} images have no embedding — those "
                "can never match. Run the drainer (it kicks Immich's smartSearch "
                "queue) or start that job by hand.",
            )
    finally:
        conn.close()


def check_api(cfg, opener=None):
    for key in cfg.keys or ():
        try:
            me = api.whoami(cfg.with_(keys=(key,)), opener=opener)
        except Exception as e:  # noqa: BLE001
            yield _bad(f"key {key.owner}", f"rejected: {e}")
            continue
        email = me.get("email") or "?"
        if not key.is_default and email.lower() != key.owner.lower():
            # The quiet killer: a key labelled for one user but minted by another
            # files nothing for that user and reports `no_permission`.
            yield _bad(
                f"key {key.owner}", f"actually belongs to {email} — owner mismatch")
        else:
            yield _ok(f"key {key.owner}", f"valid, belongs to {email}")
    try:
        model = api.clip_model(cfg, opener=opener)
        yield _ok("clip model", model)
    except (Exception, SystemExit) as e:  # noqa: BLE001 - api raises SystemExit,
        # which is a BaseException; the doctor reports, it does not exit.
        yield _bad("clip model", str(e))
    yield (
        _ok("ml server", "reachable")
        if api.ml_healthy(cfg, opener=opener)
        else _bad("ml server", "unreachable — new uploads will queue until it returns")
    )


def check_queue(cfg):
    try:
        conn = queue.connect(cfg.queue_db)
    except Exception as e:  # noqa: BLE001
        yield _bad("queue", f"cannot open {cfg.queue_db}: {e}")
        return
    try:
        n = queue.count(conn)
        yield _ok("queue", f"{n} verdicts parked")
    finally:
        conn.close()


def run(cfg, env=None, connect=None, opener=None, out=print):
    checks = (
        list(check_config(cfg, env))
        + list(check_database(cfg, connect=connect))
        + list(check_api(cfg, opener=opener))
        + list(check_queue(cfg))
    )
    for ok, name, detail in checks:
        out(f"  {'✓' if ok else '✗'}  {name:<14} {detail}")
    bad = [c for c in checks if not c[0]]
    out("")
    out(f"  {len(checks) - len(bad)} ok, {len(bad)} to look at")
    return 1 if bad else 0


def main(argv=None, env=None, connect=None, opener=None, out=print):
    ap = argparse.ArgumentParser(prog="immich-clip-doctor")
    ap.add_argument("--config", default=None, help="path to config.toml")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        cfg = load(args.config, env=env)
    except ConfigError as e:
        log(str(e))
        return 1
    return run(cfg, env=env, connect=connect, opener=opener, out=out)


if __name__ == "__main__":
    sys.exit(main())
