"""MCP OAuth authorization server: clients, codes and tokens, with Dify login as the user step."""
from __future__ import annotations

import base64
import binascii
import contextvars
import hmac
import logging
import secrets
import time
from urllib.parse import unquote, urlsplit

from pydantic import AnyUrl, PrivateAttr
from starlette.requests import Request

from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
                                      RefreshToken, RegistrationError, TokenError)
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull, OAuthToken

from .settings import Settings
from .store import Store, digest

log = logging.getLogger("difyctl_mcp.oauth")
PROBE_CLIENT = "deploy-probe"
LOOPBACK = ("localhost", "127.0.0.1", "::1")
# The /authorize wrapper hands the pending-login cookie to the response through this holder.
authorize_cookie: contextvars.ContextVar[dict | None] = contextvars.ContextVar("authorize_cookie", default=None)


def redirect_allowed(uri: str, allowed: list[str]) -> bool:
    """Exact match, or a loopback redirect whose registered form omits the port (RFC 8252 §7.3)."""
    if uri in allowed:
        return True
    try:
        candidate = urlsplit(uri)
        if candidate.fragment or candidate.scheme != "http" or candidate.hostname not in LOOPBACK:
            return False
        for entry in allowed:
            pattern = urlsplit(entry)
            if (pattern.scheme == "http" and pattern.hostname == candidate.hostname and pattern.port is None
                    and pattern.path == candidate.path and pattern.query == candidate.query):
                return True
    except ValueError:
        return False
    return False


class Client(OAuthClientInformationFull):
    _allowed: list[str] = PrivateAttr(default_factory=list)

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is None:
            # The single registered default goes through the same current-allowlist check below.
            redirect_uri = super().validate_redirect_uri(None)
        registered = [str(u) for u in self.redirect_uris or []]
        text = str(redirect_uri)
        # The current allowlist always applies, including to clients registered before it changed.
        if not (redirect_allowed(text, registered) and redirect_allowed(text, self._allowed)):
            raise InvalidRedirectUriError(f"Redirect URI '{redirect_uri}' is not allowed for this client")
        return redirect_uri


class Code(AuthorizationCode):
    pass


