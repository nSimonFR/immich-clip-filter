# Generalising nic-clip into `immich-clip-filter`

Plan to lift the working implementation out of [nic-os] into a standalone,
documented, testable project that a stranger with an Immich instance can install.

**Status:** the code exists and runs in production on one host (four rules, two
users, ~5.5k photos). Nothing here is speculative design — every constraint below
was hit and measured while building it. This plan is about *decoupling*, *proving*
and *documenting*, not about discovering whether it works.

---

## 1. What exists, and what is nic-os-shaped about it

| piece | today | why it can't ship as-is |
|---|---|---|
| `plugin.js` / `plugin.d.ts` | Extism/WASM step | hardcodes the sidecar default URL |
| `immich-clip-plugin.nix` | builds wasm + manifest | Nix-only; most Immich users run Docker |
| `nicos_scripts/immich/*` | sidecar + 3 CLI tools | lives inside a nic-os Python package |
| `immich-clip.nix` | NixOS module | agenix paths, `User=immich`, fixed port |

Hard-coded assumptions to remove:

- Postgres reached over a **unix socket with peer auth as `immich`**
- API key at a fixed **agenix** path, and exactly **one** key
- `/var/lib/immich-clip` for state
- port `8351`
- rpi5-specific systemd wiring (socket-activation, the beast ML host)

## 2. Target shape

Two artifacts, installable independently:

1. **The plugin** — `manifest.json` + `nic_clip.wasm`, dropped into
   `IMMICH_PLUGINS_INSTALL_FOLDER`. No configuration beyond what the Immich UI
   collects.
2. **The sidecar** — one long-running HTTP service plus three CLI tools
   (`profile`, `backfill`, `drain`). Distributed as **a Docker image first**
   (that is how Immich itself is run), then a Nix flake, then a pip package.

```
docker compose:  immich-server ─┬─ immich-clip-filter (sidecar)
                                └─ postgres (shared, read-only use)
```

### Configuration model

One file, env-overridable, replacing the current pile of env vars:

```toml
[immich]
url    = "http://immich-server:2283"
db_url = "postgresql://immich:...@postgres/immich"   # read-only role recommended

[[immich.keys]]        # per-OWNER keys — this is what unblocks multi-user
owner = "nsimon@pm.me"
key   = "..."

[sidecar]
listen   = "0.0.0.0:8351"
state_dir = "/var/lib/immich-clip"
max_wait  = 15

[drain]
interval      = "15m"
requeue_every = "1h"
max_age_days  = 30
```

Rules themselves stay where they already are: **in the Immich UI**, on the
workflow step (`seedAlbum`, `scoring`, `threshold`, `albumIds`). Profile files
remain only for text-prompt rules and hand-picked seed sets.

## 3. The coupling that needs naming out loud

The sidecar reads Immich's **internal Postgres schema**: `smart_search`,
`album_asset`, `album_asset_audit`, `asset`. That is not an API and it is not
promised to be stable.

It is also unavoidable: the REST smart-search endpoint returns ranked assets
**without distances**, so there is no way to apply a threshold through it. The
whole feature depends on reading the vectors.

Mitigations, all of which belong in the plan rather than in a footnote:

- **A startup schema probe.** Assert every column/table used, and refuse to start
  with a clear message naming the Immich version, rather than failing per-asset
  later.
- **A compatibility matrix** in the README, one row per tested Immich version.
- **An integration test that runs against a real Immich** (§4) so an upgrade
  breaks CI, not somebody's album.

## 4. Test strategy

Three layers. The middle one is the reason this plan is worth executing.

### 4.1 Unit (port the existing 488)

Pure logic with injected seams — vector maths, profile store, queue,
exclusions, the three-way verdict, fail-closed behaviour on every error branch.
Already written; they move over unchanged.

### 4.2 Integration, against a real Immich API

Docker compose in CI: `immich-server` + `postgres` + the sidecar.

**The key insight that makes this tractable: no ML server is needed.** Immich
computes embeddings on a GPU host, which cannot run in CI — but the sidecar only
ever *reads* `smart_search`. So the harness **inserts synthetic embeddings
directly**, which means tests control the vectors and can assert *exact*
distances rather than "roughly similar".

A test looks like:

```
create user + API key via /api/auth + /api/api-keys
upload N fixture images via /api/assets
INSERT chosen unit vectors into smart_search
create seed album + target album via /api/albums
install the plugin, restart, assert it imported
POST /api/workflows with a clipFilter step
trigger, then assert the target album's membership
```

