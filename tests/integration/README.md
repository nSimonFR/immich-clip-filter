# Integration tests

End-to-end against a **real Immich**: real uploads, real albums, real pgvector,
real permission boundaries. Every case here is a bug that was already paid for
once on a live library.

## The thing that makes this possible

**No machine-learning server runs.** Inference needs a GPU that CI does not have,
and that is exactly why the original was only ever tested by hand.

But the sidecar never computes an embedding — it only ever *reads*
`smart_search`. So the harness inserts the vectors itself:

```python
embed(asset_id, [0.0, 1.0])   # this photo points north
```

Two-dimensional unit vectors are enough to place a photo anywhere on a circle,
and cosine distance does not care about dimensionality. That turns "these photos
are sort of similar" into arithmetic: `NORTH` vs `EAST` is *exactly* 1.0, `NORTH`
vs `NORTHEAST` is *exactly* 0.2929. Every threshold in the suite is a number the
test chose, so nothing is approximately asserted and nothing is flaky.

## Running them

```bash
docker compose -f tests/integration/docker-compose.yml up -d
# Immich runs its migrations on first boot; the fixtures wait for /api/server/ping.

IMMICH_CLIP_IT_URL=http://127.0.0.1:2283 \
IMMICH_CLIP_IT_DSN=postgresql://postgres:postgres@127.0.0.1:5433/immich \
  pytest tests/integration -v
```

Without those two variables the suite skips, so `pytest` at the repo root stays
offline and fast.

Optional:

| variable | for |
|---|---|
| `IMMICH_CLIP_IT_EMAIL` / `_PASSWORD` | the admin the fixtures create |
| `IMMICH_CLIP_IT_SIDECAR` | the URL baked into the plugin build |

Two tests build the WASM plugin and need `extism-js` on `PATH`; they skip with a
message rather than failing when it is absent.

## What each test is for

| test | the bug it would catch |
|---|---|
| a matching photo is filed | the happy path, against a real album |
| filed twice does not duplicate | Immich reports an existing id as `success: false`, so the *count* lies; the album is the truth |
| past the threshold is not filed | a false positive files the whole camera roll |
| nearest vs centroid | the Eiffel Tower at 0.277 — staged exactly, with two seeds 90° apart |
| unembedded is queued | answering "no" throws the decision away, and on a sleeping GPU host that is most of them |
| queued, then embedded, then drained | the deferred half actually completes |
| unreachable database | fails **closed**, and does not queue — "cannot tell" is not "not yet" |
| removed by hand, live path | the 15-minute window where a removal did not stick |
| removed by hand, backfill path | the sweep excludes only *current* members |
| re-added by hand | one accidental removal must not ban a photo for good |
| two rules, one asset | the composite primary key; one verdict used to overwrite the other |
| another user's photo | `no_permission`, surfaced with the owner's name instead of `filed: 0` |
| that user's key configured | and then it works |
| a rebuilt plugin | Immich keys the import on the manifest hash, so a new wasm under an old manifest is silently ignored |
| the manifest validates | nested properties without `title`/`description` fail zod, and Immich skips the plugin with a warning |

## What is deliberately not covered here

**Immich dispatching the workflow step.** The suite drives the sidecar with the
exact payload the WASM step sends, and asserts the album afterwards — so
everything this project owns is covered end to end. What it does not do is create
a workflow through `POST /api/workflows` and trigger it, because on the tested
versions `WorkflowRepository.getForWorkflowRun` returns steps **unordered**
(docs/limitations.md), which makes an assertion about step dispatch a test of
Postgres's mood. That gap is why the step is self-contained: it does its own type
check and its own filing rather than chaining a later action step.
