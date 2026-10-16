from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


HUMAN_OWNER_TYPES = frozenset(
    {"support", "billing", "engineering", "sre", "security", "privacy"}
)


def _owner_type_for_outcome(outcome: str) -> str:
    if outcome == "human":
        return "support"
    if outcome.startswith("handoff_"):
        candidate = outcome.removeprefix("handoff_")
        if candidate in HUMAN_OWNER_TYPES:
            return candidate
    return ""


@dataclass(frozen=True)
class ClaimResult:
    ticket_id: int
    generation: int
    run_token: str
    is_current: bool
    should_process: bool
    reason: str
    human_owned: bool
    human_owner_type: str
    customer_last_message_id: str
    customer_message_count: int
    dirty: bool = False


@dataclass(frozen=True)
class CheckResult:
    ticket_id: int
    generation: int
    run_token: str
    is_current: bool
    reason: str
    customer_last_message_id: str
    customer_message_count: int


@dataclass(frozen=True)
class FinishResult:
    ticket_id: int
    generation: int
    run_token: str
    finished: bool
    reason: str
    outcome: str


@dataclass(frozen=True)
class DeliveryAuthorization:
    ticket_id: int
    generation: int
    run_token: str
    authorized: bool
    reason: str


@dataclass(frozen=True)
class DeliveryResult:
    ticket_id: int
    generation: int
    run_token: str
    sent: bool
    relay_status: str
    follow_up_required: bool
    next_generation: int
    next_run_token: str
    customer_last_message_id: str
    customer_message_count: int


@dataclass(frozen=True)
class CodexJobBinding:
    ticket_id: int
    generation: int
    run_token: str
    job_id: str
    bound: bool
    reason: str


@dataclass(frozen=True)
class CodexResultAcceptance:
    ticket_id: int
    generation: int
    run_token: str
    job_id: str
    accepted: bool
    reason: str
    dirty: bool
    customer_last_message_id: str
    customer_message_count: int


@dataclass(frozen=True)
class CodexContinuation:
    ticket_id: int
    generation: int
    run_token: str
    source_job_id: str
    request_id: str
    prepared: bool
    reason: str
    customer_last_message_id: str
    customer_message_count: int


