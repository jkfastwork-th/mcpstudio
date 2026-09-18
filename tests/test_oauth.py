from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from starlette.requests import Request

from mcp_studio.db import Database
from mcp_studio.gateway import GatewaySessionManager
from mcp_studio.oauth import OAuthManager, OAuthError, _b64u
from mcp_studio.settings import ServerConfig, Settings, StudioConfig


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        studio=StudioConfig(
            database=str(tmp_path / "db.sqlite3"),
            gateway_enabled=True,
            gateway_auth_mode="bearer",
            gateway_token_env="MCP_STUDIO_TEST_GATEWAY_TOKEN",
            gateway_allowed_servers=["serena-8001"],
            worker_server_id="serena-8001",
            oauth_enabled=True,
            oauth_issuer="https://serena.example.test",
            oauth_resource_url="https://serena.example.test/mcp/serena-8001",
            oauth_resource_server_id="serena-8001",
            oauth_signing_secret_env="MCP_STUDIO_TEST_OAUTH_SIGNING",
            oauth_owner_token_env="MCP_STUDIO_TEST_OAUTH_OWNER",
        ),
        servers=[ServerConfig(id="serena-8001", name="Serena", url="http://127.0.0.1:8001/mcp")],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )


def request_with_bearer(token: str) -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/mcp/serena-8001",
        "raw_path": b"/mcp/serena-8001",
        "query_string": b"",
        "headers": [
            (b"authorization", f"Bearer {token}".encode()),
            (b"user-agent", b"ChatGPT-MCP/1.0"),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("serena.example.test", 443),
    })


@pytest.mark.asyncio
async def test_oauth_metadata_client_code_token_refresh(tmp_path: Path):
    os.environ["MCP_STUDIO_TEST_OAUTH_SIGNING"] = "signing-secret-for-tests"
    os.environ["MCP_STUDIO_TEST_OAUTH_OWNER"] = "owner-secret"
    st = make_settings(tmp_path)
    db = Database(st.studio.database)
    await db.init()
    oauth = OAuthManager(st, db)

    meta = oauth.authorization_server_metadata()
    assert meta["issuer"] == "https://serena.example.test"
    assert "offline_access" in meta["scopes_supported"]
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert "none" in meta["token_endpoint_auth_methods_supported"]
    protected = oauth.protected_resource_metadata()
    assert protected["resource"].endswith("/mcp/serena-8001")

    client = await oauth.create_client(
        redirect_uris=["https://chatgpt.com/connector/oauth/test-callback"],
        token_endpoint_auth_method="none",
        scopes=["mcp:serena", "offline_access"],
    )
    assert client["client_secret"] is None

    verifier = "v" * 64
    challenge = _b64u(hashlib.sha256(verifier.encode("ascii")).digest())
    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": client["redirect_uris"][0],
        "scope": "mcp:serena offline_access",
        "state": "state-123",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": oauth.resource,
    }
    target = await oauth.approve_authorization(params, "owner-secret")
    assert target.startswith(client["redirect_uris"][0])
    assert "state=state-123" in target
    code = target.split("code=", 1)[1].split("&", 1)[0]

    token = await oauth.token({
        "grant_type": "authorization_code",
        "client_id": client["client_id"],
        "code": code,
        "redirect_uri": client["redirect_uris"][0],
        "code_verifier": verifier,
    })
    assert token["token_type"] == "Bearer"
    assert token["refresh_token"]
    claims = oauth.verify_access_token(token["access_token"])
    assert claims is not None
    assert claims.client_id == client["client_id"]
    assert "mcp:serena" in claims.scope

    refreshed = await oauth.token({
        "grant_type": "refresh_token",
        "client_id": client["client_id"],
        "refresh_token": token["refresh_token"],
    })
    assert refreshed["refresh_token"] != token["refresh_token"]
    assert oauth.verify_access_token(refreshed["access_token"]) is not None
    with pytest.raises(OAuthError) as exc:
        await oauth.token({
            "grant_type": "refresh_token",
            "client_id": client["client_id"],
            "refresh_token": token["refresh_token"],
        })
    assert exc.value.error == "invalid_grant"


