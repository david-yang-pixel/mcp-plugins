"""Zendesk OAuth (PKCE authorization code) with rotating refresh tokens.

The MCP authorizes itself once through two tools:

1. ``start_authorization`` keeps a PKCE verifier and state on the server and
   returns a Zendesk authorization URL;
2. the user signs in with their own Zendesk account; the browser is sent to
   the public client's loopback redirect (``http://127.0.0.1:8765/callback``),
   which does not need to load: its address holds a single-use code;
3. ``complete_authorization`` receives that address, exchanges the code with
   the verifier, checks the identity and stores the tokens in a private file on
   persistent storage. Every refresh rotates the refresh token in that file.

The first authorized Zendesk user is pinned: re-authorization must be the same
user unless ``ZENDESK_OAUTH_EXPECTED_USER_ID`` names another. Token values,
codes and verifiers never appear in errors, logs, status or tool results.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from requests.auth import AuthBase

# Refresh this long before the access token expires.
EXPIRY_MARGIN_SECONDS = 60
# Lifetimes requested from Zendesk (defaults for new OAuth clients).
ACCESS_TOKEN_SECONDS = 1800
REFRESH_TOKEN_SECONDS = 30 * 24 * 3600
# An authorization link must be completed within this time.
PENDING_SECONDS = 600
DEFAULT_CLIENT_ID = "dify_zendesk_cli"
# The redirect registered for the team's public PKCE client.
REDIRECT_URI = "http://127.0.0.1:8765/callback"
# Only what the MCP tools use: ticket read/update, users, views, groups,
# Help Center and attachment reads (search needs the general read scope).
DEFAULT_SCOPE = (
    "read tickets:read tickets:write users:read hc:read "
    "ticket_views:read groups:read ticket_attachments:read"
)
NOT_AUTHORIZED = "Zendesk OAuth is not authorized; call zendesk_auth_start, then zendesk_auth_complete"


class OAuthError(RuntimeError):
    """A non-sensitive OAuth failure."""

    def __init__(self, message: str, *, reauthorize: bool = False) -> None:
        super().__init__(message)
        self.reauthorize = reauthorize


class OAuthTokenProvider:
    def __init__(
        self,
        *,
        subdomain: str,
        path: str,
        client_id: str = DEFAULT_CLIENT_ID,
        scope: str = DEFAULT_SCOPE,
        expected_user_id: str = "",
        timeout: tuple[float, float] = (3.0, 20.0),
    ) -> None:
        self.subdomain = subdomain
        self.path = Path(path)
        self.pending_path = Path(str(self.path) + ".pending")
        self.client_id = client_id or DEFAULT_CLIENT_ID
        self.scope = scope or DEFAULT_SCOPE
        self.expected_user_id = expected_user_id.strip()
        self.timeout = timeout
        self.host = f"{subdomain}.zendesk.com"
        self._memory_lock = threading.Lock()
        self._cached: Optional[tuple[str, float]] = None
        self.last_error: Optional[str] = None

    # ----------------------------------------------------------- authorization
    def start_authorization(self) -> dict[str, Any]:
        """Create a PKCE request and return the URL the user must open."""
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        with self._file_lock():
            # A new link replaces any earlier unfinished one.
            _write_private(self.pending_path, {"state": state, "verifier": verifier, "created_at": time.time()})
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": REDIRECT_URI,
                "scope": self.scope,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return {
            "authorization_url": f"https://{self.host}/oauth/authorizations/new?{query}",
            "redirect_uri": REDIRECT_URI,
            "expires_in_seconds": PENDING_SECONDS,
        }

    def complete_authorization(self, callback_url: str) -> dict[str, Any]:
        """Exchange the code from the copied redirect address and store tokens."""
        code, state = _parse_callback(callback_url)
        with self._file_lock():
            pending = _read_private(self.pending_path) if self.pending_path.exists() else None
            if not pending or not isinstance(pending.get("state"), str):
                raise OAuthError("No authorization is in progress; call zendesk_auth_start first")
            if not hmac.compare_digest(pending["state"], state):
                raise OAuthError("The address does not belong to the latest authorization link; start again")
            if time.time() - _number(pending.get("created_at")) > PENDING_SECONDS:
                _remove(self.pending_path)
                raise OAuthError("The authorization link expired; call zendesk_auth_start again")
            # The code is single-use: whatever happens next, this request is spent.
            _remove(self.pending_path)

            started = time.time()
            tokens = self._token_request(
                {
                    "grant_type": "authorization_code",
                    "client_id": self.client_id,
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": pending["verifier"],
                    "expires_in": ACCESS_TOKEN_SECONDS,
                    "refresh_token_expires_in": REFRESH_TOKEN_SECONDS,
                },
                rejected_message="Zendesk rejected the authorization code; call zendesk_auth_start again",
            )
            refresh = tokens.get("refresh_token")
            if not isinstance(refresh, str) or not refresh:
                raise self._fail("Zendesk did not return a refresh token; check the OAuth client")
            user = self._current_user(tokens["access_token"])
            user_id = user["id"]
            expected = self.expected_user_id or self._pinned_user_id()
            if expected and str(user_id) != expected:
                raise self._fail(
                    "Authorized Zendesk user does not match the user this MCP is pinned to; credentials not saved"
                )
            data = {
                "subdomain": self.subdomain,
                "client_id": self.client_id,
                "grant_type": "authorization_code",
                "scope": self.scope,
                "granted_scope": tokens.get("scope", self.scope),
                "user_id": user_id,
                "access_token": tokens["access_token"],
                "expires_at": started + float(tokens["expires_in"]),
                "refresh_token": refresh,
                "refreshed_at": started,
                "authorized_at": started,
            }
            _write_private(self.path, data)
            self.last_error = None
            self._remember(data)
        return {"user_id": user_id, "name": user.get("name"), "granted_scope": data["granted_scope"]}

    # ------------------------------------------------------------------ tokens
    def access_token(self, *, rejected: Optional[str] = None) -> str:
        """Return a valid access token, refreshing when needed.

        ``rejected`` is a token Zendesk just answered 401 for: it is never
        returned again, so the caller gets a newly refreshed one.
        """
        with self._memory_lock:
            cached = self._cached
            if cached and cached[0] != rejected and cached[1] > time.time() + EXPIRY_MARGIN_SECONDS:
                return cached[0]
        with self._file_lock():
            data = self._load()
            token = data.get("access_token")
            if (
                isinstance(token, str)
                and token
                and token != rejected
                and _number(data.get("expires_at")) > time.time() + EXPIRY_MARGIN_SECONDS
            ):
                self._remember(data)
                return token
            return self._refresh(data)["access_token"]

    def keepalive(self, max_age_seconds: float) -> bool:
        """Rotate tokens that have not been refreshed recently.

        Refresh tokens expire after a period without use; refreshing on a
        schedule keeps an idle server authorized. Returns True if refreshed.
        """
        if not self.path.is_file():
            return False
        with self._file_lock():
            data = self._load()
            refreshed_at = _number(data.get("refreshed_at"))
            if refreshed_at and time.time() - refreshed_at < max_age_seconds:
                return False
            self._refresh(data)
            return True

    def status(self) -> dict[str, Any]:
        """Non-sensitive state for the health endpoint and the status tool.

        Never contacts Zendesk or waits for the refresh lock: files are
        replaced atomically, so an unlocked read is consistent.
        """
        result: dict[str, Any] = {"authorized": False}
        if self.last_error:
            result["last_error"] = self.last_error
        if self.pending_path.exists():
            result["authorization_pending"] = True
        if not self.path.exists():
            return result
        try:
            data = _read_private(self.path)
        except OAuthError as exc:
            result["error"] = str(exc)
            return result
        now = time.time()
        result.update(
            authorized=True,
            user_id=data.get("user_id"),
            granted_scope=data.get("granted_scope"),
            access_token_valid=_number(data.get("expires_at")) > now,
            last_refresh_age_seconds=(
                int(now - _number(data["refreshed_at"])) if data.get("refreshed_at") else None
            ),
        )
        return result

    # ---------------------------------------------------------------- internals
    def _remember(self, data: dict[str, Any]) -> None:
        with self._memory_lock:
            self._cached = (data["access_token"], _number(data.get("expires_at")))

    def _pinned_user_id(self) -> str:
        if not self.path.exists():
            return ""
        try:
            stored = _read_private(self.path)
        except OAuthError:
            # An unreadable file must not silently lift the user pin.
            raise self._fail("Stored Zendesk OAuth credentials are unreadable; fix the file before re-authorizing")
        return str(stored.get("user_id") or "")

    def _refresh(self, data: dict[str, Any]) -> dict[str, Any]:
        started = time.time()
        tokens = self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": data["client_id"],
                "refresh_token": data["refresh_token"],
                "expires_in": ACCESS_TOKEN_SECONDS,
            },
            rejected_message="Zendesk OAuth refresh was rejected; call zendesk_auth_start, then zendesk_auth_complete",
        )
        rotated = tokens.get("refresh_token")
        if not isinstance(rotated, str) or not rotated:
            raise self._fail("Zendesk OAuth omitted the rotated refresh token; re-authorize", reauthorize=True)
        updated = dict(data)
        updated.update(
            access_token=tokens["access_token"],
            expires_at=started + float(tokens["expires_in"]),
            refreshed_at=started,
            refresh_token=rotated,
        )
        if tokens.get("scope"):
            updated["granted_scope"] = tokens["scope"]
        _write_private(self.path, updated)
        self.last_error = None
        self._remember(updated)
        return updated

    def _token_request(self, payload: dict[str, Any], *, rejected_message: str) -> dict[str, Any]:
        try:
            response = requests.post(
                f"https://{self.host}/oauth/tokens",
                json=payload,
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise self._fail("Zendesk OAuth token request failed") from None
        if response.status_code in (400, 401, 403):
            raise self._fail(f"{rejected_message} (HTTP {response.status_code})", reauthorize=True)
        if response.status_code != 200:
            raise self._fail(f"Zendesk OAuth token request failed (HTTP {response.status_code})")
        try:
            tokens = response.json()
            access = tokens["access_token"]
            lifetime = float(tokens["expires_in"])
            if not isinstance(access, str) or not access or lifetime <= 0:
                raise ValueError
            if str(tokens.get("token_type", "bearer")).lower() != "bearer":
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise self._fail("Invalid Zendesk OAuth token response") from None
        return tokens

    def _current_user(self, access_token: str) -> dict[str, Any]:
        try:
            response = requests.get(
                f"https://{self.host}/api/v2/users/me.json",
                headers={"Authorization": "Bearer " + access_token, "Accept": "application/json"},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise self._fail("Zendesk identity check failed; credentials not saved") from None
        try:
            user = response.json()["user"] if response.status_code == 200 else None
        except (ValueError, KeyError, TypeError):
            user = None
        # An anonymous user has no id: the token did not authenticate anyone.
        if not isinstance(user, dict) or not isinstance(user.get("id"), int):
            raise self._fail("Zendesk identity check failed; credentials not saved")
        return user

    def _fail(self, message: str, *, reauthorize: bool = False) -> OAuthError:
        self.last_error = message
        return OAuthError(message, reauthorize=reauthorize)

    @contextmanager
    def _file_lock(self):
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise OAuthError("Cannot open the Zendesk OAuth credential lock") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self) -> dict[str, Any]:
        """Read stored credentials. Caller holds the lock."""
        if not self.path.exists():
            raise OAuthError(NOT_AUTHORIZED, reauthorize=True)
        data = _read_private(self.path)
        if data.get("subdomain") != self.subdomain:
            raise self._fail("Zendesk OAuth credentials belong to another Zendesk account", reauthorize=True)
        if (
            data.get("grant_type") != "authorization_code"
            or not isinstance(data.get("client_id"), str)
            or not data["client_id"]
            or not isinstance(data.get("refresh_token"), str)
            or not data["refresh_token"]
        ):
            raise self._fail("Stored Zendesk OAuth credentials are incomplete; re-authorize", reauthorize=True)
        return data


class ZendeskBearerAuth(AuthBase):
    """Attach the OAuth token only to this Zendesk account's HTTPS origin."""

    def __init__(self, provider: OAuthTokenProvider) -> None:
        self.provider = provider

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        parsed = urlsplit(request.url or "")
        if parsed.scheme != "https" or parsed.hostname != self.provider.host or parsed.port not in (None, 443):
            # Attachment CDNs and other hosts must never receive the credential.
            request.headers.pop("Authorization", None)
            return request
        request.headers["Authorization"] = "Bearer " + self.provider.access_token()
        return request


