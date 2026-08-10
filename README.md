# immich-clip-filter

Content-based auto-filing for [Immich](https://immich.app): a **CLIP filter step
for Workflows**. Point a rule at an album of example photos, and matching uploads
are filed automatically from then on.

```
   Album "Food examples"          every new upload
   ┌────────────────────┐        ┌──────────────┐
   │  🍝  🍜  🥗  🍰    │ ─────▶ │  clipFilter  │ ──▶ Album "Food"
   └────────────────────┘        └──────────────┘
      15 photos you picked        threshold 0.30
```

Immich ships a Workflows engine, but its bundled filters only see filenames,
EXIF, type, date and location — nothing about *what is in the picture*. This adds
that.

**It runs no inference.** Immich already computes a CLIP embedding for every
asset; this reads those. No second GPU pass, no decoding originals, and the
numbers agree with Immich's own smart search because they are Immich's numbers.

## What you get

- **Rules you define in the Immich UI.** Make an album of examples, point a
  workflow step at it by name. Its members are read live, so adding photos
  sharpens the rule immediately — nothing to rebuild, no shell.
- **Nothing is lost while the ML server sleeps.** A photo Immich has not embedded
  yet is *undecided*, not "no": it is parked on a queue and filed once the
  embedding arrives. On a deployment whose GPU host is usually off, that is most
  uploads.
- **Removals stick.** Take a photo out of the album by hand and nothing will ever
  file it back. Put it back and that is remembered too.
- **It fails closed.** An unknown rule, a dead database, a malformed request all
  answer "no". A false negative leaves one photo out of an album; a false
  positive files the whole camera roll.
- **Shared libraries work**, with one API key per user — and it tells you by name
  when one is missing, instead of quietly filing nothing.

## Quickstart

```bash
# 1. build the plugin for your deployment (the sidecar URL is baked in)
SIDECAR_URL=http://immich-clip-filter:8351/classify ./plugin/build.sh dist/plugins

# 2. run the sidecar, and point Immich's plugin folder at dist/plugins
docker compose up -d immich-clip-filter
docker compose run --rm immich-clip-filter immich-clip-doctor

# 3. pick a threshold against your own library — it is not guessable
immich-clip-backfill --seed-album "Food examples" --album "Food" --create-album
```

Then add the workflow: trigger **Asset Metadata Extraction**, one step **Filter
by content (CLIP)**.

Full walkthrough: **[docs/install.md](./docs/install.md)**.

## Documentation

| | |
|---|---|
| [install.md](./docs/install.md) | Docker, NixOS, bare Python; and multi-user |
| [concepts.md](./docs/concepts.md) | seed albums vs profiles, and **nearest vs centroid** — read this one |
| [calibration.md](./docs/calibration.md) | reading the histogram, picking a threshold |
| [operations.md](./docs/operations.md) | the queue, the drainer, the embedding backlog, backups |
| [limitations.md](./docs/limitations.md) | version coupling, and the upstream quirks that shaped the design |

## The one thing people get wrong

Given several example photos, you can score a new one against the **nearest**
example or against their **average**. It is not a tuning knob:

> Seeds sharing a **subject** → `centroid`. Seeds sharing only a **theme** →
> `nearest`.

Backwards, on a real library, a `food` rule built from 19 different dishes
matched an **Eiffel Tower at 0.277** — closer to the average of nineteen food
photos than a gelato was (0.321). [concepts.md §3](./docs/concepts.md#3-nearest-vs-centroid--the-choice-that-decides-whether-it-works)
has the numbers.

## How it fits together

```
   Immich (workflow engine)                  immich-clip-filter
   ┌──────────────────────┐                  ┌──────────────────┐
   │ clipFilter step      │ ──── POST ─────▶ │ /classify        │
   │  (WASM, in-process)  │ ◀─── verdict ─── │  decide + file   │
   └──────────────────────┘                  └────────┬─────────┘
                                                      │ reads smart_search
                                              ┌───────▼─────────┐
                                              │ Immich Postgres │
                                              └─────────────────┘
```

Two halves, because one of them cannot do the job alone. The WASM plugin's only
way out is Immich's `httpRequest` host function, which returns
`body: await res.text()` — no image bytes in, no multipart out — so it cannot call
CLIP itself. The sidecar answers instead, from embeddings Immich already wrote.

⚠️ It reads Immich's **internal database schema**, which is not an API. That is
unavoidable (the REST smart-search endpoint returns ranked assets *without
distances*, so a threshold cannot be applied through it) and it is handled
head-on: the sidecar probes the schema at startup and refuses to run on a shape it
does not recognise, and a contract suite you can run against your own database
catches an upgrade before it costs you an album. See
[limitations.md](./docs/limitations.md).

## Development

```bash
pip install -e ".[test]"
pytest tests/unit                 # 169 tests, no network, no database, under a second
```

Three layers:

| suite | count | needs | what it is for |
|---|---|---|---|
| `tests/unit` | 169 | nothing | the logic, and every failure branch |
| `tests/contract` | 36 | a real Immich | is Immich still the shape this assumes — schema *and* routes. Green against 3.1.0 |
| `tests/integration` | 15 | a real Immich (compose) | the assumptions, end to end — see its [README](./tests/integration/README.md) |

Run the contract suite against your own Immich before upgrading it — that is what
it is for:

```bash
IMMICH_CLIP_CONTRACT_DSN=postgresql://immich@/immich \
IMMICH_CLIP_CONTRACT_URL=http://127.0.0.1:2283 \
IMMICH_CLIP_CONTRACT_KEY=... pytest tests/contract -v
```

Every function that does I/O takes the doer as a parameter (`connect=`,
`opener=`, `log=`, `sleep=`, `now=`), which is why the unit suite needs no
mocking library and nothing reads the environment at import time.

`nix develop` gets you pytest, psycopg2 and the wasm toolchain.

## Status

Extracted from a working private deployment. The implementation has run in
production for months — four rules, two users, ~5,500 photos — and this repo is
that code with the host-specific parts lifted out. [PLAN.md](./PLAN.md) records
what that extraction involved and what is left.

## Licence

MIT.
