from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from .db import Database
from .settings import Settings


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OAuthError(RuntimeError):
    def __init__(self, error: str, description: str, status_code: int = 400):
        self.error = error
        self.description = description
        self.status_code = status_code
        super().__init__(f"{error}: {description}")


@dataclass(slots=True)
class OAuthAccessClaims:
    client_id: str
    scope: str
    resource: str
    subject: str
    expires_at: int
    token_id: str


class OAuthManager:
    """Minimal OAuth 2.1-style facade for ChatGPT custom MCP apps.

    Design goals:
    - Authorization Code + PKCE S256 only for interactive authorization.
    - Refresh-token rotation when ``offline_access`` is granted.
    - Opaque refresh tokens and authorization codes are stored only as SHA-256
      hashes; access tokens are HMAC-signed and self-contained.
    - User-defined OAuth clients are pre-registered locally. No DCR endpoint is
      exposed in M5.3.1.
    - The authorization page requires a separate owner approval token, so a
      public visitor cannot authorize themselves merely by knowing client_id.
    """

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    @property
    def enabled(self) -> bool:
        return bool(self.settings.studio.oauth_enabled)

    @property
    def issuer(self) -> str:
        return self.settings.studio.oauth_issuer.rstrip("/")

    @property
    def resource(self) -> str:
        configured = (self.settings.studio.oauth_resource_url or "").strip()
        if configured:
            return configured.rstrip("/")
        server_id = self.settings.studio.oauth_resource_server_id or self.settings.studio.worker_server_id
        return f"{self.issuer}/mcp/{server_id}"

    @property
    def authorization_endpoint(self) -> str:
        return f"{self.issuer}/oauth/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"{self.issuer}/oauth/token"

    @property
    def protected_resource_metadata_url(self) -> str:
        server_id = self.settings.studio.oauth_resource_server_id or self.settings.studio.worker_server_id
        return f"{self.issuer}/.well-known/oauth-protected-resource/mcp/{server_id}"

    def _signing_secret(self) -> bytes:
        value = os.environ.get(self.settings.studio.oauth_signing_secret_env, "")
        if not value:
            raise OAuthError("server_error", "OAuth signing secret is not configured", 503)
        return value.encode("utf-8")

    def owner_token_configured(self) -> bool:
        return bool(os.environ.get(self.settings.studio.oauth_owner_token_env, ""))

    def verify_owner_token(self, supplied: str) -> bool:
        expected = os.environ.get(self.settings.studio.oauth_owner_token_env, "")
        return bool(expected and supplied and secrets.compare_digest(supplied, expected))

    def _owner_token_fingerprint(self) -> str:
        import hashlib
        expected = os.environ.get(self.settings.studio.oauth_owner_token_env, "")
        if not expected:
            return ""
        return hashlib.sha256(expected.encode("utf-8")).hexdigest()[:24]

    def issue_owner_session(self) -> str:
        """Return a signed browser assertion for recent owner approval.

        The raw owner token is never stored in the cookie. Rotating the owner
        token invalidates every previously issued owner-session assertion.
        """
        import base64
        import hashlib
        import hmac
        import json
        import time

        now = int(time.time())
        ttl = int(self.settings.studio.oauth_owner_session_ttl_seconds)
        payload = {
            "v": 1,
            "purpose": "owner-approval",
            "iat": now,
            "exp": now + ttl,
            "owner_fp": self._owner_token_fingerprint(),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        signed = ("owner-session:" + body).encode("ascii")
        sig = hmac.new(self._signing_secret(), signed, hashlib.sha256).digest()
        sig_text = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
        return f"{body}.{sig_text}"

    def verify_owner_session(self, token: str) -> bool:
        import base64
        import hashlib
        import hmac
        import json
        import time

        if not token or "." not in token:
            return False
        try:
            body, supplied_sig = token.split(".", 1)
            signed = ("owner-session:" + body).encode("ascii")
            expected_sig = hmac.new(self._signing_secret(), signed, hashlib.sha256).digest()
            sig_pad = "=" * (-len(supplied_sig) % 4)
            decoded_sig = base64.urlsafe_b64decode((supplied_sig + sig_pad).encode("ascii"))
            if not secrets.compare_digest(decoded_sig, expected_sig):
                return False

            body_pad = "=" * (-len(body) % 4)
            payload = json.loads(
                base64.urlsafe_b64decode((body + body_pad).encode("ascii")).decode("utf-8")
            )
        except Exception:
            return False

        now = int(time.time())
        owner_fp = self._owner_token_fingerprint()
        return bool(
            owner_fp
            and payload.get("v") == 1
            and payload.get("purpose") == "owner-approval"
            and int(payload.get("iat", 0)) <= now
            and int(payload.get("exp", 0)) >= now
            and secrets.compare_digest(str(payload.get("owner_fp", "")), owner_fp)
        )

    def authorization_server_metadata(self) -> dict[str, Any]:
        scopes = list(dict.fromkeys(self.settings.studio.oauth_scopes_supported))
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
            "scopes_supported": scopes,
        }

    def protected_resource_metadata(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "scopes_supported": list(dict.fromkeys(self.settings.studio.oauth_scopes_supported)),
            "bearer_methods_supported": ["header"],
        }

    async def create_client(
        self,
        *,
        redirect_uris: list[str],
        client_name: str = "ChatGPT MCP Studio",
        token_endpoint_auth_method: str = "none",
        scopes: list[str] | None = None,
        issue_secret: bool = False,
    ) -> dict[str, Any]:
        if token_endpoint_auth_method not in {"none", "client_secret_basic", "client_secret_post"}:
            raise ValueError("unsupported token_endpoint_auth_method")
        if not redirect_uris or any(not x.startswith("https://") for x in redirect_uris):
            raise ValueError("at least one HTTPS redirect URI is required")
        requested_scopes = scopes or list(self.settings.studio.oauth_scopes_supported)
        allowed = set(self.settings.studio.oauth_scopes_supported)
        if not set(requested_scopes).issubset(allowed):
            raise ValueError("client scopes exceed server-supported scopes")
        client_id = "mcpstudio-" + secrets.token_urlsafe(18)
        client_secret = secrets.token_urlsafe(36) if issue_secret or token_endpoint_auth_method != "none" else None
        item = await self.db.create_oauth_client(
            client_id=client_id,
            client_secret_hash=_sha256(client_secret) if client_secret else None,
            client_name=client_name,
            redirect_uris=redirect_uris,
            scopes=requested_scopes,
            token_endpoint_auth_method=token_endpoint_auth_method,
        )
        item["client_secret"] = client_secret
        return item

    async def validate_authorization_request(self, params: dict[str, str]) -> dict[str, Any]:
        response_type = params.get("response_type", "")
        if response_type != "code":
            raise OAuthError("unsupported_response_type", "Only response_type=code is supported")
        client_id = params.get("client_id", "")
        if not client_id:
            raise OAuthError("invalid_request", "client_id is required")
        try:
            client = await self.db.get_oauth_client(client_id)
        except KeyError:
            raise OAuthError("unauthorized_client", "Unknown OAuth client")
        if not client.get("enabled"):
            raise OAuthError("unauthorized_client", "OAuth client is disabled")
        redirect_uri = params.get("redirect_uri", "")
        if redirect_uri not in client["redirect_uris"]:
            raise OAuthError("invalid_request", "redirect_uri is not registered")
        code_challenge = params.get("code_challenge", "")
        method = params.get("code_challenge_method", "")
        if self.settings.studio.oauth_require_pkce:
            if not code_challenge or method != "S256":
                raise OAuthError("invalid_request", "PKCE S256 is required")
        scope_text = (params.get("scope") or " ".join(self.settings.studio.oauth_default_scopes)).strip()
        scopes = [x for x in scope_text.split() if x]
        allowed = set(client["scopes"])
        if not scopes or not set(scopes).issubset(allowed):
            raise OAuthError("invalid_scope", "Requested scope is not allowed for this client")
        resource = (params.get("resource") or self.resource).rstrip("/")
        if resource != self.resource:
            raise OAuthError("invalid_target", "Requested resource does not match MCP Studio resource")
        return {
            "client": client,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": params.get("state", ""),
            "code_challenge": code_challenge,
            "code_challenge_method": method or "S256",
            "resource": resource,
        }

    async def approve_authorization(
        self,
        params: dict[str, str],
        owner_token: str,
        owner_session_token: str = "",
    ) -> str:
        if not (
            self.verify_owner_token(owner_token)
            or self.verify_owner_session(owner_session_token)
        ):
            raise OAuthError("access_denied", "Owner approval is required", 403)
        req = await self.validate_authorization_request(params)
        code = secrets.token_urlsafe(36)
        await self.db.create_oauth_authorization_code(
            code_hash=_sha256(code),
            client_id=req["client_id"],
            redirect_uri=req["redirect_uri"],
            scope=req["scope"],
            code_challenge=req["code_challenge"],
            code_challenge_method=req["code_challenge_method"],
            resource=req["resource"],
            lifetime_seconds=self.settings.studio.oauth_authorization_code_ttl_seconds,
        )
        query = {"code": code}
        if req["state"]:
            query["state"] = req["state"]
        return req["redirect_uri"] + ("&" if "?" in req["redirect_uri"] else "?") + urlencode(query)

    async def _authenticate_client(self, form: dict[str, str], authorization_header: str) -> dict[str, Any]:
        client_id = form.get("client_id", "")
        supplied_secret = form.get("client_secret", "")
        basic_client_id = ""
        basic_secret = ""
        if authorization_header.startswith("Basic "):
            try:
                raw = base64.b64decode(authorization_header[6:]).decode("utf-8")
                basic_client_id, basic_secret = raw.split(":", 1)
            except Exception:
                raise OAuthError("invalid_client", "Malformed HTTP Basic client authentication", 401)
            client_id = basic_client_id
            supplied_secret = basic_secret
        if not client_id:
            raise OAuthError("invalid_client", "client_id is required", 401)
        try:
            client = await self.db.get_oauth_client(client_id)
        except KeyError:
            raise OAuthError("invalid_client", "Unknown OAuth client", 401)
        method = client["token_endpoint_auth_method"]
        if method == "none":
            if authorization_header.startswith("Basic "):
                raise OAuthError("invalid_client", "Public client must use token endpoint auth method none", 401)
        elif method == "client_secret_basic":
            if not basic_client_id:
                raise OAuthError("invalid_client", "HTTP Basic client authentication required", 401)
            if not supplied_secret or not hmac.compare_digest(_sha256(supplied_secret), client.get("client_secret_hash") or ""):
                raise OAuthError("invalid_client", "Invalid client secret", 401)
        elif method == "client_secret_post":
            if authorization_header.startswith("Basic "):
                raise OAuthError("invalid_client", "client_secret_post required", 401)
            if not supplied_secret or not hmac.compare_digest(_sha256(supplied_secret), client.get("client_secret_hash") or ""):
                raise OAuthError("invalid_client", "Invalid client secret", 401)
        return client

    def _issue_access_token(self, *, client_id: str, scope: str, resource: str) -> tuple[str, int]:
        now = int(time.time())
        exp = now + self.settings.studio.oauth_access_token_ttl_seconds
        payload = {
            "iss": self.issuer,
            "sub": client_id,
            "aud": resource,
            "scope": scope,
            "iat": now,
            "exp": exp,
            "jti": secrets.token_urlsafe(16),
            "typ": "mcp_access",
        }
        body = _b64u(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        sig = _b64u(hmac.new(self._signing_secret(), body.encode("ascii"), hashlib.sha256).digest())
        return f"mso1.{body}.{sig}", exp - now

    def verify_access_token(self, token: str) -> OAuthAccessClaims | None:
        if not token.startswith("mso1."):
            return None
        try:
            _, body, sig = token.split(".", 2)
            expected = _b64u(hmac.new(self._signing_secret(), body.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, expected):
                return None
            payload = json.loads(_b64u_decode(body))
            if payload.get("typ") != "mcp_access" or payload.get("iss") != self.issuer:
                return None
            if int(payload.get("exp", 0)) <= int(time.time()):
                return None
            if str(payload.get("aud", "")).rstrip("/") != self.resource:
                return None
            scopes = set(str(payload.get("scope", "")).split())
            if "mcp:serena" not in scopes:
                return None
            return OAuthAccessClaims(
                client_id=str(payload["sub"]),
                scope=str(payload.get("scope", "")),
                resource=str(payload["aud"]),
                subject=str(payload["sub"]),
                expires_at=int(payload["exp"]),
                token_id=str(payload.get("jti", "")),
            )
        except Exception:
            return None

    async def token(self, form: dict[str, str], authorization_header: str = "") -> dict[str, Any]:
        client = await self._authenticate_client(form, authorization_header)
        grant_type = form.get("grant_type", "")
        if grant_type == "authorization_code":
            code = form.get("code", "")
            redirect_uri = form.get("redirect_uri", "")
            verifier = form.get("code_verifier", "")
            if not code or not redirect_uri or not verifier:
                raise OAuthError("invalid_request", "code, redirect_uri and code_verifier are required")
            try:
                record = await self.db.consume_oauth_authorization_code(_sha256(code))
            except KeyError:
                raise OAuthError("invalid_grant", "Authorization code is invalid or expired")
            if record["client_id"] != client["client_id"] or record["redirect_uri"] != redirect_uri:
                raise OAuthError("invalid_grant", "Authorization code client or redirect mismatch")
            challenge = _b64u(hashlib.sha256(verifier.encode("ascii")).digest())
            if not hmac.compare_digest(challenge, record["code_challenge"]):
                raise OAuthError("invalid_grant", "PKCE verification failed")
            scope = record["scope"]
            resource = record["resource"]
            access_token, expires_in = self._issue_access_token(client_id=client["client_id"], scope=scope, resource=resource)
            result: dict[str, Any] = {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": expires_in,
                "scope": scope,
            }
            if "offline_access" in set(scope.split()):
                refresh = secrets.token_urlsafe(48)
                await self.db.create_oauth_refresh_token(
                    token_hash=_sha256(refresh),
                    client_id=client["client_id"],
                    scope=scope,
                    resource=resource,
                    lifetime_seconds=self.settings.studio.oauth_refresh_token_ttl_seconds,
                )
                result["refresh_token"] = refresh
            return result

        if grant_type == "refresh_token":
            refresh = form.get("refresh_token", "")
            if not refresh:
                raise OAuthError("invalid_request", "refresh_token is required")
            try:
                record = await self.db.consume_oauth_refresh_token(_sha256(refresh), client["client_id"])
            except KeyError:
                raise OAuthError("invalid_grant", "Refresh token is invalid, expired, or already rotated")
            requested_scope = (form.get("scope") or record["scope"]).strip()
            if not set(requested_scope.split()).issubset(set(record["scope"].split())):
                raise OAuthError("invalid_scope", "Refresh request cannot expand scope")
            access_token, expires_in = self._issue_access_token(
                client_id=client["client_id"], scope=requested_scope, resource=record["resource"]
            )
            next_refresh = secrets.token_urlsafe(48)
            await self.db.create_oauth_refresh_token(
                token_hash=_sha256(next_refresh),
                client_id=client["client_id"],
                scope=requested_scope,
                resource=record["resource"],
                lifetime_seconds=self.settings.studio.oauth_refresh_token_ttl_seconds,
                rotated_from=record["token_hash"],
            )
            return {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": expires_in,
                "scope": requested_scope,
                "refresh_token": next_refresh,
            }

        raise OAuthError("unsupported_grant_type", "Only authorization_code and refresh_token are supported")
