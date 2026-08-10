{ lib, python3Packages }:

python3Packages.buildPythonPackage {
  pname = "immich-clip-filter";
  version = "1.0.0";
  format = "pyproject";

  src = lib.cleanSourceWith {
    src = ../.;
    # Keeps the wasm toolchain, CI config and docs out of the store path, so a
    # docs edit does not rebuild the package.
    filter = path: type:
      let rel = lib.removePrefix (toString ../. + "/") (toString path);
      in !(lib.hasPrefix "docs" rel || lib.hasPrefix ".github" rel
           || lib.hasPrefix "dist" rel || lib.hasPrefix "docker" rel);
  };

  nativeBuildInputs = [
    python3Packages.setuptools
    python3Packages.pythonRelaxDepsHook
  ];

  # pyproject.toml asks for `psycopg2-binary`, which is the right answer for pip
  # and for the Docker image: it ships wheels, so nobody needs libpq headers and a
  # compiler to install this. Nix has no such attribute — the prebuilt wheel would
  # be a broken binary here anyway — so the dependency is dropped by name and the
  # real `psycopg2` supplied instead. Same module, same import.
  pythonRemoveDeps = [ "psycopg2-binary" ];
  propagatedBuildInputs = [ python3Packages.psycopg2 ];
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  # Only the offline suite. The others need a live Immich, which the sandbox has
  # neither network nor business to start.
  pytestFlags = [ "tests/unit" ];
  preCheck = ''export PYTHONPATH="$PWD/tests:$PYTHONPATH"'';

  # Catches an `os.environ[...]` or a `connect()` that crept up to module level —
  # in the sandbox, at build time, rather than on a timer at 04:50.
  pythonImportsCheck = [
    "immich_clip"
    "immich_clip.config"
    "immich_clip.sidecar"
    "immich_clip.drain"
    "immich_clip.backfill"
    "immich_clip.profile"
    "immich_clip.doctor"
    "immich_clip.schema"
    "immich_clip.store"
    "immich_clip.api"
    "immich_clip.queue"
    "immich_clip.exclusions"
    "immich_clip.vectors"
  ];

  meta = {
    description = "Content-based auto-filing for Immich, using the CLIP embeddings it already computes";
    homepage = "https://github.com/nSimonFR/immich-clip-filter";
    license = lib.licenses.mit;
    mainProgram = "immich-clip-sidecar";
  };
}
