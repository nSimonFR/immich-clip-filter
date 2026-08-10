#!/usr/bin/env bash
# Build the WASM workflow step into a folder Immich can load.
#
#   ./plugin/build.sh [outdir]
#
# Immich reads plugins from SUBDIRECTORIES of IMMICH_PLUGINS_INSTALL_FOLDER, each
# holding manifest.json plus the wasm named by `wasmPath`. So the output is
#   <outdir>/clip-filter/{manifest.json,clip_filter.wasm}
# and IMMICH_PLUGINS_INSTALL_FOLDER should point at <outdir>, not at the plugin.
#
# Needs `extism-js` (github.com/extism/js-pdk) and binaryen on PATH; extism-js
# shells out to wasm-merge and wasm-opt.
#
# SIDECAR_URL is baked in twice, and both matter: as the step's default in the
# manifest, and as the manifest's allowedHosts entry, which the host checks before
# it will make the call at all. Point it at wherever the sidecar listens *from
# Immich's point of view* — in Docker Compose that is the service name, not
# 127.0.0.1.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-${here}/../dist/plugins}"
name="${PLUGIN_NAME:-clip-filter}"
sidecar="${SIDECAR_URL:-http://127.0.0.1:8351/classify}"

for tool in extism-js python3; do
  command -v "$tool" >/dev/null || { echo "missing $tool on PATH" >&2; exit 1; }
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cp "$here/plugin.js" "$here/plugin.d.ts" "$work/"

# The literal in DEFAULTS is the fallback used when a step leaves the box empty,
# so it has to agree with the manifest default. Rewritten on a copy, which keeps
# plugin.js itself valid, lintable JavaScript.
python3 - "$work/plugin.js" "$sidecar" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
p.write_text(re.sub(r'sidecar: "[^"]*"', f'sidecar: "{sys.argv[2]}"', p.read_text()))
PY

(cd "$work" && extism-js plugin.js -i plugin.d.ts -o clip_filter.wasm)

dest="$out/$name"
mkdir -p "$dest"
install -m444 "$work/clip_filter.wasm" "$dest/clip_filter.wasm"
# Hash the REWRITTEN source, so two builds with different sidecar URLs get
# different manifests and Immich actually re-imports when you change it.
python3 "$here/manifest.py" \
  --sidecar-url "$sidecar" --name "$name" \
  --source "$work/plugin.js" --source "$work/plugin.d.ts" \
  > "$dest/manifest.json"

echo "built $dest"
echo "  set IMMICH_ALLOW_EXTERNAL_PLUGINS=true and IMMICH_PLUGINS_INSTALL_FOLDER=$out"
