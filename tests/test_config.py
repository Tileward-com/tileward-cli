from __future__ import annotations

import json
import os
import stat

from tileward.config import DEFAULT_BASE_URL, Config, fingerprint


def test_defaults_when_nothing_is_stored(isolated_config):
    cfg = Config()
    assert cfg.base_url == DEFAULT_BASE_URL
    assert cfg.api_key is None
    assert cfg.profile == "default"


def test_environment_beats_the_file(isolated_config, monkeypatch):
    cfg = Config()
    cfg.set("base_url", "https://from-file.test")
    monkeypatch.setenv("TILEWARD_BASE_URL", "https://from-env.test")
    assert Config().base_url == "https://from-env.test"


def test_profiles_are_separate(isolated_config):
    Config("staging").set_credentials(api_key="tw_live_staging")
    Config("prod").set_credentials(api_key="tw_live_prod")
    assert Config("staging").api_key == "tw_live_staging"
    assert Config("prod").api_key == "tw_live_prod"
    assert Config("default").api_key is None


def test_credentials_file_is_not_group_or_world_readable(isolated_config):
    cfg = Config()
    cfg.set_credentials(api_key="tw_live_secret")
    mode = os.stat(cfg.credentials_path).st_mode
    assert not mode & stat.S_IRGRP
    assert not mode & stat.S_IROTH
    assert mode & stat.S_IRUSR


def test_secrets_live_only_in_the_credentials_file(isolated_config):
    """`config.json` must be safe to paste into an issue."""
    cfg = Config()
    cfg.set("base_url", "https://x.test")
    cfg.set_credentials(api_key="tw_live_secret", session_token="sess")
    written = json.loads(cfg.config_path.read_text())
    assert "tw_live_secret" not in json.dumps(written)
    assert "sess" not in json.dumps(written)


def test_clearing_credentials_removes_them(isolated_config):
    cfg = Config()
    cfg.set_credentials(api_key="tw_live_x", session_token="s")
    Config().clear_credentials()
    assert Config().api_key is None
    assert Config().session_token is None


def test_as_dict_redacts_by_default(isolated_config):
    Config().set_credentials(api_key="tw_live_abcdefgh", session_token="s")
    shown = Config().as_dict()
    assert shown["api_key"] == "tw_live_…efgh"
    assert shown["session"] == "present"
    assert "abcdefgh" not in json.dumps(shown)


def test_fingerprint_never_returns_the_whole_key():
    assert fingerprint("tw_live_abcdefgh") == "tw_live_…efgh"
    assert "abcdefgh" not in fingerprint("tw_live_abcdefgh")
