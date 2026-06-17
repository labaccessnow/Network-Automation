#!/usr/bin/env python3
"""netbackup — read-only, multi-vendor network config backup over SSH / telnet / API.

Pulls each device's running/boot config to a local directory as timestamped files
with N-day retention. Makes **no changes** to any device.

Devices are defined in an inventory file (default: ``devices.yaml``). Credentials are
NEVER stored in the inventory — passwords and API secrets are read from environment
variables, so you can source them from a vault / SOPS / CI secret at runtime.

Supported device types
  edgeos    Ubiquiti EdgeRouter (EdgeOS)  — ``cat /config/config.boot`` (exec)
  ios       IOS-like CLI (Cisco IOS/IOS-XE, Ubiquiti EdgeSwitch/FASTPATH) —
            ``enable`` + ``show running-config`` with --More-- paging handled
  routeros  MikroTik RouterOS            — ``/export``
  opnsense  OPNsense                     — full config.xml via the REST API
  slp       Lantronix SecureLinx SLP PDU — telnet state snapshot
  digicm    Digi CM terminal server      — legacy-KEX SSH, dumps /tmp/cnf/*
  console   A device reached ONLY through a serial terminal-server redirect port
            (set ``console_type: routeros`` or ``edgeos``)
  Plus an optional Proxmox SDN snapshot (zones + vnets) if PROXMOX_* env vars are set.

Usage
  cp devices.example.yaml devices.yaml      # describe your estate (no secrets in here)
  export NB_CORE_SWITCH_PASS=...            # one var per device label
  python3 netbackup.py [--inventory devices.yaml] [--out ./backups] [--retain-days 30]

Credential env convention (label uppercased, non-alphanumeric -> _):
  NB_<LABEL>_PASS          device password
  NB_<LABEL>_APIKEY        OPNsense API key
  NB_<LABEL>_APISECRET     OPNsense API secret
  NB_CONSOLE_GW_PASS       the shared console-gateway password (for console devices)

Requires: paramiko, PyYAML (+ pexpect for digicm/console, proxmoxer for the SDN snapshot).
"""
import argparse, base64, datetime, glob, json, os, re, ssl, sys, time, urllib.request
import paramiko
import yaml


def env_key(label, suffix):
    return "NB_" + re.sub(r"[^A-Z0-9]+", "_", label.upper()) + "_" + suffix


def _connect(d):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(d["host"], username=d.get("user", ""), password=d.get("password", ""),
              timeout=15, look_for_keys=False, allow_agent=False)
    return c


def capture_exec(d, cmd):
    c = _connect(d)
    _, out, _ = c.exec_command(cmd, timeout=30)
    data = out.read().decode(errors="replace")
    c.close()
    return data


