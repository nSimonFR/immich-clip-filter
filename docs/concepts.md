# Concepts

Four ideas. The third one is the one people get wrong.

## 1. It does no inference

Immich already runs CLIP over your library and writes the result to
`smart_search.embedding`. This project **reads that**. It never calls the ML
server to classify anything.

That is not a shortcut, it is the design:

- no second GPU pass, so a rule costs nothing to add;
- no decoding HEIC originals on whatever small machine runs the sidecar;
- the numbers agree with Immich's own smart search, because they *are* Immich's
  numbers.

The consequence is the one thing you have to hold in your head: **a photo Immich
has not embedded yet cannot be judged.** See §4.

## 2. A rule is an album of examples

The normal way to write a rule is to make an album, put example photos in it, and
point a workflow step at it by name.

```
Album "Food examples"  ──▶  clipFilter(seedAlbum="Food examples", threshold=0.30,
                                       albumIds=["Food"])
```

The album's membership is read **live**, on every decision. Drop three more
photos into it and the rule sharpens immediately — nothing to rebuild, no shell,
no restart. That is the whole reason `seedAlbum` exists.

**Profiles** (`immich-clip-profile`) are the alternative, and are needed for
exactly two things: a rule built from a *text prompt* rather than photos, and a
hand-picked seed set you do not want to make an album for. A profile is a JSON
file holding one vector.

## 3. Nearest vs centroid — the choice that decides whether it works

Given several example photos, there are two ways to ask "is this new photo like
them":

| mode | question | right when |
|---|---|---|
| `nearest` | how far is it from the **closest single example**? | the examples share a *theme*, not a subject |
| `centroid` | how far is it from their **average**? | the examples all show the **same thing** in different places |

This is not a tuning knob. Getting it backwards produces a rule that confidently
files the wrong photos, and here is what that looked like on a real library:

**A `food` rule — 19 photos of different dishes, in different restaurants.**
Averaging them cancels the food. What nineteen restaurant photos actually have in
common is *daylight, a phone camera, a table, a trip* — so the mean drifts to the
middle of the library and drags ordinary photos in. Measured:

```
Eiffel Tower photo  →  0.277 from the centroid      ← matched at threshold 0.30
                    →  0.367 from the nearest food photo
                    →  0.434 from the average one
a real gelato       →  0.321 from the average seed  ← further than the tower was
```

The tower was *closer to the average of nineteen food photos than a gelato was*.
Nearest-seed scoring removed the artifact outright.

**A `burgie` rule — 118 photos of one plush toy, everywhere.** Here the average
is exactly right: the toy is the only consistent element, so it survives the
averaging and everything else cancels. Nearest-seed was *worse*, because it
matched the **scene** of whichever seed was closest — river selfies, apartment
interiors, a desk fan — since those scenes really are near-identical to a seed
photo that happened to be taken there.

> **Seeds sharing a subject → `centroid`. Seeds sharing only a theme →
> `nearest`.**

Thresholds do **not** carry between the two modes, or between image-seeded and
text-seeded rules — CLIP's text and image towers sit at a systematic offset.
Recalibrate after switching. See [calibration.md](./calibration.md).

## 4. Three outcomes, not two

A verdict is one of:

| outcome | what it means | what happens |
|---|---|---|
| **match** | distance ≤ threshold | filed into the target album(s) |
| **no** | distance > threshold | nothing; a decided no is final |
| **undecided** | Immich has not embedded this photo yet | parked on a queue, decided later |

The third one is the whole reason this project has a drainer. A freshly uploaded
photo normally has no embedding when the workflow fires, and if the ML server is
a GPU box that sleeps, it may not have one for days. Answering "not food" would
be a *lie that gets thrown away* — so it is recorded instead, and
`immich-clip-drain` finishes it once Immich catches up.

Everything else fails **closed**: an unknown rule, an unreachable database, a
malformed request all answer "no" without queueing. A false negative leaves one
photo out of an album. A false positive files the whole camera roll.

## 5. Filing is append-only, and removals are learned

Nothing here ever takes a photo out of an album. A threshold set too loose is a
cleanup job, not data loss.

But that means a photo you delete from the album by hand would come straight
back on the next sweep. So **a removal is treated as a decision**: Immich records
every one in `album_asset_audit`, the sidecar copies those into its own table
(Immich prunes its audit after ~31 days), and no path ever files that pair again.

Putting the photo back by hand is just as clear a signal, and clears the
exclusion. Nothing extra to run, in either direction.
