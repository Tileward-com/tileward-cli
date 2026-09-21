"""`twcli proxy serve`: the proxy a supervisor runs in the foreground and owns."""

from __future__ import annotations

import socket
import threading

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
