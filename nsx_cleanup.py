#!/usr/bin/env python3
"""Remove stale, unconfigured NSX VIBs from ESXi hosts, a cluster at a time.

Why this exists
---------------
A vLCM image cannot be enabled on a cluster whose hosts carry NSX but have no
Transport Node Profile attached, and the VCF import will not take a cluster that
is still baseline-managed. Hosts imaged from an older build can arrive with NSX
VIBs installed and never configured - no VTEPs, no N-VDS, nothing in net-dvs.
Those are the hosts this clears.

What it refuses to touch
------------------------
A host with real NSX configuration. VTEP vmkernels, NSX properties on the
distributed switch or an N-VDS mean the host was genuinely prepared, and
unwinding that is the careful path in KB 451192, not a loop. This script checks
first and stops rather than guessing.

It also never runs `esxcli software vib remove`. Broadcom calls that an
incorrect uninstall method: it leaves a stale N-VDS behind and hosts have lost
networking entirely as a result (KB 337062). The supported command is
`nsxcli -c "del nsx"`, which is what runs here.

Per host, in order: survey, enter maintenance mode, del nsx, reboot, wait for
the host to come back, verify no NSX VIBs remain, leave maintenance mode. One
host at a time within a cluster, because vSAN. Stops at the first failure.

    python3 nsx_cleanup.py --vcenter vc01.example.com --cluster cluster-a
    python3 nsx_cleanup.py --vcenter vc01.example.com --cluster cluster-a --commit

Nothing is written to disk but the run log, and no password is ever logged.
"""

import argparse
import datetime
import getpass
import os
import re
import ssl
import sys
import threading
import time

OK, CLEAN, DIRTY, SKIP, FAIL = "ok", "cleaned", "needs-care", "skipped", "failed"
NSX_SERVICES = ("TSM-SSH",)
VTEP_HINTS = ("vmk10", "vmk11", "vmk50")

# `del nsx` asks three times, each worded differently: before its prechecks, after
# them, and once more about irreversibility. Matched against the tail of what the
# host has said, so order does not matter. The bracket forms are the catch-all -
# note it uses both (Y/N) and [Y/N], which is how the third one got missed once.
DEL_NSX_PROMPTS = (
    ("are you ready to proceed", "Y"),
    ("would you like to proceed", "Y"),
    ("do you wish to continue", "Y"),
    ("respond with 'y'", "Y"),
    ("(y/n)", "Y"),
    ("[y/n]", "Y"),
)


# ---------------------------------------------------------------- bootstrapping
def candidate_pythons():
    """Interpreters that might carry both modules, nearest first.

    Compared as written, never through realpath: a venv's bin/python3 is a symlink
    to the system interpreter, so resolving it makes every venv look like the one
    we are already running under and the search finds nothing.
    """
    import glob
    home = os.path.expanduser("~")
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    if os.environ.get("VIRTUAL_ENV"):
        out.append(os.path.join(os.environ["VIRTUAL_ENV"], "bin", "python3"))
    for d in (here, home, os.path.join(home, "pyscripts"), os.path.join(home, ".deploy_vc")):
        for name in ("venv", ".venv"):
            out.append(os.path.join(d, name, "bin", "python3"))
    for pat in ("*/venv/bin/python3", "*/.venv/bin/python3"):
        out.extend(sorted(glob.glob(os.path.join(home, pat))))
    seen, uniq = set(), []
    for q in out:
        if q not in seen and q != sys.executable and os.path.isfile(q):
            seen.add(q)
            uniq.append(q)
    return uniq


