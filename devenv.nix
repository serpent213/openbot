{pkgs, ...}: {
  packages = with pkgs; [python313 uv nodejs_24 pnpm_10];
  env.ELECTRON_SKIP_BINARY_DOWNLOAD = "1";
}
