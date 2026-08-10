// Interface file for `extism-js plugin.js -i plugin.d.ts -o clip_filter.wasm`.
//
// "main" declares what the wasm EXPORTS — the name must match the method name in
// manifest.json, because Immich calls `plugin.call(methodName, ...)`.
//
// "extism:host".user declares what it IMPORTS. Immich registers its host
// functions under the `extism:host/user` namespace (plugin.repository.js), and
// only hands the real implementations to plugins whose method sets
// `hostFunctions: true` — everything else gets a stub that throws.
declare module "main" {
  export function clipFilter(): I32;
}

declare module "extism:host" {
  interface user {
    // The only host function this plugin needs. Filing is done by the sidecar
    // (which also owns the deferred path), not by addAssetsToAlbums here.
    httpRequest(ptr: I64): I64;
  }
}
