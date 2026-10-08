"""Root-only administration: register per-developer connector instances.

    sudo /opt/frappe-lab-mcp/.venv/bin/frappe-lab-mcp-admin configure [user]
    sudo ... list | status | remove <user> | rotate-secret <user> | nginx-map | restart [user]

Each instance is:
  /etc/frappe-lab-mcp/instances/<user>.yaml       root:<user> 0640 -- policy + secret hash.
                                                  Readable by the process, NOT writable by it,
                                                  so the model cannot widen its own allowlist.
  /etc/supervisor/conf.d/frappe-lab-mcp-<user>.conf   program running AS <user>
  /etc/frappe-lab-mcp/nginx-users.map             "<user> <port>;" lines, included by nginx
  /var/log/frappe-lab-mcp/<user>.log              process log
Runtime state (OAuth tokens, audit log, bench job logs) lives in the user's
~/.local/state/frappe-lab-mcp because the process must write it.
"""

from __future__ import annotations

import argparse
import getpass
import grp
import os
import pwd
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import yaml

from .auth import hash_secret

DEFAULT_DOMAIN = "mcp.isambane.co.za"
FIRST_PORT = 8710
OPT = Path("/opt/frappe-lab-mcp")
PROGRAM = "frappe-lab-mcp-{user}"


class Paths:
    """System paths, optionally under a prefix (--root) for dry runs/testing."""

    def __init__(self, root: str | None):
        r = Path(root) if root else Path("/")
        self.live = root is None
        self.etc = r / "etc/frappe-lab-mcp"
        self.instances = self.etc / "instances"
        self.nginx_map = self.etc / "nginx-users.map"
        self.supervisor = r / "etc/supervisor/conf.d"
        self.sudoers = r / "etc/sudoers.d"
        self.logs = r / "var/log/frappe-lab-mcp"
        self.bin = OPT / ".venv/bin/frappe-lab-mcp"

    def instance(self, user: str) -> Path:
        return self.instances / f"{user}.yaml"

    def program_conf(self, user: str) -> Path:
        return self.supervisor / f"{PROGRAM.format(user=user)}.conf"


# ---------------------------------------------------------------- prompting

def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        val = input(f"{prompt}{suffix}: ").strip()
        if val:
            return val
        if default is not None:
            return default


def ask_yes(prompt: str, default: bool = True) -> bool:
    val = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not val else val.startswith("y")


def die(msg: str):
    sys.exit(f"error: {msg}")


def run(cmd: list[str], paths: Paths, check: bool = True) -> str:
    if not paths.live:
        print(f"  (dry run) would run: {' '.join(cmd)}")
        return ""
    res = subprocess.run(cmd, capture_output=True, text=True)
    if check and res.returncode:
        die(f"{' '.join(cmd)} failed:\n{res.stdout}{res.stderr}")
    return res.stdout + res.stderr