def capture_opnsense_api(d):
    """OPNsense full config.xml via /api/core/backup/download/this (api_key + api_secret)."""
    scheme = d.get("scheme") or "https"
    url = f"{scheme}://{d['host']}/api/core/backup/download/this"
    auth = base64.b64encode(f"{d['api_key']}:{d['api_secret']}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        return r.read().decode(errors="replace")


def capture_routeros(d):
    """RouterOS `/export` over an interactive SSH shell (single privilege level, no enable)."""
    c = _connect(d)
    sh = c.invoke_shell(width=400, height=4000)

    def rd(maxwait=60):
        buf, last = "", time.time()
        while time.time() - last < maxwait:
            time.sleep(0.3)
            if sh.recv_ready():
                buf += sh.recv(65535).decode(errors="replace"); last = time.time()
            elif re.search(r"\]\s*>\s*$", buf):
                break
        return buf

    rd(6)
    sh.send("/export\r\n")
    cfg = rd(120)
    c.close()
    cfg = re.sub(r"^/export\s*\r?\n", "", cfg)
    cfg = re.sub(r"\[[^\]]+\]\s*>\s*$", "", cfg)
    cfg = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", cfg)
    return cfg.replace("\r\n", "\n")


def capture_shell(d):
    """IOS-like CLI: enable, answer --More-- paging, show running-config."""
    c = _connect(d)
    sh = c.invoke_shell(width=400, height=2000)

    def rd(maxwait=120):
        buf, last = "", time.time()
        while time.time() - last < maxwait:
            time.sleep(0.3)
            if sh.recv_ready():
                ch = sh.recv(65535).decode(errors="replace"); buf += ch; last = time.time()
                if "More" in ch:
                    sh.send(" ")
            elif buf.rstrip().endswith(("#", ">")):
                break
        return buf

    rd(8)
    sh.send("enable\r\n"); e = rd(6)
    if "assword" in e:
        sh.send(d.get("password", "") + "\r\n"); rd(6)
    sh.send("show running-config\r\n"); cfg = rd(120)
    c.close()
    cfg = re.sub(r"--More-- or \(q\)uit", "", cfg)
    cfg = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", cfg)
    cfg = "".join(ch for ch in cfg if ch in "\t\n\r" or 32 <= ord(ch) < 127)
    return re.sub(r"[ \t]+\r?\n", "\n", cfg)


def capture_digicm(d):
    """Digi CM terminal server: its SSH only offers legacy KEX paramiko refuses, so drive
    system `ssh` via pexpect. Device state lives under /tmp/cnf/; dump each file."""
    import pexpect
    cmd = (
        "ssh -o StrictHostKeyChecking=accept-new "
        "-o KexAlgorithms=+diffie-hellman-group1-sha1,diffie-hellman-group-exchange-sha1,diffie-hellman-group14-sha1 "
        "-o HostKeyAlgorithms=+ssh-rsa,ssh-dss -o PubkeyAcceptedKeyTypes=+ssh-rsa,ssh-dss "
        f"-o PreferredAuthentications=password {d['user']}@{d['host']}"
    )
    p = pexpect.spawn(cmd, timeout=20, encoding="utf-8")
    p.expect("assword:", timeout=10); p.sendline(d.get("password", ""))
    p.expect(r"[#$>]\s*$", timeout=15)
    p.sendline("ls /tmp/cnf/"); p.expect(r"[#$>]\s*$", timeout=10)
    lines = [l.strip() for l in p.before.splitlines() if l.strip() and not l.lstrip().startswith("ls ")]
    files = [f for ln in lines for f in ln.split() if f and not f.startswith("/")]
    out = ["=== version ===\n"]
    p.sendline("cat /tmp/cnf/version 2>/dev/null"); p.expect(r"[#$>]\s*$", timeout=8); out.append(p.before)
    for f in files:
        if f == "version":
            continue
        p.sendline(f"echo === {f} ===; cat /tmp/cnf/{f} 2>/dev/null")
        p.expect(r"[#$>]\s*$", timeout=10); out.append(p.before)
    p.sendline("exit")
    return "\n".join(out)


def capture_console(d, gw, ctype):
    """A device reachable ONLY through a serial terminal-server redirect port. SSH to the
    gateway's per-port TCP socket (same legacy-KEX fallback), authenticate to the gateway,
    log into the attached device, and pull its config.
      routeros: login as `<user>+ct` (dumb terminal, avoids redraw mangling) -> /export
      edgeos:   login -> operational shell -> cat /config/config.boot
    """
    import pexpect
    OPER, CONF = r"@[\w.-]+:~\$", r"@[\w.-]+#"

    def strip(s):
        s = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s)
        s = re.sub(r"\x1b[=>cZ78]", "", s)
        return s.replace("\r\n", "\n").replace("\r", "")

    def read_idle(p, idle=2.5, cap=120):
        buf, last = "", time.time()
        while time.time() - last < cap:
            try:
                buf += p.read_nonblocking(65535, timeout=idle); last = time.time()
            except (pexpect.TIMEOUT, pexpect.EOF):
                break
        return buf

    cmd = (
        "ssh -o StrictHostKeyChecking=accept-new "
        "-o KexAlgorithms=+diffie-hellman-group1-sha1,diffie-hellman-group-exchange-sha1,diffie-hellman-group14-sha1 "
        "-o HostKeyAlgorithms=+ssh-rsa,ssh-dss -o PubkeyAcceptedKeyTypes=+ssh-rsa,ssh-dss "
        f"-o PreferredAuthentications=password -p {d['port']} {gw['user']}@{d['host']}"
    )
    p = pexpect.spawn(cmd, timeout=25, encoding="utf-8")
    if p.expect(["assword:", "in use", pexpect.TIMEOUT, pexpect.EOF], timeout=12) != 0:
        raise RuntimeError(f"console port {d['port']} in use / no gateway prompt")
    p.sendline(gw.get("password", "")); time.sleep(1); p.sendline("")

    if ctype == "routeros":
        if p.expect([r"ogin:", r"\][^>]*>", pexpect.TIMEOUT], timeout=12) == 0:
            p.sendline(d["user"] + "+ct")
            p.expect("assword:", timeout=8); p.sendline(d.get("password", ""))
            p.expect(r"\][^>]*>", timeout=15)
        p.sendline("/export")
        cfg = strip(read_idle(p, 2.5, 120))
        cfg = re.sub(r"^\s*/export[^\n]*\n", "", cfg)
        cfg = re.sub(r"\n\[[^\]]+\][^>\n]*>\s*$", "", cfg)
        try: p.sendline("/quit")
        except Exception: pass
        return cfg

    j = p.expect([r"ogin:", OPER, CONF, pexpect.TIMEOUT], timeout=12)
    if j == 0:
        p.sendline(d["user"]); p.expect("assword:", timeout=8); p.sendline(d.get("password", ""))
        if p.expect([OPER, CONF, pexpect.TIMEOUT], timeout=15) == 1:
            p.sendline("exit"); p.expect(OPER, timeout=8)
    elif j == 2:
        p.sendline("exit"); p.expect(OPER, timeout=8)
    p.sendline("cat /config/config.boot")
    cfg = strip(read_idle(p, 2.5, 40))
    cfg = re.sub(r"^.*?cat /config/config\.boot[^\n]*\n", "", cfg, flags=re.DOTALL)
    cfg = re.sub(r"\n[\w.-]+@[\w.-]+:~\$\s*$", "", cfg)
    try: p.sendline("exit")
    except Exception: pass
    return cfg


