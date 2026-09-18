#!/usr/bin/env python3
from __future__ import annotations
import os, sqlite3, sys
from pathlib import Path
from mcp_studio.settings import load_settings

def die(msg: str) -> None:
    print(f"PRODUCTION_PREFLIGHT_FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)

cfg = os.environ.get("MCP_STUDIO_CONFIG")
if not cfg:
    die("MCP_STUDIO_CONFIG is not set")
s = load_settings(cfg)
st = s.studio
if not st.production_mode:
    die("production_mode=false")
if not st.operations_enabled:
    die("operations_enabled=false")
for env_name in (st.gateway_token_env, st.oauth_signing_secret_env, st.oauth_owner_token_env):
    if not os.environ.get(env_name, "").strip():
        die(f"missing required secret environment variable: {env_name}")
if not st.oauth_resource_url.startswith("https://"):
    die("oauth_resource_url must use https")
if st.oauth_resource_server_id not in {None, st.worker_server_id}:
    die("oauth_resource_server_id must match worker_server_id for this deployment")

if st.managed_session_enabled:
    serena_exe = Path(st.managed_session_serena_executable).expanduser()
    serena_cwd = Path(st.managed_session_serena_working_directory).expanduser()
    if not serena_exe.is_file() or not os.access(serena_exe, os.X_OK):
        die(f"managed Serena executable is missing/not executable: {serena_exe}")
    if not serena_cwd.is_dir():
        die(f"managed Serena working directory is missing: {serena_cwd}")
    if not st.managed_session_workspace_roots:
        die("managed sessions enabled without approved workspace roots")
    for value in st.managed_session_workspace_roots:
        root = Path(value).expanduser()
        if not root.is_dir():
            die(f"managed workspace root is missing: {root}")
        if not os.access(root, os.R_OK | os.X_OK):
            die(f"managed workspace root is not readable/traversable: {root}")

db_path = Path(st.database)
if not db_path.is_absolute():
    db_path = Path(cfg).resolve().parent / db_path
db_path.parent.mkdir(parents=True, exist_ok=True)
if db_path.exists():
    try:
        con = sqlite3.connect(db_path)
        result = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()
    except Exception as exc:
        die(f"database integrity check failed: {exc}")
    if result != "ok":
        die(f"database integrity_check returned {result!r}")
print("PRODUCTION_PREFLIGHT_PASS")
print(f"config={Path(cfg).resolve()}")
print(f"database={db_path.resolve()}")
print(f"issuer={st.oauth_issuer}")
print(f"resource={st.oauth_resource_url}")
