"""frappe-lab-mcp serve | set-secret | revoke-all | check"""

from __future__ import annotations

import argparse
import secrets
import sys

import yaml

from .config import load_config


def _set_secret(cfg_path, provided: str | None):
    from .auth import hash_secret

    secret = provided or secrets.token_urlsafe(18)
    with open(cfg_path) as f:
        raw = yaml.safe_load(f) or {}
    raw["login_secret_hash"] = hash_secret(secret)
    with open(cfg_path, "w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)
    print("Login secret set. Enter this on the connector sign-in page (it is not stored in plain text):")
    print(f"\n    {secret}\n")
    print("Restart the service for it to take effect.")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="frappe-lab-mcp")
    ap.add_argument("--config", help="config.yaml path (default ~/.config/frappe-lab-mcp/config.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the MCP server")
    s = sub.add_parser("set-secret", help="dev only: set the secret in a user-owned config (installed instances: frappe-lab-mcp-admin rotate-secret)")
    s.add_argument("--secret", help="use this secret instead of generating one")
    sub.add_parser("revoke-all", help="revoke every issued token (disconnects ChatGPT)")
    sub.add_parser("check", help="validate config and show what is writable")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)

    if args.cmd == "set-secret":
        _set_secret(cfg.config_path, args.secret)
    elif args.cmd == "revoke-all":
        from .auth import LabOAuthProvider
        print(f"Revoked {LabOAuthProvider(cfg).revoke_all()} tokens.")
    elif args.cmd == "check":
        from .policy import current_branch
        print(f"user={cfg.user} site={cfg.site} bench={cfg.bench_path}")
        print(f"public={cfg.public_base_url}  mcp={cfg.resource_url}  listen={cfg.listen_host}:{cfg.listen_port}")
        print(f"bench python: {cfg.bench_python} ({'ok' if cfg.bench_python.exists() else 'MISSING'})")
        print(f"bench cli:    {cfg.bench_bin}")
        print(f"login secret: {'set' if cfg.login_secret_hash else 'NOT SET (run set-secret)'}")
        for app, branches in cfg.writable.items():
            exists = (cfg.apps_path / app).is_dir()
            br = current_branch(cfg, app) if exists else "-"
            state = "WRITABLE" if exists and br in branches else "read-only now"
            print(f"  {app:24} on {br:14} allowed {branches} -> {state}")
    elif args.cmd == "serve":
        import uvicorn
        from .server import build_app

        if not cfg.login_secret_hash:
            sys.exit("No login secret configured; run `frappe-lab-mcp set-secret` first.")
        uvicorn.run(build_app(cfg), host=cfg.listen_host, port=cfg.listen_port,
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1", log_level="info")


if __name__ == "__main__":
    main()
