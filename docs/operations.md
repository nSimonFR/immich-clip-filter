# Operating it

## The three moving parts

| what | when it runs | what it does |
|---|---|---|
| **the sidecar** | on every workflow trigger | decides, and files matches |
| **the drainer** | on a timer, every ~15 min | finishes verdicts that could not be reached |
| **the backfill** | by hand | calibrates, and sweeps the whole library |

## Start here when something looks wrong

```bash
immich-clip-doctor
```

```
  ✓  config file    /etc/immich-clip/config.toml
  ✓  api keys       nico@example.com, alfie@example.com
  ✓  immich url     http://immich-server:2283
  ✓  schema         all 6 tables present, pgvector OK
  ✗  embeddings     2075 images have no embedding — those can never match.
  ✓  key nico@…     valid, belongs to nico@example.com
  ✗  key alfie@…    actually belongs to nico@example.com — owner mismatch
  ✓  clip model     ViT-H-14-378-quickgelu
  ✗  ml server      unreachable — new uploads will queue until it returns
  ✓  queue          14 verdicts parked
```

Every check is one whose failure is invisible from the Immich UI: a rule that
matches and files nothing looks exactly like a rule that never matched.

## The pending queue

A photo with no embedding yet is *undecided*, not "no". It is parked in
`<state_dir>/pending.sqlite` and finished later.

```bash
immich-clip-drain              # dry run: says what it WOULD do
immich-clip-drain --apply      # the timer runs this
```

A pass:

1. retires entries older than `max_age_days`, or whose asset was deleted;
2. learns any hand-removals from Immich's audit log, so nothing is put back;
3. scores everything now embedded, files the matches, drops the decided;
4. if anything is *still* unembedded and the ML server is up, kicks Immich's
   `smartSearch` queue.

Step 4 is not belt-and-braces — see below.

Queue depth is a health signal. Steady growth with an ML server that is up means
Immich is not embedding: check that.

## The embedding backlog

**Immich never re-queues missing CLIP embeddings.** `handleNightlyJobs` covers
missing *thumbnails* and face clustering and nothing else, so a SmartSearch job
that failed while the ML server was down stays failed **forever**. On the
reference library that was ~2,000 assets with a preview and no embedding — none
of which any rule could ever match.

The drainer kicks that queue (`POST /api/jobs/smartSearch`, `force: false` —
"embed what has no embedding", not "re-embed everything"), rate-limited by
`requeue_every` because the job covers the whole library rather than just the
photos you care about.

To clear a large backlog now: Administration → Jobs → **Smart Search** →
*Missing*.

## Rules live in the Immich UI

A rule that names a seed album needs nothing on disk. Add photos to the album and
the next decision uses them. Change the threshold in the step's config box and it
takes effect on the next trigger. No restart, no rebuild.

The only thing that needs a shell is a **profile** (`immich-clip-profile`), which
exists for text prompts and hand-picked seed sets.

## Backups

`<state_dir>` holds two things:

- **`profiles/`** — a cache. Every profile records exactly what it was built
  from (the prompt, or the seed asset ids), so `immich-clip-profile` reproduces
  it byte for byte, and the embeddings it averages live in Immich's database.
  Not a source of truth.
- **`pending.sqlite`** — the queue *and* the exclusion table. The queue is
  transient. **The exclusions are not**: they are the record of every photo you
  took out of an album by hand, and losing them means the next backfill puts them
  all back.

So: back up `pending.sqlite`, or accept that a rebuild means re-removing by hand.

## Reading the logs

The sidecar logs one line per decision:

```
[immich-clip-filter] 7dc540e7-… -> {"match": true, "distance": 0.0, "waitedSec": 0.1,
                                    "profile": "album:Food examples", "filed": 1}
```

| field | meaning |
|---|---|
| `distance` | cosine distance to the rule; lower is closer |
| `waitedSec` | how long it waited for an embedding |
| `filed` | albums it was **newly** added to (an id already present counts 0) |
| `excluded` | albums skipped because you removed it from them by hand |
| `queued` | no embedding yet; the drainer will finish it |
| `ownerError` | matched, but no API key is configured for the photo's owner |
| `fileError` | the verdict stands; the album write failed and will be retried |

## Upgrading Immich

This project reads Immich's internal schema, so an upgrade can break it. The
sidecar probes the schema at startup and **refuses to run** on a shape it does
not recognise, naming the missing table or column — that is deliberate, and far
better than filing nothing for six weeks.

If that happens: check [limitations.md](./limitations.md) for the compatibility
matrix, and open an issue with the version and the message. `IMMICH_CLIP_CHECK_SCHEMA=0`
starts it anyway, and filing will probably fail.

Run the contract suite against a copy of your database before upgrading:

```bash
IMMICH_CLIP_CONTRACT_DSN=postgresql://immich@/immich pytest tests/contract -v
```

## Changing the CLIP model

Changing `machineLearning.clip.modelName` re-embeds the whole library into a
different vector space.

- **Stored profiles are refused** — they record the model they were built for,
  and the error tells you to rebuild.
- **Album rules keep working**, but their distances are on a new scale.
  Recalibrate every threshold ([calibration.md](./calibration.md)).
