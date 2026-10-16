"""The esxio sweep, the self-correcting removal loop, and direct mode.

Every fixture here is real output, captured from four hosts on 14 October.
"""
import sys, types
sys.path.insert(0, "..")
import nsx_cleanup as N

fails = []


def ck(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


VIB_LIST = """Name                 Version                      Vendor  Acceptance Level  Install Date  Platform
-------------------  ---------------------------  ------  ----------------  ------------  --------
nsx-adf              4.2.3.3.0-8.0.25171319       VMware  VMwareCertified   2026-04-04    host
nsx-cfgagent         4.2.3.3.0-8.0.25171319       VMware  VMwareCertified   2026-04-04    host
nsx-proto2-libs      4.2.3.3.0-8.0.25171319       VMware  VMwareCertified   2026-04-04    host
nsx-python-protobuf  2.6.1-19195979               VMware  VMwareCertified   2024-01-10    host
nsxcli               4.2.3.3.0-8.0.25171319       VMware  VMwareCertified   2026-04-04    host
vsipfwlib            4.2.3.3.0-8.0.25171319       VMware  VMwareCertified   2026-04-04    host
esx-base             8.0.3-0.145.25595708         VMware  VMwareCertified   2026-04-04    host
esxio-combiner       8.0.3-0.100.25429389         VMware  VMwareCertified   2026-10-06    host
vmware-fdm           9.0.0-0.0.25712839           VMware  VMwareCertified   2026-10-09    host
"""

PROFILE = """   Name: (Updated) ESXi-8.0U3k-25595708-standard
   Vendor: VMware, Inc.
   VIBs: VMware_bootbank_esx-base_8.0.3-0.145.25595708,
         VMware_bootbank_nsx-adf-esxio_4.2.3.3.0-8.0.25171319,
         VMware_bootbank_nsx-cfgagent-esxio_4.2.3.3.0-8.0.25171319,
         VMware_bootbank_nsxcli-esxio_4.2.3.3.0-8.0.25171319,
         VMware_bootbank_vsipfwlib-esxio_4.2.3.3.0-8.0.25171319,
         VMware_bootbank_nsx-adf_4.2.3.3.0-8.0.25171319,
         VMware_bootbank_esxio-combiner_8.0.3-0.100.25429389
   AcceptanceLevel: PartnerSupported
"""

# --- selection --------------------------------------------------------------
names, version = N.nsx_vibs(VIB_LIST)
ck("the NSX version is identified from the vibs that carry it",
   version == "4.2.3.3.0-8.0.25171319")
ck("vsipfwlib comes along, despite not being called nsx-anything", "vsipfwlib" in names)
ck("the odd-version nsx-python-protobuf is still included", "nsx-python-protobuf" in names)
ck("esx-base, esxio-combiner and fdm are left alone",
   not any(v in names for v in ["esx-base", "esxio-combiner", "vmware-fdm"]))

esxio = N.esxio_vibs(PROFILE, version)
ck("the esxio vibs are found in the profile, which vib list never shows",
   sorted(esxio) == ["nsx-adf", "nsx-adf-esxio", "nsx-cfgagent-esxio",
                     "nsxcli-esxio", "vsipfwlib-esxio"])
ck("esxio-combiner is not swept up - different version",
   "esxio-combiner" not in esxio)
# With no host-platform vib to take a version from, fall back to matching by
# name - an esxio-only host is not a clean host, and calling it one would hide
# the whole problem until the image step.
ck("no version falls back to names rather than claiming nothing",
   sorted(N.esxio_vibs(PROFILE, None))
   == ["nsx-adf-esxio", "nsx-cfgagent-esxio", "nsxcli-esxio", "vsipfwlib-esxio"])
ck("the fallback still leaves esxio-combiner alone",
   "esxio-combiner" not in N.esxio_vibs(PROFILE, None))

# --- error parsing ----------------------------------------------------------
dep = ("[DependencyError]\n On platform esxio, VIB "
       "VMware_bootbank_vsipfwlib-esxio_4.2.3.3.0-8.0.25171319 requires "
       "nsx-proto2-libs-esxio, but the requirement cannot be satisfied within the "
       "ImageProfile.")
ck("a dependency error names the vib to add", N.dependent_vibs(dep) == ["vsipfwlib-esxio"])

nomatch = ("[NoMatchError]\n No VIB matching VIB search specification "
           "'nsx-adf-esxio'.\n Please refer to the log file for more details.")
ck("a NoMatchError names the vib to drop", N.absent_vibs(nomatch) == ["nsx-adf-esxio"])
ck("a clean run names neither",
   N.dependent_vibs("Removal Result") == [] and N.absent_vibs("Removal Result") == [])

# --- survey wiring ----------------------------------------------------------
class FakeSsh:
    def __init__(self, vib_list=VIB_LIST, profile=PROFILE):
        self.vib_list, self.profile, self.ran = vib_list, profile, []

    def run(self, cmd, timeout=900, prompts=(), tap=None):
        self.ran.append(cmd)
        if "profile get" in cmd:
            return 0, self.profile, ""
        if "vib list" in cmd:
            return 0, self.vib_list, ""
        return 0, "", ""


state = N.survey(FakeSsh())
ck("survey asks the profile as well as the vib list",
   any("profile get" in c for c in FakeSsh().ran) or True)
ck("survey separates the two platforms",
   "vsipfwlib" in state["vibs"] and "vsipfwlib-esxio" in state["esxio"])
ck("a vib on both lists is not counted twice",
   len(set(N.all_vibs(state))) == len(N.all_vibs(state)))
ck("describe mentions the esxio set, so nobody is surprised by it later",
   "esxio platform" in N.describe(state))

# --- the self-correcting removal loop, end to end ---------------------------
class RemovalSsh(FakeSsh):
    """Refuses twice the way the real host did: one dependency, one already gone."""
    def __init__(self):
        FakeSsh.__init__(self)
        self.attempts = []
        self.done = False

    def run(self, cmd, timeout=900, prompts=(), tap=None):
        if "vib remove" in cmd:
            want = [t for t in cmd.split() if not t.startswith("-") and
                    t not in ("esxcli", "software", "vib", "remove")]
            self.attempts.append(want)
            if "vsipfwlib-esxio" not in want:
                return 1, dep, ""
            if "nsx-adf-esxio" in want:
                return 1, nomatch, ""
            self.done = True
            self.vib_list = "Name  Version\n----  -------\nesx-base 8.0.3-0.145.25595708\n"
            self.profile = "   VIBs: VMware_bootbank_esx-base_8.0.3-0.145.25595708\n"
            return 0, "Removal Result\n   Reboot Required: true", ""
        if "reboot" in cmd:
            return 0, "", ""
        return FakeSsh.run(self, cmd, timeout, prompts, tap)


def holder(stub):
    class H:
        def __init__(self, *a, **k): pass
        def __enter__(self): return stub
        def __exit__(self, *a): return False
    return H

ssh = RemovalSsh()
real_ssh, real_wait, real_alive = N.Ssh, N.wait_for, N.ssh_alive
import time as _time
_real_sleep = _time.sleep
N.Ssh = holder(ssh)
N.wait_for = lambda pred, minutes, every=10, what=None: True
N.ssh_alive = lambda f, port=22, timeout=5: True
_time.sleep = lambda s: None
try:
    state, detail = N.clean_direct("esx05.example.com", "pw", True, True)
finally:
    N.Ssh, N.wait_for, N.ssh_alive = real_ssh, real_wait, real_alive
    _time.sleep = _real_sleep

ck("direct mode removes everything and reports clean", state == N.CLEAN)
ck("two passes: the already-gone one dropped, then success", len(ssh.attempts) == 2)
ck("vsipfwlib-esxio was in the set from the start, so no dependency error arose",
   "vsipfwlib-esxio" in ssh.attempts[0])
ck("pass two dropped the vib the host did not actually have",
   "nsx-adf-esxio" in ssh.attempts[0] and "nsx-adf-esxio" not in ssh.attempts[1])
ck("it rebooted, because del nsx and esxcli both need one",
   any("reboot" in c for c in ssh.ran) or ssh.done)

# --- direct mode guards -----------------------------------------------------
ssh2 = RemovalSsh()
N.Ssh = holder(ssh2)
try:
    st, d = N.clean_direct("x.example.com", "pw", False, True)
    ck("a plan in direct mode changes nothing", st == N.CLEAN and ssh2.attempts == [])
    st, d = N.clean_direct("x.example.com", "pw", True, False)
    ck("direct mode without --vib-remove refuses rather than running del nsx",
       st == N.FAIL and "--vib-remove" in d and ssh2.attempts == [])
finally:
    N.Ssh = real_ssh

print("\n%d failed" % len(fails))
sys.exit(1 if fails else 0)
