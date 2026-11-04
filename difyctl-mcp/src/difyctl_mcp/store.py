"""SQLite persistence for OAuth clients, pending logins, users, codes and tokens.

Secrets (client secrets, cookies, codes, tokens) are stored only as SHA-256 digests.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    secret_hash TEXT,
    redirect_uris TEXT NOT NULL,
    client_name TEXT,
    info TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    used_at INTEGER
);
CREATE TABLE IF NOT EXISTS pending (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    params TEXT NOT NULL,
    config_dir TEXT,
    cookie_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    user_code TEXT,
    verification_uri TEXT,
    last_poll_at REAL NOT NULL DEFAULT 0,
    error TEXT,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    account_id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    config_dir TEXT,
    session_expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    redirect_uri_explicit INTEGER NOT NULL,
    resource TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS access_tokens (
    token_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT NOT NULL,
    refresh_hash TEXT,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER,
    family TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS refresh_family ON refresh_tokens(family, revoked_at);
CREATE INDEX IF NOT EXISTS access_account ON access_tokens(account_id, client_id);
CREATE INDEX IF NOT EXISTS refresh_account ON refresh_tokens(account_id, client_id);
"""


# Retired refresh tokens kept per token family to detect reuse; older ones are deleted (constant space).
RETIRED_PER_FAMILY = 5


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class TokenRow:
    client_id: str
    account_id: str
    scopes: list[str]
    resource: str
    expires_at: int
    revoked_at: int | None
    refresh_hash: str | None = None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        os.chmod(path, 0o600)
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(SCHEMA)

    def _one(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def _all(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def _run(self, sql: str, args=()) -> int:
        with self.lock:
            return self.db.execute(sql, args).rowcount

    # Clients -----------------------------------------------------------------
    def client_count(self) -> int:
        return self._one("SELECT COUNT(*) FROM clients")[0]

    def save_client(self, client_id: str, secret: str | None, info: dict, max_clients: int | None = None) -> bool:
        """Register atomically; at capacity, evict the oldest client that never completed an authorization.

        Returns False only when every registered client is in use.
        """
        with self.lock:
            if max_clients is not None and self.client_count() >= max_clients:
                evicted = self._run("DELETE FROM clients WHERE client_id = (SELECT client_id FROM clients "
                                    "WHERE used_at IS NULL ORDER BY created_at, rowid LIMIT 1)")
                if not evicted:
                    return False
            self._run("INSERT INTO clients (client_id, secret_hash, redirect_uris, client_name, info, created_at, used_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                      (client_id, digest(secret) if secret else None, json.dumps(info.get("redirect_uris") or []),
                       info.get("client_name"), json.dumps(info), int(time.time())))
        return True

    def mark_client_used(self, client_id: str) -> None:
        self._run("UPDATE clients SET used_at = ? WHERE client_id = ?", (int(time.time()), client_id))

    def client(self, client_id: str):
        row = self._one("SELECT * FROM clients WHERE client_id = ?", (client_id,))
        if row is None:
            return None
        return {"info": json.loads(row["info"]), "secret_hash": row["secret_hash"]}

    # Pending authorizations ----------------------------------------------------
    def create_pending(self, pending_id: str, client_id: str, params: dict, cookie: str, ttl: int) -> None:
        now = int(time.time())
        self._run("INSERT INTO pending (id, client_id, params, cookie_hash, status, expires_at, created_at) "
                  "VALUES (?, ?, ?, ?, 'new', ?, ?)",
                  (pending_id, client_id, json.dumps(params), digest(cookie), now + ttl, now))

    def pending(self, pending_id: str):
        row = self._one("SELECT * FROM pending WHERE id = ?", (pending_id,))
        return dict(row) if row is not None else None

    def update_pending(self, pending_id: str, expected_status: str | None = None, **fields) -> bool:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        sql = f"UPDATE pending SET {assignments} WHERE id = ?"
        args = [*fields.values(), pending_id]
        if expected_status is not None:
            sql += " AND status = ?"
            args.append(expected_status)
        return self._run(sql, args) == 1

    def claim_poll(self, pending_id: str, interval: int) -> bool:
        """Allow at most one upstream status check per pending login per interval."""
        now = time.time()
        return self._run("UPDATE pending SET last_poll_at = ? WHERE id = ? AND status = 'waiting' AND last_poll_at <= ?",
                         (now, pending_id, now - interval)) == 1

    # Users ---------------------------------------------------------------------
    def user(self, account_id: str):
        row = self._one("SELECT * FROM users WHERE account_id = ?", (account_id,))
        return dict(row) if row is not None else None

    def adopt_session(self, account_id: str, email: str, config_dir: str, expires_at: int) -> str | None:
        """Point the account at its newest Dify login and return the replaced directory."""
        with self.lock:
            old = self.user(account_id)
            self._run("INSERT INTO users VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET "
                      "email = excluded.email, config_dir = excluded.config_dir, "
                      "session_expires_at = excluded.session_expires_at, updated_at = excluded.updated_at",
                      (account_id, email, config_dir, expires_at, int(time.time())))
        return old["config_dir"] if old and old["config_dir"] != config_dir else None

    def clear_session(self, account_id: str) -> str | None:
        with self.lock:
            old = self.user(account_id)
            self._run("UPDATE users SET config_dir = NULL WHERE account_id = ?", (account_id,))
        return old["config_dir"] if old else None

    def user_dirs(self) -> set[str]:
        return {row[0] for row in self._all("SELECT config_dir FROM users WHERE config_dir IS NOT NULL")}

    def referenced_dirs(self) -> set[str]:
        dirs = {row[0] for row in self._all("SELECT config_dir FROM users WHERE config_dir IS NOT NULL")}
        dirs |= {row[0] for row in self._all("SELECT config_dir FROM pending WHERE config_dir IS NOT NULL")}
        return dirs

    # Authorization codes -----------------------------------------------------------
    def save_code(self, code: str, client_id: str, account_id: str, scopes: list[str], challenge: str,
                  redirect_uri: str, explicit: bool, resource: str, ttl: int) -> None:
        self._run("INSERT INTO auth_codes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (digest(code), client_id, account_id, " ".join(scopes), challenge, redirect_uri,
                   int(explicit), resource, int(time.time()) + ttl))

    def take_code(self, code: str):
        """Load and delete in one step: a code can be presented only once, even if PKCE then fails."""
        with self.lock:
            row = self._one("SELECT * FROM auth_codes WHERE code_hash = ?", (digest(code),))
            if row is not None:
                self._run("DELETE FROM auth_codes WHERE code_hash = ?", (digest(code),))
        return dict(row) if row is not None else None

    # Tokens ----------------------------------------------------------------------
    def save_tokens(self, access: str, refresh: str, client_id: str, account_id: str, scopes: list[str],
                    resource: str, access_expires: int, refresh_expires: int, family: str | None = None) -> None:
        with self.lock:
            self._run("INSERT INTO refresh_tokens (token_hash, client_id, account_id, scopes, resource, expires_at, "
                      "revoked_at, family) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                      (digest(refresh), client_id, account_id, " ".join(scopes), resource, refresh_expires,
                       family or digest(refresh)))
            self._run("INSERT INTO access_tokens VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                      (digest(access), client_id, account_id, " ".join(scopes), resource, digest(refresh), access_expires))

    @staticmethod
    def _token(row) -> TokenRow | None:
        if row is None:
            return None
        keys = row.keys()
        return TokenRow(row["client_id"], row["account_id"], row["scopes"].split(), row["resource"], row["expires_at"],
                        row["revoked_at"], row["refresh_hash"] if "refresh_hash" in keys else None)

    def access_token(self, token: str) -> TokenRow | None:
        return self._token(self._one("SELECT * FROM access_tokens WHERE token_hash = ?", (digest(token),)))

    def refresh_token(self, token: str) -> TokenRow | None:
        return self._token(self._one("SELECT * FROM refresh_tokens WHERE token_hash = ?", (digest(token),)))

    def rotate_refresh(self, token: str) -> str | None:
        """Retire a refresh token, drop its access tokens and return its family; None if it was already used.

        Only the newest RETIRED_PER_FAMILY retired tokens of a family are kept, so endless rotation
        cannot grow the database while recent reuse is still detected.
        """
        now = int(time.time())
        with self.lock:
            row = self._one("SELECT family FROM refresh_tokens WHERE token_hash = ? AND revoked_at IS NULL", (digest(token),))
            if row is None:
                return None
            self._run("UPDATE refresh_tokens SET revoked_at = ? WHERE token_hash = ?", (now, digest(token)))
            self._run("DELETE FROM access_tokens WHERE refresh_hash = ?", (digest(token),))
            self._run("DELETE FROM refresh_tokens WHERE family = ? AND revoked_at IS NOT NULL AND token_hash NOT IN "
                      "(SELECT token_hash FROM refresh_tokens WHERE family = ? AND revoked_at IS NOT NULL "
                      "ORDER BY revoked_at DESC, rowid DESC LIMIT ?)", (row["family"], row["family"], RETIRED_PER_FAMILY))
        return row["family"]

    def revoke_family(self, *, access: str | None = None, refresh: str | None = None) -> None:
        now = int(time.time())
        with self.lock:
            refresh_hash = digest(refresh) if refresh else None
            if access:
                row = self._one("SELECT refresh_hash FROM access_tokens WHERE token_hash = ?", (digest(access),))
                self._run("UPDATE access_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                          (now, digest(access)))
                refresh_hash = refresh_hash or (row["refresh_hash"] if row else None)
            if refresh_hash:
                self._run("UPDATE refresh_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                          (now, refresh_hash))
                self._run("UPDATE access_tokens SET revoked_at = ? WHERE refresh_hash = ? AND revoked_at IS NULL",
                          (now, refresh_hash))

    def revoke_account(self, account_id: str, client_id: str | None = None) -> None:
        """Revoke every MCP token of an account, optionally only those issued to one client."""
        now = int(time.time())
        clause, args = ("account_id = ?", [account_id]) if client_id is None else \
            ("account_id = ? AND client_id = ?", [account_id, client_id])
        with self.lock:
            for table in ("access_tokens", "refresh_tokens"):
                self._run(f"UPDATE {table} SET revoked_at = ? WHERE {clause} AND revoked_at IS NULL", [now, *args])

    # Maintenance -------------------------------------------------------------------
    def expired_pending(self, now: int | None = None) -> list[dict]:
        now = int(time.time()) if now is None else now
        return [dict(row) for row in self._all("SELECT * FROM pending WHERE expires_at < ?", (now,))]

    def delete_pending(self, pending_id: str) -> None:
        self._run("DELETE FROM pending WHERE id = ?", (pending_id,))

    def purge(self, now: int | None = None, unused_client_ttl: int = 24 * 3600) -> None:
        now = int(time.time()) if now is None else now
        # Revoked rows are kept until they expire so that refresh-token reuse remains detectable.
        with self.lock:
            self._run("DELETE FROM auth_codes WHERE expires_at < ?", (now,))
            self._run("DELETE FROM access_tokens WHERE expires_at < ?", (now,))
            self._run("DELETE FROM refresh_tokens WHERE expires_at < ?", (now,))
            # Registrations that never completed an authorization are reclaimed after a day.
            self._run("DELETE FROM clients WHERE used_at IS NULL AND created_at < ?", (now - unused_client_ttl,))