def capture_slp(d):
    """Lantronix SecureLinx SLP PDU over telnet (its SSH is too old). State snapshot:
    VERSION + STATUS + SHOW USERS/NETWORK/SYSTEM, walking past More (Y/es N/o) paging."""
    import telnetlib
    tn = telnetlib.Telnet(d["host"], 23, timeout=15)
    tn.read_until(b"Username:", timeout=8); tn.write(d["user"].encode() + b"\r\n")
    tn.read_until(b"Password:", timeout=8); tn.write(d.get("password", "").encode() + b"\r\n")

    def run(cmd, max_wait=20):
        tn.read_very_eager(); tn.write(cmd.encode() + b"\r\n")
        buf, start = b"", time.time()
        while time.time() - start < max_wait:
            try:
                chunk = tn.read_until(b"SLP: ", timeout=2)
            except EOFError:
                break
            buf += chunk
            if b"More (Y/es N/o):" in chunk:
                tn.write(b"Y\r\n"); continue
            if chunk.endswith(b"SLP: "):
                break
        return buf.decode(errors="replace").replace("\r", "")

    parts = [f"=== {cmd} ===\n{run(cmd)}\n" for cmd in ("VERSION", "STATUS", "SHOW USERS", "SHOW NETWORK", "SHOW SYSTEM")]
    tn.write(b"LOGOUT\r\n")
    try: tn.close()
    except Exception: pass
    return "\n".join(parts)


TYPE_METHOD = {
    "edgeos": "exec", "ios": "shell", "routeros": "routeros",
    "opnsense": "opnsense_api", "slp": "slp", "digicm": "digicm", "console": "console",
}


def load_creds(label, dev, gateway):
    d = {k: v for k, v in dev.items() if k not in ("label", "type", "console_type")}
    d["password"] = os.environ.get(env_key(label, "PASS"), "")
    if dev.get("type") == "opnsense":
        d["api_key"] = os.environ.get(env_key(label, "APIKEY"), "")
        d["api_secret"] = os.environ.get(env_key(label, "APISECRET"), "")
    return d


