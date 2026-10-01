#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="$repo_root/packaging/chatgpt/hirda"
dist_dir="$repo_root/dist"
version="1.0.0"
zip_name="hirda-chatgpt-$version.zip"
tar_name="hirda-chatgpt-$version.tar.gz"

required=(plugin.json mcp.json skills/hirda/SKILL.md)
for path in "${required[@]}"; do
  if [[ ! -f "$source_dir/$path" ]]; then
    printf 'Missing required package file: %s\n' "$source_dir/$path" >&2
    exit 1
  fi
done

python3 - "$source_dir" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
plugin = json.loads((root / "plugin.json").read_text())
mcp = json.loads((root / "mcp.json").read_text())
assert plugin["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
assert plugin["name"] == "hirda" and plugin["version"] == "1.0.0"
assert mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
assert set(mcp) == {"$schema", "mcpServers"}
assert set(mcp["mcpServers"]) == {"hirda"}
server = mcp["mcpServers"]["hirda"]
assert server == {
    "type": "streamable-http",
    "url": "https://serena.jkfast.work/mcp/serena-8001",
}
PY

mkdir -p "$dist_dir"
python3 - "$source_dir" "$dist_dir" "$zip_name" "$tar_name" <<'PY'
import gzip
import hashlib
import pathlib
import sys
import tarfile
import zipfile

source = pathlib.Path(sys.argv[1])
dist = pathlib.Path(sys.argv[2])
zip_path = dist / sys.argv[3]
tar_path = dist / sys.argv[4]
files = ("plugin.json", "mcp.json", "skills/hirda/SKILL.md")

for name in (zip_path.name, tar_path.name, "SHA256SUMS"):
    (dist / name).unlink(missing_ok=True)

with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for name in files:
        info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, (source / name).read_bytes())

with tar_path.open("wb") as raw_tar:
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw_tar, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name in files:
                info = tarfile.TarInfo(name)
                content = (source / name).read_bytes()
                info.size = len(content)
                info.mtime = 1577836800
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o644
                import io
                archive.addfile(info, io.BytesIO(content))

with (dist / "SHA256SUMS").open("w") as sums:
    for path in (zip_path, tar_path):
        sums.write(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
PY
printf 'Built %s, %s, and SHA256SUMS\n' "$dist_dir/$zip_name" "$dist_dir/$tar_name"
