"""Per-user configuration.

One config file per developer, owned by that developer's Unix user. The MCP
process runs as that user, so everything it does is bounded by the OS
permissions of the lab account; the policy here is a guardrail on top.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("~/.config/frappe-lab-mcp/config.yaml").expanduser()

# bench subcommands the model may run. Values say whether --site is injected.
DEFAULT_BENCH_COMMANDS: dict[str, bool] = {
    "migrate": True,
    "clear-cache": True,
    "clear-website-cache": True,
    "run-tests": True,
    "install-app": True,
    "list-apps": True,
    "export-fixtures": True,
    "build": False,
    "restart": False,
    "get-app": False,
    "version": False,
    "setup requirements": False,
}


@dataclass
class Config:
    user: str
    public_base_url: str  # e.g. https://mcp.isambane.co.za/eben (issuer; MCP lives at <this>/mcp)
    bench_path: Path
    site: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 8710
    allowed_hosts: list[str] = field(default_factory=list)
    # app -> branches the model may write to / commit / push on
    writable: dict[str, list[str]] = field(default_factory=dict)
    bench_commands: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_BENCH_COMMANDS))
    login_secret_hash: str = ""
    allowed_redirect_prefixes: list[str] = field(
        default_factory=lambda: [
            "https://chatgpt.com/",
            "https://chat.openai.com/",
            "http://localhost",
            "http://127.0.0.1",
        ]
    )
    state_dir: Path = Path("~/.local/state/frappe-lab-mcp").expanduser()
    access_token_ttl: int = 8 * 3600
    refresh_token_ttl: int = 30 * 24 * 3600
    max_output_chars: int = 12000
    extra_path: list[str] = field(default_factory=list)  # prepended to PATH for bench/git
    config_path: Path = DEFAULT_CONFIG_PATH

    @property
    def apps_path(self) -> Path:
        return self.bench_path / "apps"

    @property
    def sites_path(self) -> Path:
        return self.bench_path / "sites"

    @property
    def bench_python(self) -> Path:
        return self.bench_path / "env" / "bin" / "python"

    @property
    def bench_bin(self) -> Path:
        # bench CLI installed in the user's PATH (pipx/uv tool) or in the bench env
        for candidate in (self.bench_path / "env" / "bin" / "bench", Path("~/.local/bin/bench").expanduser()):
            if candidate.exists():
                return candidate
        return Path("bench")

    @property
    def resource_url(self) -> str:
        return self.public_base_url.rstrip("/") + "/mcp"


def load_config(path: Path | str | None = None) -> Config:
    path = Path(path or os.environ.get("FRAPPE_LAB_MCP_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    bench_commands = dict(DEFAULT_BENCH_COMMANDS)
    bench_commands.update(raw.pop("extra_bench_commands", None) or {})
    for cmd in raw.pop("disabled_bench_commands", None) or []:
        bench_commands.pop(cmd, None)

    cfg = Config(
        user=raw["user"],
        public_base_url=raw["public_base_url"].rstrip("/"),
        bench_path=Path(raw["bench_path"]).expanduser().resolve(),
        site=raw["site"],
        listen_host=raw.get("listen_host", "127.0.0.1"),
        listen_port=int(raw.get("listen_port", 8710)),
        allowed_hosts=list(raw.get("allowed_hosts") or []),
        writable={app: list(branches) for app, branches in (raw.get("writable") or {}).items()},
        bench_commands=bench_commands,
        login_secret_hash=raw.get("login_secret_hash", ""),
        state_dir=Path(raw.get("state_dir", "~/.local/state/frappe-lab-mcp")).expanduser(),
        config_path=path,
    )
    for key in ("allowed_redirect_prefixes", "extra_path"):
        if key in raw:
            setattr(cfg, key, [str(Path(p).expanduser()) if key == "extra_path" else p for p in raw[key]])
    for key in ("access_token_ttl", "refresh_token_ttl", "max_output_chars"):
        if key in raw:
            setattr(cfg, key, int(raw[key]))
    return cfg
