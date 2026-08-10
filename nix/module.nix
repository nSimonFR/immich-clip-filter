# NixOS module — the second distribution, after Docker.
#
# It holds configuration and nothing else: the package, the plugin and every
# behavioural decision live in this repo, so a consuming flake keeps a handful of
# lines instead of a copy.
#
#   imports = [ inputs.immich-clip-filter.nixosModules.default ];
#   services.immich-clip-filter = {
#     enable = true;
#     keyFile = config.age.secrets.immich-api-key.path;
#   };
self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.immich-clip-filter;
  inherit (lib) mkEnableOption mkIf mkOption types;

  sidecarUrl = "http://${cfg.listenAddress}:${toString cfg.port}/classify";

  plugin = pkgs.callPackage ../nix/plugin.nix {
    src = ../plugin;
    inherit sidecarUrl;
    inherit (cfg) pluginName;
  };

  configFile = (pkgs.formats.toml { }).generate "immich-clip.toml" {
    immich = {
      url = cfg.immichUrl;
      clip_model = cfg.clipModel;
      keys =
        (lib.optional (cfg.keyFile != null) { owner = "*"; key_file = cfg.keyFile; })
        ++ lib.mapAttrsToList (owner: key_file: { inherit owner key_file; }) cfg.ownerKeyFiles;
    } // lib.optionalAttrs (cfg.databaseUrl != null) { db_url = cfg.databaseUrl; };
    sidecar = {
      listen = "${cfg.listenAddress}:${toString cfg.port}";
      state_dir = cfg.stateDir;
      max_wait = cfg.maxWait;
    };
    drain = {
      requeue_every = cfg.requeueEvery;
      max_age_days = cfg.maxAgeDays;
      apply = true;   # the timer is where dry-run is opted out of
    };
  };

  # Common to the sidecar and the drainer: both read Immich's database, and on a
  # same-host install they do it over the unix socket under peer auth, which is
  # why they run as the immich user and carry no password.
  hardening = {
    NoNewPrivileges = true;
    PrivateDevices = true;
    PrivateTmp = true;
    ProtectHome = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectControlGroups = true;
    RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
    RestrictNamespaces = true;
    RestrictRealtime = true;
    SystemCallArchitectures = "native";
  };
