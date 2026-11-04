import base64
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
import uvicorn

from difyctl_mcp.app import create_app
from difyctl_mcp.settings import Settings

FAKE = Path(__file__).with_name("fake_difyctl.py")
API_KEY = "synthetic-probe-key"
REDIRECT = "https://client.example/callback"


@pytest.fixture
def settings(tmp_path):
    FAKE.chmod(FAKE.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    skills = tmp_path / "skills" / "difyctl" / "references"
    skills.mkdir(parents=True)
    (skills.parent / "SKILL.md").write_text("# difyctl\n")
    (skills / "operations.md").write_text("# Running operations\n")
    value = Settings(public_url="http://127.0.0.1:1/difyctl-mcp", allowed_emails=["alice@example.com"],
                     allowed_redirect_uris=[REDIRECT, "http://localhost/callback"], api_key=API_KEY,
                     data_dir=tmp_path / "data", difyctl_bin=str(FAKE), skills_dir=tmp_path / "skills",
                     dify_server="https://cloud.dify.example", poll_interval_seconds=0)
    value.validate()
    return value


def control(settings, **values):
    path = settings.home_dir / "fake.json"
    current = json.loads(path.read_text()) if path.exists() else {}
    current.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current))


def calls(settings):
    path = settings.home_dir / "fake-log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Live:
    """A real uvicorn listener with public_url pointing at it, plus OAuth client helpers."""

    def __init__(self, settings):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        settings.public_url = f"http://127.0.0.1:{self.port}/difyctl-mcp"
        self.settings = settings
        self.base = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level="error", access_log=False))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.listener]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.server.started
        self.http = httpx2.Client(base_url=self.base, trust_env=False, follow_redirects=False, timeout=30)

    def close(self):
        self.http.close()
        self.server.should_exit = True
        self.thread.join(timeout=10)

    # OAuth helpers ---------------------------------------------------------------------
    def register(self, redirect=REDIRECT, **extra):
        return self.http.post("/register", json={"redirect_uris": [redirect], "client_name": "Test client", **extra})

    def authorize(self, client, scope=None, verifier=None, redirect=REDIRECT, resource=None):
        verifier = verifier or secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-1"}
        if scope:
            params["scope"] = scope
        if resource:
            params["resource"] = resource
        response = self.http.get("/authorize", params=params)
        return response, verifier

    def login_path(self, response):
        location = urlsplit(response.headers["location"])
        return location.path.replace("/difyctl-mcp", "", 1) + "?" + location.query

    def cookie(self, response):
        """The Cookie header value for this pending login: difyctl_login_<id>=<secret>."""
        return re.search(r"(difyctl_login_[A-Za-z0-9_-]+=[^;]+)", response.headers["set-cookie"]).group(1)

    def start(self, path, csrf, headers):
        request = parse_qs(urlsplit(path).query)["request"][0]
        return self.http.post("/login/start", data={"request": request, "csrf": csrf}, headers=headers)

    def approve(self, client, scope=None, email="alice@example.com", account_id="acc-1"):
        """Drive the whole browser step and return (code, verifier, cookie, login path)."""
        response, verifier = self.authorize(client, scope)
        assert response.status_code == 302, response.text
        path, cookie = self.login_path(response), self.cookie(response)
        headers = {"Cookie": cookie}
        page = self.http.get(path, headers=headers)
        assert page.status_code == 200 and "Test client" in page.text and "client.example" in page.text
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
        started = self.start(path, csrf, headers)
        assert started.status_code == 303
        waiting = self.http.get(path, headers=headers)
        assert "ABCD-1234" in waiting.text, waiting.text
        control(self.settings, approve=True, email=email, account_id=account_id)
        done = self.http.get(path, headers=headers)
        return done, verifier, cookie, path

    def exchange(self, client, code, verifier, redirect=REDIRECT):
        return self.http.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                                              "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
                                              "code_verifier": verifier})

    def tokens(self, client=None, scope=None):
        client = client or self.register().json()
        done, verifier, _, _ = self.approve(client, scope)
        assert done.status_code == 303, done.text
        code = parse_qs(urlsplit(done.headers["location"]).query)["code"][0]
        response = self.exchange(client, code, verifier)
        assert response.status_code == 200, response.text
        return client, response.json()

    def rpc(self, token, method, params=None):
        response = self.http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                                  headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"})
        return response

    def call(self, token, name, arguments=None):
        response = self.rpc(token, "tools/call", {"name": name, "arguments": arguments or {}})
        assert response.status_code == 200, response.text
        return response.json()["result"]["structuredContent"]


@pytest.fixture
def live(settings):
    server = Live(settings)
    yield server
    server.close()
