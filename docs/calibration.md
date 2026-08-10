# Calibration

The threshold is not guessable. It depends on the rule, on the scoring mode, and
on your library — a value that works for one person's food photos will be wrong
for yours. Ten minutes here saves an album you have to clean out by hand.

## The tool

```bash
immich-clip-backfill --seed-album "Food examples" --album "Food"
```

Dry run by default: it writes nothing, and `--apply` is the only thing that does.
It scores **every image in the library** against the rule and prints the
distribution.

## Reading the histogram

```
[immich-clip-backfill] scored 5412 candidates against 'album:Food examples'
                       (19 examples, nearest scoring)
  0.15–0.20       3 #
  0.20–0.25      14 #####
  0.25–0.30      41 ###############
  0.30–0.35      12 ####
  0.35–0.40      89 ################################
  0.40–0.45     404 ################################################
  0.45–0.50     977 ################################################
  ...

  nearest 40:
  0.1712  IMG_4821.HEIC  0a342213-…
  0.1980  IMG_5106.HEIC  fba7dd29-…
  ...
```

**Look for the gap.** A rule that works has two populations: a small clump near
zero (the thing you meant) and the bulk of the library much further out. The
threshold goes **in the valley between them** — here, around `0.32`.

If there is no valley, the rule is not separating anything. That is a *rule*
problem, not a threshold problem: usually the wrong scoring mode
([concepts.md §3](./concepts.md#3-nearest-vs-centroid--the-choice-that-decides-whether-it-works))
or examples that have nothing visual in common.

## Then look at the actual photos

The histogram tells you a threshold exists. It does not tell you it is the
*right* one. Open the nearest 40 by filename and check where the list stops being
what you meant — that number is your threshold, and it is often tighter than the
valley suggests.

This is the step that caught the Eiffel Tower at 0.277: the histogram looked
perfectly healthy, and the filenames did not.

## Prefer too tight

A threshold that is too tight leaves a few photos out; you notice, and nudge it.
A threshold that is too loose files hundreds, and every one of them is a manual
removal — and, because removals are learned as decisions, a permanent exclusion
you then have to think about.

Start one bucket tighter than the valley. Widen after a week.

## Apply it

```bash
immich-clip-backfill --seed-album "Food examples" --album "Food" \
  --threshold 0.30 --apply
```

This files everything already in the library. The workflow step then handles new
uploads from that point on, with the same number in its config box.

## When to recalibrate

| what changed | why the threshold moves |
|---|---|
| the CLIP model | every embedding is recomputed in a different vector space — a stored profile is refused outright, and an album rule silently changes scale |
| `nearest` ↔ `centroid` | different question, different numbers; they do not carry |
| a text profile ↔ photo seeds | CLIP's text and image towers sit at a systematic offset |
| you added many examples | usually tightens; worth a re-check, not urgent |

Adding a few photos to a seed album does **not** need a recalibration — that is
the point of reading the album live.

## Nothing to score?

```
(nothing to score)
```

means no candidate assets came back at all. Almost always one of:

- **the library is not embedded.** Immich never re-queues missing CLIP
  embeddings on its own, so a library can carry a large unembedded tail
  indefinitely. `immich-clip-doctor` reports the count;
  [operations.md](./operations.md#the-embedding-backlog) says how to clear it.
- the seed album name does not match (it is exact, and case-sensitive);
- everything that matches is already in the target album — which is success.
