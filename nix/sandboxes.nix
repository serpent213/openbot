# Optional provisioning only. Import explicitly; this module never activates itself.
{ config, lib, pkgs, ... }:
let
  cfg = config.services.openbot.sandboxes;
  slots = lib.genList (i: {
    name = "${cfg.userPrefix}${toString (i + 1)}";
    uid = cfg.uidBase + i;
  }) cfg.poolSize;
in {
  options.services.openbot.sandboxes = {
    enable = lib.mkEnableOption "preprovisioned Openbot sandbox workers";
    poolSize = lib.mkOption { type = lib.types.ints.positive; default = 16; };
    userPrefix = lib.mkOption { type = lib.types.str; default = "openbot-sbx"; };
    uidBase = lib.mkOption {
      type = lib.types.ints.positive;
      description = "Explicit collision-free stable UID/GID base, compatible with the host/LXC mapping.";
    };
    homeRoot = lib.mkOption { type = lib.types.str; default = "/home/openbot-sandboxes"; };
    controllerUser = lib.mkOption { type = lib.types.str; default = "openbot"; };
    controllerUid = lib.mkOption { type = lib.types.ints.positive; };
    python = lib.mkOption {
      type = lib.types.path;
      description = "Absolute Python executable with this Openbot package and its existing dependencies installed.";
    };
    packages = lib.mkOption { type = lib.types.listOf lib.types.package; default = [ pkgs.bash pkgs.coreutils ]; };
  };
  config = lib.mkIf cfg.enable {
    assertions = [{
      assertion = config.users.users.${cfg.controllerUser}.uid == cfg.controllerUid;
      message = "Openbot sandbox controller requires an existing account with the specified stable UID.";
    }];
    users.users = lib.listToAttrs (map (slot: lib.nameValuePair slot.name {
      isSystemUser = true;
      uid = slot.uid;
      group = slot.name;
      home = "${cfg.homeRoot}/${slot.name}";
      createHome = true;
      homeMode = "0700";
      shell = "${pkgs.shadow}/bin/nologin";
    }) slots) // {
      ${cfg.controllerUser}.extraGroups = map (slot: slot.name) slots;
    };
    users.groups = lib.listToAttrs (map (slot: lib.nameValuePair slot.name { gid = slot.uid; }) slots);
    systemd.tmpfiles.rules = [ "d ${cfg.homeRoot} 0755 root root -" ];
    systemd.services = lib.listToAttrs (map (slot: lib.nameValuePair slot.name {
      description = "Openbot worker ${slot.name} (externally managed persistent slot)";
      wantedBy = [ "multi-user.target" ];
      path = cfg.packages;
      environment.HOME = "${cfg.homeRoot}/${slot.name}";
      serviceConfig = {
        User = slot.name;
        Group = slot.name;
        RuntimeDirectory = slot.name;
        RuntimeDirectoryMode = "0750";
        WorkingDirectory = "${cfg.homeRoot}/${slot.name}";
        ExecStartPre = "${pkgs.coreutils}/bin/mkdir -p ${cfg.homeRoot}/${slot.name}/workspace";
        ExecStart = "${cfg.python} -m openbot.sandboxes.worker --resource ${slot.name} --socket /run/${slot.name}/worker.sock --root ${cfg.homeRoot}/${slot.name}/workspace --controller-uid ${toString cfg.controllerUid}";
        # An unexpected death requires core quarantine/reconciliation, not auto-replay.
        Restart = "no";
        KillMode = "control-group";
        TimeoutStopSec = 10;
        UMask = "0077";
        NoNewPrivileges = true;
        CapabilityBoundingSet = "";
        RestrictSUIDSGID = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectSystem = "strict";
        ProtectHome = "tmpfs";
        BindPaths = [ "${cfg.homeRoot}/${slot.name}" ];
        ReadWritePaths = [ "${cfg.homeRoot}/${slot.name}" "/run/${slot.name}" ];
        ProtectProc = "invisible";
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        TasksMax = 128;
        MemoryMax = "2G";
      };
    }) slots);
    # Network grants, Nix daemon access/store visibility and server-state masks
    # are site-specific and MUST be reviewed before real credentials are provisioned.
    # Do not shrink/renumber this declarative pool until affected slots are drained
    # and quarantined. No safe shrink/recycling activation hook is supplied yet.
  };
}
