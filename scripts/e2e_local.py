"""End-to-end smoke test against a locally running server (no nginx).

    FRAPPE_LAB_MCP_SECRET=... .venv/bin/python scripts/e2e_local.py [--port 8710] [--write-app safety]

Runs the full OAuth flow (DCR, authorize, login, token, refresh), then calls the tools,
including policy denials. Uses dry_run for anything that would change the database and
only writes/deletes a scratch file in --write-app.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
from urllib.parse import parse_qs, urlparse

import httpx2 as httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


def oauth(base: str, secret: str, headers: dict | None = None) -> dict:
    c = httpx.Client(base_url=base, follow_redirects=False, timeout=30, headers=headers or {})
    bad = c.post("register", json={"redirect_uris": ["https://evil.example/cb"], "client_name": "evil"})
    check("DCR rejects foreign redirect URI", bad.status_code == 400, bad.text)

    reg = c.post("register", json={"redirect_uris": [REDIRECT], "client_name": "ChatGPT (test)",
                                    "token_endpoint_auth_method": "client_secret_post",
                                    "grant_types": ["authorization_code", "refresh_token"]}).json()
    check("DCR registers ChatGPT-style client", "client_id" in reg, str(reg))

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = c.get("authorize", params={"response_type": "code", "client_id": reg["client_id"],
                                    "redirect_uri": REDIRECT, "code_challenge": challenge,
                                    "code_challenge_method": "S256", "state": "xyz", "scope": "lab"})
    check("authorize redirects to login page", r.status_code == 302 and "/login?req=" in r.headers["location"],
          f"{r.status_code} {r.text[:200]}")
    req_id = parse_qs(urlparse(r.headers["location"]).query)["req"][0]

    page = c.get("login", params={"req": req_id})
    check("login page renders", page.status_code == 200 and "ChatGPT (test)" in page.text)
    wrong = c.post("login", data={"req": req_id, "secret": "nope"})
    check("wrong secret refused", wrong.status_code == 200 and "Incorrect secret" in wrong.text)
    ok = c.post("login", data={"req": req_id, "secret": secret})
    loc = ok.headers.get("location", "")
    q = parse_qs(urlparse(loc).query)
    check("right secret redirects back with code+state", ok.status_code == 302 and loc.startswith(REDIRECT)
          and q.get("state") == ["xyz"] and "code" in q, loc)

    tok = c.post("token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                 "redirect_uri": REDIRECT, "code_verifier": verifier,
                                 "client_id": reg["client_id"], "client_secret": reg.get("client_secret", "")}).json()
    check("token exchange", "access_token" in tok, str(tok))
    replay = c.post("token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                    "redirect_uri": REDIRECT, "code_verifier": verifier,
                                    "client_id": reg["client_id"], "client_secret": reg.get("client_secret", "")})
    check("code cannot be replayed", replay.status_code == 400)

    ref = c.post("token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                 "client_id": reg["client_id"], "client_secret": reg.get("client_secret", "")}).json()
    check("refresh rotates tokens", "access_token" in ref and ref["access_token"] != tok["access_token"], str(ref))
    old = c.get("mcp", headers={"Authorization": f"Bearer {tok['access_token']}"})
    check("old access token revoked after refresh", old.status_code == 401)
    return ref


async def tools(base: str, token: str, write_app: str, headers: dict | None = None):
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}", **(headers or {})}, timeout=httpx.Timeout(300))
    async with streamable_http_client(f"{base}/mcp", http_client=http) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            check("initialize returns instructions", bool(init.instructions))
            names = {t.name: t for t in (await s.list_tools()).tools}
            print("      tools:", ", ".join(sorted(names)))
            check("read tools marked readOnly", names["read_file"].annotations.read_only_hint is True)

            async def call(_tool, **args):
                res = await s.call_tool(_tool, args)
                text = "\n".join(getattr(c, "text", "") for c in res.content)
                return res.is_error, text

            err, out = await call("lab_info")
            check("lab_info", not err and write_app in out, out[:300])

            err, out = await call("read_file", path=f"{write_app}/{write_app}/hooks.py", max_lines=5)
            check("read_file in app", not err and out.startswith(f"{write_app}/"), out[:300])
            err, out = await call("read_file", path="frappe/frappe/__init__.py", max_lines=3)
            check("read_file in core app", not err, out[:300])
            err, out = await call("read_file", path="../sites/common_site_config.json")
            check("read outside apps/ refused", err, out[:200])
            err, out = await call("read_file", path=f"{write_app}/.git/config")
            check("read .git refused", err, out[:200])

            err, out = await call("search_code", pattern="def get_meta\\b", path="frappe", glob="*.py", max_results=5)
            check("search_code", not err and "get_meta" in out, out[:300])

            err, out = await call("get_doctype", doctype="User")
            check("get_doctype User", not err and '"fieldname"' in out and "frappe" in out, out[:300])
            err, out = await call("find_doctypes", query="Employee", limit_rows=5)
            check("find_doctypes", not err and "Employee" in out, out[:300])

            err, out = await call("run_python", code="x = frappe.db.count('User')\nprint('users', x)\nx", dry_run=True)
            check("run_python returns value + stdout", not err and "users" in out, out[:300])
            err, out = await call("run_python", code="1/0", dry_run=True)
            check("run_python error surfaces traceback", err and "ZeroDivisionError" in out, out[:300])
            err, out = await call("run_sql", query="select name from tabDocType limit 3", dry_run=True)
            check("run_sql", not err and '"rows"' in out, out[:300])

            err, out = await call("write_file", path="frappe/frappe/zz_test.py", content="x=1\n")
            check("write to core app refused", err and "read-only" in out, out[:200])
            dt_json = next(iter(__import__("glob").glob(
                f"{BENCH}/apps/{write_app}/{write_app}/**/doctype/*/*.json", recursive=True)), None)
            if dt_json:
                rel = os.path.relpath(dt_json, f"{BENCH}/apps")
                err, out = await call("edit_file", path=rel, old_string='"doctype"', new_string='"doctype"')
                check("edit DocType JSON refused", err and "save_document" in out, out[:200])
            err, out = await call("write_file", path=f"{write_app}/{write_app}/zz_labmcp_e2e.json",
                                  content=json.dumps({"doctype": "Workspace", "name": "x"}))
            check("write new doc-shaped JSON refused", err and "save_document" in out, out[:200])

            scratch = f"{write_app}/zz_labmcp_e2e.txt"
            err, out = await call("write_file", path=scratch, content="hello\nworld\n")
            check("write_file in writable app", not err, out)
            err, out = await call("edit_file", path=scratch, old_string="world", new_string="lab")
            check("edit_file", not err, out)
            err, out = await call("read_file", path=scratch)
            check("edit applied", "2|lab" in out, out)
            err, out = await call("delete_file", path=scratch)
            check("delete_file", not err, out)

            err, out = await call("save_document", doctype="DocType", name="User", values={"description": "x"})
            check("save_document on core DocType refused", err and "not writable" in out, out[:300])

            err, out = await call("bench", command="version")
            check("bench version", not err and '"status": "ok"' in out.replace("'", '"') or "frappe" in out, out[:300])
            err, out = await call("bench", command="--site other.site migrate")
            check("bench --site refused", err, out[:200])
            err, out = await call("bench", command="drop-site eben.isambane.co.za")
            check("bench drop-site refused", err and "Not allowed" in out, out[:200])
            err, out = await call("bench", command="update")
            check("bench update refused", err, out[:200])

            err, out = await call("git", app=write_app, action="status")
            check("git status", not err, out[:200])
            err, out = await call("git", app="frappe", action="commit", message="x")
            check("git commit on core app refused", err, out[:200])
            err, out = await call("git", app=write_app, action="checkout", branch="main-not-allowed")
            check("checkout non-allowed branch refused", err, out[:200])


BENCH = "/home/eben/eben-bench"

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8710)
    ap.add_argument("--write-app", default="safety")
    ap.add_argument("--bench", default=BENCH)
    ap.add_argument("--base", help="e.g. http://127.0.0.1:18080/eben to go through nginx")
    ap.add_argument("--host-header", help="e.g. mcp.isambane.co.za")
    a = ap.parse_args()
    BENCH = a.bench
    base = (a.base or f"http://127.0.0.1:{a.port}").rstrip("/") + "/"
    headers = {"Host": a.host_header} if a.host_header else {}
    tok = oauth(base, os.environ["FRAPPE_LAB_MCP_SECRET"], headers)
    asyncio.run(tools(base.rstrip("/"), tok["access_token"], a.write_app, headers))
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
