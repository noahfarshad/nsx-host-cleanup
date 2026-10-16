"""Drive nsx_cleanup's decisions against stub hosts - no vCenter, no SSH."""
import sys, types
sys.path.insert(0, "..")

# paramiko and pyVmomi are present here; the module imports them at load time.
import nsx_cleanup as N

fails = []


def ck(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


class FakeSsh:
    """Stands in for the Ssh context manager; serves canned command output."""
    def __init__(self, vibs, vteps=(), props=(), rc=0, after=None):
        self.vibs, self.vteps, self.props, self.rc = vibs, vteps, props, rc
        self.after = after
        self.ran = []
        self.visits = 0

    def __call__(self, host, ip, pw, user="root"):
        self.visits += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cmd, timeout=900, prompts=(), tap=None):
        self.ran.append(cmd)
        if "del nsx" in cmd:
            if self.rc == 0 and self.after is not None:
                self.vibs = self.after        # a del nsx that really removed them
            return self.rc, "Successfully deleted NSX" if self.rc == 0 else "", \
                   "" if self.rc == 0 else "vdl2 busy"
        vibs = self.vibs
        if "vib list" in cmd:
            head = "Name  Version  Vendor\n----  -------  ------\n"
            return 0, head + "\n".join(
                "%s 4.2.3.3.0-8.0.25171319 VMware" % v for v in vibs), ""
        if "esxcfg-vmknic" in cmd:
            return 0, "\n".join(["vmk0 40 IPv4 10.0.60.117"] +
                                ["%s 10 IPv4 10.0.70.1" % v for v in self.vteps]), ""
        if "net-dvs" in cmd:
            return 0, "\n".join(self.props), ""
        if "dvs vmware list" in cmd:
            return 0, "", ""
        return 0, "", ""


def host(name="esx17.example.com", connected=True, mm=False):
    vnic = types.SimpleNamespace(device="vmk0",
                                 spec=types.SimpleNamespace(
                                     ip=types.SimpleNamespace(ipAddress="10.0.60.117")))
    return types.SimpleNamespace(
        name=name,
        runtime=types.SimpleNamespace(
            connectionState="connected" if connected else "notResponding",
            powerState="poweredOn", inMaintenanceMode=mm),
        config=types.SimpleNamespace(network=types.SimpleNamespace(vnic=[vnic])),
        parent=types.SimpleNamespace(configurationEx=types.SimpleNamespace(
            vsanConfigInfo=types.SimpleNamespace(enabled=True))))


NSX = ["nsx-adf", "nsx-cfgagent", "nsx-esx-datapath", "nsxcli"]


def with_ssh(stub, fn):
    real_ssh, real_reboot, real_mm, real_xmm = N.Ssh, N.reboot_and_wait, \
        N.enter_maintenance, N.exit_maintenance
    moves = []
    N.Ssh = stub
    N.reboot_and_wait = lambda h, minutes=25, tag="": moves.append("reboot")
    N.enter_maintenance = lambda h, e, minutes=60: moves.append("mm") or True
    N.exit_maintenance = lambda h, minutes=30: moves.append("exit-mm")
    try:
        return fn(), moves
    finally:
        N.Ssh, N.reboot_and_wait = real_ssh, real_reboot
        N.enter_maintenance, N.exit_maintenance = real_mm, real_xmm


# --- a plan changes nothing -------------------------------------------------
s = FakeSsh(NSX)
(res, moves) = with_ssh(s, lambda: N.clean_host(host(), "pw", "ensureObjectAccessibility", False))
ck("plan: reports what it would clean and touches nothing",
   res[0] == N.CLEAN and moves == [] and not any("del nsx" in c for c in s.ran))
ck("plan: says the vibs are inert, which is why this is safe", "inert" in res[1])

# --- the happy path ---------------------------------------------------------
s = FakeSsh(NSX, after=[])
(res, moves) = with_ssh(s, lambda: N.clean_host(host(), "pw", "ensureObjectAccessibility", True))
ck("commit: maintenance mode, del nsx, reboot, verify, exit - in that order",
   res[0] == N.CLEAN and moves == ["mm", "reboot", "exit-mm"])
ck("commit: it is nsxcli that removes them, never esxcli vib remove",
   any('nsxcli -c "del nsx"' in c for c in s.ran)
   and not any("vib remove" in c for c in s.ran))
ck("commit: the host is surveyed again after the reboot", s.visits == 2)

# --- a host that is genuinely NSX-prepared ----------------------------------
s = FakeSsh(NSX, vteps=["vmk10", "vmk50"])
(res, moves) = with_ssh(s, lambda: N.clean_host(host(), "pw", "ensureObjectAccessibility", True))
ck("configured host: refused, not cleaned", res[0] == N.DIRTY and moves == [])
ck("configured host: points at the careful path", "KB 451192" in res[1])

s = FakeSsh(NSX, props=["com.vmware.nsx.vdl2.enabled = true"])
(res, moves) = with_ssh(s, lambda: N.clean_host(host(), "pw", "ensureObjectAccessibility", True))
ck("nsx properties on the vds count as configured too", res[0] == N.DIRTY and moves == [])

# --- nothing to do ----------------------------------------------------------
s = FakeSsh([])
(res, moves) = with_ssh(s, lambda: N.clean_host(host(), "pw", "ensureObjectAccessibility", True))
ck("a clean host is left alone and reported ok", res[0] == N.OK and moves == [])

# --- del nsx refuses --------------------------------------------------------
s = FakeSsh(NSX, rc=1)
(res, moves) = with_ssh(s, lambda: N.clean_host(host(), "pw", "ensureObjectAccessibility", True))
ck("a failed del nsx stops before the reboot", res[0] == N.FAIL and "reboot" not in moves)
ck("and names the KB rather than suggesting a workaround", "KB 451192" in res[1])

# --- vibs survive the reboot ------------------------------------------------
s = FakeSsh(NSX, after=NSX)
(res, moves) = with_ssh(s, lambda: N.clean_host(host(), "pw", "ensureObjectAccessibility", True))
ck("a del nsx that removes nothing is a failure, and the host stays in maintenance mode",
   res[0] == N.FAIL and "exit-mm" not in moves)

# --- a disconnected host ----------------------------------------------------
s = FakeSsh(NSX)
(res, moves) = with_ssh(s, lambda: N.clean_host(host(connected=False), "pw", "x", True))
ck("a disconnected host is skipped, not forced", res[0] == N.SKIP and s.visits == 0)

# --- stop on first failure --------------------------------------------------
results = []
hosts = [host("esx17"), host("esx18"), host("esx19")]
real = N.clean_host
N.clean_host = (lambda h, pw, e, a, tag="", vib_remove=False:
                (N.FAIL, "boom") if h.name == "esx18" else (N.CLEAN, "ok"))
try:
    N.run_group("vdi-cluster-01", hosts, "pw", "ensureObjectAccessibility", True, results)
finally:
    N.clean_host = real
states = {r[1]: r[2] for r in results}
ck("group: stops at the first failure and marks the rest not reached",
   states == {"esx17": N.CLEAN, "esx18": N.FAIL, "esx19": N.SKIP})

# --- vsan guard -------------------------------------------------------------
h = host()
ck("a vsan cluster asks for a decommission mode", N.on_vsan(h))
h.parent.configurationEx.vsanConfigInfo.enabled = False
ck("a cluster without vsan does not", not N.on_vsan(h))

print("\n%d failed" % len(fails))
sys.exit(1 if fails else 0)
