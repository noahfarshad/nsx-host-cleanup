# Stale NSX off the hosts

Remove stale, unconfigured NSX VIBs from ESXi hosts, a cluster at a time: survey, maintenance mode, `del nsx`, reboot, verify, back in. Both VIB platforms, host and esxio. Plan by default. Nothing is changed without `--commit`.

License: GPL-3.0. Built and proved out for [essential.coach](https://essential.coach).
Full write-up: [The NSX That Nobody Installed: Clearing Stale VIBs Before a VCF Import](https://essential.coach/the-nsx-that-nobody-installed/)

A vLCM image cannot be enabled on a cluster whose hosts carry NSX but have no transport node profile attached, and the VCF import will not take a cluster that is still baseline-managed. Hosts imaged from an older build can arrive with NSX VIBs installed and never configured: no VTEPs, no N-VDS, nothing in `net-dvs`. Those are the hosts this clears.

It refuses a host with real NSX configuration. VTEP vmkernels, NSX properties on the distributed switch or an N-VDS mean the host was genuinely prepared, and unwinding that is the careful path in [KB 451192](https://knowledge.broadcom.com/external/article/451192), not a loop. It never runs `esxcli software vib remove` on such a host: Broadcom calls that an incorrect uninstall method, and hosts have lost networking entirely as a result ([KB 337062](https://knowledge.broadcom.com/external/article/337062)). The supported `nsxcli -c "del nsx"` is what runs here, one host at a time within a cluster, because vSAN. The run stops at the first failure.

## Run

```bash
python3 nsx_cleanup.py --vcenter vc01.example.com --cluster cluster-a            # survey: changes nothing
python3 nsx_cleanup.py --vcenter vc01.example.com --cluster cluster-a --commit   # one host at a time
python3 nsx_cleanup.py --direct --host esx05.example.com --vib-remove            # a host that has joined nothing yet; add --commit
```

`--host` names a single host (repeatable). `--direct` goes straight to the hosts over SSH with no vCenter, for hosts that have not joined anything yet, which is the cheapest moment to clear stale NSX; it needs SSH already enabled and `--vib-remove`. `--vib-remove` is for when `del nsx` leaves the VIBs in place, which it does on a host that has no transport node; it is only ever reached on a host the script has already confirmed has no N-VDS, no VTEP and nothing in `net-dvs`. `--evacuate` picks the vSAN decommission mode. `--parallel` runs clusters at the same time, still one host at a time within each. `--all-at-once` takes every host in a cluster together and is refused if any host has a powered-on VM unless `--force` says they may be interrupted.

VIBs are chosen by version, not by name: not every NSX VIB is called `nsx-`something, and `vsipfwlib` is the one a dependency chain needs removed too. The tool finds the dominant version among the `nsx-*` VIBs and claims everything at that version, including the esxio platform's, which only `esxcli software profile get` shows. Removal is a self-correcting loop: a `DependencyError` adds the VIB it names, a `NoMatchError` drops one that is already gone, and it retries.

Nothing is written to disk but the run log, and no password is ever logged. Needs `pyvmomi` and `paramiko`; if the Python you typed lacks them, the tool looks for one that has them, or prints the one command that fixes it.

## Tests

```bash
cd tests && python3 test_nsx_cleanup.py && python3 test_esxio.py
```

38 checks against stub hosts, with no vCenter and no SSH: the survey and the refusals, the per-host order, the esxio sweep, the removal loop and direct mode. The esxio fixtures are real output.

## Where it sits

The cheapest moment is before a host joins anything: once [esxi-vcf-host-prep](https://github.com/noahfarshad/esxi-vcf-host-prep) has it on the build and standalone, run this in `--direct` mode. After that it is the cluster that will not convert to a vLCM image in [vcf-import-prep](https://github.com/noahfarshad/vcf-import-prep)'s `image` step, one host at a time through vCenter. The overlay the import leaves out afterwards is [nsx-overlay-prep](https://github.com/noahfarshad/nsx-overlay-prep).