Cases worth pinning end-to-end, each of which is a bug already paid for once:

| case | asserts |
|---|---|
| match | filed, exactly once |
| over threshold | not filed |
| no embedding yet | queued, not answered "no" |
| embedding appears, drain runs | filed late |
| sidecar stopped | fails closed, no exception storm |
| photo removed by hand | not refiled, live *and* through backfill |
| photo re-added by hand | exclusion cleared |
| two rules, one asset | both decide independently |
| asset owned by another user | `no_permission` surfaced, not silent |
| plugin rebuilt | new wasm actually loads (the manifest-hash trap) |

### 4.3 Contract tests against Immich internals

Small, fast, and the early-warning system for §3:

- every column the SQL touches still exists
- `AVG(vector)` / `<=>` still behave
- `getForWorkflowRun` still returns steps **unordered** (if upstream ever fixes
  it, the single-step workaround can be relaxed)
- the plugin manifest still validates against Immich's own zod schema — this is
  already how the manifest is checked today and it caught a real error

## 5. Documentation

| doc | contents |
|---|---|
| `README.md` | what it does, a 3-step quickstart, the compatibility matrix |
| `docs/install.md` | Docker compose, Nix flake, bare Python |
| `docs/concepts.md` | seed albums vs profiles; **nearest vs centroid** |
| `docs/calibration.md` | reading the histogram, picking a threshold, the visual check |
| `docs/operations.md` | the pending queue, the drainer, the ML backlog kick |
| `docs/limitations.md` | the upstream quirks below, and version coupling |

`concepts.md` should teach with the real example, because it is the thing users
will get wrong: a centroid of 19 photos of *different dishes* drifts to the
middle of the library and matched an Eiffel Tower at 0.277 while the nearest
actual food photo was 0.367 away — but a centroid of 118 photos of *one toy* is
exactly right, and nearest-seed there matched river selfies and a desk fan
instead. **Seeds sharing a subject → centroid. Seeds sharing only a theme →
nearest.**

### Upstream Immich quirks to document (all measured on 3.1.0)

1. **Workflow steps run unordered** — `getForWorkflowRun` has no `ORDER BY
   "order"`, and the order changes between calls. A filter cannot gate a later
   action step; hence one self-contained step.
2. **Plugin import is keyed on the manifest hash, not the wasm** — and bumping
   `version` fails on `UNIQUE(name)` vs the `(name, version)` upsert. Deleting
   the plugin row cascades to `workflow_step`.
3. **`vchordrq` is an ANN index** that errors `need 1 probes` at *plan* time;
   `enable_indexscan = off` does not avoid it. Score inside a MATERIALIZED CTE.
4. **Immich never re-queues missing CLIP embeddings** — nightly jobs cover
   thumbnails and faces only. A third of the reference library was unembedded.
5. **The ML server's endpoints are not JSON** — `/ping` returns `pong`,
   `/predict` returns a pgvector *string*.
6. **`GET /api/albums/{id}`** reports `assetCount` but returns an empty `assets`.

## 6. Multi-user (the `Alfie` gap)

Today the sidecar holds one API key and can only file its owner's assets;
another user's photos match and return `filed: 0`. Workflows are always owned by
their creator (`ownerId: auth.user.id`, no impersonation).

Fix: the `[[immich.keys]]` table above. The sidecar resolves the asset's
`ownerId` and picks that owner's key; if none is configured, it says so in the
log instead of silently filing nothing. Workflows for other users still have to
be created by them (or inserted directly), which the docs must state plainly.

## 7. Phasing

| phase | outcome | rough size |
|---|---|---|
| 0 | repo skeleton, licence, CI, this plan | done |
| 1 | code lifted, all paths/creds injectable, unit tests green | ~1 day |
| 2 | docker-compose harness + the §4.2 matrix | ~2 days |
| 3 | docs | ~1 day |
| 4 | per-owner keys (fixes Alfie) | ~half day |
| 5 | nic-os consumes it as a flake input; in-tree copies deleted | ~half day |

Phase 5 mirrors how nic-os already consumes `sure-nix`, `airtrail-nix`,
`ryot-nix` — a flake input plus a thin NixOS module holding only configuration.

## 8. Deliberately out of scope

- Doing the inference here. Immich already computes the embeddings; recomputing
  them would double GPU cost and add a HEIC-decoding problem.
- Removing photos from albums. Everything is append-only, so a bad threshold is
  a cleanup job rather than data loss.
- Replacing Immich smart search. This is auto-filing, not search.

[nic-os]: https://github.com/nSimonFR/nic-os
