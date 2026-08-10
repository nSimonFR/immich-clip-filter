# Limitations, and the upstream quirks behind them

Everything here was measured, not inferred. Where a number appears, it came off a
real library of ~5,500 photos.

## The big one: this reads Immich's internal database

The sidecar queries `smart_search`, `album`, `album_asset`, `album_asset_audit`,
`asset` and `user` directly. **That is not an API and carries no stability
promise.**

It is also unavoidable. Immich's REST smart-search endpoint returns *ranked*
assets with **no distances**, so there is no way to apply a threshold through it.
The whole feature depends on reading the vectors.

Three things make that survivable rather than reckless:

1. **A startup probe.** The sidecar asserts every table and column it uses and
   refuses to start on an unknown shape, naming what is missing — instead of
   failing per asset months later.
2. **A compatibility matrix** (below), one row per tested version.
3. **Contract tests** you can run against your own database before an upgrade:
   `IMMICH_CLIP_CONTRACT_DSN=… pytest tests/contract`. CI runs them weekly, so an
   upstream change breaks a build rather than somebody's album.

Nothing here ever writes to Immich's database. Every album change goes through
the REST API, so Immich's own bookkeeping stays correct. A **read-only Postgres
role** is a good idea and works.

### Compatibility

| Immich | status | notes |
|---|---|---|
| **3.1.0** | tested in production, **and the contract suite runs green against it** | the reference deployment; every quirk below was measured here |
| 3.0.x | expected to work, untested | same major, same Workflows engine |
| < 3.0 | **unsupported** | no Workflows engine — there is no step to add |

The integration harness pins `v3.1.0` and the images upstream's own compose pairs
with it (`valkey:9`, `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0`).

### What the contract suite actually checks

36 tests, in two halves — run them against your own database before upgrading:

```bash
IMMICH_CLIP_CONTRACT_DSN=postgresql://immich@/immich \
IMMICH_CLIP_CONTRACT_URL=http://127.0.0.1:2283 \
IMMICH_CLIP_CONTRACT_KEY=... pytest tests/contract -v
```

**The schema half** asserts every table and column the SQL touches, that pgvector
still answers `<=>` and `AVG(vector)`, that the backfill scan still plans as a Seq
Scan rather than reaching for the ANN index, that an embedding still comes back as
a text literal, and that `smart_search.embedding` is still a fixed-width
`vector(N)` — which is what the integration harness has to pad its synthetic
vectors to.

**The API half** probes all thirteen endpoints this project and its harness drive.
It distinguishes a missing *route* from a missing *resource* (NestJS answers an
unrouted request with `Cannot GET /api/…`), so every probe sends an empty body and
nothing is created, modified or deleted.

Both halves include a test that deliberately asks for something that cannot exist,
so a suite that passed vacuously would fail — a guard that never fires is worse
than no guard.

## Upstream quirks that shape the design

### 1. Workflow steps run **unordered**

`WorkflowRepository.getForWorkflowRun` selects `workflow_step` with **no
`ORDER BY "order"`**. Postgres is then free to return the steps in any order, and
it does — the same query returned `[typeFilter, addToAlbums, clipFilter]` on one
call and the declared order on the next.

When the add lands first, **every asset is filed regardless of the verdict**.
Observed, not theorised.

So a filter step cannot reliably gate a later action step. **This is why the
plugin is one self-contained step** that does its own asset-type check and hands
`albumIds` to the sidecar to file. It still returns `workflow.continue`, so
chaining will work again if upstream orders the query.

→ *Do not chain `immich-plugin-core#assetAddToAlbums` after this step.*

### 2. Plugin import is keyed on the manifest hash, not the wasm

Immich short-circuits the import when the SHA-256 of `manifest.json`'s **contents**
matches what it already has. A rebuilt wasm under an unchanged manifest is
**silently ignored** and the old bytes keep running.