def write_file(path: Path, content: str, mode: int, owner: str = "root", group: str = "root", paths: Paths | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(tmp, mode)
    if paths is None or paths.live:
        shutil.chown(tmp, owner, group)
    os.replace(tmp, path)


# ---------------------------------------------------------------- discovery

def git_apps(bench: Path) -> list[tuple[str, str]]:
    apps = []
    for d in sorted((bench / "apps").iterdir()):
        if (d / ".git").exists():
            br = subprocess.run(["git", "-c", f"safe.directory={d}", "-C", str(d), "branch", "--show-current"],
                                capture_output=True, text=True).stdout.strip()
            apps.append((d.name, br))
    return apps


def find_benches(home: Path) -> list[Path]:
    try:
        return sorted(p.parent.parent for p in home.glob("*/sites/apps.txt"))
    except PermissionError:
        return []


def find_sites(bench: Path) -> list[str]:
    return sorted(p.parent.name for p in (bench / "sites").glob("*/site_config.json"))


def load_instances(paths: Paths) -> dict[str, dict]:
    out = {}
    if paths.instances.is_dir():
        for f in sorted(paths.instances.glob("*.yaml")):
            out[f.stem] = yaml.safe_load(f.read_text()) or {}
    return out


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def next_port(instances: dict, user: str) -> int:
    used = {int(c.get("listen_port", 0)) for u, c in instances.items() if u != user}
    port = FIRST_PORT
    while port in used or not port_free(port):
        port += 1
    return port


# ---------------------------------------------------------------- rendering

def supervisor_conf(user: str, home: Path, paths: Paths, cfg_path: Path) -> str:
    prog = PROGRAM.format(user=user)
    return f"""; managed by frappe-lab-mcp-admin -- edit with `frappe-lab-mcp-admin configure {user}`
[program:{prog}]
command={paths.bin} --config {cfg_path} serve
user={user}
directory={home}
environment=HOME="{home}",USER="{user}",LOGNAME="{user}"
autostart=true
autorestart=true
startsecs=3
stopsignal=TERM
stopwaitsecs=15
stdout_logfile={paths.logs / (user + ".log")}
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=5
redirect_stderr=true
"""


def write_nginx_map(paths: Paths, instances: dict):
    lines = ["# managed by frappe-lab-mcp-admin: <user> <port>;"]
    lines += [f"{u} {int(c['listen_port'])};" for u, c in sorted(instances.items())]
    write_file(paths.nginx_map, "\n".join(lines) + "\n", 0o644, paths=paths)


def reload_nginx(paths: Paths):
    if not paths.live:
        print("  (dry run) would run: nginx -t && systemctl reload nginx")
        return
    if not shutil.which("nginx"):
        print("  nginx not found; skipped reload")
        return
    t = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if t.returncode:
        print(f"  nginx -t failed, NOT reloading:\n{t.stderr}")
        return
    run(["systemctl", "reload", "nginx"], paths)
    print("  nginx reloaded")


# ---------------------------------------------------------------- secret

def prompt_secret(has_existing: bool) -> tuple[str | None, str | None]:
    """Returns (hash, plaintext_to_show_once)."""
    opts = "[g]enerate, [t]ype" + (", [k]eep current" if has_existing else "")
    while True:
        choice = ask(f"Login secret for the ChatGPT connector: {opts}", "k" if has_existing else "g").lower()[:1]
        if choice == "k" and has_existing:
            return None, None
        if choice == "g":
            s = secrets.token_urlsafe(18)
            return hash_secret(s), s
        if choice == "t":
            s1 = getpass.getpass("  Secret (min 12 chars, hidden): ")
            if len(s1) < 12:
                print("  Too short.")
                continue
            if getpass.getpass("  Repeat: ") != s1:
                print("  Did not match.")
                continue
            return hash_secret(s1), None


# ---------------------------------------------------------------- commands

def cmd_configure(args, paths: Paths):
    instances = load_instances(paths)
    user = args.user or ask("Linux user")
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        die(f"no such user '{user}'")
    if pw.pw_uid < 1000:
        die(f"'{user}' is a system account (uid {pw.pw_uid}); refusing.")
    home = Path(pw.pw_dir)
    existing = instances.get(user, {})
    print(f"\n== {'Reconfiguring' if existing else 'Registering'} connector for {user} ({home})\n")

    benches = find_benches(home)
    default_bench = existing.get("bench_path") or (str(benches[0]) if benches else str(home / f"{user}-bench"))
    if benches:
        print("Benches found: " + ", ".join(str(b) for b in benches))
    bench = Path(ask("Bench folder", default_bench)).resolve()
    if not (bench / "sites").is_dir() or not (bench / "apps").is_dir():
        die(f"{bench} is not a bench (needs apps/ and sites/)")
    if bench.stat().st_uid != pw.pw_uid:
        die(f"{bench} is owned by uid {bench.stat().st_uid}, not {user}; refusing to cross users.")

    sites = find_sites(bench)
    if not sites:
        die(f"no sites in {bench}/sites")
    site = ask(f"Site ({', '.join(sites)})", existing.get("site") or sites[0])
    if site not in sites:
        die(f"site '{site}' not found in {bench}/sites")

    port = int(ask("Local port", str(existing.get("listen_port") or next_port(instances, user))))
    clash = [u for u, c in instances.items() if u != user and int(c.get("listen_port", 0)) == port]
    if clash:
        die(f"port {port} is already used by {clash[0]}")

    domain = ask("Public connector domain", (existing.get("public_base_url", "").split("/")[2]
                                             if existing.get("public_base_url") else DEFAULT_DOMAIN))

    # writable apps
    apps = git_apps(bench)
    old_writable: dict = existing.get("writable") or {}
    print("\nGit apps in this bench:")
    for i, (app, br) in enumerate(apps, 1):
        mark = f"  writable on {old_writable[app]}" if app in old_writable else ""
        print(f"  {i:3}  {app:28} {br}{mark}")
    if old_writable:
        default_sel = ",".join(a for a, _ in apps if a in old_writable)
    else:
        default_sel = ",".join(a for a, br in apps if br == user)
    print("\nWritable apps: numbers or names, comma-separated ('-' for none).")
    sel = ask("Writable apps", default_sel or "-")
    chosen: list[str] = []
    if sel != "-":
        names = {a for a, _ in apps}
        for tok in (t.strip() for t in sel.split(",") if t.strip()):
            app = apps[int(tok) - 1][0] if tok.isdigit() and 0 < int(tok) <= len(apps) else tok
            if app not in names:
                die(f"unknown app '{tok}'")
            if app not in chosen:
                chosen.append(app)
    branches_of = dict(apps)
    writable = {}
    for app in chosen:
        default_br = ",".join(old_writable.get(app) or [branches_of[app]])
        writable[app] = [b.strip() for b in ask(f"  Allowed branches for {app}", default_br).split(",") if b.strip()]

    secret_hash, show_secret = prompt_secret(bool(existing.get("login_secret_hash")))

    had_rule = (paths.sudoers / f"frappe-lab-mcp-{user}").exists()
    allow_restart = ask_yes(f"\nLet {user} restart/stop/view their own connector via sudo supervisorctl?",
                            default=had_rule or not existing)

    cfg = {
        "user": user,
        "public_base_url": f"https://{domain}/{user}",
        "listen_port": port,
        "bench_path": str(bench),
        "site": site,
        "state_dir": str(home / ".local/state/frappe-lab-mcp"),
        "writable": writable,
        "login_secret_hash": secret_hash or existing.get("login_secret_hash"),
    }
    for key in ("extra_bench_commands", "disabled_bench_commands", "extra_path", "access_token_ttl",
                "refresh_token_ttl", "max_output_chars", "allowed_redirect_prefixes", "allowed_hosts"):
        if key in existing:
            cfg[key] = existing[key]

    print("\nSummary:")
    print(f"  user {user}  bench {bench}  site {site}  port {port}")
    print(f"  connector URL  https://{domain}/{user}/mcp")
    print("  writable       " + (", ".join(f"{a}@{'|'.join(b)}" for a, b in writable.items()) or "none"))
    if not ask_yes("Apply?"):
        die("aborted")

    group = grp.getgrgid(pw.pw_gid).gr_name
    paths.instances.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.etc, 0o755)
    os.chmod(paths.instances, 0o751)  # users can open their own file but not list others
    header = "# managed by frappe-lab-mcp-admin; root-owned so the connector cannot change its own policy\n"
    write_file(paths.instance(user), header + yaml.safe_dump(cfg, sort_keys=False), 0o640, "root", group, paths)

    paths.logs.mkdir(parents=True, exist_ok=True)
    write_file(paths.program_conf(user), supervisor_conf(user, home, paths, paths.instance(user)), 0o644, paths=paths)

    sudo_file = paths.sudoers / f"frappe-lab-mcp-{user}"
    if allow_restart:
        prog = PROGRAM.format(user=user)
        rule = (f"# managed by frappe-lab-mcp-admin\n{user} ALL=(root) NOPASSWD: "
                f"/usr/bin/supervisorctl restart {prog}, /usr/bin/supervisorctl stop {prog}, "
                f"/usr/bin/supervisorctl start {prog}, /usr/bin/supervisorctl status {prog}, "
                f"/usr/bin/supervisorctl tail {prog}, /usr/bin/supervisorctl tail -f {prog}\n")
        write_file(sudo_file, rule, 0o440, paths=paths)
        if paths.live and subprocess.run(["visudo", "-cf", str(sudo_file)], capture_output=True).returncode:
            sudo_file.unlink()
            print("  sudoers rule failed validation and was removed")
    elif sudo_file.exists():
        sudo_file.unlink()

    instances[user] = cfg
    write_nginx_map(paths, instances)

    # state dir must exist and belong to the user
    state = Path(cfg["state_dir"])
    if paths.live:
        subprocess.run(["runuser", "-u", user, "--", "mkdir", "-p", "-m", "700", str(state)], check=False)

    prog = PROGRAM.format(user=user)
    run(["supervisorctl", "reread"], paths)
    run(["supervisorctl", "update", prog], paths)
    if existing:
        run(["supervisorctl", "restart", prog], paths, check=False)
    if secret_hash and existing:
        _revoke(user, paths)
    reload_nginx(paths)
    print("\n" + run(["supervisorctl", "status", prog], paths, check=False).strip())

    print(f"\nDone. In ChatGPT: Settings -> Connectors -> Create\n  URL:  https://{domain}/{user}/mcp   (OAuth)")
    if show_secret:
        print("\n  Login secret (shown ONCE -- give it to the developer over a private channel):\n")
        print(f"      {show_secret}\n")


