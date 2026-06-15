"""Config loading + validation — the 'did I set this up right?' guarantee.

If someone clones the repo and edits config.toml, these are the mistakes they'll
make. Each should produce a clear ConfigError, not a confusing crash deep in the
pull. Also covers default-filling and relative-path resolution (paths resolve
against the config file's dir, so schedulers can launch from anywhere).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ytmetrics.config import ChannelConfig, Config, ConfigError, load_config

MINIMAL = """
[[channels]]
name = "main"
channel_id = "mine"
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


# --- happy path -------------------------------------------------------------

def test_minimal_config_loads_with_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL))
    assert isinstance(cfg, Config)
    assert len(cfg.channels) == 1
    ch = cfg.channels[0]
    assert ch.name == "main"
    assert ch.secret_backend == "file"            # default
    assert ch.include_revenue is False            # default
    assert ch.track_revisions is True             # default
    # defaults for the top-level knobs
    assert cfg.backup_before_pull is True
    assert cfg.backup_retention_days == 14
    assert cfg.max_api_calls_per_run == 2000
    assert cfg.log_level == "INFO"


def test_relative_paths_resolve_against_config_dir(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL))
    assert cfg.db_path == tmp_path / "ytmetrics.db"
    assert cfg.backup_dir == tmp_path / "backups"
    assert cfg.channels[0].token_file == tmp_path / "secrets" / "token_main.json"


def test_log_level_is_uppercased(tmp_path):
    # log_level is a top-level key; it must be normalized to upper case.
    cfg = load_config(_write(tmp_path, 'log_level = "debug"\n' + MINIMAL))
    assert cfg.log_level == "DEBUG"


# --- validation errors ------------------------------------------------------

def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_no_channels_raises(tmp_path):
    with pytest.raises(ConfigError, match="at least one"):
        load_config(_write(tmp_path, 'db_path = "x.db"\n'))


def test_channel_missing_name_raises(tmp_path):
    with pytest.raises(ConfigError, match="missing 'name'"):
        load_config(_write(tmp_path, '[[channels]]\nchannel_id = "mine"\n'))


def test_duplicate_channel_names_raise(tmp_path):
    text = (
        '[[channels]]\nname = "main"\nchannel_id = "mine"\n'
        '[[channels]]\nname = "main"\nchannel_id = "UC123"\n'
    )
    with pytest.raises(ConfigError, match="duplicate channel name"):
        load_config(_write(tmp_path, text))


def test_missing_channel_id_raises(tmp_path):
    with pytest.raises(ConfigError, match="missing 'channel_id'"):
        load_config(_write(tmp_path, '[[channels]]\nname = "main"\n'))


def test_invalid_secret_backend_raises(tmp_path):
    text = '[[channels]]\nname = "main"\nchannel_id = "mine"\nsecret_backend = "vault"\n'
    with pytest.raises(ConfigError, match="invalid secret_backend"):
        load_config(_write(tmp_path, text))


def test_on_failure_requires_exactly_one_action(tmp_path):
    both = MINIMAL + '\n[on_failure]\ncommand = "x"\nwebhook = "https://h"\n'
    with pytest.raises(ConfigError, match="exactly one"):
        load_config(_write(tmp_path, both))

    neither = MINIMAL + '\n[on_failure]\nother = "x"\n'
    with pytest.raises(ConfigError, match="exactly one"):
        load_config(_write(tmp_path, neither))


def test_on_failure_with_one_action_is_accepted(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL + '\n[on_failure]\nwebhook = "https://h"\n'))
    assert cfg.on_failure == {"webhook": "https://h"}


# --- ChannelConfig / Config helpers ----------------------------------------

def test_analytics_ids_for_mine_and_explicit():
    base = dict(
        client_secret=Path("c.json"), secret_backend="file",
        token_file=Path("t.json"), include_revenue=False, track_revisions=True,
    )
    assert ChannelConfig(name="a", channel_id="mine", **base).analytics_ids == "channel==MINE"
    assert ChannelConfig(name="a", channel_id="MINE", **base).analytics_ids == "channel==MINE"
    assert (
        ChannelConfig(name="a", channel_id="UCabc", **base).analytics_ids
        == "channel==UCabc"
    )


def test_channel_lookup_and_missing(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL))
    assert cfg.channel("main").name == "main"
    with pytest.raises(ConfigError, match="no channel named"):
        cfg.channel("ghost")