class Provider:
    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store

    # Clients ---------------------------------------------------------------------
    def _client(self, info: dict) -> Client:
        client = Client.model_validate({**info, "client_secret": None})
        client._allowed = self.settings.allowed_redirect_uris
        return client

    def static_client(self) -> Client | None:
        if not self.settings.static_client_id:
            return None
        redirects = [u for u in self.settings.allowed_redirect_uris if "://" in u]
        return self._client({
            "client_id": self.settings.static_client_id, "client_name": "Pre-registered client",
            "redirect_uris": redirects or None, "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": " ".join(self.settings.scopes),
            "token_endpoint_auth_method": "client_secret_post" if self.settings.static_client_secret else "none"})

    async def get_client(self, client_id: str) -> Client | None:
        static = self.static_client()
        if static and client_id == static.client_id:
            return static
        row = self.store.client(client_id)
        return self._client(row["info"]) if row else None

    def secret_matches(self, client_id: str, supplied: str | None) -> bool:
        static = self.static_client()
        if static and client_id == static.client_id:
            expected = self.settings.static_client_secret
            return not expected or (supplied is not None and hmac.compare_digest(expected.encode(), supplied.encode()))
        row = self.store.client(client_id)
        if row is None:
            return False
        if row["secret_hash"] is None:
            return True
        return supplied is not None and hmac.compare_digest(row["secret_hash"], digest(supplied))

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not self.settings.configured:
            raise RegistrationError("invalid_client_metadata", "OAuth is not configured on this server")
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or any(not redirect_allowed(u, self.settings.allowed_redirect_uris) for u in uris):
            raise RegistrationError("invalid_redirect_uri", "Redirect URI is not in MCP_ALLOWED_REDIRECT_URIS")
        if set(client_info.grant_types) - {"authorization_code", "refresh_token"}:
            raise RegistrationError("invalid_client_metadata", "Only authorization_code and refresh_token are supported")
        info = client_info.model_dump(mode="json", exclude={"client_secret"}, exclude_none=True)
        if not self.store.save_client(client_info.client_id, client_info.client_secret, info, self.settings.max_clients):
            raise RegistrationError("invalid_client_metadata", "Client registration limit reached")

    # Authorization -----------------------------------------------------------------
    async def authorize(self, client: Client, params: AuthorizationParams) -> str:
        if not self.settings.configured:
            raise AuthorizeError("temporarily_unavailable", "OAuth is not configured on this server")
        if params.resource and params.resource.rstrip("/") != self.settings.resource:
            raise AuthorizeError("invalid_target", "Tokens can only be issued for this MCP server")
        allowed = set(self.settings.scopes)
        scopes = params.scopes or [s for s in (client.scope or "").split() if s in allowed] or list(allowed)
        if not set(scopes) <= allowed:
            raise AuthorizeError("invalid_scope", "Requested scope exceeds this server's maximum effect")
        pending_id = secrets.token_urlsafe(24)
        cookie = secrets.token_urlsafe(32)
        self.store.create_pending(pending_id, client.client_id, {
            "state": params.state, "scopes": scopes, "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri), "explicit": params.redirect_uri_provided_explicitly,
            "resource": self.settings.resource}, cookie, self.settings.pending_ttl_seconds)
        holder = authorize_cookie.get()
        if holder is not None:
            holder["pending"], holder["cookie"] = pending_id, cookie
        return f"{self.settings.issuer}/login?request={pending_id}"

    def issue_code(self, pending: dict, params: dict, account_id: str) -> str:
        code = secrets.token_urlsafe(32)
        self.store.save_code(code, pending["client_id"], account_id, params["scopes"], params["code_challenge"],
                             params["redirect_uri"], params["explicit"], params["resource"], self.settings.code_ttl_seconds)
        return code

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> Code | None:
        row = self.store.take_code(authorization_code)
        if row is None:
            return None
        return Code(code=authorization_code, scopes=row["scopes"].split(), expires_at=row["expires_at"],
                    client_id=row["client_id"], code_challenge=row["code_challenge"], redirect_uri=AnyUrl(row["redirect_uri"]),
                    redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]), resource=row["resource"],
                    subject=row["account_id"])

    def _session(self, account_id: str) -> int:
        user = self.store.user(account_id)
        if not user or not user["config_dir"] or user["session_expires_at"] <= int(time.time()):
            raise TokenError("invalid_grant", "The Dify login has ended; authorize again")
        return user["session_expires_at"]

    def _issue(self, client_id: str, account_id: str, scopes: list[str], resource: str, session_end: int,
               family: str | None = None) -> OAuthToken:
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        access_expires = min(now + self.settings.access_token_ttl_seconds, session_end)
        self.store.save_tokens(access, refresh, client_id, account_id, scopes, resource, access_expires, session_end, family)
        self.store.mark_client_used(client_id)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=max(access_expires - now, 1),
                          refresh_token=refresh, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: Code) -> OAuthToken:
        session_end = self._session(authorization_code.subject)
        return self._issue(client.client_id, authorization_code.subject, authorization_code.scopes,
                           authorization_code.resource or self.settings.resource, session_end)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self.store.refresh_token(refresh_token)
        if row is None or row.client_id != client.client_id:
            return None
        if row.revoked_at is not None:
            # A retired refresh token came back: assume theft and revoke everything this client holds.
            log.warning("refresh token reuse detected; revoking client tokens")
            self.store.revoke_account(row.account_id, row.client_id)
            return None
        return RefreshToken(token=refresh_token, client_id=row.client_id, scopes=row.scopes, expires_at=row.expires_at,
                            resource=row.resource, subject=row.account_id)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        family = self.store.rotate_refresh(refresh_token.token)
        if family is None:
            self.store.revoke_account(refresh_token.subject, client.client_id)
            raise TokenError("invalid_grant", "Refresh token was already used")
        session_end = self._session(refresh_token.subject)
        return self._issue(client.client_id, refresh_token.subject, scopes, refresh_token.resource or self.settings.resource,
                           session_end, family)

    async def load_access_token(self, token: str) -> AccessToken | None:
        key = self.settings.api_key
        if key and hmac.compare_digest(token.encode(), key.encode()):
            # Deployment probe: may list tools, but carries no account and no scope, so every tool call is refused.
            return AccessToken(token=token, client_id=PROBE_CLIENT, scopes=[], expires_at=None,
                               resource=self.settings.resource, subject=None)
        row = self.store.access_token(token)
        now = int(time.time())
        if row is None or row.revoked_at is not None or row.expires_at <= now:
            return None
        user = self.store.user(row.account_id)
        if not user or not user["config_dir"] or user["session_expires_at"] <= now:
            return None
        return AccessToken(token=token, client_id=row.client_id, scopes=row.scopes, expires_at=row.expires_at,
                           resource=row.resource, subject=row.account_id)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            if token.client_id != PROBE_CLIENT:
                self.store.revoke_family(access=token.token)
        else:
            self.store.revoke_family(refresh=token.token)

    async def exchange_identity_assertion(self, client, params) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "Not supported")


class HashedClientAuthenticator(ClientAuthenticator):
    """Client authentication against stored SHA-256 secrets (the SDK compares plaintext)."""

    provider: Provider

    async def authenticate_request(self, request: Request) -> OAuthClientInformationFull:
        form = await request.form()
        client_id = form.get("client_id")
        supplied = None
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            # RFC 6749 §2.3.1: Basic credentials may carry the client id without repeating it in the body.
            try:
                user, separator, password = base64.b64decode(header[6:], validate=True).decode().partition(":")
            except (ValueError, UnicodeDecodeError, binascii.Error):
                raise AuthenticationError("Invalid Basic authentication header") from None
            if not separator or not user:
                raise AuthenticationError("Invalid Basic authentication header")
            if client_id is not None and unquote(user) != client_id:
                raise AuthenticationError("Client ID mismatch in Basic auth")
            client_id, supplied = unquote(user), unquote(password)
        elif isinstance(form.get("client_secret"), str):
            supplied = form.get("client_secret")
        if not isinstance(client_id, str) or not client_id:
            raise AuthenticationError("Missing client_id")
        client = await self.provider.get_client(client_id)
        if client is None:
            raise AuthenticationError("Invalid client_id")
        if not self.provider.secret_matches(client_id, supplied):
            raise AuthenticationError("Invalid client credentials")
        return client
