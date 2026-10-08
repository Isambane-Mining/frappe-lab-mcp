"""Path and write policy.

Reads: anything under <bench>/apps except git internals and key material.
Writes: only inside apps listed in `writable`, only while that app's checked-out
branch is one of its allowed branches, and never to Frappe document JSON
(DocType, Workspace, Report, fixtures, ...) -- those must go through
save_document / bench so Frappe writes them itself.
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .config import Config

DENY_SEGMENTS = {".git"}
DENY_NAME_GLOBS = ["*.pem", "*.key", "id_rsa*", "id_ed25519*", ".env", ".env.*", "*.p12", "*.pfx"]
MAX_READ_BYTES = 2 * 1024 * 1024


def resolve_app_path(cfg: Config, rel_path: str) -> tuple[str, Path]:
    """Resolve a path given relative to apps/ (e.g. "safety/safety/hooks.py").

    Returns (app_name, absolute_path). Rejects anything that escapes apps/,
    including via symlinks.
    """
    rel_path = (rel_path or "").strip().lstrip("/")
    if rel_path.startswith("apps/"):
        rel_path = rel_path[len("apps/"):]
    if not rel_path:
        raise ToolError("Path is empty. Paths are relative to the bench apps/ folder, e.g. 'safety/safety/hooks.py'.")

    apps_root = cfg.apps_path.resolve()
    target = (apps_root / rel_path).resolve()
    try:
        rel = target.relative_to(apps_root)
    except ValueError:
        raise ToolError(f"'{rel_path}' is outside the bench apps/ folder.") from None

    parts = rel.parts
    if not parts:
        raise ToolError("Path must point inside an app.")
    if DENY_SEGMENTS.intersection(parts):
        raise ToolError("Access to .git internals is not allowed; use the git tool.")
    if any(fnmatch.fnmatch(target.name, g) for g in DENY_NAME_GLOBS):
        raise ToolError(f"'{target.name}' looks like key material and is blocked.")
    return parts[0], target


def current_branch(cfg: Config, app: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(cfg.apps_path / app), "branch", "--show-current"],
        capture_output=True, text=True, timeout=15,
    )
    return res.stdout.strip()


def writable_apps(cfg: Config) -> list[str]:
    """Apps that are writable right now (configured AND on an allowed branch)."""
    return [app for app, branches in cfg.writable.items()
            if (cfg.apps_path / app).is_dir() and current_branch(cfg, app) in branches]


def assert_app_writable(cfg: Config, app: str) -> str:
    if app not in cfg.writable:
        raise ToolError(
            f"App '{app}' is read-only for this lab. Writable apps: {', '.join(sorted(cfg.writable)) or 'none'}."
        )
    branch = current_branch(cfg, app)
    allowed = cfg.writable[app]
    if branch not in allowed:
        raise ToolError(
            f"App '{app}' is on branch '{branch or '(detached)'}', which is not writable. "
            f"Allowed branches: {', '.join(allowed)}. Use git action 'checkout' to switch."
        )
    return branch


def is_frappe_document_json(text: str) -> bool:
    """True for Frappe document exports / fixtures (top-level `doctype` key)."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False
    if isinstance(data, dict):
        return "doctype" in data
    if isinstance(data, list) and data:
        return all(isinstance(d, dict) and "doctype" in d for d in data[:20])
    return False


def assert_path_writable(cfg: Config, rel_path: str, new_content: str | None = None) -> tuple[str, Path]:
    app, target = resolve_app_path(cfg, rel_path)
    assert_app_writable(cfg, app)
    if target.suffix == ".json":
        existing = target.read_text(errors="replace") if target.is_file() else None
        if (existing and is_frappe_document_json(existing)) or (
            new_content is not None and is_frappe_document_json(new_content)
        ):
            raise ToolError(
                "This is a Frappe document JSON file (DocType/Workspace/Report/fixture...). "
                "Do not edit it directly: change the document with save_document (or run_python) "
                "so Frappe exports the JSON itself, or use bench export-fixtures for fixtures."
            )
    return app, target
