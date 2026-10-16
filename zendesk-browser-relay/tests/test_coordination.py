from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from zendesk_browser_relay.coordination import TicketRunCoordinator


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _coordinator(
    tmp_path: Path,
    *,
    clock: FakeClock | None = None,
    active_lease_seconds: int = 1_800,
) -> TicketRunCoordinator:
    return TicketRunCoordinator(
        tmp_path / "ticket-runs.sqlite3",
        clock=clock,
        active_lease_seconds=active_lease_seconds,
    )


def test_claim_is_idempotent_for_the_same_event(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)

    first = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    duplicate = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )

    assert first.generation == 1
    assert first.run_token == duplicate.run_token
    assert duplicate.generation == 1
    assert duplicate.is_current is True
    assert duplicate.should_process is False
    assert duplicate.reason == "duplicate_active_event"


def test_ticket_allowlist_fails_closed_without_creating_actor_state(
    tmp_path: Path,
) -> None:
    coordinator = TicketRunCoordinator(
        tmp_path / "ticket-allowlist.sqlite3",
        ticket_allowlist={999999999},
    )

    result = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )

    assert result.should_process is False
    assert result.is_current is False
    assert result.reason == "not_allowlisted"
    assert result.generation == 0
    assert result.run_token == ""


def test_same_workflow_can_resume_a_claim_after_lost_tool_response(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)

    first = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        worker_id="workflow-run-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    resumed = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        worker_id="workflow-run-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    duplicate_webhook = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        worker_id="workflow-run-2",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )

    assert resumed.run_token == first.run_token
    assert resumed.should_process is True
    assert resumed.reason == "resumed_by_active_worker"
    assert duplicate_webhook.run_token == first.run_token
    assert duplicate_webhook.should_process is False
    assert duplicate_webhook.reason == "duplicate_active_event"


def test_new_customer_event_supersedes_the_active_run(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    first = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )

    second = coordinator.claim(
        ticket_id=3880,
        event_id="message-2",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )

    assert second.generation == 2
    assert second.run_token != first.run_token
    assert second.is_current is True
    assert second.should_process is True
    assert second.reason == "superseded_by_new_customer_message"
    assert (
        coordinator.check(
            ticket_id=3880,
            run_token=first.run_token,
            customer_last_message_id="message-1",
            customer_message_count=1,
        ).reason
        == "superseded"
    )
    assert (
        coordinator.check(
            ticket_id=3880,
            run_token=second.run_token,
            customer_last_message_id="message-2",
            customer_message_count=2,
        ).is_current
        is True
    )


def test_new_customer_event_can_be_coalesced_into_one_active_codex_run(
    tmp_path: Path,
) -> None:
    coordinator = TicketRunCoordinator(
        tmp_path / "ticket-runs-coalesced.sqlite3",
        coalesce_active_customer_messages=True,
    )
    first = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )

    second = coordinator.claim(
        ticket_id=3880,
        event_id="message-2",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )

    assert second.generation == first.generation
    assert second.run_token == first.run_token
    assert second.is_current is True
    assert second.should_process is False
    assert second.reason == "coalesced_new_customer_message"
    assert second.customer_last_message_id == "message-2"
    assert second.customer_message_count == 2
    assert second.dirty is True
    stale_snapshot = coordinator.check(
        ticket_id=3880,
        run_token=first.run_token,
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    assert stale_snapshot.is_current is False
    assert stale_snapshot.reason == "customer_snapshot_changed"


def test_codex_job_binding_is_idempotent_and_rejects_a_different_job(
    tmp_path: Path,
) -> None:
    coordinator = TicketRunCoordinator(tmp_path / "ticket-job-binding.sqlite3")
    claim = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )

    first = coordinator.bind_codex_job(
        ticket_id=3880,
        run_token=claim.run_token,
        job_id="11111111-1111-4111-8111-111111111111",
    )
    repeated = coordinator.bind_codex_job(
        ticket_id=3880,
        run_token=claim.run_token,
        job_id="11111111-1111-4111-8111-111111111111",
    )
    conflicting = coordinator.bind_codex_job(
        ticket_id=3880,
        run_token=claim.run_token,
        job_id="22222222-2222-4222-8222-222222222222",
    )

    assert first.bound is True
    assert first.reason == "bound"
    assert repeated.bound is True
    assert repeated.reason == "already_bound"
    assert conflicting.bound is False
    assert conflicting.reason == "job_conflict"


