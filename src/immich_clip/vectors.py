"""Vector helpers — pure maths, no I/O, no psycopg2.

Immich stores embeddings in `smart_search.embedding` as a pgvector `vector(N)`.
No vector adapter is registered on our connections, so psycopg2 hands them back
as the text literal `[0.1,-0.2,...]`; these are the parse/format pair for that,
plus the two operations a centroid needs.

Distances themselves are computed in SQL (`embedding <=> %s::vector`) so the
1024 floats stay in Postgres — see `store.distance_to`.
"""

import math


# pgvector rejects a literal with spaces after the commas, and Python's repr of a
# float is round-trippable, so a plain join is both correct and lossless.
def format_vector(vec):
    """Python floats -> the pgvector literal `[a,b,c]`."""
    if not vec:
        raise ValueError("refusing to format an empty vector")
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def parse_vector(text):
    """The pgvector literal `[a,b,c]` -> Python floats."""
    if isinstance(text, (list, tuple)):  # a future adapter may hand us the real thing
        return [float(x) for x in text]
    raw = str(text).strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        raise ValueError(f"not a pgvector literal: {raw[:40]!r}")
    body = raw[1:-1].strip()
    if not body:
        raise ValueError("refusing to parse an empty vector")
    return [float(part) for part in body.split(",")]


def l2_normalize(vec):
    """Scale to unit length.

    CLIP embeddings arrive normalised, but the mean of several of them is not —
    and cosine distance against a non-unit centroid still *ranks* correctly while
    reporting distances on a different scale. Normalising keeps a threshold
    calibrated against one profile meaningful against the next.
    """
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    if norm == 0:
        raise ValueError("cannot normalise a zero vector")
    return [float(x) / norm for x in vec]


def mean_vector(vectors):
    """Component-wise mean — the centroid of a seed set."""
    rows = list(vectors)
    if not rows:
        raise ValueError("no vectors to average")
    dim = len(rows[0])
    for row in rows:
        if len(row) != dim:
            raise ValueError(f"dimension mismatch: {len(row)} != {dim}")
    return [sum(float(row[i]) for row in rows) / len(rows) for i in range(dim)]
