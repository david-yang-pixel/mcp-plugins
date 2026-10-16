"""Test teleport status parser."""
from datetime import timedelta

from db_gateway.teleport import (
    TshStatus,
    parse_tsh_status,
    _parse_duration,
)


def test_parse_tsh_status_normal():
    output = """> Profile URL:        https://teleport.dify.dev:443
  Logged in as:       yangyonggang
  Cluster:            teleport.dify.dev
  Roles:              production-read-all, staging-read-all
  Kubernetes:         enabled
  Valid until:        2026-04-22 23:34:52 +0800 CST [valid for 11h59m]
"""
    status = parse_tsh_status(output)
    assert isinstance(status, TshStatus)
    assert status.user == "yangyonggang"
    assert status.cluster == "teleport.dify.dev"
    assert "production-read-all" in status.roles
    assert "staging-read-all" in status.roles
    assert status.remaining > timedelta(hours=11)
    assert status.remaining < timedelta(hours=12)


def test_parse_tsh_status_not_logged_in():
    assert parse_tsh_status("Not logged in") is None


def test_parse_tsh_status_empty():
    assert parse_tsh_status("") is None


def test_parse_tsh_status_minimal_30m():
    output = """  Logged in as:       bot
  Cluster:            cl
  Roles:              r1
  Valid until:        2026-04-22 12:00:00 +0000 UTC [valid for 30m]
"""
    status = parse_tsh_status(output)
    assert status is not None
    assert status.remaining == timedelta(minutes=30)


def test_parse_duration_combinations():
    assert _parse_duration("11h59m") == timedelta(hours=11, minutes=59)
    assert _parse_duration("5h") == timedelta(hours=5)
    assert _parse_duration("30m") == timedelta(minutes=30)
    assert _parse_duration("45s") == timedelta(seconds=45)
    assert _parse_duration("1h30m45s") == timedelta(hours=1, minutes=30, seconds=45)
    assert _parse_duration("") == timedelta()


def test_parse_tsh_status_garbage():
    assert parse_tsh_status("some random garbage") is None