_PROVIDERS: dict[tuple[str, ...], OAuthTokenProvider] = {}
_PROVIDERS_LOCK = threading.Lock()


def get_provider(
    *,
    subdomain: str,
    path: str,
    client_id: str = DEFAULT_CLIENT_ID,
    scope: str = DEFAULT_SCOPE,
    expected_user_id: str = "",
    timeout: tuple[float, float] = (3.0, 20.0),
) -> OAuthTokenProvider:
    """One provider per credential file, so threads share the cached token."""
    key = (subdomain, path, client_id, scope, expected_user_id)
    with _PROVIDERS_LOCK:
        provider = _PROVIDERS.get(key)
        if provider is None:
            provider = OAuthTokenProvider(
                subdomain=subdomain,
                path=path,
                client_id=client_id,
                scope=scope,
                expected_user_id=expected_user_id,
                timeout=timeout,
            )
            _PROVIDERS[key] = provider
        return provider


def bearer_token(request_headers: Any) -> Optional[str]:
    value = request_headers.get("Authorization") if request_headers is not None else None
    if isinstance(value, str) and value.startswith("Bearer "):
        return value[len("Bearer "):]
    return None


def _parse_callback(callback_url: str) -> tuple[str, str]:
    if not isinstance(callback_url, str) or not callback_url.strip():
        raise OAuthError("callback_url must be the full address shown after authorizing")
    text = callback_url.strip()
    parsed = urlsplit(text if "://" in text else "http://127.0.0.1:8765/callback?" + text.lstrip("?"))
    query = parse_qs(parsed.query)
    if query.get("error"):
        raise OAuthError("Zendesk authorization was denied; call zendesk_auth_start to try again")
    code, state = query.get("code", [""])[0], query.get("state", [""])[0]
    if not code or not state:
        raise OAuthError("callback_url must contain the code and state from the redirected address")
    return code, state


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _read_private(path: Path) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise OAuthError("Zendesk OAuth credential file must be private (chmod 600)")
            data = json.loads(stream.read())
    except OAuthError:
        raise
    except (OSError, ValueError):
        raise OAuthError("Cannot read Zendesk OAuth credentials") from None
    if not isinstance(data, dict):
        raise OAuthError("Cannot read Zendesk OAuth credentials")
    return data


def _write_private(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".oauth-", dir=str(path.parent))
    except OSError:
        raise OAuthError("Cannot save Zendesk OAuth credentials") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise OAuthError("Cannot save Zendesk OAuth credentials") from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
