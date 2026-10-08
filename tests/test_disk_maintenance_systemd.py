from pathlib import Path


def test_codex_maintenance_systemd_override_is_exact_and_scoped():
    source = Path(__file__).resolve().parents[1] / "systemd" / "mcp-studio-disk-maintenance.conf"
    content = source.read_text()
    directives = [
        line.strip() for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and not line.startswith("[")
    ]
    assert content.startswith("[Service]\n")
    assert len(directives) == 1
    assert directives == [
        "ReadWritePaths=/home/alfred/.codex/packages/standalone/releases "
        "/home/alfred/.codex/packages/app-server-daemon/releases"
    ]
    for prohibited in (
        "ProtectHome=false",
        "ProtectSystem=false",
        "ReadWritePaths=/home/alfred ",
        "ReadWritePaths=/home/alfred/.codex ",
        "ExecStart=",
        "NoNewPrivileges=false",
    ):
        assert prohibited not in content
