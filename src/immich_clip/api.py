"""The Immich REST calls every tool shares.

Kept apart from store.py (disk + Postgres) because these are the only places that
need an API key, and apart from the entry points because all of them resolve
albums and need to know which CLIP model is live.

`clip_model` and `ml_url` read Immich's own system config rather than taking the
answer from configuration. The model name is what makes a stored centroid valid
or worthless — deriving it from the server that will be compared against removes
the chance of the two drifting apart.

Every call takes an `owner`, because a key can only act on its own owner's
assets. See `config.ApiKey`.
"""

import json
import urllib.request

from .httpjson import get_json, post_json, put_json

BATCH = 500


class NoKeyForOwner(Exception):
    """No API key is configured for the user who owns this asset.

    Raised rather than returning 0, because "filed nothing" and "cannot possibly
    file anything, fix your config" look identical in a counter and are very
    different problems.
    """


def headers(cfg, owner=None):
    """Auth header for acting as `owner`.

    Raises only when a *specific* owner was asked for and nothing resolves — that
    is the multi-user gap, and it is a configuration error that will not fix
    itself. With no owner (the CLI tools, system-config reads) an empty key is
    passed through instead, so the failure is Immich's own 401 rather than a
    different error message for the same thing.
    """
    key = cfg.key_for(owner)
    if owner and not key:
        raise NoKeyForOwner(
            f"no Immich API key configured for {owner!r} — add an "
            "[[immich.keys]] entry for them (see docs/install.md)"
        )
    return {"x-api-key": key}


def system_config(cfg, opener=None, owner=None):
    # Long timeout on purpose: Immich may be socket-activated or simply slow to
    # boot, and this is often the call that WAKES it. A cold NestJS start on
    # modest hardware takes well past the 30s default.
    return get_json(
        f"{cfg.immich_url}/api/system-config", headers(cfg, owner), timeout=120, opener=opener
    )


def whoami(cfg, opener=None, owner=None):
    """The user a key belongs to — how `doctor` proves a key is for whom it says."""
    return get_json(f"{cfg.immich_url}/api/users/me", headers(cfg, owner), opener=opener)


def clip_model(cfg, opener=None):
    """The live clip.modelName, preferring an explicit override."""
    if cfg.model:
        return cfg.model
    name = (
        system_config(cfg, opener)
        .get("machineLearning", {})
        .get("clip", {})
        .get("modelName")
    )
    if not name:
        raise SystemExit("could not read machineLearning.clip.modelName from Immich")
    return name


def ml_url(cfg, opener=None):
    """The first configured ML server, preferring an explicit override."""
    if cfg.ml_url:
        return cfg.ml_url
    urls = system_config(cfg, opener).get("machineLearning", {}).get("urls") or []
    if not urls:
        raise SystemExit("Immich has no machineLearning.urls configured")
    return urls[0].rstrip("/")


def ml_healthy(cfg, opener=None):
    """Is the ML server up? Same probe Immich uses: GET {url}/ping.

    ⚠️ /ping answers with the bare string `pong`, NOT JSON. Parsing it as JSON
    raises, and since any failure here means "not reachable", that silently
    reported a perfectly healthy ML host as down. Status code only.

    Returns False rather than raising — "the GPU box is off again" is a normal
    state, not an error, and the caller simply tries on the next tick.
    """
    try:
        url = ml_url(cfg, opener=opener)
    except (Exception, SystemExit):  # noqa: BLE001 - resolving the URL can fail
        # for as many reasons as reaching it: Immich down, no key, none
        # configured. All of them mean the same thing to every caller, and none
        # of them may propagate — `immich-clip-doctor` crashed on exactly this
        # when pointed at an Immich it had no key for.
        return False
    req = urllib.request.Request(f"{url}/ping")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


def start_job(cfg, name, force=False, opener=None):
    """Kick one of Immich's job queues.

    Used for `smartSearch` with force=False, i.e. "embed the assets that have no
    embedding". Immich never does this on its own — `handleNightlyJobs` queues
    missing THUMBNAILS and face clustering but not missing CLIP embeddings, so a
    SmartSearch job that failed while the ML server was down stays failed forever.
    This is the only thing that clears that backlog.
    """
    return put_json(
        f"{cfg.immich_url}/api/jobs/{name}",
        {"command": "start", "force": force},
        headers(cfg),
        opener=opener,
    )


def create_album(cfg, name, description="", opener=None, owner=None):
    status, body = post_json(
        f"{cfg.immich_url}/api/albums",
        {"albumName": name, "description": description},
        headers(cfg, owner),
        opener=opener,
    )
    if status not in (200, 201):
        raise SystemExit(f"could not create album {name!r}: HTTP {status} {body[:200]}")
    return json.loads(body)["id"]


def add_assets(cfg, album_id, asset_ids, opener=None, log=print, owner=None):
    """PUT the ids in batches. Returns how many the server reported as added.

    Immich answers per id, and reports an id that was already in the album as
    `success: false` — so this count is "newly added", not "present".
    """
    added = 0
    hdrs = headers(cfg, owner)
    for i in range(0, len(asset_ids), BATCH):
        chunk = asset_ids[i : i + BATCH]
        results = put_json(
            f"{cfg.immich_url}/api/albums/{album_id}/assets",
            {"ids": chunk},
            hdrs,
            opener=opener,
        )
        ok = sum(1 for r in results if r.get("success"))
        added += ok
        # Say WHY the rest were refused. The common one is `no_permission`: a
        # shared library contains other people's assets, and a key can only put
        # its own owner's photos into its own owner's album. Reporting a bare
        # "53/78" made that look like a failure rather than a boundary.
        reasons = {}
        for r in results:
            if not r.get("success"):
                why = r.get("error", "unknown")
                reasons[why] = reasons.get(why, 0) + 1
        detail = "".join(f", {n} {why}" for why, n in sorted(reasons.items()))
        log(f"batch {i // BATCH + 1}: {ok}/{len(chunk)} added{detail}")
    return added
