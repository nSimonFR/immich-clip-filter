# immich-clip-filter

Content-based auto-filing for [Immich](https://immich.app): a **CLIP filter step
for Workflows**. Point a rule at an album of example photos, and matching uploads
are filed automatically from then on.

> **Status: extracted from a working private deployment, not yet packaged for
> general use.** The implementation runs in production (four rules, two users,
> ~5.5k photos) but currently lives inside [nic-os]. [`PLAN.md`](./PLAN.md) is
> the plan to make it installable by anyone. Do not expect the steps below to
> work until phase 1 lands.

## What it does

Immich 3.1 ships a Workflows engine, but its bundled filters only see filenames,
EXIF, type, date and location — nothing about *what is in the picture*. This adds
that step:

```
AssetMetadataExtraction → clipFilter(seedAlbum="Food seeds", albumIds=[Food])
```

A rule is three actions in the Immich UI, no shell:

1. make an album of example photos
2. make the album you want matches filed into
3. create a workflow with the `clipFilter` step pointing at both

Adding photos to the example album sharpens the rule immediately — nothing to
rebuild.

## How it works

The plugin is WASM (Extism) running inside Immich, and it **cannot do the CLIP
work itself**: its only way out is Immich's `httpRequest` host function, which
returns `body: await res.text()` — no image bytes in or out. So it asks a small
sidecar for a verdict.

The sidecar runs **no inference either**. Immich has already embedded every asset
for its own smart search; the sidecar reads those vectors from Postgres and
compares them to the examples. Consequences:

- no second GPU pass, and no GPU needed on the machine running this
- an asset with no embedding yet is **undecided**, not "no" — it is queued and
  filed once Immich catches up
- everything fails closed: an unknown rule, a dead database or a stopped sidecar
  all mean *do not file*, because a false positive files the whole camera roll
  while a false negative loses one photo

## Two ways to compare, and they are not interchangeable

| your examples | use | why |
|---|---|---|
| all show the **same subject** in different places | `centroid` | the subject is the only constant, so the average isolates it |
| share a **theme** but not a subject | `nearest` | averaging cancels the theme and leaves the shared context |

Getting this wrong is the most likely way to end up with a bad album — see
`PLAN.md §5` for the measured example (an Eiffel Tower scoring as food).

Thresholds do **not** transfer between the two modes. Calibrate with the
histogram tool before trusting a number.

## Licence

MIT.

[nic-os]: https://github.com/nSimonFR/nic-os
