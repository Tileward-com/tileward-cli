from __future__ import annotations

import pytest

from tileward.client import Tileward


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Never read or write the developer's real ~/.config/tileward while testing.

    Autouse and not opt-in: one test that forgets it would silently pick up a live key and, worse,
    could overwrite the credentials of whoever ran the suite.
    """
    monkeypatch.setenv("TILEWARD_CONFIG_DIR", str(tmp_path / "config"))
    for name in (
        "TILEWARD_API_KEY",
        "TILEWARD_SESSION",
        "TILEWARD_BASE_URL",
        "TILEWARD_CONTEXT_URL",
        "TILEWARD_CONVERSATION",
        "TILEWARD_MODEL",
        "TILEWARD_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "config"


@pytest.fixture
def client():
    return Tileward(
        api_key="tw_live_testkey1234",
        session_token="session.token",
        base_url="https://api.test",
        # A DIFFERENT host on purpose. api.tileward.com serves /v1 only and answers the session
        # surface with 404 wrong_host, so a fixture that collapses the two cannot catch a call
        # sent to the wrong one.
        console_url="https://console.test",
        context_url="https://context.test",
        load_config=False,
    )