def _revoke(user: str, paths: Paths):
    run(["runuser", "-u", user, "--", str(paths.bin), "--config", str(paths.instance(user)), "revoke-all"],
        paths, check=False)


def cmd_rotate_secret(args, paths: Paths):
    f = paths.instance(args.user)
    if not f.exists():
        die(f"no instance for {args.user}")
    cfg = yaml.safe_load(f.read_text())
    secret_hash, show = prompt_secret(False)
    cfg["login_secret_hash"] = secret_hash
    group = grp.getgrgid(pwd.getpwnam(args.user).pw_gid).gr_name
    text = f.read_text().split("\n", 1)[0] + "\n" + yaml.safe_dump(cfg, sort_keys=False)
    write_file(f, text, 0o640, "root", group, paths)
    _revoke(args.user, paths)
    run(["supervisorctl", "restart", PROGRAM.format(user=args.user)], paths, check=False)
    print("Secret rotated and all existing ChatGPT sessions revoked; reconnect the connector.")
    if show:
        print(f"\n  New login secret (shown once):\n\n      {show}\n")


def cmd_list(args, paths: Paths):
    instances = load_instances(paths)
    if not instances:
        print("No instances registered.")
        return
    status = run(["supervisorctl", "status"], paths, check=False) if paths.live else ""
    st = {l.split()[0]: l.split()[1] for l in status.splitlines() if l.strip()}
    print(f"{'user':12} {'port':6} {'status':10} {'site':28} bench / writable")
    for u, c in instances.items():
        w = ", ".join(c.get("writable") or {}) or "-"
        print(f"{u:12} {c.get('listen_port', ''):<6} {st.get(PROGRAM.format(user=u), '?'):10} "
              f"{c.get('site', ''):28} {c.get('bench_path', '')}\n{'':58} writable: {w}")


