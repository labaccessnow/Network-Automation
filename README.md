# Network Automation — multi-vendor config backup

`netbackup.py` is a read-only, multi-vendor network configuration backup tool. Point it
at an inventory of devices and it pulls each one's running/boot config to timestamped
files with automatic retention. It makes **no changes** to any device.

I built and run this against a live, mixed-vendor estate (routers, switches, firewalls,
PDUs, and console servers) on a daily cron. This is the generalized, secrets-free version.

## Supported devices

| `type`     | Devices | How it captures |
|------------|---------|-----------------|
| `edgeos`   | Ubiquiti EdgeRouter | `cat /config/config.boot` over SSH (the restorable config) |
| `ios`      | Cisco IOS / IOS-XE, Ubiquiti EdgeSwitch (FASTPATH) | `enable` → `show running-config`, `--More--` paging handled |
| `routeros` | MikroTik RouterOS | `/export` over an interactive shell |
| `opnsense` | OPNsense | full `config.xml` via the REST API (key + secret) |
| `slp`      | Lantronix SecureLinx SLP PDU | telnet snapshot (VERSION/STATUS/USERS/NETWORK/SYSTEM) |
| `digicm`   | Digi CM terminal server | legacy-KEX SSH (via system `ssh`), dumps `/tmp/cnf/*` |
| `console`  | Anything reachable **only** through a serial terminal-server port | logs in through the gateway, then `routeros` `/export` or `edgeos` config.boot |

Plus an optional **Proxmox SDN** snapshot (zones + vnets) when `PROXMOX_*` env vars are set.

Why so many transports? Real networks aren't one vendor. The interesting parts here are the
per-vendor quirks — RouterOS's single privilege level and prompt handling, FASTPATH/IOS
`--More--` paging, OPNsense's API-only full config, and old gear (Lantronix, Digi) that only
speaks telnet or legacy SSH key exchange and has to be driven with `pexpect` + system `ssh`.

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # paramiko, PyYAML; pexpect for digicm/console; proxmoxer optional
```
> Lantronix `slp` uses the stdlib `telnetlib`, removed in Python 3.13 — use Python 3.12 if you need it.

## Configure

1. Describe your estate (no secrets in this file):
   ```bash
   cp devices.example.yaml devices.yaml   # then edit
   ```
2. Provide credentials via environment variables — one per device label, where the label is
   uppercased and non-alphanumeric characters become `_`:
   ```
   label: core-switch     ->  NB_CORE_SWITCH_PASS
   label: fw-1 (opnsense) ->  NB_FW_1_APIKEY  +  NB_FW_1_APISECRET
   console_gateway        ->  NB_CONSOLE_GW_PASS
   ```
   Source them from whatever you already use (a vault, SOPS, CI secrets):
   ```bash
   export NB_CORE_SWITCH_PASS="$(sops -d --extract '["core_switch"]["password"]' secrets.json)"
   ```

## Run

```bash
python3 netbackup.py --inventory devices.yaml --out ./backups --retain-days 30
```
Each device is written to `./backups/<label>-<UTC-timestamp>.conf` (mode 600); files older
than the retention window are pruned. The exit code is non-zero if any device failed or
returned a suspiciously short capture, so it's safe to alert on from cron:

```cron
0 1 * * *  cd /opt/netbackup && . .venv/bin/activate && python3 netbackup.py >> backup.log 2>&1
```

## Design notes

- **Read-only by construction** — every transport only reads; there is no write path.
- **No secrets on disk** — the inventory is non-sensitive; credentials live only in the
  environment at runtime, so `devices.yaml` is safe to commit if you want.
- **Fail loud** — empty or truncated captures are flagged and set a non-zero exit.
- **Portable** — pure Python; the optional bits (pexpect, proxmoxer) degrade gracefully.

## Why this still matters (context)

Network automation has matured fast — **NetBox** as a source of truth with nightly drift
reconciliation, and **Event-Driven Ansible** (GA in AAP 2.4; expanded in **AAP 2.5, Sep 30 2024**)
reacting to change as it happens. But all of that assumes one thing: a reliable, current capture of
what each device is *actually* running. You can't diff, reconcile, or recover what you never backed up.

This tool is the dependable layer underneath: a timestamped, **read-only** record of every device's
real config — across a fleet that doesn't share one API or even one transport (SSH exec, interactive
shell, REST, telnet, legacy KEX, and serial-over-terminal-server). It pairs naturally with a
source-of-truth/GitOps workflow (commit the captures, diff against intent) and is small enough to run
from cron on day one.

## Lessons learned
- **Old gear doesn't speak modern SSH, and that's the whole design.** The Lantronix PDUs only do
  telnet; the Digi terminal server only offers legacy key exchange (`diffie-hellman-group1-sha1`)
  that paramiko refuses outright. So the tool uses paramiko for the modern fleet and drives the
  legacy boxes through system `ssh` with explicit `KexAlgorithms=+...` via pexpect. One backup
  tool, three different ways in — because a real estate is never one vendor or one transport.
- **A "successful" backup of an empty file is worse than a failure.** It looks fine right up until
  you need to restore. The tool flags any capture under ~200 bytes and exits non-zero, so cron
  actually alerts instead of quietly archiving nothing.
- **Each vendor fights scripted reads differently.** RouterOS redraws the screen and mangles input
  (the `+ct` dumb-terminal login fixes it); FASTPATH/IOS pages with `--More--`. There is no
  one-size capture loop — each transport needed its own, and pretending otherwise just gives you
  truncated configs.
- **Pin the runtime.** `telnetlib` was removed from the Python 3.13 standard library, so the
  Lantronix path needs 3.12. The Python version is part of the tool's contract, not an afterthought.

## License

GPL-3.0 — see [LICENSE](LICENSE).