in
{
  options.services.immich-clip-filter = {
    enable = mkEnableOption "the CLIP content filter for Immich Workflows";

    package = mkOption {
      type = types.package;
      # This flake's own build, so a consumer gets the tested one by default.
      # NOT `nullOr` with a null default and `cfg.package or …` as the fallback:
      # Nix's `or` only catches a failed ATTRIBUTE LOOKUP, so a null default is
      # simply null, and it coerces into the ExecStart string as the delightful
      # `cannot coerce null to a string`. Caught by evaluating this against a real
      # host config rather than by reading it.
      default = self.packages.${pkgs.system}.immich-clip-filter;
      defaultText =
        lib.literalExpression "immich-clip-filter.packages.\${system}.immich-clip-filter";
      description = "The sidecar package to run.";
    };

    user = mkOption {
      type = types.str;
      default = "immich";
      description = ''
        Which user the sidecar runs as. The default exists for one reason: with
        no host set, psycopg2 uses the Postgres unix socket and `local all all
        peer` then applies — so running as `immich` needs no password and no new
        database role. Point `databaseUrl` somewhere and this can be anything.
      '';
    };

    port = mkOption { type = types.port; default = 8351; };
    listenAddress = mkOption { type = types.str; default = "127.0.0.1"; };

    immichUrl = mkOption {
      type = types.str;
      default = "http://127.0.0.1:2283";
    };

    databaseUrl = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = ''
        A libpq DSN. Leave null for the unix socket + peer auth described under
        `user`, which is the right answer when Immich is on this host.
      '';
    };

    keyFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      description = ''
        Path to a file holding an Immich API key — the default key, used for
        every owner without one of their own.

        ⚠️ It must be readable by `user`. With agenix that usually means
        decrypting the same secret a second time with `owner = "immich"`, since
        the copy for your login user is mode 0400.
      '';
    };

    ownerKeyFiles = mkOption {
      type = types.attrsOf types.path;
      default = { };
      example = lib.literalExpression ''{ "alfie@example.com" = "/run/agenix/alfie-immich-key"; }'';
      description = ''
        Per-owner API keys, keyed by the owner's Immich email.

        A key can only file its own owner's photos, and a workflow is always
        owned by whoever created it — so on a shared library, one key means the
        other user's photos match the rule and then file nothing. Each user has
        to mint their own key: an admin cannot do it for them.
      '';
    };

    clipModel = mkOption {
      type = types.str;
      default = config.services.immich.settings.machineLearning.clip.modelName or "";
      defaultText = lib.literalExpression "config.services.immich.settings.machineLearning.clip.modelName";
      description = ''
        Derived, never typed twice. A centroid built against one CLIP model is
        meaningless under another, so the sidecar refuses a profile whose recorded
        model does not match this.
      '';
    };

    stateDir = mkOption { type = types.str; default = "/var/lib/immich-clip"; };

    maxWait = mkOption {
      type = types.int;
      default = 120;
      description = ''
        Ceiling on what a workflow step may ask to wait for. Immich's workflow
        queue runs a handful of jobs and the extism plugin pool is small — an
        unbounded wait typed into a config box could pin all of them during an
        import.
      '';
    };

    requeueEvery = mkOption { type = types.str; default = "1h"; };
    maxAgeDays = mkOption { type = types.int; default = 30; };

    drainInterval = mkOption {
      type = types.str;
      default = "15min";
      description = "systemd OnUnitActiveSec for the drain timer.";
    };

    installPlugin = mkOption {
      type = types.bool;
      default = true;
      description = ''
        Point Immich's plugin folder at the built wasm and switch external
        plugins on. Turn off if you install the plugin some other way.
      '';
    };

    pluginName = mkOption {
      type = types.str;
      default = "clip-filter";
      description = ''
        The name Immich registers the plugin under.

        ⚠️ **Changing this on a working install orphans every workflow that uses
        it.** A workflow step points at a `plugin_method` row belonging to one
        `plugin` row, keyed by name — so under a new name Immich imports a second,
        separate plugin, the old one stops being loaded (it is no longer in the
        folder), and every existing step references something that is not there.
        Each one has to be deleted and recreated by hand.

        Set it only to *keep* an existing name across a migration — for example an
        install that ran an earlier build under its own name. Back the step
        configs up first either way; see docs/limitations.md.
      '';
    };
  };

  config = mkIf cfg.enable {
    assertions = [{
      assertion = cfg.keyFile != null || cfg.ownerKeyFiles != { };
      message = ''
        services.immich-clip-filter needs at least one API key: the sidecar can
        decide but cannot file anything without one. Set `keyFile`, or an entry
        in `ownerKeyFiles`.
      '';
    }];

    # `environment` is a freeform attrsOf str and NixOS merges it across modules,
    # so the whole feature stays here and the Immich module is left alone.
    services.immich.environment = mkIf cfg.installPlugin {
      IMMICH_ALLOW_EXTERNAL_PLUGINS = "true";
      IMMICH_PLUGINS_INSTALL_FOLDER = "${plugin}";
    };

    systemd.services.immich-clip-filter = {
      description = "CLIP filter sidecar for the Immich workflow step";
      wantedBy = [ "multi-user.target" ];
      after = [ "postgresql.service" "network.target" ];
      wants = [ "postgresql.service" ];
      environment.IMMICH_CLIP_CONFIG = "${configFile}";
      serviceConfig = hardening // {
        User = cfg.user;
        ExecStart = "${cfg.package}/bin/immich-clip-sidecar";
        Restart = "on-failure";
        RestartSec = 5;
        StateDirectory = baseNameOf cfg.stateDir;
        StateDirectoryMode = "0750";
      };
    };

    # The deferred half. Immich does NOT re-queue missing CLIP embeddings on its
    # own — `handleNightlyJobs` covers missing thumbnails and face clustering and
    # nothing else — so without this a photo uploaded while the ML server was
    # down waits forever, no matter how patient the sidecar is.
    systemd.services.immich-clip-drain = {
      description = "Finish CLIP verdicts parked while the ML server was offline";
      after = [ "postgresql.service" "network-online.target" ];
      wants = [ "network-online.target" ];
      environment.IMMICH_CLIP_CONFIG = "${configFile}";
      serviceConfig = hardening // {
        Type = "oneshot";
        User = cfg.user;
        ExecStart = "${cfg.package}/bin/immich-clip-drain";
        StateDirectory = baseNameOf cfg.stateDir;
        StateDirectoryMode = "0750";
      };
    };

    systemd.timers.immich-clip-drain = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "8min";
        OnUnitActiveSec = cfg.drainInterval;
        # So a pass that was due while the machine was off happens on the next
        # boot rather than waiting a full interval.
        Persistent = true;
      };
    };

    # `sudo -u immich immich-clip-backfill --seed-album "Food" --album Food`
    environment.systemPackages = [ cfg.package ];

    # The sidecar's StateDirectory creates this, but the by-hand tools may run
    # before the service ever has.
    systemd.tmpfiles.rules = [
      "d ${cfg.stateDir} 0750 ${cfg.user} ${cfg.user} -"
      "d ${cfg.stateDir}/profiles 0750 ${cfg.user} ${cfg.user} -"
    ];
  };
}
