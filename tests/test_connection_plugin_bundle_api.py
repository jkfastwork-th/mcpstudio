import io
import json
import tarfile
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_studio.plugin_bundle import router


app = FastAPI()
app.include_router(router)
CLIENT = TestClient(app)
BASE = {
    "plugin_name": "hirda",
    "version": "1.0.0",
    "mcp_url": "https://serena.jkfast.work/mcp/serena-8001",
}
MEMBERS = {"plugin.json", "mcp.json", "skills/hirda/SKILL.md"}


@pytest.mark.parametrize("archive_format", ["zip", "tar.gz"])
def test_connection_plugin_bundle_download(archive_format):
    response = CLIENT.post(
        "/api/connections/plugin-bundle",
        json={**BASE, "format": archive_format},
    )
    assert response.status_code == 200
    assert response.headers["content-disposition"].endswith(
        f'filename="hirda-chatgpt-1.0.0.{archive_format}"'
    )
    content = io.BytesIO(response.content)
    if archive_format == "zip":
        with zipfile.ZipFile(content) as bundle:
            assert set(bundle.namelist()) == MEMBERS
            files = {name: bundle.read(name) for name in bundle.namelist()}
    else:
        with tarfile.open(fileobj=content, mode="r:gz") as bundle:
            assert {member.name for member in bundle.getmembers()} == MEMBERS
            files = {name: bundle.extractfile(name).read() for name in MEMBERS}
    plugin = json.loads(files["plugin.json"])
    mcp = json.loads(files["mcp.json"])
    assert plugin["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert plugin["name"] == "hirda" and plugin["version"] == "1.0.0"
    assert mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert mcp["mcpServers"] == {
        "hirda": {"type": "streamable-http", "url": BASE["mcp_url"]}
    }
    combined = b"\n".join(files.values()).lower()
    for forbidden in (b"authorization: bearer", b"client_secret", b"access_token", b"refresh_token", b"api_key"):
        assert forbidden not in combined


@pytest.mark.parametrize(
    "changes",
    [
        {"client_secret": "secret-value"},
        {"description": "Authorization: Bearer secret-value"},
        {"mcp_url": "https://user:password@example.test/mcp"},
        {"mcp_url": "https://example.test/mcp?access_token=secret"},
        {"mcp_url": "http://example.test/mcp"},
        {"format": "tar"},
    ],
)
def test_connection_plugin_bundle_rejects_invalid_or_secret_like_input(changes):
    response = CLIENT.post(
        "/api/connections/plugin-bundle",
        json={**BASE, "format": "zip", **changes},
    )
    assert response.status_code == 422