The obvious fix — bump `version` — does not work either. The `plugin` table
carries both `UNIQUE (name, version)` and `UNIQUE (name)`, while the upsert only
declares `onConflict(['name','version'])`. A new version is therefore an INSERT
that trips the name-only constraint (`Key (name)=(clip-filter) already exists`),
the import fails, and the **old plugin stays loaded**.

So the build keeps `version` fixed and puts a source hash in `description`. The
manifest moves whenever the sources do, the upsert matches, and `wasmBytes` is
updated in place.

> ⚠️ Bumping `version` deliberately requires deleting the `plugin` row first,
> which **CASCADES through `plugin_method` to `workflow_step`** — every workflow
> using the plugin loses its steps and must have them recreated. Back the step
> configs up first.

### 3. Nested manifest schema properties require `title` and `description`

In `dtos/json-schema.dto.js` only the top level makes them optional. A property
without them fails zod validation and Immich skips the plugin with a bare
`Invalid plugin manifest` warning — not an error, and easy to miss.

### 4. `vchordrq` is an approximate index that errors at plan time

`smart_search.embedding` carries a vchordrq (ANN) index. It answers
`ORDER BY embedding <=> const` by probing a subset of lists — fine for "show me 20
similar photos", wrong for "every asset at or under this threshold" (visibly
out-of-order distances at `probes=1`). It also raises `need 1 probes, but 0 probes
provided` outright when the GUC is unset, **at plan time**, so
`SET LOCAL enable_indexscan = off` does not avoid it: the `EXPLAIN` itself errors.

The backfill therefore scores inside a `MATERIALIZED` CTE with no `ORDER BY`,
leaving the index nothing to serve; the sort happens outside on a plain float.
The sidecar's per-asset lookup is a primary-key select and never touches the index
at all.

### 5. Immich never re-queues missing CLIP embeddings

`handleNightlyJobs` re-queues missing *thumbnails* and face clustering, and
nothing else. A SmartSearch job that failed while the ML server was down stays
failed forever — on the reference library, ~2,000 assets had a preview and no
embedding. The drainer kicks the queue for exactly this reason.

### 6. The ML server's endpoints are not JSON

`/ping` answers with the bare string `pong`. Parsing that as JSON raises, and
since any failure there means "unreachable", a perfectly healthy ML host was
reported as down — after which the drainer never kicked the backlog. Status code
only.

`/predict` returns the embedding as a **pgvector literal string**
(`"[-0.0327,0.0078,…]"`), not a JSON array. Immich never notices because it passes
the value straight into an INSERT.

### 7. `GET /api/albums/{id}` reports `assetCount` but returns an empty `assets`

So album membership is read from `album_asset` instead — which also keeps it
unpaginated and consistent with the rest of the queries.

### 8. `album_asset_audit` is pruned after ~31 days

`MAX_DAYS = 30` in `sync.service.js`. It is where removals are recorded, but it
cannot be the memory itself, so each pass copies new rows into a table of our own
that is never pruned.

## Deliberately out of scope

- **Running the inference here.** Immich already computes the embeddings;
  recomputing them would double the GPU cost and add a HEIC-decoding problem.
- **Removing photos from albums.** Everything is append-only, so a bad threshold
  is a cleanup job rather than data loss.
- **Replacing Immich smart search.** This is auto-filing, not search.
- **Video.** No CLIP embedding, so there is nothing to compare. The step's
  `assetTypes` defaults to `["IMAGE"]` for that reason.

## Known gaps

- **Each user must create their own workflow.** Workflows are owned by their
  creator with no impersonation, so multi-user setups need one workflow per user
  (and one API key per user — see [install.md](./install.md#multiple-users)).
- **The plugin's target URL is baked in at build time**, because the manifest's
  `allowedHosts` is checked by the host before the call is made. Moving the
  sidecar means rebuilding the plugin.
- **No metrics endpoint.** Queue depth and verdicts are in the logs and in
  `immich-clip-doctor`; there is no Prometheus surface yet.
