#!/usr/bin/env python3
"""Interactive, owner-only OpenClaw disk maintenance client.

Only targets Codex old releases. This never accepts arbitrary paths/commands.
The owner token is read from the local terminal or an existing environment
variable, never included in the command line or printed.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from shutil import disk_usage
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


URL = "http://127.0.0.1:8100"
TARGET = "codex.old_releases"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        return None


def api(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    headers = {"X-HIRDA-OWNER-TOKEN": token, "Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(URL + path, data=body, method=method, headers=headers)
    try:
        with build_opener(_NoRedirect()).open(req, timeout=90) as response:
            return json.load(response)
    except HTTPError as exc:
        # Do not include the auth token or request headers in errors.
        raise RuntimeError(f"Maintenance API {path} rejected request (HTTP {exc.code})") from exc
    except URLError as exc:
        raise RuntimeError(f"Local HIRDA API unreachable: {exc.reason}") from exc


def free_gib() -> float:
    return disk_usage(Path.home()).free / (1024 ** 3)


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenClaw owner-approved disk cleanup")
    parser.add_argument("--apply", action="store_true",
                        help="Allow owner to confirm and execute selected Codex cleanup")
    args = parser.parse_args()
    token = os.environ.get("MCP_STUDIO_OAUTH_OWNER_TOKEN", "")
    if not token:
        token = getpass.getpass("HIRDA owner token (not echoed): ")
    if not token:
        print("No token supplied; no changes made.")
        return 2

    try:
        status = api("GET", "/api/maintenance/disk/status", token)
        if status.get("machine_id") != "openclaw" or not status.get("machine_local"):
            raise RuntimeError("This command can only run on local OpenClaw")
        if not status.get("enabled") or not status.get("maintenance_permission"):
            raise RuntimeError("Owner-approved maintenance feature/policy is not active yet")
        before = free_gib()
        plan = api("POST", "/api/maintenance/disk/plan", token,
                   {"targets": [TARGET]})
        candidates = plan["candidates"]
        if plan.get("dry_run") is not True or any(
            not str(item.get("id", "")).startswith("codex:") for item in candidates
        ):
            raise RuntimeError("Server returned a plan outside the requested Codex scope")
        print(f"Free before: {before:.2f} GiB")
        print(f"Proposed cleanup: {len(candidates)} old Codex release directories")
        for c in candidates:
            print(f"  {c['id']}: {c['bytes'] / 1024**3:.2f} GiB")
        print(f"Estimated bytes in files: {plan['estimated_bytes'] / 1024**3:.2f} GiB")
        print(f"Plan fingerprint: {plan['fingerprint']}")
        print("Skipped candidates are always preserved:")
        for c in plan["skipped"]:
            print(f"  {c['id']}: {c['reason']}")
        if not candidates or not args.apply:
            print("DRY RUN ONLY: No files deleted.")
            return 0
        digest = plan["fingerprint"][:12]
        challenge = "APPROVE " + digest
        if input(f"To approve exactly this plan, type {challenge}: ").strip() != challenge:
            print("Approval not given; no files deleted.")
            return 2
        # Server-side approval and apply are single-use, time-limited.
        approval = api("POST", "/api/maintenance/disk/approve", token, {
            "plan_id": plan["plan_id"], "fingerprint": plan["fingerprint"],
        })
        result = api("POST", "/api/maintenance/disk/apply", token, {
            "plan_id": plan["plan_id"],
            "approval_token": approval["approval_token"],
        })
        after = free_gib()
        print("Completed targets:", len(result["completed"]))
        print(f"Free space: {before:.2f} GiB -> {after:.2f} GiB")
        print(f"Measured space reclaimed: {after - before:.2f} GiB")
        return 0
    except (RuntimeError, ValueError, KeyError) as exc:
        print(f"Maintenance stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
