from __future__ import annotations

from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request

from . import __version__
from .auth import SCOPE, LabOAuthProvider
from .config import Config
from .tools import register_tools

INSTRUCTIONS = """\
You are connected to one developer's Frappe lab: a full bench (apps/ checkouts) and one site with
developer_mode on. Ground every answer in this lab -- do not guess Frappe APIs, field names or
hooks when you can look them up.

Workflow:
1. lab_info first: apps, branches, and which apps are writable.
2. Before writing code that touches a DocType, call get_doctype for its real fields/permissions,
   and read neighbouring code (search_code, read_file) to match the app's conventions.
   Frappe/ERPNext/HRMS source is in apps/ too -- read it to confirm framework APIs.
3. Code changes: edit_file (small) or write_file, only in writable apps. Paths are relative to apps/.
4. DocType / Workspace / Report / Print Format / Custom Field changes: never edit their JSON
   files; use save_document (or run_python with frappe ORM). Frappe exports the JSON itself.
5. After changing Python: bench restart. After DocType/schema/patches/hooks changes: bench migrate.
   After JS/CSS in public/: bench build --app <app>. Then verify (run_python / run_sql / run-tests).
6. git: review with diff, then add, commit and push only when the developer asks.
run_python and run_sql commit by default; use dry_run to explore safely.
"""


def build_server(cfg: Config) -> tuple[MCPServer, LabOAuthProvider]:
    provider = LabOAuthProvider(cfg)
    mcp = MCPServer(
        name=f"frappe-lab-{cfg.user}",
        title=f"Frappe lab ({cfg.user})",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=cfg.public_base_url,
            resource_server_url=cfg.resource_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
        ),
    )

    @mcp.custom_route("/login", methods=["GET", "POST"], include_in_schema=False)
    async def login(request: Request):
        return await provider.handle_login(request)

    register_tools(mcp, cfg)
    return mcp, provider


def build_app(cfg: Config):
    mcp, _ = build_server(cfg)
    public_host = urlparse(cfg.public_base_url).netloc
    hosts = {public_host, f"127.0.0.1:{cfg.listen_port}", f"localhost:{cfg.listen_port}", *cfg.allowed_hosts}
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=sorted(hosts),
            allowed_origins=["https://chatgpt.com", "https://chat.openai.com", f"https://{public_host}"],
        ),
        host=cfg.listen_host,
    )
