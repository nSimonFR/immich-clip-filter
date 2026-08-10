"""Minimal JSON-over-HTTP on urllib — no requests, no httpx, no dependency.

`opener` is the seam. It defaults to `urllib.request.urlopen`, so production code
passes nothing; tests pass a fake that returns a canned body and records the
request it was handed. Every function here takes it, which is why the entire unit
suite runs with no network and no mocking library.
"""

import json
import urllib.request


def _urlopen(opener):
    return opener or urllib.request.urlopen


def http_json(req, timeout=30, opener=None):
    """Send a prepared Request, decode the JSON body ({} when empty)."""
    with _urlopen(opener)(req, timeout=timeout) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def get_json(url, headers=None, timeout=30, opener=None):
    req = urllib.request.Request(url, headers=headers or {})
    return http_json(req, timeout=timeout, opener=opener)


def post_json(url, payload, headers=None, timeout=60, opener=None):
    """POST a JSON body. Returns (status, raw_text) — callers check the status
    themselves, because Immich answers 200 and 201 interchangeably."""
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=hdrs, method="POST"
    )
    with _urlopen(opener)(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode()


def put_json(url, payload, headers=None, timeout=60, opener=None):
    """PUT a JSON body, decode the JSON reply.

    Immich's album membership endpoint is a PUT that answers with a per-id result
    list rather than a bare status, so unlike post_json this one decodes.
    """
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=hdrs, method="PUT"
    )
    return http_json(req, timeout=timeout, opener=opener)
