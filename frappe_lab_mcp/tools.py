"""The MCP tools. Every tool is scoped to the one bench/site in the config."""

from __future__ import annotations

import functools
import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import policy
from .config import Config

RUNNER = Path(__file__).with_name("runner.py")
MARKER = "__LABMCP_RESULT__:"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", "dist", ".mypy_cache"}

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def clip(text: str, limit: int) -> str:
    """Keep the head and (mostly) the tail -- errors are usually at the end."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    return f"{text[:head]}\n... [{len(text) - limit} chars omitted] ...\n{text[-tail:]}"


class Audit:
    def __init__(self, cfg: Config):
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(cfg.state_dir, 0o700)
        self.path = cfg.state_dir / "audit.jsonl"
        self.lock = threading.Lock()

    def write(self, entry: dict):
        line = json.dumps(entry, default=str)
        with self.lock, open(self.path, "a") as f:
            f.write(line + "\n")
        os.chmod(self.path, 0o600)


def _short_args(kwargs: dict) -> dict:
    out = {}
    for k, v in kwargs.items():
        s = v if isinstance(v, str) else json.dumps(v, default=str)
        out[k] = s if len(s) <= 500 else s[:500] + f"...[{len(s)} chars]"
    return out


class BenchJobs:
    """Long bench commands (migrate, build, get-app) outlive a single tool call."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def running(self) -> dict | None:
        with self.lock:
            return next((j for j in self.jobs.values() if j["proc"].poll() is None), None)

    def start(self, argv: list[str], cwd: Path, log_dir: Path) -> dict:
        job_id = uuid.uuid4().hex[:8]
        log_path = log_dir / f"bench-{job_id}.log"
        log = os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w")
        proc = subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=_env(), start_new_session=True)
        job = {"id": job_id, "argv": argv, "proc": proc, "log": log_path, "started": time.time()}
        with self.lock:
            self.jobs[job_id] = job
        return job

    def get(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            raise ToolError(f"No bench job '{job_id}'. Known: {', '.join(self.jobs) or 'none'}")
        return job


_PATH_PREFIX: list[str] = []


def _node_bin() -> str | None:
    """Newest nvm-installed node for this user (supervisor does not load nvm)."""
    versions = Path("~/.nvm/versions/node").expanduser()
    if not versions.is_dir():
        return None

    def key(p: Path):
        return tuple(int(x) for x in p.name.lstrip("v").split(".") if x.isdigit())

    candidates = sorted((p for p in versions.iterdir() if (p / "bin" / "node").exists()), key=key)
    return str(candidates[-1] / "bin") if candidates else None


def configure_env(cfg: Config) -> None:
    _PATH_PREFIX[:] = [*cfg.extra_path, str(cfg.bench_path / "env" / "bin")]
    node = _node_bin()
    if node:
        _PATH_PREFIX.insert(len(cfg.extra_path), node)


def _env() -> dict:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("VIRTUAL_ENV", None)
    env["PATH"] = os.pathsep.join([*_PATH_PREFIX, env.get("PATH", "/usr/local/bin:/usr/bin:/bin")])
    return env


def register_tools(mcp: MCPServer, cfg: Config) -> None:
    configure_env(cfg)
    audit = Audit(cfg)
    jobs = BenchJobs()
    limit = cfg.max_output_chars

    def tool(annotations: ToolAnnotations):
        """mcp.tool + audit logging of every call."""
        def deco(fn):
            @functools.wraps(fn)
            def wrapper(**kwargs):
                started = time.time()
                token = get_access_token()
                entry = {"ts": started, "user": cfg.user, "subject": getattr(token, "subject", None),
                         "client": getattr(token, "client_id", None), "tool": fn.__name__,
                         "args": _short_args(kwargs)}
                try:
                    result = fn(**kwargs)
                    entry["ok"] = True
                    return result
                except Exception as e:
                    entry["ok"] = False
                    entry["error"] = f"{type(e).__name__}: {e}"[:1000]
                    raise
                finally:
                    entry["ms"] = int((time.time() - started) * 1000)
                    audit.write(entry)
            return mcp.tool(annotations=annotations)(wrapper)
        return deco

    def run(argv: list[str], cwd: Path | None = None, timeout: int = 120) -> tuple[int, str]:
        try:
            res = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                                 env=_env(), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or "") if isinstance(e.stdout, str) else (e.stdout or b"").decode(errors="replace")
            raise ToolError(f"Timed out after {timeout}s.\n{clip(out, limit)}") from None
        return res.returncode, (res.stdout or "") + (res.stderr or "")

    def frappe_call(action: str, timeout: int = 300, **payload: Any) -> dict:
        req = {"action": action, "site": cfg.site, "writable_apps": policy.writable_apps(cfg), **payload}
        try:
            res = subprocess.run([str(cfg.bench_python), str(RUNNER)], cwd=cfg.sites_path,
                                 input=json.dumps(req), capture_output=True, text=True,
                                 timeout=timeout, env=_env())
        except subprocess.TimeoutExpired:
            raise ToolError(f"Frappe call timed out after {timeout}s (transaction rolled back).") from None
        line = next((l for l in reversed(res.stdout.split("\n")) if l.startswith(MARKER)), None)
        if line is None:
            raise ToolError(f"Runner failed (exit {res.returncode}):\n{clip(res.stdout + res.stderr, limit)}")
        out = json.loads(line[len(MARKER):])
        if not out.get("ok"):
            msg = out.get("error") or "error"
            tb = out.get("traceback") or ""
            extra = "\n".join(filter(None, [out.get("stdout"), *(out.get("messages") or [])]))
            raise ToolError(clip(f"{msg}\n{tb}\n{extra}".strip(), limit))
        return out

    # ------------------------------------------------------------------ orientation

    @tool(READ)
    def lab_info() -> dict:
        """Overview of this lab: bench, site, every app with its git branch, whether it is
        writable right now, and uncommitted-change count. Call this first."""
        apps = []
        for app_dir in sorted(p for p in cfg.apps_path.iterdir() if p.is_dir()):
            app = app_dir.name
            branch = policy.current_branch(cfg, app) if (app_dir / ".git").exists() else ""
            _, status = run(["git", "-C", str(app_dir), "status", "--porcelain"], timeout=20)
            apps.append({
                "app": app, "branch": branch,
                "writable": app in cfg.writable and branch in cfg.writable[app],
                "allowed_branches": cfg.writable.get(app, []),
                "uncommitted_changes": len([l for l in status.splitlines() if l.strip()]),
            })
        installed = (cfg.sites_path / "apps.txt").read_text().split()
        return {"user": cfg.user, "bench": str(cfg.bench_path), "site": cfg.site,
                "site_url": f"https://{cfg.site}", "apps": apps, "apps_in_bench_order": installed,
                "bench_commands": sorted(cfg.bench_commands)}

    # ------------------------------------------------------------------ reading code

    @tool(READ)
    def list_dir(
        path: Annotated[str, Field(description="Relative to apps/, e.g. 'safety' or 'safety/safety/doctype'")],
        depth: Annotated[int, Field(ge=1, le=4)] = 1,
    ) -> str:
        """List a directory inside apps/ (skips .git, node_modules, __pycache__)."""
        _, root = policy.resolve_app_path(cfg, path)
        if not root.is_dir():
            raise ToolError(f"Not a directory: {path}")
        lines: list[str] = []

        def walk(d: Path, level: int):
            for p in sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
                if p.name in SKIP_DIRS:
                    continue
                lines.append("  " * (level - 1) + p.name + ("/" if p.is_dir() else ""))
                if len(lines) > 1500:
                    return
                if p.is_dir() and level < depth:
                    walk(p, level + 1)

        walk(root, 1)
        return clip("\n".join(lines), limit)

    @tool(READ)
    def read_file(
        path: Annotated[str, Field(description="Relative to apps/, e.g. 'safety/safety/hooks.py'")],
        start_line: Annotated[int, Field(ge=1)] = 1,
        max_lines: Annotated[int, Field(ge=1, le=2000)] = 400,
    ) -> str:
        """Read a file from any app. Output lines are prefixed 'N|' (line number) -- strip that
        prefix when quoting text back to edit_file."""
        _, target = policy.resolve_app_path(cfg, path)
        if not target.is_file():
            raise ToolError(f"Not a file: {path}")
        if target.stat().st_size > policy.MAX_READ_BYTES:
            raise ToolError("File is larger than 2 MB; use search_code instead.")
        data = target.read_bytes()
        if b"\x00" in data[:8192]:
            raise ToolError("Binary file.")
        all_lines = data.decode(errors="replace").splitlines()
        chunk = all_lines[start_line - 1:start_line - 1 + max_lines]
        body = "\n".join(f"{i}|{l}" for i, l in enumerate(chunk, start_line))
        end = start_line + len(chunk) - 1
        header = f"{path} (lines {start_line}-{end} of {len(all_lines)})"
        return clip(f"{header}\n{body}", max(limit, 60000))

    @tool(READ)
    def search_code(
        pattern: Annotated[str, Field(description="Regex (ripgrep syntax)")],
        path: Annotated[str, Field(description="App or folder relative to apps/; empty = all apps")] = "",
        glob: Annotated[str, Field(description="Optional file glob, e.g. '*.py' or '*.js'")] = "",
        ignore_case: bool = False,
        max_results: Annotated[int, Field(ge=1, le=500)] = 80,
    ) -> str:
        """Search code across apps with ripgrep. Returns 'path:line:text' relative to apps/."""
        target = cfg.apps_path if not path.strip() else policy.resolve_app_path(cfg, path)[1]
        argv = ["rg", "-n", "--no-heading", "--color=never", "--max-columns=240",
                "--max-columns-preview", "-g", "!node_modules", "-g", "!*.min.js", "-g", "!dist",
                "-g", "!*.map", "-g", "!.git"]
        if ignore_case:
            argv.append("-i")
        if glob:
            argv += ["-g", glob]
        argv += ["-e", pattern, str(target)]
        code, out = run(argv, cwd=cfg.apps_path, timeout=60)
        if code == 1:
            return "No matches."
        if code not in (0, 1):
            raise ToolError(clip(out, 2000))
        prefix = str(cfg.apps_path) + "/"
        lines = [l.replace(prefix, "", 1) for l in out.splitlines()]
        more = len(lines) - max_results
        text = "\n".join(lines[:max_results])
        return text + (f"\n... {more} more matches; narrow the search." if more > 0 else "")

    # ------------------------------------------------------------------ writing code

    @tool(WRITE)
    def write_file(
        path: Annotated[str, Field(description="Relative to apps/, inside a writable app")],
        content: str,
    ) -> str:
        """Create or overwrite a file in a writable app (on an allowed branch). Not for Frappe
        document JSON (DocType/Workspace/Report/fixtures) -- use save_document for those."""
        _, target = policy.assert_path_writable(cfg, path, content)
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"{'Updated' if existed else 'Created'} {path} ({len(content.splitlines())} lines)."

    @tool(WRITE)
    def edit_file(
        path: Annotated[str, Field(description="Relative to apps/, inside a writable app")],
        old_string: Annotated[str, Field(description="Exact text to replace (no 'N|' line prefixes)")],
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        """Replace an exact string in a file. old_string must match exactly once unless
        replace_all is true. Prefer this over write_file for small changes."""
        _, target = policy.assert_path_writable(cfg, path)
        if not target.is_file():
            raise ToolError(f"Not a file: {path}")
        text = target.read_text()
        count = text.count(old_string)
        if count == 0:
            raise ToolError("old_string not found. Re-read the file and copy the text exactly.")
        if count > 1 and not replace_all:
            raise ToolError(f"old_string matches {count} times; add context or set replace_all.")
        new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        policy.assert_path_writable(cfg, path, new_text)
        target.write_text(new_text)
        return f"Edited {path}: {count if replace_all else 1} replacement(s)."

    @tool(DESTRUCTIVE)
    def delete_file(path: Annotated[str, Field(description="Relative to apps/, inside a writable app")]) -> str:
        """Delete a single file in a writable app (git can restore it if committed)."""
        _, target = policy.assert_path_writable(cfg, path)
        if not target.is_file():
            raise ToolError(f"Not a file: {path}")
        target.unlink()
        return f"Deleted {path}."

    # ------------------------------------------------------------------ Frappe model / data

    @tool(READ)
    def find_doctypes(
        query: Annotated[str, Field(description="Substring of the DocType name")] = "",
        app: str = "",
        module: str = "",
        limit_rows: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict:
        """Find DocTypes by name, app or module on the lab site."""
        return frappe_call("find_doctypes", query=query, app=app, module=module, limit=limit_rows)["result"]

    @tool(READ)
    def get_doctype(doctype: str) -> dict:
        """The live model of a DocType (including custom fields and property setters): fields,
        permissions, naming, links, owning app and the folder holding its controller/JSON.
        Always check this before writing code that touches a DocType."""
        return frappe_call("meta", doctype=doctype, timeout=120)["result"]

    @tool(WRITE)
    def save_document(
        doctype: Annotated[str, Field(description="e.g. 'DocType', 'Custom Field', 'Workspace', 'Report', or any data DocType")],
        values: Annotated[dict, Field(description="Fields to set. For child tables (e.g. DocType.fields) pass the FULL list -- it replaces the existing rows.")],
        name: Annotated[str, Field(description="Existing document name to update, or the name to create")] = "",
    ) -> dict:
        """Create or update any document via the Frappe ORM and commit. This is how DocType /
        Workspace / Report / Print Format changes are made: in developer mode Frappe exports the
        JSON into the owning app itself (with a correct `modified` timestamp). Documents whose
        module belongs to a read-only app are refused."""
        out = frappe_call("save_doc", doctype=doctype, values=values, name=name or None)
        res = out["result"]
        if out.get("messages"):
            res["messages"] = out["messages"]
        return res

    @tool(WRITE)
    def run_python(
        code: Annotated[str, Field(description="Python with `frappe` already connected to the lab site as Administrator")],
        dry_run: Annotated[bool, Field(description="Roll back instead of committing")] = False,
        timeout: Annotated[int, Field(ge=5, le=900)] = 300,
    ) -> dict:
        """Run Python against the lab site (a non-interactive bench console). Prints are captured;
        the value of the last expression (or a variable named `result`) is returned. Commits on
        success, rolls back on error or when dry_run is true."""
        out = frappe_call("exec", code=code, dry_run=dry_run, timeout=timeout)
        return {"value": out["result"]["value"], "stdout": clip(out.get("stdout", ""), limit),
                "messages": out.get("messages") or [], "committed": not dry_run}

    @tool(WRITE)
    def run_sql(
        query: str,
        max_rows: Annotated[int, Field(ge=1, le=2000)] = 200,
        dry_run: bool = False,
    ) -> dict:
        """Run SQL on the lab site's MariaDB database (as the site's DB user). Returns rows as
        dicts. Writes are committed unless dry_run is true."""
        out = frappe_call("sql", query=query, max_rows=max_rows, dry_run=dry_run, timeout=300)
        res = out["result"]
        text = json.dumps(res, default=str)
        if len(text) > limit * 3:
            res = {"rowcount": res["rowcount"], "truncated": True,
                   "rows_json_clipped": clip(text, limit * 3), "hint": "select fewer columns/rows"}
        return res

    # ------------------------------------------------------------------ bench

    @tool(WRITE)
    def bench(
        command: Annotated[str, Field(description="e.g. 'migrate', 'build --app safety', 'run-tests --app safety --module safety.safety.doctype.x.test_x', 'restart', 'clear-cache'. Never pass --site; it is fixed.")],
        wait_seconds: Annotated[int, Field(ge=0, le=280, description="How long to wait before returning a job id to poll")] = 120,
    ) -> dict:
        """Run an allowlisted bench command against this lab. Long commands keep running in the
        background: if not finished within wait_seconds, poll with bench_job."""
        try:
            args = shlex.split(command)
        except ValueError as e:
            raise ToolError(f"Could not parse command: {e}") from None
        if args and args[0] == "bench":
            args = args[1:]
        if any(a == "--site" or a.startswith("--site=") for a in args):
            raise ToolError("Do not pass --site; this lab's site is injected automatically.")
        two = " ".join(args[:2])
        if two in cfg.bench_commands:
            sub, rest, site_scoped = args[:2], args[2:], cfg.bench_commands[two]
        elif args and args[0] in cfg.bench_commands:
            sub, rest, site_scoped = args[:1], args[1:], cfg.bench_commands[args[0]]
        else:
            raise ToolError(f"Not allowed. Allowed bench commands: {', '.join(sorted(cfg.bench_commands))}")
        if sub[0] == "export-fixtures":
            if "--app" not in rest:
                raise ToolError("export-fixtures needs --app <writable app>.")
            policy.assert_app_writable(cfg, rest[rest.index("--app") + 1])
        argv = [str(cfg.bench_bin)] + (["--site", cfg.site] if site_scoped else []) + sub + rest

        busy = jobs.running()
        if busy:
            raise ToolError(f"Bench job {busy['id']} ({' '.join(busy['argv'][1:])}) is still running; "
                            f"poll it with bench_job first.")
        job = jobs.start(argv, cfg.bench_path, cfg.state_dir)
        return _job_status(job, wait_seconds)

    @tool(READ)
    def bench_job(job_id: str, wait_seconds: Annotated[int, Field(ge=0, le=280)] = 60) -> dict:
        """Poll a background bench command started by the bench tool."""
        return _job_status(jobs.get(job_id), wait_seconds)

    def _job_status(job: dict, wait_seconds: int) -> dict:
        try:
            job["proc"].wait(timeout=wait_seconds)
        except subprocess.TimeoutExpired:
            pass
        code = job["proc"].poll()
        log = job["log"].read_text(errors="replace")
        return {"job_id": job["id"], "command": " ".join(job["argv"][1:]),
                "status": "running" if code is None else ("ok" if code == 0 else "failed"),
                "exit_code": code, "elapsed_s": int(time.time() - job["started"]),
                "output": clip(log, limit)}

    # ------------------------------------------------------------------ git

    @tool(WRITE)
    def git(
        app: str,
        action: Literal["status", "diff", "log", "fetch", "pull", "add", "commit", "push", "checkout", "branches"],
        paths: Annotated[list[str], Field(description="add/diff: paths relative to the app root; empty = all")] = [],
        message: Annotated[str, Field(description="commit message")] = "",
        branch: Annotated[str, Field(description="checkout: an allowed branch")] = "",
        staged: Annotated[bool, Field(description="diff: show staged changes")] = False,
    ) -> str:
        """Git for one app. status/diff/log/fetch/branches work on any app. pull/add/commit/push/
        checkout only on writable apps, only on allowed branches; push never forces and only
        pushes the current branch to its same-named upstream branch."""
        app_dir = cfg.apps_path / app
        if not (app_dir / ".git").exists():
            raise ToolError(f"'{app}' is not a git checkout in this bench.")
        g = ["git", "-C", str(app_dir)]

        if action == "status":
            return run(g + ["status", "-sb"])[1] or "clean"
        if action == "diff":
            code, out = run(g + ["diff"] + (["--staged"] if staged else []) + ["--"] + paths)
            return clip(out, limit * 2) or "No changes."
        if action == "log":
            return run(g + ["log", "--oneline", "--decorate", "-n", "25"])[1]
        if action == "fetch":
            return run(g + ["fetch", "--all", "--prune"], timeout=180)[1] or "Fetched."
        if action == "branches":
            return run(g + ["branch", "-a", "-vv", "--no-color"])[1]

        if action == "checkout":
            if branch not in cfg.writable.get(app, []):
                raise ToolError(f"Branch '{branch}' is not allowed for {app}. Allowed: {cfg.writable.get(app, [])}")
            if run(g + ["status", "--porcelain"])[1].strip():
                raise ToolError("Working tree has uncommitted changes; commit them first.")
            code, out = run(g + ["checkout", branch], timeout=60)
            if code:
                raise ToolError(out)
            return out + "\nRun bench migrate if DocTypes differ between branches."

        current = policy.assert_app_writable(cfg, app)
        if action == "pull":
            code, out = run(g + ["pull", "--no-rebase", "--no-edit"], timeout=300)
            if code:
                raise ToolError(clip(out, limit))
            return out
        if action == "add":
            code, out = run(g + ["add", "-A", "--"] + (paths or ["."]))
            if code:
                raise ToolError(out)
            return run(g + ["status", "-sb"])[1]
        if action == "commit":
            if not message.strip():
                raise ToolError("A commit message is required.")
            code, out = run(g + ["commit", "-m", message])
            if code:
                raise ToolError(out)
            return out
        if action == "push":
            remote = run(g + ["config", f"branch.{current}.remote"])[1].strip() or "origin"
            code, out = run(g + ["push", remote, f"HEAD:refs/heads/{current}"], timeout=300)
            if code:
                raise ToolError(clip(out, limit))
            return out or "Pushed."
        raise ToolError(f"Unknown action {action}")
