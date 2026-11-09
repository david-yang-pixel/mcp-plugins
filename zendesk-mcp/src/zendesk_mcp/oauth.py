"""Zendesk OAuth credentials with rotating refresh tokens.

The credential file uses the same JSON layout as the team ``zd auth login``
command, so an authorization made on a workstation can seed the server:

* the deployment mounts a read-only seed (``ZENDESK_OAUTH_SEED_FILE``);
* the first use copies it to a private, writable file on persistent storage
  (``ZENDESK_OAUTH_FILE``); every refresh rotates the refresh token there;
* a seed whose content changed (re-authorization) replaces the stored file,
  an unchanged seed never overwrites rotated tokens after a redeploy.

Token values never appear in errors, logs or status output.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import requests
from requests.auth import AuthBase

# Refresh this long before the access token expires.
EXPIRY_MARGIN_SECONDS = 60
# Lifetime requested for new access tokens (Zendesk default for new clients).
ACCESS_TOKEN_SECONDS = 1800


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
        seed_path: str = "",
        expected_user_id: str = "",
        timeout: tuple[float, float] = (3.0, 20.0),
    ) -> None:
        self.subdomain = subdomain
        self.path = Path(path)
        self.seed_path = Path(seed_path) if seed_path else None
        self.expected_user_id = expected_user_id.strip()
        self.timeout = timeout
        self.host = f"{subdomain}.zendesk.com"
        self._memory_lock = threading.Lock()
        self._cached: Optional[tuple[str, float]] = None
        self.last_error: Optional[str] = None

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
            data = self._refresh(data)
            return data["access_token"]

    def keepalive(self, max_age_seconds: float) -> bool:
        """Rotate tokens that have not been refreshed recently.

        Refresh tokens expire after a period without use; refreshing on a
        schedule keeps an idle server authorized. Returns True if refreshed.
        """
        if not self._has_credentials():
            return False
        with self._file_lock():
            data = self._load()
            refreshed_at = _number(data.get("refreshed_at"))
            if refreshed_at and time.time() - refreshed_at < max_age_seconds:
                return False
            self._refresh(data)
            return True

    def status(self) -> dict[str, Any]:
        """Non-sensitive state for the health endpoint.

        Never contacts Zendesk, imports a seed or waits for the refresh lock:
        the file is replaced atomically, so an unlocked read is consistent.
        """
        result: dict[str, Any] = {
            "credentials": False,
            "seed": bool(self.seed_path and self.seed_path.is_file()),
        }
        if self.last_error:
            result["last_error"] = self.last_error
        if not self.path.exists():
            # A seed is imported on first use (keepalive runs shortly after start).
            return result
        try:
            data = _read_private(self.path)
        except OAuthError as exc:
            result["error"] = str(exc)
            return result
        now = time.time()
        result.update(
            credentials=True,
            grant_type=data.get("grant_type"),
            user_id=data.get("user_id"),
            access_token_valid=_number(data.get("expires_at")) > now,
            last_refresh_age_seconds=(
                int(now - _number(data["refreshed_at"])) if data.get("refreshed_at") else None
            ),
            seed_imported=bool(data.get("seed_sha256")),
        )
        return result

    # ---------------------------------------------------------------- internals
    def _remember(self, data: dict[str, Any]) -> None:
        with self._memory_lock:
            self._cached = (data["access_token"], _number(data.get("expires_at")))

    def _refresh(self, data: dict[str, Any]) -> dict[str, Any]:
        grant = data.get("grant_type")
        payload: dict[str, Any] = {"client_id": data["client_id"], "expires_in": ACCESS_TOKEN_SECONDS}
        if data.get("client_secret"):
            payload["client_secret"] = data["client_secret"]
        if grant == "client_credentials":
            payload.update(grant_type=grant, scope=data["scope"])
        elif grant == "authorization_code" and data.get("refresh_token"):
            payload.update(grant_type="refresh_token", refresh_token=data["refresh_token"])
        else:
            raise self._fail("Zendesk OAuth credentials cannot be refreshed; reauthorize", reauthorize=True)

        started = time.time()
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
            raise self._fail(
                f"Zendesk OAuth refresh rejected (HTTP {response.status_code}); reauthorize and update the seed",
                reauthorize=True,
            )
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

        updated = dict(data)
        updated.update(access_token=access, expires_at=started + lifetime, refreshed_at=started)
        if grant == "authorization_code":
            rotated = tokens.get("refresh_token")
            if not isinstance(rotated, str) or not rotated:
                raise self._fail("Zendesk OAuth omitted the rotated refresh token; reauthorize", reauthorize=True)
            updated["refresh_token"] = rotated
        if tokens.get("scope"):
            updated["granted_scope"] = tokens["scope"]
        _write_private(self.path, updated)
        self.last_error = None
        self._remember(updated)
        return updated

    def _fail(self, message: str, *, reauthorize: bool = False) -> OAuthError:
        self.last_error = message
        return OAuthError(message, reauthorize=reauthorize)

    def _has_credentials(self) -> bool:
        return self.path.is_file() or bool(self.seed_path and self.seed_path.is_file())

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
        """Read stored credentials, importing a new seed first. Caller holds the lock."""
        seed_bytes = _read_seed(self.seed_path)
        stored = _read_private(self.path) if self.path.exists() else None
        if seed_bytes is not None:
            digest = hashlib.sha256(seed_bytes).hexdigest()
            if stored is None or stored.get("seed_sha256") != digest:
                stored = self._import_seed(seed_bytes, digest)
        if stored is None:
            raise OAuthError("Zendesk OAuth credentials are not configured")
        self._validate(stored)
        return stored

    def _import_seed(self, raw: bytes, digest: str) -> dict[str, Any]:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            raise self._fail("Zendesk OAuth seed is not a JSON object", reauthorize=True) from None
        self._validate(data)
        if self.expected_user_id and str(data.get("user_id", "")) != self.expected_user_id:
            raise self._fail("Zendesk OAuth seed belongs to an unexpected user", reauthorize=True)
        data = {**data, "seed_sha256": digest}
        data.pop("refreshed_at", None)
        _write_private(self.path, data)
        with self._memory_lock:
            self._cached = None
        return data

    def _validate(self, data: dict[str, Any]) -> None:
        if data.get("subdomain") != self.subdomain:
            raise self._fail("Zendesk OAuth credentials belong to another Zendesk account", reauthorize=True)
        if not isinstance(data.get("client_id"), str) or not data["client_id"]:
            raise self._fail("Zendesk OAuth credentials have no client_id", reauthorize=True)
        grant = data.get("grant_type")
        if grant == "authorization_code":
            if not isinstance(data.get("refresh_token"), str) or not data["refresh_token"]:
                raise self._fail("Zendesk OAuth credentials have no refresh token", reauthorize=True)
        elif grant == "client_credentials":
            if not data.get("client_secret") or not data.get("scope"):
                raise self._fail("Zendesk OAuth client credentials are incomplete", reauthorize=True)
        else:
            raise self._fail("Unsupported Zendesk OAuth grant type", reauthorize=True)


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


_PROVIDERS: dict[tuple[str, str, str, str], OAuthTokenProvider] = {}
_PROVIDERS_LOCK = threading.Lock()


def get_provider(
    *,
    subdomain: str,
    path: str,
    seed_path: str = "",
    expected_user_id: str = "",
    timeout: tuple[float, float] = (3.0, 20.0),
) -> OAuthTokenProvider:
    """One provider per credential file, so threads share the cached token."""
    key = (subdomain, path, seed_path, expected_user_id)
    with _PROVIDERS_LOCK:
        provider = _PROVIDERS.get(key)
        if provider is None:
            provider = OAuthTokenProvider(
                subdomain=subdomain,
                path=path,
                seed_path=seed_path,
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


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _read_seed(path: Optional[Path]) -> Optional[bytes]:
    if path is None or not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        raise OAuthError("Cannot read the Zendesk OAuth seed") from None
    return raw if raw.strip() else None


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
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".oauth-", dir=str(path.parent))
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
