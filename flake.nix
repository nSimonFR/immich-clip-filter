{
  description = "Content-based auto-filing for Immich: a CLIP filter step for Workflows";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      # The NixOS module is system-independent, so it lives outside eachSystem.
      # A consumer does `imports = [ inputs.immich-clip-filter.nixosModules.default ]`
      # and gets `services.immich-clip-filter.*` — the same shape sure-nix,
      # airtrail-nix and ryot-nix use.
      moduleOutputs = {
        nixosModules.default = import ./nix/module.nix self;
        overlays.default = final: prev: {
          immich-clip-filter = final.callPackage ./nix/package.nix { };
          immich-clip-plugin = final.callPackage ./nix/plugin.nix { src = ./plugin; };
        };
      };
    in
    moduleOutputs // flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        immich-clip-filter = pkgs.callPackage ./nix/package.nix { };
        immich-clip-plugin = pkgs.callPackage ./nix/plugin.nix { src = ./plugin; };
      in
      {
        packages = {
          default = immich-clip-filter;
          inherit immich-clip-filter immich-clip-plugin;
        };

        # `nix flake check` runs the unit suite. Only the unit suite: the
        # integration one needs a live Immich, which a Nix sandbox has no business
        # starting.
        checks = {
          unit = immich-clip-filter;
          plugin = immich-clip-plugin;
        };

        devShells.default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pytest ps.psycopg2 ]))
            pkgs.extism-js
            pkgs.binaryen
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/src:$PWD/tests:$PYTHONPATH"
            echo "pytest tests/unit            — offline, a second"
            echo "./plugin/build.sh            — builds dist/plugins/clip-filter"
            echo "see tests/integration/README.md for the rest"
          '';
        };
      });
}
