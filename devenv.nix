{pkgs, ...}: {
  packages = with pkgs; [python313 uv nodejs_24 pnpm_10];
  env.ELECTRON_SKIP_BINARY_DOWNLOAD = "1";

  # Start with `devenv up postgres`; normal development still defaults to SQLite.
  services.postgres = {
    enable = true;
    package = pkgs.postgresql_17;
    extensions = extensions: [extensions.pgvector];
    listen_addresses = "127.0.0.1";
    port = 15432;
    initialDatabases = [{name = "openbot_dev";}];
  };
}
