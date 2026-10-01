import hashlib
import json
import pathlib
import subprocess
import tarfile
import zipfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packaging/chatgpt/hirda"
ENDPOINT = "https://serena.jkfast.work/mcp/serena-8001"
EXPECTED_FILES = {"plugin.json", "mcp.json", "skills/hirda/SKILL.md"}


def test_manifest_and_mcp_endpoint_structure():
    plugin = json.loads((PACKAGE / "plugin.json").read_text())
    mcp = json.loads((PACKAGE / "mcp.json").read_text())

    assert plugin["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert plugin["name"] == "hirda"
    assert plugin["version"] == "1.0.0"
    assert mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert set(mcp) == {"$schema", "mcpServers"}
    assert set(mcp["mcpServers"]) == {"hirda"}
    assert mcp["mcpServers"]["hirda"] == {"type": "streamable-http", "url": ENDPOINT}


def test_package_hygiene():
    assert {p.relative_to(PACKAGE).as_posix() for p in PACKAGE.rglob("*") if p.is_file()} == EXPECTED_FILES
    text = "\n".join(path.read_text() for path in PACKAGE.rglob("*") if path.is_file())
    lowered = text.lower()
    for marker in ("authorization: bearer", "client_secret", "access_token", "refresh_token", "api_key"):
        assert marker not in lowered
    assert "oauth" in (PACKAGE / "skills/hirda/SKILL.md").read_text().lower()


def test_build_outputs_and_clean_archive_layout():
    subprocess.run([str(ROOT / "scripts/build-chatgpt-plugin.sh")], cwd=ROOT, check=True)
    dist = ROOT / "dist"
    zip_path = dist / "hirda-chatgpt-1.0.0.zip"
    tar_path = dist / "hirda-chatgpt-1.0.0.tar.gz"
    sums = dist / "SHA256SUMS"
    assert zip_path.is_file() and tar_path.is_file() and sums.is_file()
    with zipfile.ZipFile(zip_path) as archive:
        assert set(archive.namelist()) == EXPECTED_FILES
        assert json.loads(archive.read("mcp.json"))["mcpServers"]["hirda"]["url"] == ENDPOINT
    with tarfile.open(tar_path, "r:gz") as archive:
        assert {member.name for member in archive.getmembers()} == EXPECTED_FILES
        assert json.load(archive.extractfile("plugin.json"))["name"] == "hirda"
    checksum_lines = sums.read_text().splitlines()
    assert len(checksum_lines) == 2
    expected_hashes = {
        zip_path.name: hashlib.sha256(zip_path.read_bytes()).hexdigest(),
        tar_path.name: hashlib.sha256(tar_path.read_bytes()).hexdigest(),
    }
    assert {line.split()[1].lstrip("*"): line.split()[0] for line in checksum_lines} == expected_hashes
