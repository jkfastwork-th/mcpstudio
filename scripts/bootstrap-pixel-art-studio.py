#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".hirda-cache" / "pixel-art-studio"
META = CACHE / ".hirda-upstream.json"
UPSTREAM_COMMIT = "f8c246635c4621a6c2b427833149afdc3dc3c719"
TARBALL = (
    "https://codeload.github.com/Gamezxz/pixel-art-studio/tar.gz/"
    + UPSTREAM_COMMIT
)


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> Path:
    members = archive.getmembers()
    roots: set[str] = set()
    dest_resolved = destination.resolve()
    for member in members:
        if member.issym() or member.islnk():
            raise RuntimeError("upstream archive contains symlink/hardlink")
        name = member.name.replace("\\", "/")
        first = name.split("/", 1)[0]
        if first:
            roots.add(first)
        target = (destination / name).resolve()
        if target != dest_resolved and dest_resolved not in target.parents:
            raise RuntimeError("upstream archive path traversal")
    if len(roots) != 1:
        raise RuntimeError("unexpected upstream archive layout")
    archive.extractall(destination, filter="data")
    return destination / next(iter(roots))


def main() -> int:
    commit = UPSTREAM_COMMIT
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hirda-pixel-art-studio-") as td:
        tmp = Path(td)
        archive_path = tmp / "source.tar.gz"
        request = urllib.request.Request(TARBALL, headers={"User-Agent": "HIRDA-P8/1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            archive_path.write_bytes(response.read())

        extracted_root_dir = tmp / "extract"
        extracted_root_dir.mkdir()
        with tarfile.open(archive_path, mode="r:gz") as archive:
            extracted = _safe_extract(archive, extracted_root_dir)

        required = [
            extracted / "scripts" / "pixelstudio.py",
            extracted / "scripts" / "pixelpipe.py",
            extracted / "scripts" / "study.py",
            extracted / "LICENSE",
        ]
        missing = [str(path.relative_to(extracted)) for path in required if not path.is_file()]
        if missing:
            raise RuntimeError("upstream archive missing: " + ", ".join(missing))

        staging = CACHE.parent / ".pixel-art-studio.staging"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(extracted, staging)
        (staging / ".hirda-upstream.json").write_text(
            json.dumps(
                {
                    "schema": "hirda-pixel-art-studio-upstream-v1",
                    "repository": "https://github.com/Gamezxz/pixel-art-studio",
                    "commit": commit,
                    "ref": "main",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if CACHE.exists():
            shutil.rmtree(CACHE)
        staging.rename(CACHE)

    print(json.dumps({"ok": True, "root": str(CACHE), "commit": commit}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
