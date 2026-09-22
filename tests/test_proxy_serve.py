"""`twcli proxy serve`: the proxy a supervisor runs in the foreground and owns."""

from __future__ import annotations

import os
import socket
import threading
import time

import httpx
import pytest
from click.testing import CliRunner

from tileward.cli.agents import responses, runner
from tileward.cli.agents.proxy import Proxy, run_until_stopped
from tileward.cli.main import cli
from tileward.client import Tileward

ENV = {
    "TILEWARD_BASE_URL": "https://api.test",
    "TILEWARD_API_KEY": "tw_live_testkey",
    "TILEWARD_CONFIG_DIR": "",
}


def offline_client() -> Tileward:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected outbound call: {request.url}")

    return Tileward(
        api_key="tw_live_testkey",
        base_url="https://api.test",
        load_config=False,
        http_client=httpx.Client(transport=httpx.MockTransport(refuse)),
    )


def test_a_given_token_is_the_one_the_proxy_checks():
    proxy = Proxy(
        client=offline_client(), adapter=responses, model="m",
        routes={"/v1/responses": "responses"}, token="fixed-token",
    )
    proxy.start()
    try:
        assert proxy.token == "fixed-token"
        wrong = httpx.post(
            f"{proxy.base_url}/v1/responses", json={}, headers={"Authorization": "Bearer nope"}
        )
        assert wrong.status_code == 401
    finally:
        proxy.stop()


def test_without_a_token_each_proxy_mints_its_own():
    a = Proxy(client=offline_client(), adapter=responses, model="m", routes={})
    b = Proxy(client=offline_client(), adapter=responses, model="m", routes={})
    try:
        assert a.token and b.token and a.token != b.token
    finally:
        a._httpd.server_close()
        b._httpd.server_close()


def test_run_until_stopped_serves_then_releases_the_port():
    proxy = Proxy(
        client=offline_client(), adapter=responses, model="m",
        routes={"/v1/responses": "responses"}, token="t",
    )
    port = proxy.port
    stop, ready = threading.Event(), threading.Event()
    worker = threading.Thread(target=run_until_stopped, args=(proxy, ready.set, stop))
    worker.start()
    assert ready.wait(5)
    assert httpx.get(f"http://127.0.0.1:{port}/nope").status_code == 404
    stop.set()
    worker.join(5)
    assert not worker.is_alive()
    # Released: a supervisor restarting on the same fixed port must be able to bind it again,
    # straight away. The request above leaves that port in TIME_WAIT, so this is the real case —
    # and it is a new Proxy, not a bare socket, because the proxy's server sets SO_REUSEADDR and a
    # bare socket would fail where the restart succeeds.
    again = Proxy(
        client=offline_client(), adapter=responses, model="m", routes={}, port=port, token="t"
    )
    again._httpd.server_close()


def test_the_proxy_answers_at_once_when_reverse_dns_is_slow(monkeypatch):
    """The stdlib server looks up its own host name before it listens. Where reverse DNS is slow,
    nothing can connect until that returns, and a supervisor waiting on the proxy gives up."""
    lookups, unblock = [], threading.Event()

    def unanswered_lookup(name=""):
        lookups.append(name)
        unblock.wait(30)
        return name

    monkeypatch.setattr(socket, "getfqdn", unanswered_lookup)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stop = threading.Event()

    def serve():
        proxy = Proxy(
            client=offline_client(), adapter=responses, model="m", routes={}, port=port, token="t"
        )
        run_until_stopped(proxy, lambda: None, stop, watch_parent=False)

    worker = threading.Thread(target=serve)
    worker.start()
    try:
        deadline = time.monotonic() + 1.0
        while True:
            try:
                answer = httpx.get(f"http://127.0.0.1:{port}/nope", timeout=1)
                break
            except httpx.TransportError:
                assert time.monotonic() < deadline, f"not answering after 1s; lookups: {lookups}"
                time.sleep(0.02)
        assert answer.status_code == 404
    finally:
        unblock.set()
        stop.set()
        worker.join(5)


def test_an_empty_token_file_is_refused(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("  \n")
    result = CliRunner().invoke(
        cli, ["proxy", "serve", "--port", "51799", "--token-file", str(token)], env=ENV
    )
    assert result.exit_code != 0
    assert "is empty" in result.output


def test_a_missing_token_file_is_refused(tmp_path):
    result = CliRunner().invoke(
        cli, ["proxy", "serve", "--port", "51799", "--token-file", str(tmp_path / "nope")], env=ENV
    )
    assert result.exit_code != 0


def test_a_port_already_in_use_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "resolve_model", lambda ctx, requested: "m")
    token = tmp_path / "token"
    token.write_text("t")
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        result = CliRunner().invoke(
            cli, ["proxy", "serve", "--port", str(port), "--token-file", str(token)], env=ENV
        )
    assert result.exit_code != 0
    assert f"127.0.0.1:{port}" in result.output


@pytest.mark.parametrize("port", ["0", "70000"])
def test_the_port_must_be_a_real_one(tmp_path, port):
    token = tmp_path / "token"
    token.write_text("t")
    result = CliRunner().invoke(
        cli, ["proxy", "serve", "--port", port, "--token-file", str(token)], env=ENV
    )
    assert result.exit_code == 2


def test_serve_exits_when_the_process_that_started_it_dies(tmp_path):
    """A supervisor that crashes cannot stop its proxy. The proxy must notice and stop itself,
    or it holds the port and the supervisor's next launch can never bind it."""
    import subprocess
    import sys
    import time

    token = tmp_path / "token"
    token.write_text("t")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    pid_file = tmp_path / "serve.pid"
    # A parent that starts `twcli proxy serve` and then dies without stopping it.
    parent = subprocess.run(
        [sys.executable, "-c", f"""
import os, subprocess, sys, time
env = dict(os.environ, TILEWARD_API_KEY="tw_live_testkey", TILEWARD_BASE_URL="https://api.test",
           TILEWARD_MODEL="m")  # a default resolves with no network call
child = subprocess.Popen(
    [sys.executable, "-c", "import sys; from tileward.cli.main import main; sys.exit(main())",
     "proxy", "serve", "--port", "{port}", "--token-file", "{token}"],
    env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open("{pid_file}", "w").write(str(child.pid))
import socket
for _ in range(200):
    try:
        socket.create_connection(("127.0.0.1", {port}), 0.1).close()
        sys.exit(0)  # serving: now die without stopping it
    except OSError:
        time.sleep(0.05)
sys.exit(1)  # never served, so this test would prove nothing
"""],
        timeout=30,
    )
    assert parent.returncode == 0, "serve never started listening"
    child_pid = int(pid_file.read_text())
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(child_pid, 9)
        raise AssertionError("serve outlived the process that started it")
