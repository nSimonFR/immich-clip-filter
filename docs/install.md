# Installing

Three routes. Docker first, because that is how Immich is run.

Whichever you pick, the shape is the same: **a plugin folder Immich reads**, and
**a sidecar it can reach over HTTP**.

```
   Immich (workflow engine)                  immich-clip-filter
   ┌──────────────────────┐                  ┌──────────────────┐
   │ clipFilter step      │ ──── POST ─────▶ │ /classify        │
   │  (WASM, in-process)  │ ◀─── verdict ─── │                  │
   └──────────────────────┘                  └────────┬─────────┘
                                                      │ reads smart_search
                                              ┌───────▼─────────┐
                                              │ Immich Postgres │
                                              └─────────────────┘
```

## Before you start

**An API key.** Immich → Account Settings → API Keys → New. Copy the secret; it
is shown once.

**A shared library needs one key per user.** A key can only put its own owner's
photos into its own owner's album, and an admin cannot mint a key for somebody
else. See [Multiple users](#multiple-users) below — get this wrong and the other
user's rules will match and file nothing.

---

## Docker Compose

### 1. Build the plugin

The plugin is a WASM module, and the URL it calls is baked in at build time —
both as the step's default and as the manifest's `allowedHosts`, which Immich
checks before it will let the call out at all. So it has to be built for *your*
deployment.

```bash
git clone https://github.com/nSimonFR/immich-clip-filter
cd immich-clip-filter
SIDECAR_URL=http://immich-clip-filter:8351/classify ./plugin/build.sh dist/plugins
```

Needs [`extism-js`](https://github.com/extism/js-pdk) and `binaryen`. Prebuilt
artifacts are attached to each release if you would rather not.

> `immich-clip-filter` there is the **compose service name**, not `127.0.0.1` —
> the plugin runs inside the Immich container.

### 2. Add the services

Copy the two services from [`docker/docker-compose.yml`](../docker/docker-compose.yml)
into your Immich compose file, put the API key in `./immich-api-key.txt`, and add
these to `immich-server`:

```yaml
    environment:
      IMMICH_ALLOW_EXTERNAL_PLUGINS: "true"
      IMMICH_PLUGINS_INSTALL_FOLDER: /plugins
    volumes:
      - ./dist/plugins:/plugins:ro
```

```bash
docker compose up -d
docker compose restart immich-server     # plugins are read at boot
```

### 3. Check it

```bash
docker compose run --rm immich-clip-filter immich-clip-doctor
```

Every line it prints is a failure that is invisible from the Immich UI. Do not
skip it.

---

## NixOS

```nix
{
  inputs.immich-clip-filter.url = "github:nSimonFR/immich-clip-filter";

  # in your host config:
  imports = [ inputs.immich-clip-filter.nixosModules.default ];

  services.immich-clip-filter = {
    enable = true;
    keyFile = config.age.secrets.immich-clip-api-key.path;
  };
}
```

That is the whole thing: the module builds the plugin with the right URL, points
Immich's plugin folder at it, runs the sidecar as the `immich` user (so Postgres
peer auth applies and there is no password anywhere), and installs the drain
timer.

> ⚠️ **The key file must be readable by the `immich` user.** With agenix that
> usually means decrypting the same secret a second time with `owner = "immich"`
> — the copy for your login user is mode 0400 and the sidecar cannot read it.

## Bare Python

```bash
pip install immich-clip-filter
cp config.example.toml /etc/immich-clip/config.toml && $EDITOR /etc/immich-clip/config.toml
immich-clip-doctor
immich-clip-sidecar
```

Then build the plugin as above and point
`IMMICH_PLUGINS_INSTALL_FOLDER` at `dist/plugins`.

---

## Writing your first rule

1. **Make an album of examples.** 15–30 photos is plenty. Put them in an album
   called, say, `Food examples`. Read
   [concepts.md §3](./concepts.md#3-nearest-vs-centroid--the-choice-that-decides-whether-it-works)
   first — whether your examples share a *subject* or a *theme* decides which
   scoring mode you want, and getting it backwards produces a confidently wrong
   rule.

2. **Calibrate the threshold.** It is not guessable; it depends on the rule and
   on your library.

   ```bash
   immich-clip-backfill --seed-album "Food examples" --album "Food" --create-album
   ```

   Dry run: it prints a distance histogram and the nearest 40 filenames.
   [calibration.md](./calibration.md) explains how to read it.

3. **Backfill what already exists**, once the threshold looks right:

   ```bash
   immich-clip-backfill --seed-album "Food examples" --album "Food" --threshold 0.30 --apply
   ```

4. **Add the workflow.** In Immich → Administration → Workflows:
   - trigger: **Asset Metadata Extraction**
   - one step: **Filter by content (CLIP)**
     - Learn from this album: `Food examples`
     - How to compare: `nearest`
     - Maximum cosine distance: your calibrated threshold
     - Albums to file matches into: `Food`

> ⚠️ **One step. Do not chain `assetAddToAlbums` after it.** On the tested Immich
> versions the workflow engine reads its steps *without an ORDER BY*, so a later
> step is not reliably later — when the add lands first, **every** asset is filed
> regardless of the verdict. That is why this step does its own type check and
> its own filing. See [limitations.md](./limitations.md).

---

## Multiple users

If your library has more than one user, and you want rules for more than one of
them:

```toml
[[immich.keys]]
owner = "nico@example.com"
key_file = "/run/secrets/nico_immich_key"

[[immich.keys]]
owner = "alfie@example.com"
key_file = "/run/secrets/alfie_immich_key"
```

Three things the docs owe you plainly:

1. **Each user must mint their own key.** There is no admin path to create one
   for somebody else.
2. **Each user must create their own workflow.** A workflow is owned by whoever
   created it (`ownerId: auth.user.id`, no impersonation).
3. **Sharing the target album is not enough.** The album has to be shared *and*
   the owner's key configured here; sharing alone gets you `no_permission`.

With no matching key, the sidecar says so by name (`ownerError` in the verdict,
and in the log) rather than reporting `filed: 0` and leaving you to guess.
`immich-clip-doctor` checks that each configured key really belongs to the user
it is labelled for — a key minted by the wrong account is the quiet version of
this same failure.
