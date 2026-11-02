from __future__ import annotations

from signoz_mcp.log_compact import compact_log_rows, truncate_text


TRACEBACK = (
    "Traceback (most recent call last):\n"
    + "".join(f'  File "/app/api/module_{i}.py", line {i}, in handler\n    call()\n' for i in range(20))
    + "ValueError: boom"
)
DENY = frozenset({"host.ip", "host.mac"})


def _row():
    return {
        "timestamp_utc": "2026-10-01 00:00:00.000000000",
        "body": "request failed\n" + TRACEBACK,
        "attributes_string": {
            "exception.stacktrace": TRACEBACK,
            "exception.type": "ValueError",
            "tenant_id": "t-1",
            "empty": "",
        },
        "resources_string": {
            "host.ip": "10.0.0.1",
            "host.mac": "aa:bb",
            "k8s.deployment.name": "dify-api-oltp",
        },
    }


def test_compact_drops_denylisted_and_empty_keys():
    rows, meta = compact_log_rows([_row()], denylist=DENY, max_chars=100000)

    assert rows[0]["resources_string"] == {"k8s.deployment.name": "dify-api-oltp"}
    assert "empty" not in rows[0]["attributes_string"]
    assert meta["droppedKeys"] == ["host.ip", "host.mac"]


def test_compact_keeps_one_copy_of_repeated_traceback():
    rows, meta = compact_log_rows([_row()], denylist=DENY, max_chars=100000)

    assert rows[0]["body"].endswith("ValueError: boom")
    assert rows[0]["attributes_string"]["exception.stacktrace"] == "[duplicate of body]"
    assert rows[0]["attributes_string"]["exception.type"] == "ValueError"
    assert meta["deduplicated"] == 1


def test_compact_prefers_longer_copy_when_body_is_contained_in_attribute():
    row = {"body": TRACEBACK[:200], "attributes_string": {"exception.stacktrace": TRACEBACK}}

    rows, _ = compact_log_rows([row], denylist=DENY, max_chars=100000)

    assert rows[0]["body"] == "[duplicate of attributes_string['exception.stacktrace']]"
    assert rows[0]["attributes_string"]["exception.stacktrace"] == TRACEBACK


def test_compact_truncates_long_text_with_marker_and_keeps_tail():
    rows, meta = compact_log_rows([_row()], denylist=DENY, max_chars=300)

    body = rows[0]["body"]
    assert "[truncated" in body
    assert body.endswith("ValueError: boom")
    assert len(body) < 400
    assert meta["truncated"] == 1


def test_compacted_row_is_much_smaller_than_raw():
    import json

    raw = [_row()]
    rows, _ = compact_log_rows(raw, denylist=DENY, max_chars=4000)

    assert len(json.dumps(rows)) < len(json.dumps(raw)) * 0.7


def test_truncate_text_short_value_untouched():
    assert truncate_text("abc", 10) == ("abc", False)


def test_compact_passes_through_non_list_data():
    data, meta = compact_log_rows({"unexpected": True}, denylist=DENY, max_chars=100)

    assert data == {"unexpected": True}
    assert meta == {"compact": True}