def cmd_remove(args, paths: Paths):
    user = args.user
    if not paths.instance(user).exists():
        die(f"no instance for {user}")
    if not ask_yes(f"Remove the connector for {user}? (tokens are revoked; their bench is untouched)", False):
        die("aborted")
    _revoke(user, paths)
    prog = PROGRAM.format(user=user)
    run(["supervisorctl", "stop", prog], paths, check=False)
    for p in (paths.program_conf(user), paths.instance(user), paths.sudoers / f"frappe-lab-mcp-{user}"):
        if p.exists():
            p.unlink()
    run(["supervisorctl", "reread"], paths)
    run(["supervisorctl", "update", prog], paths, check=False)
    write_nginx_map(paths, load_instances(paths))
    reload_nginx(paths)
    print(f"Removed {user}.")


def cmd_nginx_map(args, paths: Paths):
    write_nginx_map(paths, load_instances(paths))
    print(paths.nginx_map.read_text())
    reload_nginx(paths)


def cmd_restart(args, paths: Paths):
    users = [args.user] if args.user else list(load_instances(paths))
    for u in users:
        print(run(["supervisorctl", "restart", PROGRAM.format(user=u)], paths, check=False).strip())


def main(argv=None):
    ap = argparse.ArgumentParser(prog="frappe-lab-mcp-admin", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help=argparse.SUPPRESS)  # dry-run prefix for testing; skips system commands
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("configure", help="register or reconfigure a developer (interactive)")
    p.add_argument("user", nargs="?")
    sub.add_parser("list", help="list instances")
    for name, hlp in (("remove", "unregister a developer"), ("rotate-secret", "new login secret + revoke sessions")):
        sub.add_parser(name, help=hlp).add_argument("user")
    sub.add_parser("nginx-map", help="regenerate the nginx user map and reload nginx")
    sub.add_parser("restart", help="restart one or all instances").add_argument("user", nargs="?")
    args = ap.parse_args(argv)

    paths = Paths(args.root)
    if paths.live and os.geteuid() != 0:
        die("run as root (sudo)")
    {"configure": cmd_configure, "list": cmd_list, "remove": cmd_remove, "rotate-secret": cmd_rotate_secret,
     "nginx-map": cmd_nginx_map, "restart": cmd_restart}[args.cmd](args, paths)


if __name__ == "__main__":
    main()
