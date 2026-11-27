"""aws_logs: the long-running `tsh proxy aws` and the botocore client behind it."""
import time

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ProxyConnectionError,
    ReadTimeoutError,
)

from db_gateway import aws_logs
from db_gateway.aws_logs import classify_page_error, get_logs_client, parse_proxy_env
from db_gateway.tsh_daemons import stop_all

PROXY_OUT = """
Started AWS proxy on http://127.0.0.1:45137.
To avoid port randomization, you can choose the listening port using the --port flag.

Use the following credentials and HTTPS proxy setting to connect to the proxy:
  export AWS_ACCESS_KEY_ID=AKIAFAKE
  export AWS_SECRET_ACCESS_KEY=secretfake
  export AWS_CA_BUNDLE="{ca}"
  export HTTPS_PROXY="http://127.0.0.1:45137"
"""


@pytest.fixture(autouse=True)
async def _stop_daemons():
    yield
    await stop_all()


def test_parse_proxy_env_strips_quotes():
    env = parse_proxy_env(PROXY_OUT.format(ca="/x/localca.pem").splitlines())
    assert env == {"AWS_ACCESS_KEY_ID": "AKIAFAKE", "AWS_SECRET_ACCESS_KEY": "secretfake",
                   "AWS_CA_BUNDLE": "/x/localca.pem", "HTTPS_PROXY": "http://127.0.0.1:45137"}
    assert parse_proxy_env(PROXY_OUT.format(ca="/x").splitlines()[:6]) is None


async def test_proxy_logs_into_app_then_starts_proxy(cfg, fake_tsh, tmp_path):
    fake_tsh.push()                                              # tsh apps login
    fake_tsh.push(stdout=PROXY_OUT.format(ca=tmp_path / "ca.pem"), sleep=30)
    env = await aws_logs.AWS_PROXY.ensure(cfg, time.monotonic() + 10)
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:45137"
    assert [c["argv"] for c in fake_tsh.calls] == [
        ["apps", "login", "--aws-role", "SupportLambdaLogsReadOnly", "aws-support-logs"],
        ["proxy", "aws", "--app", "aws-support-logs"],
    ]


async def test_client_is_cached_and_rebuilt_after_proxy_change(cfg, monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    envs = [
        {"AWS_ACCESS_KEY_ID": "A", "AWS_SECRET_ACCESS_KEY": "S", "AWS_CA_BUNDLE": str(ca),
         "HTTPS_PROXY": "http://127.0.0.1:1"},
    ]

    async def ensure(cfg, deadline):
        return dict(envs[-1])

    monkeypatch.setattr(aws_logs.AWS_PROXY, "ensure", ensure)
    c1 = await get_logs_client(cfg, time.monotonic() + 5)
    assert await get_logs_client(cfg, time.monotonic() + 5) is c1
    assert c1.meta.region_name == "us-east-1"
    envs.append({**envs[0], "HTTPS_PROXY": "http://127.0.0.1:2"})
    assert await get_logs_client(cfg, time.monotonic() + 5) is not c1


@pytest.mark.parametrize("exc,kind", [
    (ClientError({"Error": {"Code": "403", "Message": ""}}, "FilterLogEvents"), "proxy"),
    (ClientError({"Error": {"Code": "ResourceNotFoundException",
                            "Message": "The specified log group does not exist."}}, "FilterLogEvents"), "aws"),
    (EndpointConnectionError(endpoint_url="https://logs.us-east-1.amazonaws.com"), "proxy"),
    (ProxyConnectionError(proxy_url="http://127.0.0.1:1", error="refused"), "proxy"),
    (ReadTimeoutError(endpoint_url="https://logs.us-east-1.amazonaws.com"), "timeout"),
    (RuntimeError("boom"), "aws"),
])
def test_classify_page_error(exc, kind):
    got, message = classify_page_error(exc)
    assert got == kind and message
    if kind == "aws" and isinstance(exc, ClientError):
        assert message.startswith("ResourceNotFoundException: The specified log group")
