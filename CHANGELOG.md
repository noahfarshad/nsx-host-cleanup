# Changelog

All notable changes to this project are documented here.

## [1.0.0] — 2026-10-16

### Added

- Initial public release. Surveys and removes stale, unconfigured NSX from ESXi hosts a cluster at a time, with `del nsx`, one host at a time, and refuses any host that has real NSX configuration. VIBs are selected by version, the esxio platform is swept from the software profile, and removal is a self-correcting loop. `--direct` for hosts that have not joined anything yet.
- Two test suites, 38 checks, against stub hosts.

### Notes

- Every name in the examples and tests is a placeholder.
- No password is ever logged; nothing is written to disk but the run log.
