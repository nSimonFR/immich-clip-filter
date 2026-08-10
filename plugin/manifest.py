#!/usr/bin/env python3
"""Emit the plugin manifest Immich reads, with the source hash baked in.

Run by `build.sh`; kept out of the shell script because two upstream quirks make
this the trickiest 40 lines in the project and they deserve to be explained where
they are implemented.

⚠️ 1. Immich keys the plugin import on the SHA-256 of manifest.json's *contents*
   (workflow-execution.service.js -> pluginRepository.getByHash short-circuits when
   it matches). A rebuilt wasm under an unchanged manifest is silently ignored and
   the old bytes keep running. So the manifest MUST change whenever the sources do.

⚠️ 2. The obvious way to do that — bump `version` — does not work. The `plugin`
   table carries BOTH `plugin_name_version_uq UNIQUE (name, version)` and
   `plugin_name_uq UNIQUE (name)`, while the upsert only declares
   `onConflict(['name','version'])`. A new version is therefore an INSERT that
   trips the name-only constraint: `Key (name)=(clip-filter) already exists`, the
   import fails, and the OLD plugin stays loaded. Observed, not theorised.

So `version` stays fixed and the source hash rides in `description` instead: the
manifest hash changes, the upsert matches on (name, version), and wasmBytes is
updated in place. It also means the loaded build is legible in the Immich UI.

Bumping `version` deliberately requires deleting the `plugin` row first, which
CASCADES through plugin_method to workflow_step — every workflow using this plugin
loses its steps. Back the step configs up first. See docs/limitations.md.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

# Fixed, for the upsert reason above. Not the project version.
MANIFEST_VERSION = "1.0.0"


def source_hash(paths):
    """Short digest of every source that ends up in the wasm.

    The .d.ts counts: it declares the host-function import, so editing it alone
    changes the compiled wasm just as much as editing the JS.
    """
    h = hashlib.sha256()
    for p in paths:
        h.update(Path(p).read_bytes())
    return h.hexdigest()[:8]


def build(sidecar_url, plugin_name, build_id):
    host = urlparse(sidecar_url).hostname or "127.0.0.1"
    return {
        "name": plugin_name,
        "version": MANIFEST_VERSION,
        "title": "CLIP content filter",
        # The `build` suffix is what makes the manifest hash track the sources —
        # see the header. Not cosmetic; do not remove it.
        "description": (
            "Filter workflow assets by what is in the picture, using the CLIP "
            f"embeddings Immich already computes. (build {build_id})"
        ),
        "author": "nSimonFR",
        "wasmPath": "clip_filter.wasm",
        "methods": [
            {
                "name": "clipFilter",
                "title": "Filter by content (CLIP)",
                "description": (
                    "Add the asset to an album when it looks like the examples in "
                    "another album (or a named profile), and halt the workflow "
                    "otherwise."
                ),
                "types": ["AssetV1"],
                # Needed for httpRequest; without it the host hands the plugin a
                # stub that throws, and the method loads into the worker pool
                # instead.
                "hostFunctions": True,
                # The host checks the request URL's hostname against this before
                # making the call, so it has to track the sidecar URL.
                "allowedHosts": [host],
                "uiHints": ["Filter"],
                "schema": {
                    "type": "object",
                    # Not `profile`: a rule may name a seed album instead.
                    "required": ["threshold"],
                    # ⚠️ Nested schema properties REQUIRE title and description —
                    # in dtos/json-schema.dto.js only the top level makes them
                    # optional, so omitting them on a property fails zod
                    # validation and the plugin is skipped with a bare
                    # `Invalid plugin manifest` warning rather than an error.
                    "properties": {
                        "seedAlbum": {
                            "type": "string",
                            "title": "Learn from this album",
                            "description": (
                                "Name of an Immich album holding example photos. The "
                                "rule is built from its members LIVE, so adding photos "
                                "to that album sharpens it immediately — nothing to "
                                "rebuild, and no shell needed. Leave empty to use a "
                                "named profile instead."
                            ),
                            "default": "",
                        },
                        "scoring": {
                            "type": "string",
                            "title": "How to compare",
                            "enum": ["nearest", "centroid"],
                            "description": (
                                "nearest: closest single example — right when the "
                                "examples share a THEME but not a subject (food). "
                                "centroid: their average — right when they all show "
                                "the SAME thing in different places (one toy). "
                                "Thresholds do not carry between the two."
                            ),
                            "default": "nearest",
                        },
                        "profile": {
                            "type": "string",
                            "title": "Profile (alternative to the album above)",
                            "description": (
                                "Name of a profile built with immich-clip-profile. "
                                "Needed only for text-prompt rules or hand-picked "
                                "seed sets; ignored when an album is given."
                            ),
                            "default": "",
                        },
                        "threshold": {
                            "type": "number",
                            "title": "Maximum cosine distance",
                            "description": (
                                "Lower is stricter. Calibrate with "
                                "`immich-clip-backfill` before trusting a value."
                            ),
                            "default": 0.28,
                            "minimum": 0,
                            "maximum": 2,
                            "precision": 0.01,
                        },
                        "waitSec": {
                            "type": "integer",
                            "title": "Seconds to wait for the embedding",
                            "description": (
                                "A freshly uploaded asset is not embedded yet. Past "
                                "this, the asset is parked on the pending queue and "
                                "filed later by immich-clip-drain — it is not lost."
                            ),
                            "default": 60,
                            "minimum": 0,
                            "maximum": 120,
                        },
                        "sidecar": {
                            "type": "string",
                            "title": "Sidecar URL",
                            "description": (
                                "Where immich-clip-filter listens. Its hostname must "
                                "match allowedHosts in the plugin manifest, so "
                                "changing it means rebuilding the plugin."
                            ),
                            "default": sidecar_url,
                        },
                        "albumIds": {
                            "type": "string",
                            "array": True,
                            "title": "Albums to file matches into",
                            "description": (
                                "Done by this step rather than by chaining "
                                "assetAddToAlbums, because Immich runs workflow steps "
                                "in an unordered query — a later action step is not "
                                "reliably later. Leave empty to use this purely as a "
                                "filter."
                            ),
                        },
                        "assetTypes": {
                            "type": "string",
                            "array": True,
                            "title": "Asset types to consider",
                            "description": (
                                "Checked here for the same ordering reason. Videos "
                                "carry no CLIP embedding, so letting one through "
                                "would only burn the full wait before concluding "
                                "nothing."
                            ),
                            "default": ["IMAGE"],
                        },
                    },
                },
            }
        ],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="manifest.py")
    ap.add_argument("--sidecar-url", default="http://127.0.0.1:8351/classify")
    ap.add_argument("--name", default="clip-filter")
    ap.add_argument("--source", action="append", default=None,
                    help="a source file to hash into the description (repeatable)")
    ap.add_argument("--build-id", default="", help="use this instead of hashing sources")
    args = ap.parse_args(argv)

    here = Path(__file__).parent
    sources = args.source or [here / "plugin.js", here / "plugin.d.ts"]
    build_id = args.build_id or source_hash(sources)
    # Compact and key-sorted so the same inputs always give the same bytes, and
    # therefore the same hash Immich keys the import on.
    print(json.dumps(build(args.sidecar_url, args.name, build_id),
                     sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