def main():
    ap = argparse.ArgumentParser(description="Read-only multi-vendor network config backup.")
    ap.add_argument("--inventory", default=os.environ.get("NB_INVENTORY", "devices.yaml"))
    ap.add_argument("--out", default=os.environ.get("NB_BACKUP_DIR", "./backups"))
    ap.add_argument("--retain-days", type=int, default=int(os.environ.get("NB_RETAIN_DAYS", "30")))
    args = ap.parse_args()

    with open(args.inventory) as f:
        inv = yaml.safe_load(f) or {}
    devices = inv.get("devices", [])
    gw = inv.get("console_gateway") or {}
    if gw:
        gw = dict(gw); gw["password"] = os.environ.get("NB_CONSOLE_GW_PASS", "")

    os.makedirs(args.out, mode=0o700, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    rc = 0

    for dev in devices:
        label, dtype = dev.get("label"), dev.get("type")
        if not label or dtype not in TYPE_METHOD:
            print(f"  {label or '?'}: skipped (missing/unknown type {dtype!r})"); continue
        d = load_creds(label, dev, gw)
        try:
            method = TYPE_METHOD[dtype]
            if method == "exec":
                data = capture_exec(d, dev.get("cmd", "cat /config/config.boot"))
            elif method == "opnsense_api":
                data = capture_opnsense_api(d)
            elif method == "routeros":
                data = capture_routeros(d)
            elif method == "slp":
                data = capture_slp(d)
            elif method == "digicm":
                data = capture_digicm(d)
            elif method == "console":
                data = capture_console(d, gw, dev.get("console_type", "edgeos"))
            else:
                data = capture_shell(d)
            if not data.strip() or len(data) < 200:
                print(f"  {label}: SUSPICIOUSLY SHORT ({len(data)} bytes)"); rc = 1; continue
            path = os.path.join(args.out, f"{label}-{ts}.conf")
            with open(path, "w") as f:
                f.write(data)
            os.chmod(path, 0o600)
            print(f"  {label}: saved {path} ({len(data)} bytes)")
        except Exception as ex:
            print(f"  {label}: ERROR {type(ex).__name__}: {str(ex)[:140]}"); rc = 1

    # Optional Proxmox SDN snapshot (zones + vnets). Skipped unless PROXMOX_* env vars set.
    try:
        if all(os.getenv(k) for k in ("PROXMOX_HOST", "PROXMOX_USER", "PROXMOX_TOKEN_NAME", "PROXMOX_TOKEN_VALUE")):
            from proxmoxer import ProxmoxAPI
            api = ProxmoxAPI(
                os.environ["PROXMOX_HOST"], user=os.environ["PROXMOX_USER"],
                token_name=os.environ["PROXMOX_TOKEN_NAME"], token_value=os.environ["PROXMOX_TOKEN_VALUE"],
                port=int(os.getenv("PROXMOX_PORT", "8006")),
                verify_ssl=os.getenv("PROXMOX_VERIFY_SSL", "").lower() in ("1", "true", "yes", "on"),
            )
            sdn = {"zones": api.cluster.sdn.zones.get(), "vnets": api.cluster.sdn.vnets.get()}
            path = os.path.join(args.out, f"proxmox-sdn-{ts}.json")
            with open(path, "w") as f:
                json.dump(sdn, f, indent=2, default=str)
            os.chmod(path, 0o600)
            print(f"  proxmox-sdn: saved {path} — {len(sdn['zones'])} zones, {len(sdn['vnets'])} vnets")
    except Exception as ex:
        print(f"  proxmox-sdn: ERROR {type(ex).__name__}: {str(ex)[:140]}"); rc = 1

    for pattern in ("*.conf", "*.json"):
        for f in glob.glob(os.path.join(args.out, pattern)):
            if os.path.getmtime(f) < time.time() - args.retain_days * 86400:
                os.remove(f)

    print("network backup complete" if rc == 0 else "network backup had errors")
    sys.exit(rc)


if __name__ == "__main__":
    main()
