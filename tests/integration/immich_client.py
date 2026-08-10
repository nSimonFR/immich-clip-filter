"""A tiny Immich REST client for the integration harness.

Deliberately not the project's own `api` module: these tests exist to check that
module against a real server, so driving them through it would let a wrong
assumption agree with itself.

Everything here is stdlib. The harness should not need a dependency the sidecar
does not have.
"""

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid


class Immich:
    def __init__(self, base_url, token=None, api_key=None):
        self.base = base_url.rstrip("/")
        self.token = token
        self.api_key = api_key

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _headers(self, extra=None):
        h = dict(extra or {})
        if self.api_key:
            h["x-api-key"] = self.api_key
        elif self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def request(self, method, path, payload=None, raw=None, headers=None, timeout=60):
        body, hdrs = raw, self._headers(headers)
        if payload is not None:
            body = json.dumps(payload).encode()
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{self.base}{path}", data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode()
                return resp.status, (json.loads(text) if text else None)
        except urllib.error.HTTPError as e:
            text = e.read().decode()
            try:
                return e.code, json.loads(text)
            except ValueError:
                return e.code, text

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, payload=None, **kw):
        return self.request("POST", path, payload=payload, **kw)

    def put(self, path, payload=None, **kw):
        return self.request("PUT", path, payload=payload, **kw)

    def delete(self, path, payload=None, **kw):
        return self.request("DELETE", path, payload=payload, **kw)

    # ── bootstrap ────────────────────────────────────────────────────────────
    def wait_until_up(self, timeout=300, sleep=time.sleep):
        """Immich's first boot runs migrations; on small CI runners that is slow."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                status, body = self.get("/api/server/ping", timeout=5)
                if status == 200:
                    return body
            except Exception as e:  # noqa: BLE001
                last = e
            sleep(2)
        raise TimeoutError(f"Immich never came up at {self.base} ({last})")

    def admin_signup(self, email, password, name="Integration"):
        # 400 means an admin already exists, which is fine on a re-run.
        return self.post("/api/auth/admin-sign-up",
                         {"email": email, "password": password, "name": name})

    def login(self, email, password):
        status, body = self.post("/api/auth/login", {"email": email, "password": password})
        assert status in (200, 201), (status, body)
        self.token = body["accessToken"]
        return self.token

    def create_user(self, email, password, name):
        status, body = self.post(
            "/api/admin/users", {"email": email, "password": password, "name": name})
        assert status in (200, 201), (status, body)
        return body

    def create_api_key(self, name="integration", permissions=("all",)):
        """Mint a key for whoever the current bearer token belongs to.

        There is no admin path to mint a key *for* another user — which is exactly
        why per-owner keys have to be configured rather than derived.
        """
        status, body = self.post(
            "/api/api-keys", {"name": name, "permissions": list(permissions)})
        assert status in (200, 201), (status, body)
        return body["secret"]

    def me(self):
        status, body = self.get("/api/users/me")
        assert status == 200, (status, body)
        return body

    # ── content ──────────────────────────────────────────────────────────────
    def upload(self, path, device_asset_id=None):
        """POST /api/assets as multipart. Returns the new asset's id."""
        device_asset_id = device_asset_id or f"it-{uuid.uuid4().hex}"
        stamp = "2026-01-01T00:00:00.000Z"
        fields = {
            "deviceAssetId": device_asset_id,
            "deviceId": "integration",
            "fileCreatedAt": stamp,
            "fileModifiedAt": stamp,
        }
        boundary = f"----it{uuid.uuid4().hex}"
        parts = []
        for key, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n".encode())
        filename = os.path.basename(path)
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="assetData"; '
            f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode())
        with open(path, "rb") as f:
            parts.append(f.read())
        parts.append(f"\r\n--{boundary}--\r\n".encode())

        status, body = self.request(
            "POST", "/api/assets", raw=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        assert status in (200, 201), (status, body)
        # ⚠️ Immich dedupes by checksum and answers `duplicate` with the id of the
        # asset it already had. Returning that silently hands the test a DIFFERENT
        # photo than it thinks it uploaded — which is how a "far away" fixture
        # ended up being one of the seed photos. Fail loudly instead.
        assert body.get("status") != "duplicate", (
            f"Immich deduplicated this upload and returned an existing asset "
            f"({body['id']}). Fixture images must have unique bytes — see "
            f"a_unique_png() in conftest.")
        return body["id"]

    def create_album(self, name, asset_ids=()):
        status, body = self.post(
            "/api/albums", {"albumName": name, "assetIds": list(asset_ids)})
        assert status in (200, 201), (status, body)
        return body["id"]

    def album_asset_ids(self, album_id):
        """⚠️ Read through the album's own endpoint, which is the thing that
        surprised us: as of 3.1 it reports `assetCount` but may hand back an empty
        `assets` list, so the sidecar reads membership from Postgres instead. The
        harness asserts against BOTH, so a change in either is visible."""
        status, body = self.get(f"/api/albums/{album_id}")
        assert status == 200, (status, body)
        return [a["id"] for a in (body.get("assets") or [])], body.get("assetCount")

    def add_to_album(self, album_id, asset_ids):
        return self.put(f"/api/albums/{album_id}/assets", {"ids": list(asset_ids)})

    def remove_from_album(self, album_id, asset_ids):
        return self.delete(f"/api/albums/{album_id}/assets", {"ids": list(asset_ids)})