@pytest.mark.asyncio
async def test_owner_approval_and_pkce_are_required(tmp_path: Path):
    os.environ["MCP_STUDIO_TEST_OAUTH_SIGNING"] = "signing-secret-for-tests"
    os.environ["MCP_STUDIO_TEST_OAUTH_OWNER"] = "owner-secret"
    st = make_settings(tmp_path)
    db = Database(st.studio.database)
    await db.init()
    oauth = OAuthManager(st, db)
    client = await oauth.create_client(
        redirect_uris=["https://chatgpt.com/connector/oauth/test"],
        token_endpoint_auth_method="none",
    )
    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": client["redirect_uris"][0],
        "scope": "mcp:serena offline_access",
    }
    with pytest.raises(OAuthError) as exc:
        await oauth.validate_authorization_request(params)
    assert "PKCE" in exc.value.description

    verifier = "x" * 64
    params["code_challenge"] = _b64u(hashlib.sha256(verifier.encode()).digest())
    params["code_challenge_method"] = "S256"
    with pytest.raises(OAuthError) as exc2:
        await oauth.approve_authorization(params, "wrong-owner")
    assert exc2.value.error == "access_denied"


@pytest.mark.asyncio
async def test_gateway_accepts_oauth_access_token_with_connector_identity(tmp_path: Path):
    os.environ["MCP_STUDIO_TEST_OAUTH_SIGNING"] = "signing-secret-for-tests"
    os.environ["MCP_STUDIO_TEST_OAUTH_OWNER"] = "owner-secret"
    os.environ["MCP_STUDIO_TEST_GATEWAY_TOKEN"] = "static-admin-token"
    st = make_settings(tmp_path)
    db = Database(st.studio.database)
    await db.init()
    oauth = OAuthManager(st, db)
    client = await oauth.create_client(
        redirect_uris=["https://chatgpt.com/connector/oauth/test"],
        token_endpoint_auth_method="none",
    )
    token, _ = oauth._issue_access_token(
        client_id=client["client_id"], scope="mcp:serena offline_access", resource=oauth.resource
    )
    manager = GatewaySessionManager(st, db, oauth)
    supplied, client_identity, client_type, scope, source = manager.authenticate(request_with_bearer(token))
    assert supplied == token
    assert client_identity.startswith("oauth-")
    assert client_type == "mcp-http"
    assert scope == "connector"
    assert source == "oauth-client-id"


def test_invalid_bearer_has_protected_resource_challenge(tmp_path: Path):
    os.environ["MCP_STUDIO_TEST_OAUTH_SIGNING"] = "signing-secret-for-tests"
    os.environ["MCP_STUDIO_TEST_GATEWAY_TOKEN"] = "static-admin-token"
    st = make_settings(tmp_path)
    oauth = OAuthManager(st, Database(st.studio.database))
    manager = GatewaySessionManager(st, Database(st.studio.database), oauth)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        manager.authenticate(request_with_bearer("wrong"))
    assert exc.value.status_code == 401
    assert "resource_metadata=" in exc.value.headers["WWW-Authenticate"]



def test_owner_session_cookie_is_signed_bound_and_tamper_evident(tmp_path: Path):
    os.environ["MCP_STUDIO_TEST_OAUTH_SIGNING"] = "signing-secret-for-tests"
    os.environ["MCP_STUDIO_TEST_OAUTH_OWNER"] = "owner-secret"
    st = make_settings(tmp_path)
    db = Database(st.studio.database)
    oauth = OAuthManager(st, db)

    token = oauth.issue_owner_session()
    assert oauth.verify_owner_session(token)

    body, sig = token.split(".", 1)
    tampered_body = ("A" if body[:1] != "A" else "B") + body[1:]
    assert not oauth.verify_owner_session(tampered_body + "." + sig)

    os.environ["MCP_STUDIO_TEST_OAUTH_OWNER"] = "rotated-owner-secret"
    assert not oauth.verify_owner_session(token)
