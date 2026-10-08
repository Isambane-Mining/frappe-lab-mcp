# frappe-lab-mcp

An MCP connector that gives a developer's ChatGPT (Business) workspace scoped access to **their own** Frappe lab bench on isex2, so the code it writes is based on the real apps and DocType model, not guesses.

```
ChatGPT ──HTTPS/OAuth──> nginx mcp.isambane.co.za/<user>/… ──> 127.0.0.1:<port>  frappe-lab-mcp (runs AS <user>)
                                                                    ├─ apps/ files (read all, write allowlisted)
                                                                    ├─ bench env python → frappe (meta, ORM, SQL)
                                                                    └─ bench / git subprocesses
```

Each developer gets their own process under their own Unix user, so the **OS account is the real security boundary**. The tool policy below keeps the model on the rails, but it is not a sandbox: `run_python`, `run_sql`, `bench get-app` and `migrate` can all run arbitrary code as that user.

## Tools

| Tool | What it does |
|---|---|
| `lab_info` | Apps, branches, what is writable now |
| `list_dir`, `read_file`, `search_code` | Read any app under `apps/` (not `.git/` or key files) |
| `write_file`, `edit_file`, `delete_file` | Only in `writable` apps, on an allowed branch. Frappe document JSON (DocType, Workspace, Report, fixtures) is refused |
| `find_doctypes`, `get_doctype` | Live model: fields, custom fields, property setters, permissions, owning app/folder |
| `save_document` | Create/update any document through the ORM. DocType/Workspace/… JSON gets exported by Frappe (developer_mode), so `modified` is correct. Refuses modules of read-only apps |
| `run_python` | Non-interactive console that **commits** (unlike `bench console`). `dry_run` rolls back |
| `run_sql` | SQL as the site DB user |
| `bench`, `bench_job` | Allowlisted commands; `--site` is injected and cannot be overridden. Long commands become background jobs that ChatGPT polls. One job at a time |
| `git` | status/diff/log/fetch/branches on any app; pull/add/commit/push/checkout only on writable apps + allowed branches; push never forces |

Default bench allowlist: `migrate, clear-cache, clear-website-cache, run-tests, install-app, list-apps, export-fixtures, build, restart, get-app, version, setup requirements`. Add more per user with `extra_bench_commands` and remove some with `disabled_bench_commands`.

Every call is logged to `~/.local/state/frappe-lab-mcp/audit.jsonl` (0600).

## Auth

The server is its own small OAuth 2.1 authorization server with dynamic client registration and PKCE, which is what ChatGPT expects. While adding the connector, ChatGPT opens `https://mcp.isambane.co.za/<user>/login`, and the developer enters their **lab login secret**. Only its scrypt hash is stored, in the root-owned instance file. Access tokens last 8h; refresh tokens last 30 days and are rotated on every use. Tokens are stored hashed. Redirect URIs are restricted to ChatGPT (and localhost for testing).

## Deployment (supervisor, one program per developer)

| What | Where | Owner/mode |
|---|---|---|
| Code + venv | `/opt/frappe-lab-mcp` | root, read-only to users |
| Instance policy + secret hash | `/etc/frappe-lab-mcp/instances/<user>.yaml` | `root:<user>` 0640: the connector can read it but **cannot change its own allowlist** |
| Supervisor program | `/etc/supervisor/conf.d/frappe-lab-mcp-<user>.conf` | runs `frappe-lab-mcp serve` **as `<user>`** |
| nginx user→port map | `/etc/frappe-lab-mcp/nginx-users.map` | generated; included by the nginx config |
| Optional self-service sudo | `/etc/sudoers.d/frappe-lab-mcp-<user>` | `supervisorctl start/stop/restart/status/tail frappe-lab-mcp-<user>` only |
| Process log | `/var/log/frappe-lab-mcp/<user>.log` | |
| Tokens, audit log, bench job logs | `~<user>/.local/state/frappe-lab-mcp/` | user, 0700 |

```
sudo deploy/install.sh [user]        # install/upgrade code, restart instances, optionally configure a user
A=/opt/frappe-lab-mcp/.venv/bin/frappe-lab-mcp-admin
sudo $A configure juan               # interactive: bench, site, port, writable apps/branches, secret, sudo rule
sudo $A list                         # instances + supervisor status
sudo $A rotate-secret juan           # new secret, revokes all of juan's ChatGPT sessions
sudo $A remove juan                  # stop + unregister (bench untouched)
sudo $A restart [juan]
sudo $A nginx-map                    # regenerate map + nginx -t + reload
```

`configure` finds the user's benches and sites and lists their git apps with branches. It suggests apps whose branch matches the username as writable, and offers to either generate a secret (shown once, to pass on privately) or let you type one (hidden). It refuses system accounts and benches the user doesn't own. Rerunning it reconfigures an instance, using the current values as defaults.

One-time nginx: install `deploy/nginx-mcp.isambane.co.za.conf` (set the TLS paths and drop Virtualmin's `/.well-known/` block). After that, `configure`/`remove` keep the map current and reload nginx after `nginx -t` passes.

ChatGPT (workspace admin must allow custom connectors): Settings → Connectors → Create. URL `https://mcp.isambane.co.za/<user>/mcp`, auth OAuth.

## Development and testing

```
uv venv .venv && uv pip install --python .venv/bin/python -e .
cp config.example.yaml config.yaml   # edit; git-ignored
.venv/bin/frappe-lab-mcp --config config.yaml set-secret && .venv/bin/frappe-lab-mcp --config config.yaml serve
FRAPPE_LAB_MCP_SECRET=… .venv/bin/python scripts/e2e_local.py                  # direct
FRAPPE_LAB_MCP_SECRET=… .venv/bin/python scripts/e2e_local.py \
    --base https://mcp.isambane.co.za/eben --host-header mcp.isambane.co.za   # through nginx
```

The script runs 38 checks: the OAuth flow, every tool, and the policy refusals. It writes only a scratch file in `--write-app` and uses `dry_run` for database calls.
