"""Small on-disk state: read-or-default, write-atomically.

The drainer keeps one timestamp here (when it last kicked Immich's smartSearch
queue). A half-written state file is worse than a missing one — it would either
re-kick every pass or never kick again — so writes go through a temp file and
`os.replace`.
"""

import json
import os


def load_json(path, default):
    """Return the parsed file, or `default` when absent/corrupt.

    Corrupt-as-default is deliberate: a truncated stamp file must not wedge a
    timer forever. The next successful run rewrites it.
    """
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def save_json(path, data, indent=None, sort_keys=False):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent, sort_keys=sort_keys)
    os.replace(tmp, path)


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path
