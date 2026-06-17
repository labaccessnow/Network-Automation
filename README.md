# Network Automation

Treating the network like code — a single source of truth, automated configuration,
and continuous reconciliation across a multi-site, multi-vendor estate.

## What I build
- **Source of truth** — NetBox holds intended state (IPAM, VLANs, interfaces);
  automation reads it as inventory and reconciles DNS / DHCP from it on a schedule.
- **Config backup & drift** — scheduled backups across Cisco IOS/IOS-XE, OPNsense,
  MikroTik RouterOS, Ubiquiti EdgeOS, and EdgeSwitch, with diff-against-intent so the
  network reports its own drift instead of silently rotting.
- **Routing & SDN** — BGP edge automation (re-architected an ISP edge from single- to
  dual-homed multi-homing) plus SDN/VLAN provisioning.
- **Vendor-API tooling** — operational reporting and integrations written against
  vendor REST APIs in Python.

## Approach
Intent lives in git and NetBox; changes flow through review and dry-runs; reality is
diffed against intent on a cron so nothing drifts unnoticed.
