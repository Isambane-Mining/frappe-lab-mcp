"""Minimal single-user OAuth 2.1 authorization server for the ChatGPT connector.

ChatGPT registers itself (dynamic client registration), sends the developer to
/login, and the developer enters their lab login secret once. Tokens are stored
hashed in a 0600 JSON file so they survive restarts.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
from pathlib import Path

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import Config

SCOPE = "lab"
PENDING_TTL = 600
CODE_TTL = 300
MAX_LOGIN_ATTEMPTS = 5


# ---------------------------------------------------------------- secret hashing

def hash_secret(secret: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(secret.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_secret(secret: str, stored: str) -> bool:
    try:
        scheme, salt_b64, dk_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    dk = hashlib.scrypt(secret.encode(), salt=base64.b64decode(salt_b64), n=2**15, r=8, p=1,
                        maxmem=64 * 1024 * 1024)
    return hmac.compare_digest(dk, base64.b64decode(dk_b64))


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- persistent store

class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self.data = json.loads(path.read_text())
        else:
            self.data = {}
        for key in ("clients", "pending", "codes", "access", "refresh"):
            self.data.setdefault(key, {})

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    def purge(self):
        now = time.time()
        for key in ("pending", "codes", "access", "refresh"):
            bucket = self.data[key]
            for k in [k for k, v in bucket.items() if v.get("expires_at") and v["expires_at"] < now]:
                del bucket[k]


# ---------------------------------------------------------------- provider

class LabOAuthProvider:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.state_dir / "oauth.json")

    # clients ----------------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.store.data["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not any(str(uri).startswith(p) for p in self.cfg.allowed_redirect_prefixes):
                raise RegistrationError("invalid_redirect_uri", f"Redirect URI not allowed: {uri}")
        with self.store.lock:
            self.store.data["clients"][client_info.client_id] = client_info.model_dump(mode="json")
            self.store.save()

    # authorization ----------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self.cfg.login_secret_hash:
            raise AuthorizeError("server_error", "No login secret configured for this lab.")
        req_id = secrets.token_urlsafe(24)
        with self.store.lock:
            self.store.purge()
            self.store.data["pending"][req_id] = {
                "client_id": client.client_id,
                "client_name": client.client_name or client.client_id,
                "params": params.model_dump(mode="json"),
                "attempts": 0,
                "expires_at": time.time() + PENDING_TTL,
            }
            self.store.save()
        return f"{self.cfg.public_base_url}/login?req={req_id}"

    async def handle_login(self, request: Request) -> Response:
        if request.method == "GET":
            req_id = request.query_params.get("req", "")
            pending = self.store.data["pending"].get(req_id)
            if not pending or pending["expires_at"] < time.time():
                return _page("This sign-in link has expired. Start the connection again from ChatGPT.", status=400)
            return _login_form(self.cfg, req_id, pending)

        form = await request.form()
        req_id = str(form.get("req", ""))
        secret = str(form.get("secret", ""))
        with self.store.lock:
            pending = self.store.data["pending"].get(req_id)
            if not pending or pending["expires_at"] < time.time():
                return _page("This sign-in link has expired. Start the connection again from ChatGPT.", status=400)
            pending["attempts"] += 1
            if pending["attempts"] > MAX_LOGIN_ATTEMPTS:
                del self.store.data["pending"][req_id]
                self.store.save()
                return _page("Too many attempts. Start the connection again from ChatGPT.", status=429)
            self.store.save()

        if not await asyncio.to_thread(verify_secret, secret, self.cfg.login_secret_hash):
            await asyncio.sleep(1)
            return _login_form(self.cfg, req_id, pending, error="Incorrect secret.")

        params = AuthorizationParams.model_validate(pending["params"])
        code = secrets.token_urlsafe(32)
        with self.store.lock:
            del self.store.data["pending"][req_id]
            self.store.data["codes"][_h(code)] = {
                "code": code,
                "scopes": params.scopes or [SCOPE],
                "expires_at": time.time() + CODE_TTL,
                "client_id": pending["client_id"],
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource,
                "subject": self.cfg.user,
            }
            self.store.save()
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
                                status_code=302)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        raw = self.store.data["codes"].get(_h(authorization_code))
        if not raw or raw["client_id"] != client.client_id or raw["expires_at"] < time.time():
            return None
        return AuthorizationCode(**{**raw, "redirect_uri": AnyUrl(raw["redirect_uri"])})

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        with self.store.lock:
            if self.store.data["codes"].pop(_h(authorization_code.code), None) is None:
                raise TokenError("invalid_grant", "Authorization code already used.")
            token = self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)
            self.store.save()
        return token

    # tokens ------------------------------------------------------------
    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = int(time.time())
        common = {"client_id": client_id, "scopes": scopes, "resource": resource, "subject": self.cfg.user}
        self.store.data["access"][_h(access)] = {**common, "expires_at": now + self.cfg.access_token_ttl,
                                                 "refresh": _h(refresh)}
        self.store.data["refresh"][_h(refresh)] = {**common, "expires_at": now + self.cfg.refresh_token_ttl,
                                                   "access": _h(access)}
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=self.cfg.access_token_ttl,
                          refresh_token=refresh, scope=" ".join(scopes))

    async def load_refresh_token(self, client, refresh_token: str):
        raw = self.store.data["refresh"].get(_h(refresh_token))
        if not raw or raw["client_id"] != client.client_id or raw["expires_at"] < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=raw["client_id"], scopes=raw["scopes"],
                            expires_at=raw["expires_at"], resource=raw.get("resource"), subject=raw.get("subject"))

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with self.store.lock:
            old = self.store.data["refresh"].pop(_h(refresh_token.token), None)
            if old is None:
                raise TokenError("invalid_grant", "Refresh token already used.")
            self.store.data["access"].pop(old.get("access", ""), None)
            token = self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)
            self.store.purge()
            self.store.save()
        return token

    async def load_access_token(self, token: str):
        raw = self.store.data["access"].get(_h(token))
        if not raw or raw["expires_at"] < time.time():
            return None
        return AccessToken(token=token, client_id=raw["client_id"], scopes=raw["scopes"],
                           expires_at=raw["expires_at"], resource=raw.get("resource"), subject=raw.get("subject"))

    async def revoke_token(self, token) -> None:
        with self.store.lock:
            h = _h(token.token)
            a = self.store.data["access"].pop(h, None)
            r = self.store.data["refresh"].pop(h, None)
            if a:
                self.store.data["refresh"].pop(a.get("refresh", ""), None)
            if r:
                self.store.data["access"].pop(r.get("access", ""), None)
            self.store.save()

    def revoke_all(self) -> int:
        with self.store.lock:
            n = len(self.store.data["access"]) + len(self.store.data["refresh"])
            self.store.data["access"].clear()
            self.store.data["refresh"].clear()
            self.store.save()
        return n


# ---------------------------------------------------------------- pages

_STYLE = """
body{font-family:system-ui,sans-serif;background:#f5f5f4;color:#1c1917;display:flex;justify-content:center;padding:48px 16px;margin:0}
.card{background:#fff;border:1px solid #e7e5e4;border-radius:10px;padding:28px;max-width:420px;width:100%}
h1{font-size:20px;margin:0 0 12px}p{line-height:1.5;margin:0 0 12px}code{background:#f5f5f4;padding:1px 4px;border-radius:4px}
input{width:100%;box-sizing:border-box;padding:10px;font-size:15px;border:1px solid #d6d3d1;border-radius:6px;margin:4px 0 14px}
button{background:#1c1917;color:#fff;border:0;border-radius:6px;padding:10px 16px;font-size:15px;cursor:pointer}
.err{color:#b91c1c}.muted{color:#78716c;font-size:13px}
@media (prefers-color-scheme:dark){body{background:#1c1917;color:#f5f5f4}.card{background:#292524;border-color:#44403c}
code{background:#44403c}input{background:#1c1917;color:#f5f5f4;border-color:#57534e}button{background:#f5f5f4;color:#1c1917}.muted{color:#a8a29e}}
"""


def _page(body: str, status: int = 200) -> HTMLResponse:
    doc = (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Lab connector</title><style>{_STYLE}</style></head><body><div class='card'>{body}</div></body></html>")
    return HTMLResponse(doc, status_code=status, headers={"X-Frame-Options": "DENY", "Cache-Control": "no-store",
                                                         "Referrer-Policy": "no-referrer"})


def _login_form(cfg: Config, req_id: str, pending: dict, error: str = "") -> HTMLResponse:
    from urllib.parse import urlparse
    redirect_host = urlparse(pending["params"]["redirect_uri"]).netloc
    body = (
        "<h1>Connect to your Frappe lab</h1>"
        f"<p><b>{html.escape(pending['client_name'])}</b> ({html.escape(redirect_host)}) is asking for access to "
        f"the lab bench of <code>{html.escape(cfg.user)}</code> (site <code>{html.escape(cfg.site)}</code>).</p>"
        "<p class='muted'>It will be able to read all apps, change code in your writable apps, run bench commands "
        "and read/write your lab database.</p>"
        + (f"<p class='err'>{html.escape(error)}</p>" if error else "")
        + "<form method='post' action='login'>"
        f"<input type='hidden' name='req' value='{html.escape(req_id)}'>"
        "<label for='s'>Lab login secret</label><input id='s' name='secret' type='password' autocomplete='current-password' autofocus required>"
        "<button type='submit'>Allow access</button></form>"
    )
    return _page(body)