def missing_deps(python=None):
    if python is None:
        out = []
        for mod, pkg in (("pyVmomi", "pyvmomi"), ("paramiko", "paramiko")):
            try:
                __import__(mod)
            except ImportError:
                out.append(pkg)
        return out
    import subprocess
    probe = subprocess.run([python, "-c", "import pyVmomi, paramiko"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return [] if probe.returncode == 0 else ["pyvmomi", "paramiko"]


def ensure_deps():
    """Re-exec under an interpreter that has pyVmomi and paramiko, if this one has not.

    Same trick as deploy_vc.py: the system python3 rarely has them, the venv that
    does is easy to forget, and on a PEP 668 distro installing into the system
    interpreter is refused outright.
    """
    if not missing_deps():
        return
    if not os.environ.get("NSX_CLEANUP_REEXEC"):
        for py in candidate_pythons():
            if not missing_deps(py):
                print("%s is not in %s - re-running under %s"
                      % (" and ".join(missing_deps()), sys.executable, py))
                os.environ["NSX_CLEANUP_REEXEC"] = "1"
                os.execv(py, [py, os.path.abspath(__file__)] + sys.argv[1:])
    # Nothing here has them. Name a venv that could, rather than a system pip that
    # a PEP 668 distro will refuse.
    want = " ".join(missing_deps())
    venv = next((p for p in candidate_pythons()), None)
    fix = ("  %s -m pip install %s" % (venv, want) if venv else
           "  python3 -m venv ~/pyscripts/venv\n"
           "  ~/pyscripts/venv/bin/pip install %s" % want)
    sys.exit("%s is not installed for %s, and no other Python here has %s.\n"
             "One command fixes it:\n%s"
             % (want, sys.executable, "them" if len(missing_deps()) > 1 else "it", fix))


ensure_deps()
import paramiko                                            # noqa: E402
from pyVim.connect import SmartConnect, Disconnect         # noqa: E402
from pyVmomi import vim                                    # noqa: E402


# ---------------------------------------------------------------------- output
_print_lock = threading.Lock()
LOG = {"fh": None}


def say(line=""):
    with _print_lock:
        print(line, flush=True)
        if LOG["fh"]:
            LOG["fh"].write(line + "\n")
            LOG["fh"].flush()


def die(msg):
    say("ERROR: %s" % msg)
    sys.exit(2)


# ------------------------------------------------------------------- vCenter
def connect(host, user, password):
    si = SmartConnect(host=host, user=user, pwd=password,
                      sslContext=ssl._create_unverified_context(),
                      disableSslCertValidation=True)
    return si, si.RetrieveContent()


def find_all(content, kind):
    view = content.viewManager.CreateContainerView(content.rootFolder, [kind], True)
    try:
        return list(view.view)
    finally:
        view.Destroy()


def hosts_in(content, clusters, names):
    """The hosts to work on, grouped by cluster so each group stays serial."""
    groups = []
    if names:
        wanted = {n.lower() for n in names}
        picked = [h for h in find_all(content, vim.HostSystem) if h.name.lower() in wanted]
        missing = wanted - {h.name.lower() for h in picked}
        if missing:
            die("no such host in this vCenter: %s" % ", ".join(sorted(missing)))
        groups.append(("(hosts)", picked))
    for name in clusters or []:
        cl = next((c for c in find_all(content, vim.ClusterComputeResource)
                   if c.name.lower() == name.lower()), None)
        if cl is None:
            die("no such cluster in this vCenter: %s" % name)
        groups.append((cl.name, list(cl.host or [])))
    if not groups:
        die("nothing selected - pass --cluster or --host")
    return groups


def mgmt_ip(host):
    """The address to SSH to: the management vmkernel, not DNS, which may lie."""
    for vnic in (host.config.network.vnic or []):
        if vnic.device == "vmk0" and getattr(vnic.spec.ip, "ipAddress", None):
            return vnic.spec.ip.ipAddress
    return host.name


def wait_for(predicate, minutes, every=10, what=None):
    """Poll until true. Says where it is every half minute, because a step that can
    run for twenty minutes in silence is indistinguishable from a hung one."""
    start = time.time()
    deadline = start + minutes * 60
    spoke = 0
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass                       # vCenter is allowed to be grumpy mid-reboot
        waited = int(time.time() - start)
        if what and waited - spoke >= 30:
            spoke = waited
            say("%s - %dm%02ds elapsed, %d minute limit"
                % (what, waited // 60, waited % 60, minutes))
        time.sleep(every)
    return False


def task_wait(task, minutes=60):
    deadline = time.time() + minutes * 60
    while task.info.state in ("queued", "running"):
        if time.time() > deadline:
            raise RuntimeError("task still %s after %d minutes" % (task.info.state, minutes))
        time.sleep(5)
    if task.info.state != "success":
        err = task.info.error
        raise RuntimeError(getattr(err, "msg", None) or str(err))
    return task.info.result


# ----------------------------------------------------------------------- SSH
class Ssh:
    """A shell on the host, with the SSH service switched on only if it was off.

    `host` is the vCenter managed object when there is one, so the SSH service can
    be turned on and back off. In direct mode it is None and SSH has to be on
    already, because there is no vCenter to ask.
    """

    def __init__(self, host, ip, password, user="root"):
        self.host, self.ip, self.password, self.user = host, ip, password, user
        self.client = None
        self.started = False

    def __enter__(self):
        self.started = self.start_service()
        last = None
        for _ in range(6):
            try:
                c = paramiko.SSHClient()
                c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                c.connect(self.ip, username=self.user, password=self.password, timeout=20,
                          look_for_keys=False, allow_agent=False, banner_timeout=30)
                self.client = c
                return self
            except paramiko.AuthenticationException:
                raise                               # a wrong password will not fix itself
            except Exception as e:
                last = e
                time.sleep(5)
        raise RuntimeError("cannot ssh to %s (%s): %s" % (self.host, self.ip, last))

    def __exit__(self, *exc):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        if self.started:
            self.stop_service()                     # leave it as we found it
        return False

    def service(self):
        if self.host is None:                 # direct mode: no vCenter to ask
            return None
        for svc in (self.host.configManager.serviceSystem.serviceInfo.service or []):
            if svc.key in NSX_SERVICES:
                return svc
        return None

    def start_service(self):
        svc = self.service()
        if svc is None or svc.running:
            return False
        self.host.configManager.serviceSystem.StartService(id=svc.key)
        time.sleep(3)
        return True

    def stop_service(self):
        try:
            svc = self.service()
            if svc is not None and svc.running:
                self.host.configManager.serviceSystem.StopService(id=svc.key)
        except Exception:
            pass

    def run(self, cmd, timeout=900, prompts=(), tap=None):
        """Run a command, answering any prompts it puts up.

        nsxcli expects to be talking to a person. `del nsx` asks for confirmation
        twice - once before its prechecks and once after - and without a tty and an
        answer it sits there forever while the run looks hung. prompts is a list of
        (text to look for, what to send); every match is answered, however many
        times it appears. tap= streams each line as it arrives, because a command
        that takes five minutes should not be silent for five minutes.
        """
        chan = self.client.get_transport().open_session()
        chan.settimeout(timeout)
        if prompts:
            chan.get_pty()
        chan.exec_command(cmd)

        buf, recent, out = b"", "", []
        deadline = time.time() + timeout
        while True:
            if chan.recv_ready():
                data = chan.recv(65536)
                if not data:
                    break
                buf += data
                text = data.decode(errors="replace")
                recent = (recent + text)[-4000:]
                for needle, reply in prompts:
                    if needle.lower() in recent.lower():
                        try:
                            chan.sendall((reply + "\n").encode())
                        except Exception:
                            pass
                        recent = ""          # so one prompt is answered once
                        break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    one = line.decode(errors="replace").rstrip("\r")
                    out.append(one)
                    if tap and one.strip():
                        tap(one.strip())
            elif chan.exit_status_ready():
                break
            elif time.time() > deadline:
                chan.close()
                raise RuntimeError("%s: no answer after %d seconds. The last thing it "
                                   "said was: %s"
                                   % (cmd, timeout, " / ".join(out[-3:]) or "nothing"))
            else:
                time.sleep(0.2)
        if buf:
            out.append(buf.decode(errors="replace").rstrip("\r"))
        rc = chan.recv_exit_status()
        chan.close()
        return rc, "\n".join(out), ""


# ------------------------------------------------------------------- the work
def nsx_vibs(listing):
    """The NSX VIB set, chosen by version rather than by name.

    Not every NSX VIB is called nsx-something: vsipfwlib is one, and it depends on
    nsx-proto2-libs, so a name-prefix list removes its dependency and the whole
    operation fails on a DependencyError. What they do share is a version string.
    Take the version most of the nsx-* VIBs carry and claim everything at it.
    """
    rows = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] in ("Name", "----") or set(parts[0]) == {"-"}:
            continue
        rows.append((parts[0], parts[1]))
    named = [(n, v) for n, v in rows if n.lower().startswith("nsx")]
    if not named:
        return [], None
    counts = {}
    for _n, v in named:
        counts[v] = counts.get(v, 0) + 1
    main = max(counts, key=lambda v: counts[v])
    out = [n for n, _v in named]
    # anything else installed at the same version came from the same NSX install
    out += [n for n, v in rows if v == main and n not in out]
    return out, main


def esxio_vibs(profile, version):
    """NSX VIBs on the esxio platform.

    These do not appear in `esxcli software vib list` at all - only in the image
    profile - and vLCM counts them, so a cluster stays blocked on an image while
    they are installed. They cost most of an afternoon at the second site being discovered one
    error at a time. The profile names them as vendor_bootbank_name_version, so
    the NSX version string is what picks them out, vsipfwlib-esxio included.

    The profile is the image definition rather than live state, so it can still
    name a VIB that has already been removed. The removal loop drops those when
    esxcli says NoMatchError.
    """
    out = []
    if version:
        for m in re.finditer(r"[A-Za-z0-9]+_[a-z]+_(\S+?)_" + re.escape(version), profile):
            if m.group(1) not in out:
                out.append(m.group(1))
        return out
    # No host-platform NSX vib to take a version from. That can mean the host is
    # clean, or that only the esxio side is left - and the two are not the same
    # thing, so fall back to matching by name rather than calling it clean.
    # The profile writes vendor_bootbank_name_version, so the name is bounded by
    # underscores on both sides: no \b to lean on, and the class must exclude _
    # or it runs straight through into the version.
    for m in re.finditer(r"(nsx[A-Za-z0-9.\-]*?esxio|vsipfwlib-esxio)(?=_)", profile):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def absent_vibs(text):
    """VIB names out of a NoMatchError - ones the profile lists but the host lacks."""
    return [m.group(1) for m in
            re.finditer(r"No VIB matching VIB search specification '([^']+)'", text)]


def dependent_vibs(text):
    """VIB names out of a DependencyError.

    "VIB VMware_bootbank_vsipfwlib_4.2.3.3.0-8.0.25171319 requires nsx-proto2-libs"
    means vsipfwlib has to come off too. The name is wrapped in a vendor prefix and
    a version suffix, so lift it out of the middle.
    """
    out = []
    for m in re.finditer(r"VIB\s+(\S+?)\s+requires", text):
        token = m.group(1)
        if "bootbank_" in token:
            token = token.split("bootbank_", 1)[1]
        token = re.sub(r"_\d[\w.]*-[\w.]*$", "", token)
        if token and token not in out:
            out.append(token)
    return out


def survey(ssh):
    """What NSX is on this host: installed VIBs, and whether any of it is configured."""
    _rc, vibs, _e = ssh.run("esxcli software vib list", timeout=120)
    names, version = nsx_vibs(vibs)
    _rc, profile, _e = ssh.run("esxcli software profile get || true", timeout=120)
    esxio = [v for v in esxio_vibs(profile, version) if v not in names]
    _rc, vmks, _e = ssh.run("esxcfg-vmknic -l || true", timeout=120)
    vteps = sorted({v for v in VTEP_HINTS if v in vmks})
    _rc, dvs, _e = ssh.run("net-dvs -l 2>/dev/null | grep -i 'com.vmware.nsx' || true",
                           timeout=120)
    props = [l.strip() for l in dvs.splitlines() if l.strip()]
    _rc, opaque, _e = ssh.run("esxcli network vswitch dvs vmware list 2>/dev/null "
                              "| grep -i -A1 'nsx' || true", timeout=120)
    return {"vibs": names, "esxio": esxio, "version": version, "vteps": vteps,
            "props": props,
            "opaque": [l.strip() for l in opaque.splitlines() if l.strip()]}


def all_vibs(state):
    return list(state.get("vibs") or []) + list(state.get("esxio") or [])


def describe(state):
    bits = ["%d NSX vibs" % len(state["vibs"])]
    if state.get("esxio"):
        bits.append("%d more on the esxio platform" % len(state["esxio"]))
    if state["vteps"]:
        bits.append("VTEPs %s" % ",".join(state["vteps"]))
    if state["props"]:
        bits.append("%d nsx properties on the vds" % len(state["props"]))
    return ", ".join(bits)


def configured(state):
    return bool(state["vteps"] or state["props"] or state["opaque"])


def on_vsan(host):
    cl = getattr(host, "parent", None)
    cfg = getattr(getattr(cl, "configurationEx", None), "vsanConfigInfo", None)
    return bool(cfg is not None and cfg.enabled)


def enter_maintenance(host, evacuate, minutes=60):
    if host.runtime.inMaintenanceMode:
        return False
    # A vSAN decommission mode on a cluster without vSAN is rejected, so only ask
    # for one when there is vSAN to decommission from.
    spec = None
    if on_vsan(host):
        spec = vim.host.MaintenanceSpec()
        spec.vsanMode = vim.vsan.host.DecommissionMode(objectAction=evacuate)
    task_wait(host.EnterMaintenanceMode_Task(timeout=0, evacuatePoweredOffVms=False,
                                             maintenanceSpec=spec), minutes)
    return True


def exit_maintenance(host, minutes=30):
    if not host.runtime.inMaintenanceMode:
        return
    task_wait(host.ExitMaintenanceMode_Task(timeout=0), minutes)


def reboot_and_wait(host, minutes=25, tag=""):
    """Reboot, then wait for the host to actually go away and come back.

    Waiting only for 'connected' is not enough: vCenter can still be reporting the
    pre-reboot state when we ask, and we would sail straight on to the verify.
    """
    pre = "      " if not tag else "   [%s] " % tag
    task_wait(host.RebootHost_Task(force=False), minutes=10)
    went = wait_for(lambda: host.runtime.connectionState != "connected", minutes=6, every=5,
                    what=pre + "waiting for the host to drop out of vCenter")
    say(pre + ("host is down, waiting for it to boot (firmware POST is most of this)"
               if went else
               "the host never dropped out of vCenter - continuing, it may have been quick"))
    if not wait_for(lambda: host.runtime.connectionState == "connected"
                    and host.runtime.powerState == "poweredOn", minutes=minutes,
                    what=pre + "waiting for the host to come back"):
        raise RuntimeError("did not come back within %d minutes" % minutes)
    say(pre + "back in vCenter, letting the agents start")
    time.sleep(20)                                   # let the agents finish starting


def clean_host(host, esx_pw, evacuate, apply_, tag="", vib_remove=False):
    """One host, start to finish. Returns (state, detail)."""
    name = host.name
    pre = "      " if not tag else "   [%s] " % tag

    def step(line):
        say(pre + line)

    if host.runtime.connectionState != "connected":
        return SKIP, "%s is %s" % (name, host.runtime.connectionState)
    ip = mgmt_ip(host)

    with Ssh(host, ip, esx_pw) as ssh:
        before = survey(ssh)
        # No vibs but still shut out of service means a cleanup that was interrupted
        # between del nsx and its reboot - by a Ctrl-C, or by someone running the
        # command by hand. del nsx does not finish without that reboot, so finish it
        # rather than reporting a host done and leaving it in maintenance mode.
        unfinished = bool(not all_vibs(before) and apply_ and host.runtime.inMaintenanceMode)
        if not all_vibs(before) and not unfinished:
            return OK, "%s has no NSX vibs" % name
        if all_vibs(before) and configured(before):
            return DIRTY, ("%s has NSX that is actually configured (%s). This script only "
                           "clears inert vibs - unwinding a prepared host is the careful "
                           "path in KB 451192. Left alone." % (name, describe(before)))
        if not apply_:
            return CLEAN, ("%s would be cleaned: %s, no VTEPs and nothing in net-dvs, so the "
                           "vibs are inert" % (name, describe(before)))

        if unfinished:
            step("vibs are already gone but the host is still in maintenance mode - "
                 "an interrupted cleanup. Rebooting to finish it")
            entered = False
        else:
            step("entering maintenance mode (%s)"
                 % (evacuate if on_vsan(host) else "no vsan"))
            entered = enter_maintenance(host, evacuate)
            step("in maintenance mode%s" % ("" if entered else " (it already was)"))
            step('nsxcli -c "del nsx" - removing %d vibs, usually two to five minutes'
                 % len(before["vibs"]))
            # Quoted: unquoted, the remote shell splits it and nsxcli sees "-c del"
            # with a stray "nsx" and waits for the rest of a command that never
            # comes. The tty and the Y answers are for its two confirmations.
            rc, out, err = ssh.run('nsxcli -c "del nsx"', timeout=900,
                                   prompts=DEL_NSX_PROMPTS,
                                   tap=lambda line: step("  | " + line[:160]))
            tail = (out + err).strip().splitlines()
            if rc != 0:
                return FAIL, ('%s: nsxcli -c "del nsx" exited %d: %s. Stop here - KB 451192 '
                              "has a remedy per failure, and improvising is how hosts lose "
                              "networking" % (name, rc, " / ".join(tail[-3:]) or "no output"))
            step("del nsx: %s" % (tail[-1] if tail else "done"))

            # del nsx removes a transport node. On a host that has none - "TN details
            # are not present on host", as its own precheck puts it - there is nothing
            # for it to do and the vibs stay. Check before spending a reboot on it.
            left = all_vibs(survey(ssh))
            if left and vib_remove:
                step("del nsx left %d vibs behind, so removing them directly. Safe here "
                     "and only here: this host has no N-VDS, no VTEP and nothing in "
                     "net-dvs, which is the stale state KB 337062 warns about"
                     % len(left))
                # A DependencyError names the VIB that still needs one of these.
                # Add it and go again rather than making the operator find it.
                want, tail = list(left), []
                for _attempt in range(8):
                    args = " ".join("-n %s" % v for v in want)
                    rc, out, err = ssh.run("esxcli software vib remove %s" % args,
                                           timeout=1800,
                                           tap=lambda line: step("  | " + line[:160]))
                    tail = (out + err).strip().splitlines()
                    if rc == 0:
                        break
                    # Two recoverable refusals, and the error names the vib in both:
                    # one that still depends on the set, or one the image profile
                    # lists but the host no longer has.
                    more = [v for v in dependent_vibs(out + err) if v not in want]
                    gone = [v for v in absent_vibs(out + err) if v in want]
                    if more:
                        step("%s depends on these too, adding %s and retrying"
                             % (", ".join(more), "it" if len(more) == 1 else "them"))
                        want += more
                    elif gone:
                        step("%s %s already gone, dropping %s and retrying"
                             % (", ".join(gone), "is" if len(gone) == 1 else "are",
                                "it" if len(gone) == 1 else "them"))
                        want = [v for v in want if v not in gone]
                        if not want:
                            break
                    else:
                        return FAIL, ("%s: esxcli software vib remove exited %d: %s"
                                      % (name, rc, " / ".join(tail[-3:]) or "no output"))
                else:
                    return FAIL, ("%s: esxcli software vib remove still failing after eight "
                                  "tries: %s" % (name, " / ".join(tail[-3:])))
                step("%d vibs removed, reboot pending" % len(want))
            elif left:
                return FAIL, ("%s: del nsx ran cleanly but left all %d vibs in place - it "
                              "removes a transport node and this host has none. Re-run with "
                              "--vib-remove to take them off directly, which is safe on a "
                              "host with no NSX configuration. Left in maintenance mode"
                              % (name, len(left)))

    step("rebooting")
    reboot_and_wait(host, tag=tag)

    with Ssh(host, ip, esx_pw) as ssh:
        after = survey(ssh)
    if all_vibs(after):
        left = all_vibs(after)
        return FAIL, ("%s still has %d NSX vibs after the reboot (%s). Left in maintenance "
                      "mode on purpose" % (name, len(left), ", ".join(left[:4])))
    # Bring it back if we took it out, and also if we are finishing our own
    # interrupted work - that host is only in maintenance mode because of this.
    if entered or unfinished:
        exit_maintenance(host)
    if unfinished:
        return CLEAN, ("%s: vibs were already removed, rebooted to finish the job and "
                       "returned to service" % name)
    return CLEAN, "%s: %d NSX vibs removed, host back up%s" % (
        name, len(all_vibs(before)),
        "" if entered else " (it was already in maintenance mode)")


def clean_direct(fqdn, esx_pw, apply_, vib_remove, tag=""):
    """One host with no vCenter in the picture.

    For hosts that have not joined anything yet, which is when stale NSX is
    cheapest to clear: no cluster, no maintenance mode, no vSAN to decommission
    from, and nothing running on them. Everything goes over SSH, including the
    reboot, and the host is proved clean before the run calls it done.
    """
    pre = "      " if not tag else "   [%s] " % tag

    def step(line):
        say(pre + line)

    with Ssh(None, fqdn, esx_pw) as ssh:
        before = survey(ssh)
        if not all_vibs(before):
            return OK, "%s has no NSX vibs" % fqdn
        if configured(before):
            return DIRTY, ("%s has NSX that is actually configured (%s). This script only "
                           "clears inert vibs - unwinding a prepared host is the careful "
                           "path in KB 451192. Left alone." % (fqdn, describe(before)))
        if not apply_:
            return CLEAN, ("%s would be cleaned: %s, no VTEPs and nothing in net-dvs, so "
                           "the vibs are inert" % (fqdn, describe(before)))
        if not vib_remove:
            return FAIL, ("%s: direct mode removes the vibs with esxcli, which needs "
                          "--vib-remove. del nsx is not used here - these hosts have no "
                          "transport node for it to remove" % fqdn)

        want, tail = all_vibs(before), []
        for _attempt in range(8):
            args = " ".join("-n %s" % v for v in want)
            rc, out, err = ssh.run("esxcli software vib remove %s" % args, timeout=1800,
                                   tap=lambda line: step("  | " + line[:160]))
            tail = (out + err).strip().splitlines()
            if rc == 0:
                break
            more = [v for v in dependent_vibs(out + err) if v not in want]
            gone = [v for v in absent_vibs(out + err) if v in want]
            if more:
                step("%s depends on these too, adding and retrying" % ", ".join(more))
                want += more
            elif gone:
                step("%s already gone, dropping and retrying" % ", ".join(gone))
                want = [v for v in want if v not in gone]
                if not want:
                    break
            else:
                return FAIL, ("%s: esxcli software vib remove exited %d: %s"
                              % (fqdn, rc, " / ".join(tail[-3:]) or "no output"))
        else:
            return FAIL, ("%s: esxcli software vib remove still failing after eight tries: "
                          "%s" % (fqdn, " / ".join(tail[-3:])))
        step("%d vibs removed, rebooting" % len(want))
        try:
            ssh.run("reboot", timeout=20)
        except Exception:
            pass                                  # the connection dies with the host

    if not wait_for(lambda: not ssh_alive(fqdn), minutes=6, every=5,
                    what=pre + "waiting for the host to go down"):
        say(pre + "it never stopped answering - carrying on, the reboot may have been quick")
    if not wait_for(lambda: ssh_alive(fqdn), minutes=25, every=15,
                    what=pre + "waiting for the host to come back"):
        return FAIL, "%s did not come back within 25 minutes" % fqdn
    time.sleep(20)

    with Ssh(None, fqdn, esx_pw) as ssh:
        after = survey(ssh)
    left = all_vibs(after)
    if left:
        return FAIL, ("%s still has %d NSX vibs after the reboot (%s)"
                      % (fqdn, len(left), ", ".join(left[:4])))
    return CLEAN, "%s: %d NSX vibs removed, host back up" % (fqdn, len(want))


def ssh_alive(fqdn, port=22, timeout=5):
    import socket
    try:
        s = socket.create_connection((fqdn, port), timeout)
        s.close()
        return True
    except Exception:
        return False


def run_direct(hosts, esx_pw, apply_, vib_remove, results, at_once=False):
    say("")
    say("==== %d host(s), direct - no vCenter" % len(hosts))
    ordered = sorted(hosts)

    def one(fqdn):
        try:
            state, detail = clean_direct(fqdn, esx_pw, apply_, vib_remove,
                                         tag=fqdn.split(".")[0] if at_once else "")
        except Exception as e:
            state, detail = FAIL, "%s: %s" % (fqdn, e)
        say("   %-10s %s" % (state, detail))
        results.append(("(direct)", fqdn, state, detail))

    if at_once and apply_ and len(ordered) > 1:
        say("   all at once - these hosts are not in a cluster, so there is nothing to keep up")
        threads = [threading.Thread(target=one, args=(h,)) for h in ordered]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return
    for fqdn in ordered:
        say("  -- %s" % fqdn)
        one(fqdn)


def running_vms(hosts):
    """Hosts with a powered-on VM. Taking a cluster down all at once is only safe
    when there is nothing on it, so this is checked rather than asserted."""
    out = []
    for h in hosts:
        for vm in (getattr(h, "vm", None) or []):
            if getattr(getattr(vm, "runtime", None), "powerState", "") == "poweredOn":
                out.append(h.name)
                break
    return out


def run_group(cluster, hosts, esx_pw, evacuate, apply_, results,
              at_once=False, connect_fn=None, vib_remove=False):
    say("")
    say("==== %s (%d hosts)" % (cluster, len(hosts)))
    ordered = sorted(hosts, key=lambda h: h.name)

    if at_once and apply_ and connect_fn is not None and len(ordered) > 1:
        say("   every host at once - the cluster has no powered-on VMs, so there is "
            "nothing to keep running")

        def one(name):
            # A thread cannot share this pyVmomi session, so it opens its own.
            try:
                content = connect_fn()
                host = next((x for x in find_all(content, vim.HostSystem)
                             if x.name == name), None)
                if host is None:
                    state, detail = FAIL, "%s is no longer in the inventory" % name
                else:
                    state, detail = clean_host(host, esx_pw, evacuate, apply_,
                                               tag=name.split(".")[0],
                                               vib_remove=vib_remove)
            except Exception as e:
                state, detail = FAIL, "%s: %s" % (name, e)
            say("   %-10s %s" % (state, detail))
            results.append((cluster, name, state, detail))

        threads = [threading.Thread(target=one, args=(h.name,)) for h in ordered]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return

    for host in ordered:
        say("  -- %s" % host.name)
        try:
            state, detail = clean_host(host, esx_pw, evacuate, apply_,
                                       vib_remove=vib_remove)
        except Exception as e:
            state, detail = FAIL, "%s: %s" % (host.name, e)
        say("     %-10s %s" % (state, detail))
        results.append((cluster, host.name, state, detail))
        if state in (FAIL, DIRTY) and apply_:
            say("     stopping on %s - the hosts after it were left alone" % host.name)
            for other in ordered:
                if other.name > host.name:
                    results.append((cluster, other.name, SKIP, "not reached"))
            return


def report(results, committed):
    say("")
    say("==== result")
    say("%-14s %-28s %-11s %s" % ("CLUSTER", "HOST", "STATE", "DETAIL"))
    for cluster, host, state, detail in results:
        say("%-14s %-28s %-11s %s" % (cluster[:14], host[:28], state, detail))

    bad = [r for r in results if r[2] in (FAIL, DIRTY)]
    cleaned = [r for r in results if r[2] == CLEAN]
    say("")
    if not committed:
        say("NEXT: re-run with --commit to do it.")
    elif bad:
        say("NEXT: %d host(s) need attention before the rest can run." % len(bad))
    elif cleaned:
        say("NEXT: convert the cluster to a vLCM image, then re-run the import prechecks:")
        for cluster in sorted({r[0] for r in cleaned}):
            say("  python3 deploy_vc.py build --config env_vc8.yaml --site <site> "
                "--only image --commit      # %s" % cluster)


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(
        description="Remove stale, unconfigured NSX vibs from ESXi hosts.")
    ap.add_argument("--vcenter", help="vCenter FQDN. Omit with --direct")
    ap.add_argument("--direct", action="store_true",
                    help="treat --host values as ESXi hosts and go straight to them over "
                         "SSH, with no vCenter. For hosts that have not joined anything "
                         "yet, which is the cheapest moment to clear stale NSX. Needs SSH "
                         "already enabled on them, and --vib-remove")
    ap.add_argument("--vc-user", default="administrator@vsphere.local")
    ap.add_argument("--cluster", action="append", default=[],
                    help="cluster to clean; repeatable")
    ap.add_argument("--host", action="append", default=[],
                    help="single host by name; repeatable")
    ap.add_argument("--commit", action="store_true",
                    help="actually do it. Without this, nothing is changed")
    ap.add_argument("--evacuate", default="ensureObjectAccessibility",
                    choices=["ensureObjectAccessibility", "evacuateAllData", "noAction"],
                    help="vSAN decommission mode for maintenance mode")
    ap.add_argument("--parallel", action="store_true",
                    help="clusters at the same time, still one host at a time within each")
    ap.add_argument("--all-at-once", action="store_true",
                    help="every host in a cluster together, not one at a time. Refused if "
                         "any host has a powered-on VM, because the whole cluster goes down")
    ap.add_argument("--vib-remove", action="store_true",
                    help="when del nsx leaves the vibs in place - which it does on a host "
                         "that has no transport node - remove them directly with esxcli. "
                         "Only ever reached on a host this script has already confirmed has "
                         "no N-VDS, no VTEP and nothing in net-dvs")
    ap.add_argument("--force", action="store_true",
                    help="with --all-at-once, proceed even with powered-on VMs. They will "
                         "be interrupted")
    ap.add_argument("--logdir", default="/tmp/nsx_cleanup")
    args = ap.parse_args()

    if args.direct:
        if not args.host:
            ap.error("--direct needs at least one --host")
        if args.cluster:
            ap.error("--direct cannot take --cluster: there is no vCenter to read it from")
    elif not args.vcenter:
        ap.error("--vcenter is required unless you pass --direct")

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(args.logdir, stamp)
    os.makedirs(outdir, exist_ok=True)
    LOG["fh"] = open(os.path.join(outdir, "console.txt"), "w")

    say("nsx_cleanup.py   %s   %s" % (args.vcenter or "direct to %d host(s)" % len(args.host),
                                      "COMMIT - changes will be made" if args.commit
                                      else "PLAN - nothing will be changed"))
    say("log: %s" % outdir)
    say("")

    vc_pw = ("" if args.direct else
             getpass.getpass("password for %s (%s): " % (args.vcenter, args.vc_user)))
    esx_pw = getpass.getpass("root password for the ESXi hosts: ")

    if args.direct:
        results = []
        run_direct(args.host, esx_pw, args.commit, args.vib_remove, results,
                   at_once=args.all_at_once)
        report(results, args.commit)
        sys.exit(1 if [r for r in results if r[2] in (FAIL, DIRTY)] else 0)

    try:
        si, content = connect(args.vcenter, args.vc_user, vc_pw)
    except Exception as e:
        die("cannot connect to %s: %s" % (args.vcenter, e))
    groups = hosts_in(content, args.cluster, args.host)

    # Taking every host down together is only safe on a cluster with nothing running.
    # Check it rather than take anyone's word for it, this one included.
    if args.all_at_once and args.commit and not args.force:
        busy = running_vms([h for _cl, hs in groups for h in hs])
        if busy:
            die("--all-at-once takes the whole cluster down at the same time, and %s "
                "%s a powered-on VM. Move or power them off, or pass --force if they "
                "are genuinely expendable."
                % (", ".join(sorted(set(busy))), "has" if len(set(busy)) == 1 else "have"))
        # vSAN will admit every host but the last, then refuse with "General vSAN
        # error" - keeping objects accessible is exactly what it was asked to do.
        # Say so before the run rather than on the final host.
        vsan = [cl for cl, hs in groups if len(hs) > 1 and any(on_vsan(h) for h in hs)]
        if vsan and args.evacuate != "noAction":
            die("--all-at-once cannot work on %s with --evacuate %s: vSAN keeps objects "
                "accessible, so it will take every host but the last and refuse that one. "
                "On a cluster with no VMs there is nothing to keep accessible - add "
                "--evacuate noAction. On a cluster holding real data, drop --all-at-once "
                "instead and let the hosts go one at a time."
                % (", ".join(vsan), args.evacuate))

    def new_session():
        _si, c = connect(args.vcenter, args.vc_user, vc_pw)
        return c

    results = []
    if args.parallel and len(groups) > 1 and args.commit:
        # Each worker needs its own connection; one pyVmomi session is not safe to
        # drive from several threads.
        def worker(cluster, names):
            try:
                c = new_session()
            except Exception as e:
                results.append((cluster, "-", FAIL, "second vCenter session failed: %s" % e))
                return
            mine = [h for h in find_all(c, vim.HostSystem) if h.name in names]
            run_group(cluster, mine, esx_pw, args.evacuate, args.commit, results,
                      at_once=args.all_at_once, connect_fn=new_session,
                      vib_remove=args.vib_remove)

        threads = [threading.Thread(target=worker, args=(cl, {h.name for h in hs}))
                   for cl, hs in groups]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        for cluster, hs in groups:
            run_group(cluster, hs, esx_pw, args.evacuate, args.commit, results,
                      at_once=args.all_at_once, connect_fn=new_session,
                      vib_remove=args.vib_remove)

    report(results, args.commit)
    try:
        Disconnect(si)
    except Exception:
        pass
    sys.exit(1 if [r for r in results if r[2] in (FAIL, DIRTY)] else 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\ninterrupted - any host already in maintenance mode was left there on purpose")
        sys.exit(130)
