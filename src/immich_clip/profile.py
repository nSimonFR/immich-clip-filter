#!/usr/bin/env python3
"""Build a CLIP profile — a rule stored in a file rather than in the Immich UI.

    immich-clip-profile --name food --seed-album "Food examples"
    immich-clip-profile --name food --seed-asset <uuid> --seed-asset <uuid>
    immich-clip-profile --name food --text "a plate of food, a meal, a dish"

Most rules do not need this: a workflow step can name a seed ALBUM and the sidecar
reads its members live, which means the whole rule is definable in the Immich UI
with no shell involved. Profiles remain for the two things an album cannot express
— a text prompt, and a hand-picked seed set that is not worth an album.

Seed mode averages the embeddings Immich already computed for the photos you
picked, read straight out of `smart_search`. It needs no ML server at all, and on
a personal library it beats an English sentence: it captures your camera, your
lighting and your plating rather than CLIP's idea of "food".

Text mode is the bootstrap for when there is nothing to seed from yet. It is the
only path that talks to the ML server, and it produces distances on a different
scale — CLIP's text and image towers sit at a systematic offset — so a threshold
calibrated in one mode does not carry to the other. Recalibrate with
`immich-clip-backfill` after switching.
"""

import argparse
import json
import sys
import urllib.request
import uuid

from . import api
from .config import load
from .logs import logger
from .store import album_asset_ids, album_id_by_name, connect_pg, profile_path, save_profile
from .vectors import l2_normalize, mean_vector, parse_vector

log = logger("immich-clip-profile")


def seed_embeddings(cur, asset_ids):
    """Fetch the stored embeddings for the seed assets.

    Assets with no row are reported rather than skipped silently: a seed album
    half of which was never embedded would otherwise yield a centroid built from
    an unannounced subset.
    """
    # The ::uuid[] cast is required: psycopg2 adapts a list of Python strings to
    # text[], and Postgres has no `uuid = text` operator, so the unqualified form
    # fails outright with UndefinedFunction.
    cur.execute(
        'SELECT "assetId", embedding FROM smart_search WHERE "assetId" = ANY(%s::uuid[])',
        (list(asset_ids),),
    )
    found = {str(row[0]): parse_vector(row[1]) for row in cur.fetchall()}
    missing = [a for a in asset_ids if a not in found]
    return [found[a] for a in asset_ids if a in found], missing


def encode_text(cfg, text, model, opener=None):
    """One multipart POST to the ML server — the same shape Immich sends.

    See repositories/machine-learning.repository.js: `entries` is the JSON model
    config, the payload field is `text`, and the embedding comes back under the
    `clip` key (ModelTask.SEARCH = "clip").
    """
    url = api.ml_url(cfg, opener=opener)
    entries = json.dumps({"clip": {"textual": {"modelName": model, "options": {}}}})
    boundary = f"----immichclip{uuid.uuid4().hex}"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        for name, value in (("entries", entries), ("text", text))
    ]
    body = ("".join(parts) + f"--{boundary}--\r\n").encode()
    req = urllib.request.Request(
        f"{url}/predict",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with (opener or urllib.request.urlopen)(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode())
    vector = payload.get("clip")
    if vector is None:
        raise SystemExit(f"ML server returned no clip embedding: {str(payload)[:200]}")
    # ⚠️ The live server hands this back as a pgvector LITERAL STRING
    # ("[-0.0327,0.0078,…]"), not a JSON array — Immich passes it straight into an
    # INSERT so it never has to care. parse_vector accepts either form.
    try:
        return parse_vector(vector)
    except ValueError as e:
        raise SystemExit(f"unparseable clip embedding from the ML server: {e}")


def build(cfg, args, connect=None, opener=None):
    model = api.clip_model(cfg, opener=opener)

    if args.text:
        vector = encode_text(cfg, args.text, model, opener=opener)
        built_from = {"kind": "text", "text": args.text}
        log(f"encoded {args.text!r} with {model}")
    else:
        conn = (connect or connect_pg)(cfg)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            asset_ids = list(args.seed_asset or [])
            if args.seed_album:
                album_id = album_id_by_name(cur, args.seed_album)
                if not album_id:
                    raise SystemExit(f"no album named {args.seed_album!r}")
                asset_ids += album_asset_ids(cur, album_id)
            if not asset_ids:
                raise SystemExit("no seed assets resolved")
            vectors, missing = seed_embeddings(cur, asset_ids)
        finally:
            conn.close()

        if missing:
            log(f"WARNING: {len(missing)}/{len(asset_ids)} seed assets have no "
                f"embedding yet and were left out (e.g. {missing[0]})")
        if not vectors:
            raise SystemExit("none of the seed assets are embedded — nothing to average")
        vector = mean_vector(vectors)
        built_from = {
            "kind": "seed",
            "album": args.seed_album,
            "assets": len(vectors),
            "requested": len(asset_ids),
            # The exact seed list, not just its size. Without this a profile
            # assembled by hand-picking assets cannot be rebuilt once the file is
            # gone — which is what lets the profile directory be a cache rather
            # than state you must back up.
            "assetIds": asset_ids,
        }
        log(f"averaged {len(vectors)} seed embeddings")

    payload = save_profile(
        cfg.profile_dir, args.name, model, l2_normalize(vector), built_from,
        scoring=getattr(args, "scoring", "nearest"),
    )
    log(f"wrote {profile_path(cfg.profile_dir, args.name)} "
        f"(dim {payload['dim']}, model {model}, scoring {payload['scoring']})")
    return payload


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="immich-clip-profile")
    ap.add_argument("--config", default=None, help="path to config.toml")
    ap.add_argument("--name", required=True, help="profile name, e.g. food")
    ap.add_argument("--seed-album", help="use every embedded asset in this album")
    ap.add_argument("--seed-asset", action="append", help="repeatable asset uuid")
    ap.add_argument("--text", help="build from a text prompt instead (needs the ML server)")
    ap.add_argument("--scoring", choices=("nearest", "centroid"), default="nearest",
                    help="score against the nearest seed (default; right when the "
                         "seeds share a THEME but no subject) or against their "
                         "average (right when they all show the SAME thing in "
                         "different places)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing profile")
    args = ap.parse_args(argv)
    if not (args.seed_album or args.seed_asset or args.text):
        ap.error("give --seed-album, --seed-asset or --text")
    if args.text and (args.seed_album or args.seed_asset):
        ap.error("--text cannot be combined with seed assets")
    return args


def main(argv=None, env=None, connect=None, opener=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cfg = load(args.config, env=env)
    path = profile_path(cfg.profile_dir, args.name)
    if path.exists() and not args.force:
        log(f"{path} already exists — pass --force to rebuild it")
        return 1
    build(cfg, args, connect=connect, opener=opener)
    return 0


if __name__ == "__main__":
    sys.exit(main())
