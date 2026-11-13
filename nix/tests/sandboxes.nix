# NixOS VM test for nix/sandboxes.nix and the worker's kernel-level boundaries:
# real UIDs, cgroup v2 delegation and user/mount namespaces, which the pytest
# suite replaces with one UID and fake cgroups. Needs a Linux builder with KVM.
#
#   import ./nix/tests/sandboxes.nix { inherit pkgs; runtime = <python env with openbot>; }
#
# `runtime` is a python.withPackages environment containing this backend. Site
# policy (firewall, Nix daemon access, credentials) is out of scope here.
{ pkgs, runtime }:
let
  python = "${runtime}/bin/python";
  client = "${python} -I ${./sandbox_client.py}";
  asController = "runuser -u openbot -- ${client}";
  home = "/home/openbot-sandboxes";
in
pkgs.testers.runNixOSTest {
  name = "openbot-sandboxes";

  nodes.machine = { pkgs, ... }: {
    imports = [ ../sandboxes.nix ];
    virtualisation = { cores = 2; memorySize = 2048; };
    users.users.openbot = { isSystemUser = true; uid = 1100; group = "openbot"; home = "/var/lib/openbot"; createHome = true; homeMode = "0700"; };
    users.groups.openbot = { };
    users.users.mallory = { isNormalUser = true; uid = 1200; };
    services.openbot.sandboxes = {
      enable = true;
      poolSize = 2;
      uidBase = 62000;
      controllerUid = 1100;
      inherit python;
      packages = with pkgs; [ bash coreutils procps util-linux runtime ];
    };
    # Must not reach agent commands: the worker forwards an allowlist only.
    systemd.services.openbot-sbx1.environment.SANDBOX_TEST_CANARY = "service-environment-leaked";
  };

  testScript = ''
    import json
    import shlex

    def call(*args, user="${asController}"):
        return json.loads(machine.succeed(user + " " + " ".join(shlex.quote(a) for a in args)))

    def shell(slot, command, timeout=30):
        result = call("shell", slot, command, str(timeout))
        assert result.startswith("exit code:"), result
        return result

    machine.wait_for_unit("multi-user.target")
    for slot in ("openbot-sbx1", "openbot-sbx2"):
        machine.wait_for_unit(f"{slot}.service")
        machine.wait_for_file(f"/run/{slot}/worker.sock")

    with subtest("commands run as the slot's own UID with a scrubbed environment"):
        machine.succeed("systemctl show -p Environment openbot-sbx1 | grep -q SANDBOX_TEST_CANARY")
        out = shell("openbot-sbx1", "id -u; id -un; env")
        assert "62000\nopenbot-sbx1\n" in out, out
        assert "HOME=${home}/openbot-sbx1\n" in out, out
        assert "SANDBOX_TEST_CANARY" not in out, out
        machine.succeed("test $(stat -c %a ${home}/openbot-sbx1) = 700")

    with subtest("own files are writable; other homes and controller state are not reachable"):
        machine.succeed("echo controller-secret > /var/lib/openbot/secret && chown openbot: /var/lib/openbot/secret && chmod 600 /var/lib/openbot/secret")
        assert "own-file" in shell("openbot-sbx2", "echo own-file > note && cat note")
        out = shell("openbot-sbx1", "cat ${home}/openbot-sbx2/workspace/note; ls -a ${home}; cat /var/lib/openbot/secret")
        assert "own-file" not in out and "controller-secret" not in out, out
        assert "openbot-sbx2" not in out.split("stderr:")[0], out

    with subtest("controller processes are hidden from agent commands"):
        # Bracketed patterns keep pgrep/pkill from matching the driver's own shell.
        machine.succeed("runuser -u openbot -- sleep 1005 >/dev/null 2>&1 &")
        pid = machine.wait_until_succeeds("pgrep -f 'sleep 100[5]'").strip()
        assert "hidden" in shell("openbot-sbx1", f"test -e /proc/{pid} && echo visible || echo hidden")
        machine.succeed("pkill -f 'sleep 100[5]'")

    with subtest("only the controller can use a worker socket"):
        assert call("connect", "/run/openbot-sbx1/worker.sock", user="runuser -u mallory -- ${client}") == "denied"
        out = shell("openbot-sbx1", "${client} connect /run/openbot-sbx2/worker.sock; ${client} connect /run/openbot-sbx1/worker.sock")
        assert '"denied"\n"no-reply"' in out, out
        assert call("connect", "/run/openbot-sbx2/worker.sock") == "reply"

    with subtest("commands cannot leave their operation cgroup"):
        out = shell("openbot-sbx1", """
            cg=$(cut -d: -f3 /proc/self/cgroup)
            echo $$ > "/sys/fs/cgroup''${cg%%/openbot-runs/*}/cgroup.procs" && echo MOVED
            unshare -Urm sh -c 'mount -o remount,bind,rw /sys/fs/cgroup && echo REMOUNTED'
            mount -o remount,bind,rw /sys/fs/cgroup && echo REMOUNTED
            exit 0
        """)
        assert "MOVED" not in out and "REMOUNTED" not in out, out
        assert "Read-only file system" in out, out

    with subtest("detached descendants end with their operation"):
        out = shell("openbot-sbx1", "setsid sleep 1001 </dev/null >/dev/null 2>&1 & (sleep 1002 </dev/null >/dev/null 2>&1 &); sleep 0.5; echo running=$(pgrep -c -x sleep)")
        assert "running=2" in out, out
        machine.fail("pgrep -f 'sleep 100[12]'")

    with subtest("cancelling one operation leaves its sibling running"):
        state = call("parallel", "openbot-sbx1")
        assert state["started"] and state["cancelled"] == "CancelledError", state
        assert state["cancelled_gone"], state
        assert state["sibling_alive_meanwhile"], state
        assert "sibling-done" in state["sibling"], state

    with subtest("losing the controller connection cleans up the whole run"):
        machine.succeed("${asController} hold openbot-sbx2 >/dev/null 2>&1 &")
        machine.wait_until_succeeds("pgrep -f 'sleep 100[4]'")
        machine.succeed("pkill -9 -f 'sandbox_client[.]py hold'")
        machine.wait_until_fails("pgrep -f 'sleep 100[4]'", timeout=30)
        # The worker releases its lease only after confirming the run cgroup is empty.
        machine.wait_until_succeeds("${asController} shell openbot-sbx2 'echo again' | grep -q again", timeout=30)
        machine.succeed("systemctl is-active openbot-sbx1.service openbot-sbx2.service")
  '';
}
