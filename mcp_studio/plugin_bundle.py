"""Generate portable Agent Plugins archives from the checked-in HIRDA package."""

from __future__ import annotations

import gzip
import io
import json
import re
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_SOURCE = ROOT / "packaging" / "chatgpt" / "hirda"
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
SKILL_PATH = "skills/hirda/SKILL.md"
EXPECTED_FILES = {"plugin.json", "mcp.json", SKILL_PATH}
ALLOWED_FIELDS = {"plugin_name", "version", "description", "mcp_url", "format"}
REQUIRED_FIELDS = ALLOWED_FIELDS - {"description"}
DEFAULT_DESCRIPTION = "Connect ChatGPT to HIRDA tools and usage guidance."
NAME_RE = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
SECRET_RE = re.compile(
    r"(?:authorization\s*[:=]|bearer\s+\S+|(?:client[_-]?secret|access[_-]?token|refresh[_-]?token|owner[_-]?token|oauth[_-]?token|signing[_-]?secret|api[_-]?key|password)\s*[:=])",
    re.IGNORECASE,
)
router = APIRouter()


def validate_plugin_request(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")
    unknown = set(payload) - ALLOWED_FIELDS
    missing = REQUIRED_FIELDS - set(payload)
    if unknown:
        raise ValueError("Only non-secret plugin metadata, mcp_url, and format are accepted.")
    if missing:
        raise ValueError("Required fields are plugin_name, version, mcp_url, and format.")
    if any(not isinstance(value, str) for value in payload.values()):
        raise ValueError("All accepted fields must be text.")
    values = {key: value.strip() for key, value in payload.items()}
    description = values.get("description") or DEFAULT_DESCRIPTION
    values["description"] = description
    if len(values["plugin_name"]) > 64 or not NAME_RE.fullmatch(values["plugin_name"]):
        raise ValueError("Package name must use lowercase letters, numbers, dots, or single hyphens.")
    if not VERSION_RE.fullmatch(values["version"]):
        raise ValueError("Version must be a short version string using letters, numbers, dots, plus, or hyphens.")
    if len(description) > 4000 or SECRET_RE.search(description):
        raise ValueError("Description is too long or contains credential-like text.")
    if values["format"] not in {"zip", "tar.gz"}:
        raise ValueError("Format must be zip or tar.gz.")
    url = values["mcp_url"]
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("MCP URL is invalid.") from exc
    if (
        not url
        or len(url) > 2048
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or re.search(r"(?:^|/)(?:token|secret|client[-_]?secret|access[-_]?token|refresh[-_]?token|owner[-_]?token|credential|api[-_]?key)(?:/|$)", parsed.path, re.IGNORECASE)
    ):
        raise ValueError("MCP URL must be an HTTPS address without credentials, query parameters, or secret-like paths.")
    return values


def _package_files(plugin_name: str, version: str, description: str, mcp_url: str) -> dict[str, bytes]:
    plugin = {
        "$schema": PLUGIN_SCHEMA,
        "name": plugin_name,
        "version": version,
        "description": description,
    }
    mcp = {
        "$schema": MCP_SCHEMA,
        "mcpServers": {"hirda": {"type": "streamable-http", "url": mcp_url}},
    }
    skill_path = PACKAGE_SOURCE / SKILL_PATH
    if not skill_path.is_file():
        raise FileNotFoundError(f"Missing portable HIRDA skill: {skill_path}")
    return {
        "plugin.json": (json.dumps(plugin, indent=2, ensure_ascii=False) + "\n").encode(),
        "mcp.json": (json.dumps(mcp, indent=2, ensure_ascii=False) + "\n").encode(),
        SKILL_PATH: skill_path.read_bytes(),
    }


def _archive(files: dict[str, bytes], archive_format: str) -> bytes:
    if set(files) != EXPECTED_FILES:
        raise ValueError("The archive must contain exactly plugin.json, mcp.json, and skills/hirda/SKILL.md.")
    buffer = io.BytesIO()
    if archive_format == "zip":
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, content in files.items():
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                bundle.writestr(info, content)
    elif archive_format == "tar.gz":
        with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as bundle:
                for name, content in files.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    info.mtime = 1577836800
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mode = 0o644
                    bundle.addfile(info, io.BytesIO(content))
    else:
        raise ValueError("Format must be zip or tar.gz.")
    return buffer.getvalue()


def build_plugin_archive(payload: object) -> tuple[bytes, str, str]:
    values = validate_plugin_request(payload)
    files = _package_files(
        values["plugin_name"], values["version"], values["description"], values["mcp_url"]
    )
    extension = "zip" if values["format"] == "zip" else "tar.gz"
    filename = f"{values['plugin_name']}-chatgpt-{values['version']}.{extension}"
    return _archive(files, values["format"]), filename, values["format"]


@router.post("/api/connections/plugin-bundle")
async def download_chatgpt_plugin_bundle(request: Request):
    try:
        archive, filename, archive_format = build_plugin_archive(await request.json())
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(
        content=archive,
        media_type="application/zip" if archive_format == "zip" else "application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