def test_codex_result_is_accepted_only_for_the_latest_coalesced_cursor(
    tmp_path: Path,
) -> None:
    coordinator = TicketRunCoordinator(
        tmp_path / "ticket-result-cursor.sqlite3",
        coalesce_active_customer_messages=True,
    )
    claim = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    job_id = "11111111-1111-4111-8111-111111111111"
    assert coordinator.bind_codex_job(
        ticket_id=3880,
        run_token=claim.run_token,
        job_id=job_id,
    ).bound
    coordinator.claim(
        ticket_id=3880,
        event_id="message-2",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )

    stale = coordinator.accept_codex_result(
        ticket_id=3880,
        run_token=claim.run_token,
        job_id=job_id,
        analyzed_customer_last_message_id="message-1",
        analyzed_customer_message_count=1,
    )
    current = coordinator.accept_codex_result(
        ticket_id=3880,
        run_token=claim.run_token,
        job_id=job_id,
        analyzed_customer_last_message_id="message-2",
        analyzed_customer_message_count=2,
    )

    assert stale.accepted is False
    assert stale.reason == "customer_snapshot_changed"
    assert stale.dirty is True
    assert current.accepted is True
    assert current.reason == "accepted"
    assert current.dirty is False


def test_dirty_result_prepares_one_idempotent_continuation_generation(
    tmp_path: Path,
) -> None:
    coordinator = TicketRunCoordinator(
        tmp_path / "ticket-continuation.sqlite3",
        coalesce_active_customer_messages=True,
    )
    first = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    old_job_id = "11111111-1111-4111-8111-111111111111"
    assert coordinator.bind_codex_job(
        ticket_id=3880,
        run_token=first.run_token,
        job_id=old_job_id,
    ).bound
    coordinator.claim(
        ticket_id=3880,
        event_id="message-2",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )

    prepared = coordinator.prepare_dirty_continuation(
        ticket_id=3880,
        source_job_id=old_job_id,
    )
    repeated = coordinator.prepare_dirty_continuation(
        ticket_id=3880,
        source_job_id=old_job_id,
    )

    assert prepared.prepared is True
    assert prepared.reason == "prepared"
    assert prepared.generation == first.generation + 1
    assert prepared.run_token != first.run_token
    assert prepared.request_id == f"{prepared.run_token}:2"
    assert repeated.prepared is True
    assert repeated.reason == "already_prepared"
    assert repeated.generation == prepared.generation
    assert repeated.run_token == prepared.run_token
    assert repeated.request_id == prepared.request_id

    new_job_id = "22222222-2222-4222-8222-222222222222"
    assert coordinator.bind_codex_job(
        ticket_id=3880,
        run_token=prepared.run_token,
        job_id=new_job_id,
    ).bound


def test_superseding_event_can_resume_after_the_old_run_finishes(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    first = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        worker_id="workflow-run-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    superseding = coordinator.claim(
        ticket_id=3880,
        event_id="message-2",
        worker_id="workflow-run-2",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )
    assert superseding.should_process is True
    assert superseding.reason == "superseded_by_new_customer_message"
    old_finish = coordinator.finish(
        ticket_id=3880,
        run_token=first.run_token,
        outcome="failed",
    )
    assert old_finish.finished is False
    assert old_finish.reason == "superseded"

    recovered = coordinator.claim(
        ticket_id=3880,
        event_id="message-2",
        worker_id="workflow-run-2",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )

    assert recovered.should_process is True
    assert recovered.is_current is True
    assert recovered.reason == "resumed_by_active_worker"
    assert recovered.generation == 2
    assert recovered.run_token != first.run_token


def test_snapshot_change_fails_closed_until_the_active_run_refreshes_it(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    claimed = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )

    checked = coordinator.check(
        ticket_id=3880,
        run_token=claimed.run_token,
        customer_last_message_id="message-2",
        customer_message_count=2,
    )

    assert checked.is_current is False
    assert checked.reason == "customer_snapshot_changed"


