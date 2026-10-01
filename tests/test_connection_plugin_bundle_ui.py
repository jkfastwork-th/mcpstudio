from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_portable_package_is_additive_to_existing_oauth_clients_workflow():
    html = (ROOT / "templates/index.html").read_text()
    oauth_controls = (
        'id="oauthClientForm"',
        'id="oauthServerUrl"',
        'id="oauthGenerateApprovalBtn"',
        'id="oauthClientName"',
        'id="oauthRedirectUri"',
        'id="oauthCreateBtn"',
        'id="oauthSetupCard"',
        'id="oauthClientList"',
        'id="oauthRefreshBtn"',
    )
    for control in oauth_controls:
        assert control in html
    section = html[html.index('data-page="connections"'):html.index('data-page="settings"')]
    assert section.index('id="oauthSetupCard"') < section.index('connection-plugin-card')
    assert section.index('connection-plugin-card') < section.index('id="oauthClientList"')
    for field, default in (
        ("pluginPackageName", 'value="hirda"'),
        ("pluginPackageVersion", 'value="1.0.0"'),
        ("pluginPackageDescription", "name=\"description\""),
        ("pluginMcpUrl", "name=\"mcp_url\""),
    ):
        assert f'id="{field}"' in section
        assert default in section
    assert 'data-plugin-format="zip"' in section
    assert 'data-plugin-format="tar.gz"' in section
    assert 'id="pluginZipBtn" type="button" disabled' in section
    assert "ZIP is recommended" in section
    assert "credentials are never included" in section


def test_portable_package_has_download_wiring_translations_and_responsive_styles():
    js = (ROOT / "static/app.js").read_text()
    assert "status.resource&&!packageUrl.dataset.userEdited" in js
    assert "'/api/connections/plugin-bundle'" in js
    assert "response.blob()" in js
    assert 'headers.get(\'Content-Disposition\')' in js
    assert "ดาวน์โหลด ZIP" in js and "ดาวน์โหลด tar.gz" in js
    css = (ROOT / "static/styles.css").read_text()
    assert ".connection-plugin-form" in css
    assert "@media(max-width:620px)" in css
    assert ".connection-plugin-actions .button{width:100%}" in css