class TicketRunCoordinator:
    """Durable single-worker lease and public-delivery fence per ticket."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        active_lease_seconds: int = 1_800,
        coalesce_active_customer_messages: bool = False,
        ticket_allowlist: set[int] | frozenset[int] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.database_path = Path(database_path)
        self.active_lease_seconds = active_lease_seconds
        self.coalesce_active_customer_messages = coalesce_active_customer_messages
        self.ticket_allowlist = frozenset(ticket_allowlist or ())
        self._clock = clock or time.time
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ticket_runs (
                    ticket_id INTEGER PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    run_token TEXT NOT NULL UNIQUE,
                    worker_id TEXT NOT NULL DEFAULT '',
                    event_id TEXT NOT NULL,
                    customer_last_message_id TEXT NOT NULL,
                    customer_message_count INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT '',
                    human_owned INTEGER NOT NULL DEFAULT 0,
                    human_owner_type TEXT NOT NULL DEFAULT '',
                    dirty INTEGER NOT NULL DEFAULT 0,
                    codex_job_id TEXT NOT NULL DEFAULT '',
                    codex_status TEXT NOT NULL DEFAULT '',
                    analyzed_customer_last_message_id TEXT NOT NULL DEFAULT '',
                    analyzed_customer_message_count INTEGER NOT NULL DEFAULT 0,
                    dify_dispatch_state TEXT NOT NULL DEFAULT '',
                    continuation_source_job_id TEXT NOT NULL DEFAULT '',
                    continuation_request_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    lease_expires_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS ticket_run_events (
                    ticket_id INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    run_token TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (ticket_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS ticket_deliveries (
                    ticket_id INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    run_token TEXT NOT NULL,
                    body_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    relay_status TEXT NOT NULL DEFAULT '',
                    customer_last_message_id TEXT NOT NULL DEFAULT '',
                    customer_message_count INTEGER NOT NULL DEFAULT 0,
                    next_generation INTEGER NOT NULL DEFAULT 0,
                    next_run_token TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (ticket_id, run_token)
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(ticket_runs)")
            }
            if "lease_expires_at" not in columns:
                connection.execute(
                    "ALTER TABLE ticket_runs "
                    "ADD COLUMN lease_expires_at REAL NOT NULL DEFAULT 0"
                )
            if "worker_id" not in columns:
                connection.execute(
                    "ALTER TABLE ticket_runs "
                    "ADD COLUMN worker_id TEXT NOT NULL DEFAULT ''"
                )
            if "human_owned" not in columns:
                connection.execute(
                    "ALTER TABLE ticket_runs "
                    "ADD COLUMN human_owned INTEGER NOT NULL DEFAULT 0"
                )
            if "human_owner_type" not in columns:
                connection.execute(
                    "ALTER TABLE ticket_runs "
                    "ADD COLUMN human_owner_type TEXT NOT NULL DEFAULT ''"
                )
            if "dirty" not in columns:
                connection.execute(
                    "ALTER TABLE ticket_runs "
                    "ADD COLUMN dirty INTEGER NOT NULL DEFAULT 0"
                )
            for statement in (
                "ALTER TABLE ticket_runs ADD COLUMN codex_job_id TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE ticket_runs ADD COLUMN codex_status TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE ticket_runs ADD COLUMN analyzed_customer_last_message_id TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE ticket_runs ADD COLUMN analyzed_customer_message_count INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE ticket_runs ADD COLUMN dify_dispatch_state TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE ticket_runs ADD COLUMN continuation_source_job_id TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE ticket_runs ADD COLUMN continuation_request_id TEXT NOT NULL DEFAULT ''",
            ):
                column = statement.split("ADD COLUMN ", 1)[1].split(" ", 1)[0]
                if column not in columns:
                    connection.execute(statement)
            delivery_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(ticket_deliveries)")
            }
            for statement in (
                (
                    "customer_last_message_id",
                    "ALTER TABLE ticket_deliveries ADD COLUMN "
                    "customer_last_message_id TEXT NOT NULL DEFAULT ''",
                ),
                (
                    "customer_message_count",
                    "ALTER TABLE ticket_deliveries ADD COLUMN "
                    "customer_message_count INTEGER NOT NULL DEFAULT 0",
                ),
                (
                    "next_generation",
                    "ALTER TABLE ticket_deliveries ADD COLUMN "
                    "next_generation INTEGER NOT NULL DEFAULT 0",
                ),
                (
                    "next_run_token",
                    "ALTER TABLE ticket_deliveries ADD COLUMN "
                    "next_run_token TEXT NOT NULL DEFAULT ''",
                ),
            ):
                column, sql = statement
                if column not in delivery_columns:
                    connection.execute(sql)
            now = self._clock()
            connection.execute(
                """
                UPDATE ticket_runs
                SET lease_expires_at = ?
                WHERE state = 'active' AND lease_expires_at <= 0
                """,
                (now + self.active_lease_seconds,),
            )
        try:
            self.database_path.chmod(0o600)
        except OSError:
            pass

    def claim(
        self,
        *,
        ticket_id: int,
        event_id: str,
        worker_id: str = "",
        customer_last_message_id: str,
        customer_message_count: int,
        customer_requested_human: bool = False,
    ) -> ClaimResult:
        if self.ticket_allowlist and ticket_id not in self.ticket_allowlist:
            return ClaimResult(
                ticket_id=ticket_id,
                generation=0,
                run_token="",
                is_current=False,
                should_process=False,
                reason="not_allowlisted",
                human_owned=False,
                human_owner_type="",
                customer_last_message_id=customer_last_message_id,
                customer_message_count=customer_message_count,
            )
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_event = connection.execute(
                """
                SELECT generation, run_token
                FROM ticket_run_events
                WHERE ticket_id = ? AND event_id = ?
                """,
                (ticket_id, event_id),
            ).fetchone()
            current = connection.execute(
                "SELECT * FROM ticket_runs WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            current_human_owned = bool(
                current is not None and int(current["human_owned"])
            )
            current_human_owner_type = (
                str(current["human_owner_type"]) if current is not None else ""
            )
            current_is_active = bool(
                current is not None
                and current["state"] == "active"
                and float(current["lease_expires_at"]) > now
            )
            failed_event_can_retry = bool(
                existing_event is not None
                and current is not None
                and current["run_token"] == existing_event["run_token"]
                and current["state"] == "finished"
                and current["outcome"] == "failed"
            )
            if existing_event is not None and not failed_event_can_retry:
                is_current = bool(
                    current_is_active
                    and current["run_token"] == existing_event["run_token"]
                )
                if is_current:
                    same_worker = bool(worker_id) and str(current["worker_id"]) == worker_id
                    reason = (
                        "resumed_by_active_worker"
                        if same_worker
                        else "duplicate_active_event"
                    )
                elif current_human_owned:
                    reason = "human_owned"
                elif current is not None and current["run_token"] == existing_event["run_token"]:
                    reason = str(current["outcome"] or "finished")
                else:
                    reason = "superseded"
                if current_is_active or current is None or current["state"] != "active":
                    return ClaimResult(
                        ticket_id=ticket_id,
                        generation=int(existing_event["generation"]),
                        run_token=str(existing_event["run_token"]),
                        is_current=is_current,
                        should_process=is_current and same_worker,
                        reason=reason,
                        human_owned=current_human_owned,
                        human_owner_type=current_human_owner_type,
                        customer_last_message_id=(
                            str(current["customer_last_message_id"])
                            if is_current
                            else customer_last_message_id
                        ),
                        customer_message_count=(
                            int(current["customer_message_count"])
                            if is_current
                            else customer_message_count
                        ),
                    )

            if current_is_active and current_human_owned:
                stored_count = int(current["customer_message_count"])
                stored_last_id = str(current["customer_last_message_id"])
                if customer_message_count > stored_count:
                    stored_count = customer_message_count
                    stored_last_id = customer_last_message_id
                    connection.execute(
                        """
                        UPDATE ticket_runs
                        SET event_id = ?, customer_last_message_id = ?,
                            customer_message_count = ?, updated_at = ?
                        WHERE ticket_id = ? AND run_token = ?
                        """,
                        (
                            event_id,
                            stored_last_id,
                            stored_count,
                            now,
                            ticket_id,
                            str(current["run_token"]),
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO ticket_run_events
                        (ticket_id, event_id, generation, run_token, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        ticket_id,
                        event_id,
                        int(current["generation"]),
                        str(current["run_token"]),
                        now,
                    ),
                )
                return ClaimResult(
                    ticket_id=ticket_id,
                    generation=int(current["generation"]),
                    run_token=str(current["run_token"]),
                    is_current=False,
                    should_process=False,
                    reason="human_owned",
                    human_owned=True,
                    human_owner_type=current_human_owner_type,
                    customer_last_message_id=stored_last_id,
                    customer_message_count=stored_count,
                )

            if current_is_active:
                stored_count = int(current["customer_message_count"])
                stored_last_id = str(current["customer_last_message_id"])
                human_owned = current_human_owned or customer_requested_human
                human_owner_type = (
                    "support" if customer_requested_human else current_human_owner_type
                )
                has_new_customer_cursor = bool(
                    customer_message_count > stored_count
                    or (
                        customer_last_message_id
                        and customer_last_message_id != stored_last_id
                        and customer_message_count >= stored_count
                    )
                )
                if has_new_customer_cursor:
                    if (
                        self.coalesce_active_customer_messages
                        and not customer_requested_human
                    ):
                        connection.execute(
                            """
                            INSERT INTO ticket_run_events
                                (ticket_id, event_id, generation, run_token, created_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                ticket_id,
                                event_id,
                                int(current["generation"]),
                                str(current["run_token"]),
                                now,
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE ticket_runs
                            SET event_id = ?, customer_last_message_id = ?,
                                customer_message_count = ?, dirty = 1,
                                updated_at = ?, lease_expires_at = ?
                            WHERE ticket_id = ? AND run_token = ?
                            """,
                            (
                                event_id,
                                customer_last_message_id,
                                customer_message_count,
                                now,
                                now + self.active_lease_seconds,
                                ticket_id,
                                str(current["run_token"]),
                            ),
                        )
                        return ClaimResult(
                            ticket_id=ticket_id,
                            generation=int(current["generation"]),
                            run_token=str(current["run_token"]),
                            is_current=True,
                            should_process=False,
                            reason="coalesced_new_customer_message",
                            human_owned=False,
                            human_owner_type=current_human_owner_type,
                            customer_last_message_id=customer_last_message_id,
                            customer_message_count=customer_message_count,
                            dirty=True,
                        )
                    generation = int(current["generation"]) + 1
                    run_token = secrets.token_urlsafe(32)
                    connection.execute(
                        """
                        INSERT INTO ticket_run_events
                            (ticket_id, event_id, generation, run_token, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (ticket_id, event_id, generation, run_token, now),
                    )
                    connection.execute(
                        """
                        UPDATE ticket_runs
                        SET generation = ?, run_token = ?, event_id = ?, worker_id = ?,
                            customer_last_message_id = ?, customer_message_count = ?,
                            state = 'active', outcome = '', human_owned = ?,
                            human_owner_type = ?, created_at = ?, updated_at = ?,
                            lease_expires_at = ?
                        WHERE ticket_id = ?
                        """,
                        (
                            generation,
                            run_token,
                            event_id,
                            worker_id,
                            customer_last_message_id,
                            customer_message_count,
                            int(human_owned),
                            human_owner_type,
                            now,
                            now,
                            now + self.active_lease_seconds,
                            ticket_id,
                        ),
                    )
                    return ClaimResult(
                        ticket_id=ticket_id,
                        generation=generation,
                        run_token=run_token,
                        is_current=True,
                        should_process=True,
                        reason=(
                            "human_takeover_required"
                            if customer_requested_human
                            else "superseded_by_new_customer_message"
                        ),
                        human_owned=human_owned,
                        human_owner_type=human_owner_type,
                        customer_last_message_id=customer_last_message_id,
                        customer_message_count=customer_message_count,
                    )
                if customer_requested_human and not current_human_owned:
                    connection.execute(
                        """
                        UPDATE ticket_runs
                        SET human_owned = 1, human_owner_type = 'support',
                            updated_at = ?
                        WHERE ticket_id = ? AND run_token = ?
                        """,
                        (now, ticket_id, str(current["run_token"])),
                    )
                connection.execute(
                    """
                    INSERT INTO ticket_run_events
                        (ticket_id, event_id, generation, run_token, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        ticket_id,
                        event_id,
                        int(current["generation"]),
                        str(current["run_token"]),
                        now,
                    ),
                )
                return ClaimResult(
                    ticket_id=ticket_id,
                    generation=int(current["generation"]),
                    run_token=str(current["run_token"]),
                    is_current=True,
                    should_process=False,
                    reason=(
                        "human_request_coalesced"
                        if customer_requested_human
                        else (
                            "human_owned"
                            if human_owned
                            else "coalesced_into_active_run"
                        )
                    ),
                    human_owned=human_owned,
                    human_owner_type=human_owner_type,
                    customer_last_message_id=stored_last_id,
                    customer_message_count=stored_count,
                )

            if current_human_owned:
                stored_count = int(current["customer_message_count"])
                stored_last_id = str(current["customer_last_message_id"])
                if customer_message_count > stored_count:
                    stored_count = customer_message_count
                    stored_last_id = customer_last_message_id
                    connection.execute(
                        """
                        UPDATE ticket_runs
                        SET event_id = ?, customer_last_message_id = ?,
                            customer_message_count = ?, updated_at = ?
                        WHERE ticket_id = ?
                        """,
                        (
                            event_id,
                            stored_last_id,
                            stored_count,
                            now,
                            ticket_id,
                        ),
                    )
                if existing_event is None:
                    connection.execute(
                        """
                        INSERT INTO ticket_run_events
                            (ticket_id, event_id, generation, run_token, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            ticket_id,
                            event_id,
                            int(current["generation"]),
                            str(current["run_token"]),
                            now,
                        ),
                    )
                return ClaimResult(
                    ticket_id=ticket_id,
                    generation=int(current["generation"]),
                    run_token=str(current["run_token"]),
                    is_current=False,
                    should_process=False,
                    reason="human_owned",
                    human_owned=True,
                    human_owner_type=current_human_owner_type,
                    customer_last_message_id=stored_last_id,
                    customer_message_count=stored_count,
                )

            generation = int(current["generation"]) + 1 if current else 1
            run_token = secrets.token_urlsafe(32)
            if existing_event is None:
                connection.execute(
                    """
                    INSERT INTO ticket_run_events
                        (ticket_id, event_id, generation, run_token, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ticket_id, event_id, generation, run_token, now),
                )
            else:
                connection.execute(
                    """
                    UPDATE ticket_run_events
                    SET generation = ?, run_token = ?, created_at = ?
                    WHERE ticket_id = ? AND event_id = ?
                    """,
                    (generation, run_token, now, ticket_id, event_id),
                )
            connection.execute(
                """
                INSERT INTO ticket_runs (
                    ticket_id, generation, run_token, event_id,
                    worker_id,
                    customer_last_message_id, customer_message_count,
                    state, outcome, human_owned, human_owner_type,
                    dirty, codex_job_id, codex_status,
                    analyzed_customer_last_message_id,
                    analyzed_customer_message_count,
                    dify_dispatch_state, continuation_source_job_id,
                    continuation_request_id, created_at, updated_at,
                    lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', '', ?, ?, 0, '', '', '', 0, '', '', '', ?, ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                    generation = excluded.generation,
                    run_token = excluded.run_token,
                    event_id = excluded.event_id,
                    worker_id = excluded.worker_id,
                    customer_last_message_id = excluded.customer_last_message_id,
                    customer_message_count = excluded.customer_message_count,
                    state = 'active',
                    outcome = '',
                    human_owned = excluded.human_owned,
                    human_owner_type = excluded.human_owner_type,
                    dirty = 0,
                    codex_job_id = '',
                    codex_status = '',
                    analyzed_customer_last_message_id = '',
                    analyzed_customer_message_count = 0,
                    dify_dispatch_state = '',
                    continuation_source_job_id = '',
                    continuation_request_id = '',
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    lease_expires_at = excluded.lease_expires_at
                """,
                (
                    ticket_id,
                    generation,
                    run_token,
                    event_id,
                    worker_id,
                    customer_last_message_id,
                    customer_message_count,
                    int(customer_requested_human),
                    "support" if customer_requested_human else "",
                    now,
                    now,
                    now + self.active_lease_seconds,
                ),
            )
            return ClaimResult(
                ticket_id=ticket_id,
                generation=generation,
                run_token=run_token,
                is_current=True,
                should_process=True,
                reason=(
                    "human_takeover_required"
                    if customer_requested_human
                    else (
                        "claimed_after_lease_expired"
                        if current is not None and current["state"] == "active"
                        else "claimed"
                    )
                ),
                human_owned=customer_requested_human,
                human_owner_type=("support" if customer_requested_human else ""),
                customer_last_message_id=customer_last_message_id,
                customer_message_count=customer_message_count,
            )

    def bind_codex_job(
        self,
        *,
        ticket_id: int,
        run_token: str,
        job_id: str,
    ) -> CodexJobBinding:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM ticket_runs WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if current is None or current["run_token"] != run_token:
                return CodexJobBinding(
                    ticket_id, 0, run_token, job_id, False, "superseded"
                )
            generation = int(current["generation"])
            existing_job_id = str(current["codex_job_id"])
            if existing_job_id:
                return CodexJobBinding(
                    ticket_id,
                    generation,
                    run_token,
                    job_id,
                    existing_job_id == job_id,
                    "already_bound" if existing_job_id == job_id else "job_conflict",
                )
            if current["state"] != "active" or bool(current["human_owned"]):
                return CodexJobBinding(
                    ticket_id,
                    generation,
                    run_token,
                    job_id,
                    False,
                    "human_owned" if bool(current["human_owned"]) else "not_active",
                )
            connection.execute(
                """
                UPDATE ticket_runs
                SET codex_job_id = ?, codex_status = 'running',
                    updated_at = ?, lease_expires_at = ?
                WHERE ticket_id = ? AND run_token = ? AND codex_job_id = ''
                """,
                (
                    job_id,
                    now,
                    now + self.active_lease_seconds,
                    ticket_id,
                    run_token,
                ),
            )
            return CodexJobBinding(
                ticket_id, generation, run_token, job_id, True, "bound"
            )

    def prepare_dirty_continuation(
        self,
        *,
        ticket_id: int,
        source_job_id: str,
    ) -> CodexContinuation:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM ticket_runs WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if current is None:
                return CodexContinuation(
                    ticket_id, 0, "", source_job_id, "", False, "missing", "", 0
                )
            generation = int(current["generation"])
            run_token = str(current["run_token"])
            last_id = str(current["customer_last_message_id"])
            count = int(current["customer_message_count"])
            existing_source = str(current["continuation_source_job_id"])
            existing_request = str(current["continuation_request_id"])
            if existing_source == source_job_id and existing_request:
                return CodexContinuation(
                    ticket_id,
                    generation,
                    run_token,
                    source_job_id,
                    existing_request,
                    True,
                    "already_prepared",
                    last_id,
                    count,
                )
            if str(current["codex_job_id"]) != source_job_id:
                return CodexContinuation(
                    ticket_id,
                    generation,
                    run_token,
                    source_job_id,
                    "",
                    False,
                    "source_job_mismatch",
                    last_id,
                    count,
                )
            if current["state"] != "active" or bool(current["human_owned"]):
                return CodexContinuation(
                    ticket_id,
                    generation,
                    run_token,
                    source_job_id,
                    "",
                    False,
                    "human_owned" if bool(current["human_owned"]) else "not_active",
                    last_id,
                    count,
                )
            if not bool(current["dirty"]):
                return CodexContinuation(
                    ticket_id,
                    generation,
                    run_token,
                    source_job_id,
                    "",
                    False,
                    "not_dirty",
                    last_id,
                    count,
                )

            next_generation = generation + 1
            next_run_token = secrets.token_urlsafe(32)
            request_id = f"{next_run_token}:{count}"
            connection.execute(
                """
                UPDATE ticket_runs
                SET generation = ?, run_token = ?, worker_id = '',
                    dirty = 0, codex_job_id = '',
                    codex_status = 'continuation_pending',
                    analyzed_customer_last_message_id = '',
                    analyzed_customer_message_count = 0,
                    dify_dispatch_state = '',
                    continuation_source_job_id = ?,
                    continuation_request_id = ?, updated_at = ?,
                    lease_expires_at = ?
                WHERE ticket_id = ? AND run_token = ? AND codex_job_id = ?
                """,
                (
                    next_generation,
                    next_run_token,
                    source_job_id,
                    request_id,
                    now,
                    now + self.active_lease_seconds,
                    ticket_id,
                    run_token,
                    source_job_id,
                ),
            )
            connection.execute(
                """
                UPDATE ticket_run_events
                SET generation = ?, run_token = ?
                WHERE ticket_id = ? AND event_id = ?
                """,
                (
                    next_generation,
                    next_run_token,
                    ticket_id,
                    str(current["event_id"]),
                ),
            )
            return CodexContinuation(
                ticket_id,
                next_generation,
                next_run_token,
                source_job_id,
                request_id,
                True,
                "prepared",
                last_id,
                count,
            )

    def continuation_for_source(
        self,
        *,
        ticket_id: int,
        source_job_id: str,
    ) -> CodexContinuation | None:
        with self._connect() as connection:
            current = connection.execute(
                """
                SELECT * FROM ticket_runs
                WHERE ticket_id = ? AND continuation_source_job_id = ?
                  AND continuation_request_id != ''
                """,
                (ticket_id, source_job_id),
            ).fetchone()
        if current is None:
            return None
        return CodexContinuation(
            ticket_id=ticket_id,
            generation=int(current["generation"]),
            run_token=str(current["run_token"]),
            source_job_id=source_job_id,
            request_id=str(current["continuation_request_id"]),
            prepared=True,
            reason="already_prepared",
            customer_last_message_id=str(current["customer_last_message_id"]),
            customer_message_count=int(current["customer_message_count"]),
        )

    def accept_codex_result(
        self,
        *,
        ticket_id: int,
        run_token: str,
        job_id: str,
        analyzed_customer_last_message_id: str,
        analyzed_customer_message_count: int,
    ) -> CodexResultAcceptance:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM ticket_runs WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if current is None or current["run_token"] != run_token:
                return CodexResultAcceptance(
                    ticket_id, 0, run_token, job_id, False, "superseded", False, "", 0
                )
            generation = int(current["generation"])
            stored_last_id = str(current["customer_last_message_id"])
            stored_count = int(current["customer_message_count"])
            dirty = bool(current["dirty"])
            if str(current["codex_job_id"]) != job_id:
                return CodexResultAcceptance(
                    ticket_id,
                    generation,
                    run_token,
                    job_id,
                    False,
                    "job_mismatch",
                    dirty,
                    stored_last_id,
                    stored_count,
                )
            if bool(current["human_owned"]):
                return CodexResultAcceptance(
                    ticket_id,
                    generation,
                    run_token,
                    job_id,
                    False,
                    "human_owned",
                    dirty,
                    stored_last_id,
                    stored_count,
                )
            if str(current["dify_dispatch_state"]) == "dispatched":
                return CodexResultAcceptance(
                    ticket_id,
                    generation,
                    run_token,
                    job_id,
                    True,
                    "already_dispatched",
                    False,
                    stored_last_id,
                    stored_count,
                )
            if current["state"] != "active" or float(current["lease_expires_at"]) <= now:
                reason = "lease_expired" if current["state"] == "active" else "not_active"
                return CodexResultAcceptance(
                    ticket_id,
                    generation,
                    run_token,
                    job_id,
                    False,
                    reason,
                    dirty,
                    stored_last_id,
                    stored_count,
                )
            snapshot_matches = (
                stored_last_id == analyzed_customer_last_message_id
                if stored_last_id and analyzed_customer_last_message_id
                else (
                    stored_last_id == analyzed_customer_last_message_id
                    and stored_count == analyzed_customer_message_count
                )
            )
            if not snapshot_matches:
                connection.execute(
                    """
                    UPDATE ticket_runs
                    SET codex_status = 'needs_delta', dirty = 1, updated_at = ?
                    WHERE ticket_id = ? AND run_token = ? AND codex_job_id = ?
                    """,
                    (now, ticket_id, run_token, job_id),
                )
                return CodexResultAcceptance(
                    ticket_id,
                    generation,
                    run_token,
                    job_id,
                    False,
                    "customer_snapshot_changed",
                    True,
                    stored_last_id,
                    stored_count,
                )
            connection.execute(
                """
                UPDATE ticket_runs
                SET codex_status = 'succeeded', dirty = 0,
                    dify_dispatch_state = 'pending',
                    analyzed_customer_last_message_id = ?,
                    analyzed_customer_message_count = ?, updated_at = ?,
                    lease_expires_at = ?
                WHERE ticket_id = ? AND run_token = ? AND codex_job_id = ?
                """,
                (
                    analyzed_customer_last_message_id,
                    analyzed_customer_message_count,
                    now,
                    now + self.active_lease_seconds,
                    ticket_id,
                    run_token,
                    job_id,
                ),
            )
            return CodexResultAcceptance(
                ticket_id,
                generation,
                run_token,
                job_id,
                True,
                "accepted",
                False,
                stored_last_id,
                stored_count,
            )

    def mark_dify_result_dispatched(
        self,
        *,
        ticket_id: int,
        run_token: str,
        job_id: str,
    ) -> bool:
        now = self._clock()
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE ticket_runs
                SET dify_dispatch_state = 'dispatched', updated_at = ?
                WHERE ticket_id = ? AND run_token = ? AND codex_job_id = ?
                  AND codex_status IN ('succeeded', 'failed', 'timed_out')
                """,
                (now, ticket_id, run_token, job_id),
            )
            return bool(result.rowcount)

    def accept_codex_result_for_bound_job(
        self,
        *,
        ticket_id: int,
        job_id: str,
        analyzed_customer_last_message_id: str,
        analyzed_customer_message_count: int,
    ) -> CodexResultAcceptance:
        with self._connect() as connection:
            current = connection.execute(
                """
                SELECT run_token FROM ticket_runs
                WHERE ticket_id = ? AND codex_job_id = ?
                """,
                (ticket_id, job_id),
            ).fetchone()
        if current is None:
            return CodexResultAcceptance(
                ticket_id,
                0,
                "",
                job_id,
                False,
                "job_not_bound",
                False,
                "",
                0,
            )
        return self.accept_codex_result(
            ticket_id=ticket_id,
            run_token=str(current["run_token"]),
            job_id=job_id,
            analyzed_customer_last_message_id=analyzed_customer_last_message_id,
            analyzed_customer_message_count=analyzed_customer_message_count,
        )

    def accept_codex_failure_for_bound_job(
        self,
        *,
        ticket_id: int,
        job_id: str,
        status: str,
    ) -> CodexResultAcceptance:
        if status not in {"failed", "timed_out"}:
            raise ValueError("invalid Codex terminal failure status")
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT * FROM ticket_runs
                WHERE ticket_id = ? AND codex_job_id = ?
                """,
                (ticket_id, job_id),
            ).fetchone()
            if current is None:
                return CodexResultAcceptance(
                    ticket_id, 0, "", job_id, False, "job_not_bound", False, "", 0
                )
            generation = int(current["generation"])
            run_token = str(current["run_token"])
            last_id = str(current["customer_last_message_id"])
            count = int(current["customer_message_count"])
            dirty = bool(current["dirty"])
            if bool(current["human_owned"]):
                return CodexResultAcceptance(
                    ticket_id,
                    generation,
                    run_token,
                    job_id,
                    False,
                    "human_owned",
                    dirty,
                    last_id,
                    count,
                )
            if str(current["dify_dispatch_state"]) == "dispatched":
                return CodexResultAcceptance(
                    ticket_id,
                    generation,
                    run_token,
                    job_id,
                    True,
                    "already_dispatched",
                    dirty,
                    last_id,
                    count,
                )
            connection.execute(
                """
                UPDATE ticket_runs
                SET codex_status = ?, dify_dispatch_state = 'pending', updated_at = ?
                WHERE ticket_id = ? AND codex_job_id = ?
                """,
                (status, now, ticket_id, job_id),
            )
            return CodexResultAcceptance(
                ticket_id,
                generation,
                run_token,
                job_id,
                True,
                status,
                dirty,
                last_id,
                count,
            )

    def release_human_ownership(self, *, ticket_id: int) -> bool:
        """Explicitly allow a finished human-owned ticket to re-enter automation."""
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT state FROM ticket_runs WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if current is None or current["state"] == "active":
                return False
            connection.execute(
                """
                UPDATE ticket_runs
                SET human_owned = 0, human_owner_type = '', updated_at = ?
                WHERE ticket_id = ?
                """,
                (now, ticket_id),
            )
            return True

    def mark_human_owned(
        self,
        *,
        ticket_id: int,
        run_token: str,
        handoff_type: str,
    ) -> bool:
        """Persist specialist ownership before attempting a handoff delivery."""
        if handoff_type not in HUMAN_OWNER_TYPES:
            return False
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT run_token FROM ticket_runs WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
            if current is None or current["run_token"] != run_token:
                return False
            connection.execute(
                """
                UPDATE ticket_runs
                SET human_owned = 1, human_owner_type = ?, updated_at = ?
                WHERE ticket_id = ? AND run_token = ?
                """,
                (handoff_type, now, ticket_id, run_token),
            )
            return True

    def check(
        self,
        *,
        ticket_id: int,
        run_token: str,
        customer_last_message_id: str,
        customer_message_count: int,
    ) -> CheckResult:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM ticket_runs WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            checked = self._check_row(
                current,
                ticket_id=ticket_id,
                run_token=run_token,
                customer_last_message_id=customer_last_message_id,
                customer_message_count=customer_message_count,
                now=now,
            )
            if current is not None and current["run_token"] == run_token and current["state"] == "active" and float(current["lease_expires_at"]) > now:
                connection.execute(
                    """
                    UPDATE ticket_runs
                    SET updated_at = ?, lease_expires_at = ?
                    WHERE ticket_id = ? AND run_token = ? AND state = 'active'
                    """,
                    (now, now + self.active_lease_seconds, ticket_id, run_token),
                )
            return checked

    def authorize_delivery(
        self,
        *,
        ticket_id: int,
        run_token: str,
        body: str,
        customer_last_message_id: str,
        customer_message_count: int,
    ) -> DeliveryAuthorization:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT generation, state FROM ticket_deliveries
                WHERE ticket_id = ? AND run_token = ?
                """,
                (ticket_id, run_token),
            ).fetchone()
            if existing is not None:
                reason = (
                    "already_sent"
                    if existing["state"] == "sent"
                    else "delivery_in_progress_or_ambiguous"
                )
                return DeliveryAuthorization(
                    ticket_id=ticket_id,
                    generation=int(existing["generation"]),
                    run_token=run_token,
                    authorized=False,
                    reason=reason,
                )
            current = connection.execute(
                "SELECT * FROM ticket_runs WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            checked = self._check_row(
                current,
                ticket_id=ticket_id,
                run_token=run_token,
                customer_last_message_id=customer_last_message_id,
                customer_message_count=customer_message_count,
                now=now,
            )
            if not checked.is_current:
                return DeliveryAuthorization(
                    ticket_id=ticket_id,
                    generation=checked.generation,
                    run_token=run_token,
                    authorized=False,
                    reason=checked.reason,
                )
            connection.execute(
                """
                INSERT INTO ticket_deliveries (
                    ticket_id, generation, run_token, body_hash,
                    state, relay_status,
                    customer_last_message_id, customer_message_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'sending', '', ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    checked.generation,
                    run_token,
                    hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    customer_last_message_id,
                    customer_message_count,
                    now,
                    now,
                ),
            )
            return DeliveryAuthorization(
                ticket_id=ticket_id,
                generation=checked.generation,
                run_token=run_token,
                authorized=True,
                reason="authorized",
            )

    def record_delivery_result(
        self,
        *,
        ticket_id: int,
        run_token: str,
        sent: bool,
        relay_status: str,
    ) -> DeliveryResult:
        now = self._clock()
        state = "sent" if sent else "ambiguous"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            delivery = connection.execute(
                """
                SELECT * FROM ticket_deliveries
                WHERE ticket_id = ? AND run_token = ?
                """,
                (ticket_id, run_token),
            ).fetchone()
            if delivery is None:
                raise ValueError("delivery attempt does not exist")
            connection.execute(
                """
                UPDATE ticket_deliveries
                SET state = ?, relay_status = ?, updated_at = ?
                WHERE ticket_id = ? AND run_token = ?
                """,
                (state, relay_status, now, ticket_id, run_token),
            )
            current = connection.execute(
                "SELECT * FROM ticket_runs WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            generation = int(delivery["generation"])
            stored_last_id = (
                str(current["customer_last_message_id"]) if current is not None else ""
            )
            stored_count = (
                int(current["customer_message_count"]) if current is not None else 0
            )
            delivered_last_id = str(delivery["customer_last_message_id"])
            delivered_count = int(delivery["customer_message_count"])
            human_owned = bool(
                current is not None and int(current["human_owned"])
            )
            follow_up_required = bool(
                current is not None
                and current["run_token"] == run_token
                and not human_owned
                and (
                    stored_count > delivered_count
                    or (
                        stored_count == delivered_count
                        and stored_last_id != delivered_last_id
                    )
                )
            )
            if human_owned and current is not None and current["run_token"] == run_token:
                connection.execute(
                    """
                    UPDATE ticket_runs
                    SET state = 'finished', outcome = ?, updated_at = ?
                    WHERE ticket_id = ? AND run_token = ?
                    """,
                    (
                        "sent_handoff" if sent else "handoff_delivery_ambiguous",
                        now,
                        ticket_id,
                        run_token,
                    ),
                )
                return DeliveryResult(
                    ticket_id=ticket_id,
                    generation=generation,
                    run_token=run_token,
                    sent=sent,
                    relay_status=relay_status,
                    follow_up_required=False,
                    next_generation=0,
                    next_run_token="",
                    customer_last_message_id=stored_last_id,
                    customer_message_count=stored_count,
                )
            # An unknown result for the old body must remain fenced, but it
            # must not strand customer content that arrived while that send
            # was in flight.  The next generation is authorized only for the
            # newer customer snapshot; the ambiguous body remains permanently
            # bound to this old token and is never retried automatically.
            if (
                (not sent and not follow_up_required)
                or current is None
                or current["run_token"] != run_token
            ):
                return DeliveryResult(
                    ticket_id=ticket_id,
                    generation=generation,
                    run_token=run_token,
                    sent=sent,
                    relay_status=relay_status,
                    follow_up_required=False,
                    next_generation=0,
                    next_run_token="",
                    customer_last_message_id=stored_last_id,
                    customer_message_count=stored_count,
                )
            if follow_up_required:
                next_generation = int(current["generation"]) + 1
                next_run_token = secrets.token_urlsafe(32)
                connection.execute(
                    """
                    UPDATE ticket_runs
                    SET generation = ?, run_token = ?, state = 'active',
                        outcome = '', updated_at = ?, lease_expires_at = ?
                    WHERE ticket_id = ? AND run_token = ?
                    """,
                    (
                        next_generation,
                        next_run_token,
                        now,
                        now + self.active_lease_seconds,
                        ticket_id,
                        run_token,
                    ),
                )
                connection.execute(
                    """
                    UPDATE ticket_run_events
                    SET generation = ?, run_token = ?
                    WHERE ticket_id = ? AND event_id = ?
                    """,
                    (
                        next_generation,
                        next_run_token,
                        ticket_id,
                        str(current["event_id"]),
                    ),
                )
                connection.execute(
                    """
                    UPDATE ticket_deliveries
                    SET next_generation = ?, next_run_token = ?, updated_at = ?
                    WHERE ticket_id = ? AND run_token = ?
                    """,
                    (next_generation, next_run_token, now, ticket_id, run_token),
                )
                return DeliveryResult(
                    ticket_id=ticket_id,
                    generation=generation,
                    run_token=run_token,
                    sent=sent,
                    relay_status=relay_status,
                    follow_up_required=True,
                    next_generation=next_generation,
                    next_run_token=next_run_token,
                    customer_last_message_id=stored_last_id,
                    customer_message_count=stored_count,
                )

            connection.execute(
                """
                UPDATE ticket_runs
                SET state = 'finished', outcome = 'sent', updated_at = ?
                WHERE ticket_id = ? AND run_token = ?
                """,
                (now, ticket_id, run_token),
            )
            return DeliveryResult(
                ticket_id=ticket_id,
                generation=generation,
                run_token=run_token,
                sent=True,
                relay_status=relay_status,
                follow_up_required=False,
                next_generation=0,
                next_run_token="",
                customer_last_message_id=stored_last_id,
                customer_message_count=stored_count,
            )

    def finish(
        self,
        *,
        ticket_id: int,
        run_token: str,
        outcome: str,
    ) -> FinishResult:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM ticket_runs WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if current is None or current["run_token"] != run_token:
                return FinishResult(
                    ticket_id, 0, run_token, False, "superseded", outcome
                )
            generation = int(current["generation"])
            owner_type = _owner_type_for_outcome(outcome)
            if owner_type:
                connection.execute(
                    """
                    UPDATE ticket_runs
                    SET human_owned = 1, human_owner_type = ?, updated_at = ?
                    WHERE ticket_id = ? AND run_token = ?
                    """,
                    (owner_type, now, ticket_id, run_token),
                )
            if current["state"] == "finished":
                return FinishResult(
                    ticket_id,
                    generation,
                    run_token,
                    True,
                    "already_finished",
                    str(current["outcome"]),
                )
            connection.execute(
                """
                UPDATE ticket_runs
                SET state = 'finished', outcome = ?, updated_at = ?
                WHERE ticket_id = ? AND run_token = ?
                """,
                (outcome, now, ticket_id, run_token),
            )
            return FinishResult(
                ticket_id, generation, run_token, True, "finished", outcome
            )

    def _check_row(
        self,
        current: sqlite3.Row | None,
        *,
        ticket_id: int,
        run_token: str,
        customer_last_message_id: str,
        customer_message_count: int,
        now: float,
    ) -> CheckResult:
        if current is None:
            return CheckResult(ticket_id, 0, run_token, False, "missing", "", 0)
        generation = int(current["generation"])
        stored_last_id = str(current["customer_last_message_id"])
        stored_count = int(current["customer_message_count"])
        if current["run_token"] != run_token:
            return CheckResult(
                ticket_id,
                generation,
                run_token,
                False,
                "superseded",
                stored_last_id,
                stored_count,
            )
        if current["state"] != "active":
            return CheckResult(
                ticket_id,
                generation,
                run_token,
                False,
                str(current["outcome"] or "finished"),
                stored_last_id,
                stored_count,
            )
        if float(current["lease_expires_at"]) <= now:
            return CheckResult(
                ticket_id,
                generation,
                run_token,
                False,
                "lease_expired",
                stored_last_id,
                stored_count,
            )
        # The webhook and the fully materialized ticket can count customer
        # messages from different scopes (for example, the current Messaging
        # conversation versus the complete ticket history).  A stable Zendesk
        # message ID is the authoritative cursor; use the count only when one
        # side has no cursor.
        snapshot_changed = (
            stored_last_id != customer_last_message_id
            if stored_last_id and customer_last_message_id
            else (
                stored_last_id != customer_last_message_id
                or stored_count != customer_message_count
            )
        )
        if snapshot_changed:
            return CheckResult(
                ticket_id,
                generation,
                run_token,
                False,
                "customer_snapshot_changed",
                stored_last_id,
                stored_count,
            )
        return CheckResult(
            ticket_id,
            generation,
            run_token,
            True,
            "current",
            stored_last_id,
            stored_count,
        )