def test_same_last_customer_message_is_current_across_counting_scopes(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    claimed = coordinator.claim(
        ticket_id=3726,
        event_id="event-1",
        customer_last_message_id="message-8",
        customer_message_count=8,
    )

    checked = coordinator.check(
        ticket_id=3726,
        run_token=claimed.run_token,
        customer_last_message_id="message-8",
        customer_message_count=45,
    )

    assert checked.is_current is True
    assert checked.reason == "current"


def test_delivery_fence_allows_only_one_attempt_per_generation(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    claimed = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )

    authorized = coordinator.authorize_delivery(
        ticket_id=3880,
        run_token=claimed.run_token,
        body="Customer-ready answer",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    second_begin = coordinator.authorize_delivery(
        ticket_id=3880,
        run_token=claimed.run_token,
        body="Customer-ready answer",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )

    assert authorized.authorized is True
    assert authorized.reason == "authorized"
    assert second_begin.authorized is False
    assert second_begin.reason == "delivery_in_progress_or_ambiguous"

    coordinator.record_delivery_result(
        ticket_id=3880,
        run_token=claimed.run_token,
        sent=True,
        relay_status="sent",
    )
    after_sent = coordinator.authorize_delivery(
        ticket_id=3880,
        run_token=claimed.run_token,
        body="Customer-ready answer",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    assert after_sent.authorized is False
    assert after_sent.reason == "already_sent"


def test_customer_message_arriving_during_send_starts_a_new_generation(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    claimed = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        worker_id="workflow-run-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    first_delivery = coordinator.authorize_delivery(
        ticket_id=3880,
        run_token=claimed.run_token,
        body="First customer-ready answer",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    assert first_delivery.authorized is True

    superseding = coordinator.claim(
        ticket_id=3880,
        event_id="message-2",
        worker_id="workflow-run-2",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )
    assert superseding.should_process is True
    assert superseding.reason == "superseded_by_new_customer_message"

    recorded = coordinator.record_delivery_result(
        ticket_id=3880,
        run_token=claimed.run_token,
        sent=True,
        relay_status="sent",
    )

    assert recorded.follow_up_required is False
    assert recorded.customer_last_message_id == "message-2"
    assert recorded.customer_message_count == 2

    second_delivery = coordinator.authorize_delivery(
        ticket_id=3880,
        run_token=superseding.run_token,
        body="Answer that includes the second message",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )
    assert second_delivery.authorized is True
    assert superseding.run_token != claimed.run_token

    finished = coordinator.record_delivery_result(
        ticket_id=3880,
        run_token=superseding.run_token,
        sent=True,
        relay_status="sent",
    )
    assert finished.follow_up_required is False


def test_customer_message_survives_an_ambiguous_earlier_delivery(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    claimed = coordinator.claim(
        ticket_id=3926,
        event_id="ticket-created",
        worker_id="workflow-run-1",
        customer_last_message_id="",
        customer_message_count=0,
    )
    assert coordinator.authorize_delivery(
        ticket_id=3926,
        run_token=claimed.run_token,
        body="What can we help you with?",
        customer_last_message_id="",
        customer_message_count=0,
    ).authorized

    superseding = coordinator.claim(
        ticket_id=3926,
        event_id="message-refund",
        worker_id="workflow-run-2",
        customer_last_message_id="message-refund",
        customer_message_count=1,
    )
    assert superseding.reason == "superseded_by_new_customer_message"
    assert superseding.should_process is True

    recorded = coordinator.record_delivery_result(
        ticket_id=3926,
        run_token=claimed.run_token,
        sent=False,
        relay_status="exception:RuntimeError",
    )

    assert recorded.sent is False
    assert recorded.relay_status == "exception:RuntimeError"
    assert recorded.follow_up_required is False
    assert recorded.next_generation == 0
    assert recorded.next_run_token == ""
    assert recorded.customer_last_message_id == "message-refund"
    assert recorded.customer_message_count == 1
    assert coordinator.check(
        ticket_id=3926,
        run_token=superseding.run_token,
        customer_last_message_id="message-refund",
        customer_message_count=1,
    ).is_current


def test_customer_message_after_reply_commit_starts_a_new_generation(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    first = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        worker_id="workflow-run-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    delivery = coordinator.authorize_delivery(
        ticket_id=3880,
        run_token=first.run_token,
        body="First answer",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    coordinator.record_delivery_result(
        ticket_id=3880,
        run_token=first.run_token,
        sent=True,
        relay_status="sent",
    )

    second = coordinator.claim(
        ticket_id=3880,
        event_id="message-2",
        worker_id="workflow-run-2",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )

    assert second.should_process is True
    assert second.generation == first.generation + 1
    assert second.run_token != first.run_token


def test_human_request_locks_future_customer_events(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    requested = coordinator.claim(
        ticket_id=3880,
        event_id="message-human",
        worker_id="workflow-human",
        customer_last_message_id="message-human",
        customer_message_count=1,
        customer_requested_human=True,
    )

    assert requested.should_process is True
    assert requested.reason == "human_takeover_required"
    assert requested.human_owned is True
    assert requested.human_owner_type == "support"

    coordinator.finish(
        ticket_id=3880,
        run_token=requested.run_token,
        outcome="human",
    )
    follow_up = coordinator.claim(
        ticket_id=3880,
        event_id="message-follow-up",
        worker_id="workflow-follow-up",
        customer_last_message_id="message-follow-up",
        customer_message_count=2,
    )

    assert follow_up.should_process is False
    assert follow_up.reason == "human_owned"
    assert follow_up.human_owned is True
    assert follow_up.human_owner_type == "support"
    assert follow_up.generation == requested.generation
    assert follow_up.run_token == requested.run_token
    assert follow_up.customer_last_message_id == "message-follow-up"
    assert follow_up.customer_message_count == 2


def test_human_request_during_active_run_supersedes_and_locks(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    active = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        worker_id="workflow-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    takeover = coordinator.claim(
        ticket_id=3880,
        event_id="message-human",
        worker_id="workflow-2",
        customer_last_message_id="message-human",
        customer_message_count=2,
        customer_requested_human=True,
    )

    assert takeover.should_process is True
    assert takeover.reason == "human_takeover_required"
    assert takeover.human_owned is True
    assert takeover.run_token != active.run_token
    assert takeover.generation == 2
    assert coordinator.check(
        ticket_id=3880,
        run_token=active.run_token,
        customer_last_message_id="message-1",
        customer_message_count=1,
    ).reason == "superseded"


def test_human_lock_survives_restart_and_requires_explicit_release(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ticket-runs.sqlite3"
    first_process = TicketRunCoordinator(database_path)
    requested = first_process.claim(
        ticket_id=3926,
        event_id="message-human",
        customer_last_message_id="message-human",
        customer_message_count=1,
        customer_requested_human=True,
    )
    first_process.finish(
        ticket_id=3926,
        run_token=requested.run_token,
        outcome="human",
    )

    second_process = TicketRunCoordinator(database_path)
    blocked = second_process.claim(
        ticket_id=3926,
        event_id="message-more-context",
        customer_last_message_id="message-more-context",
        customer_message_count=2,
    )
    assert blocked.should_process is False
    assert blocked.reason == "human_owned"

    assert second_process.release_human_ownership(ticket_id=3926) is True
    resumed = second_process.claim(
        ticket_id=3926,
        event_id="message-after-release",
        customer_last_message_id="message-after-release",
        customer_message_count=3,
    )
    assert resumed.should_process is True
    assert resumed.human_owned is False
    assert resumed.generation == requested.generation + 1


def test_finish_handoff_outcome_locks_future_customer_events(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    claimed = coordinator.claim(
        ticket_id=3880,
        event_id="message-billing",
        customer_last_message_id="message-billing",
        customer_message_count=1,
    )

    finished = coordinator.finish(
        ticket_id=3880,
        run_token=claimed.run_token,
        outcome="handoff_billing",
    )
    assert finished.finished is True

    later = coordinator.claim(
        ticket_id=3880,
        event_id="message-after-handoff",
        customer_last_message_id="message-after-handoff",
        customer_message_count=2,
    )
    assert later.should_process is False
    assert later.reason == "human_owned"
    assert later.human_owned is True
    assert later.human_owner_type == "billing"


def test_mark_human_owned_before_delivery_survives_sent_result(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    claimed = coordinator.claim(
        ticket_id=3880,
        event_id="message-sre",
        customer_last_message_id="message-sre",
        customer_message_count=1,
    )

    assert coordinator.mark_human_owned(
        ticket_id=3880,
        run_token=claimed.run_token,
        handoff_type="sre",
    ) is True
    assert coordinator.authorize_delivery(
        ticket_id=3880,
        run_token=claimed.run_token,
        body="Customer-safe technical-team handoff.",
        customer_last_message_id="message-sre",
        customer_message_count=1,
    ).authorized
    coalesced = coordinator.claim(
        ticket_id=3880,
        event_id="message-during-sre-handoff",
        customer_last_message_id="message-during-sre-handoff",
        customer_message_count=2,
    )
    assert coalesced.should_process is False
    assert coalesced.reason == "human_owned"

    delivered = coordinator.record_delivery_result(
        ticket_id=3880,
        run_token=claimed.run_token,
        sent=True,
        relay_status="sent",
    )
    assert delivered.follow_up_required is False

    later = coordinator.claim(
        ticket_id=3880,
        event_id="message-after-sre-handoff",
        customer_last_message_id="message-after-sre-handoff",
        customer_message_count=2,
    )
    assert later.should_process is False
    assert later.reason == "human_owned"
    assert later.human_owner_type == "sre"


def test_state_and_delivery_fence_survive_process_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "ticket-runs.sqlite3"
    first_process = TicketRunCoordinator(database_path)
    claimed = first_process.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    assert first_process.authorize_delivery(
        ticket_id=3880,
        run_token=claimed.run_token,
        body="Customer-ready answer",
        customer_last_message_id="message-1",
        customer_message_count=1,
    ).authorized
    first_process.record_delivery_result(
        ticket_id=3880,
        run_token=claimed.run_token,
        sent=True,
        relay_status="sent",
    )

    restarted_process = TicketRunCoordinator(database_path)
    duplicate_claim = restarted_process.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    duplicate_send = restarted_process.authorize_delivery(
        ticket_id=3880,
        run_token=claimed.run_token,
        body="Customer-ready answer",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )

    assert duplicate_claim.run_token == claimed.run_token
    assert duplicate_claim.should_process is False
    assert duplicate_send.authorized is False
    assert duplicate_send.reason == "already_sent"


def test_two_processes_leave_only_one_active_worker(tmp_path: Path) -> None:
    database_path = tmp_path / "ticket-runs.sqlite3"
    first_process = TicketRunCoordinator(
        database_path, coalesce_active_customer_messages=True
    )
    second_process = TicketRunCoordinator(
        database_path, coalesce_active_customer_messages=True
    )
    barrier = Barrier(2)

    def claim(coordinator: TicketRunCoordinator, event_number: int):
        barrier.wait()
        return coordinator.claim(
            ticket_id=3880,
            event_id=f"message-{event_number}",
            customer_last_message_id=f"message-{event_number}",
            customer_message_count=event_number,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(claim, first_process, 1),
            executor.submit(claim, second_process, 2),
        ]
        results = [future.result() for future in futures]

    assert {result.generation for result in results} == {1}
    assert len({result.run_token for result in results}) == 1
    assert sum(result.should_process for result in results) == 1
    latest_count = max(result.customer_message_count for result in results)
    latest_id = f"message-{latest_count}"
    checks = [
        first_process.check(
            ticket_id=3880,
            run_token=result.run_token,
            customer_last_message_id=latest_id,
            customer_message_count=latest_count,
        )
        for result in results
    ]
    assert all(check.is_current for check in checks)


def test_expired_worker_is_replaced_by_a_new_generation(tmp_path: Path) -> None:
    clock = FakeClock()
    coordinator = _coordinator(
        tmp_path,
        clock=clock,
        active_lease_seconds=30,
    )
    first = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    clock.advance(31)
    second = coordinator.claim(
        ticket_id=3880,
        event_id="message-2",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )

    assert second.generation == 2
    assert second.run_token != first.run_token
    assert second.should_process is True
    assert second.reason == "claimed_after_lease_expired"

    stale_finish = coordinator.finish(
        ticket_id=3880,
        run_token=first.run_token,
        outcome="stale",
    )
    current_check = coordinator.check(
        ticket_id=3880,
        run_token=second.run_token,
        customer_last_message_id="message-2",
        customer_message_count=2,
    )

    assert stale_finish.finished is False
    assert stale_finish.reason == "superseded"
    assert current_check.is_current is True


def test_new_customer_cursor_supersedes_even_after_lease_renewal(tmp_path: Path) -> None:
    clock = FakeClock()
    coordinator = _coordinator(
        tmp_path,
        clock=clock,
        active_lease_seconds=30,
    )
    first = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )

    clock.advance(20)
    assert coordinator.check(
        ticket_id=3880,
        run_token=first.run_token,
        customer_last_message_id="message-1",
        customer_message_count=1,
    ).is_current
    clock.advance(20)
    second = coordinator.claim(
        ticket_id=3880,
        event_id="message-2",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )

    assert second.run_token != first.run_token
    assert second.should_process is True
    assert second.reason == "superseded_by_new_customer_message"


def test_out_of_order_webhook_does_not_regress_latest_customer_snapshot(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    current = coordinator.claim(
        ticket_id=3880,
        event_id="message-2",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )

    delayed = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )

    assert delayed.run_token == current.run_token
    assert delayed.customer_last_message_id == "message-2"
    assert delayed.customer_message_count == 2
    assert coordinator.check(
        ticket_id=3880,
        run_token=current.run_token,
        customer_last_message_id="message-2",
        customer_message_count=2,
    ).is_current
