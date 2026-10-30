"""설정 파일·단축키. 파일은 pytest 임시 폴더에만 쓴다."""

import pytest

from hangulkana import config
from hangulkana.config import Config


def test_defaults_roundtrip():
    assert config.parse(config.dump(Config())) == Config()


def test_load_creates_default_file(tmp_path):
    path = tmp_path / "HangulKana" / "config.toml"
    cfg = config.load(str(path))
    assert cfg == Config() and path.exists()
    assert config.load(str(path)) == Config()


def test_load_without_create(tmp_path):
    path = tmp_path / "config.toml"
    config.load(str(path), create=False)
    assert not path.exists()


def test_save_and_load(tmp_path):
    path = str(tmp_path / "config.toml")
    config.save(path, Config(overlay=False, preview=False, pause_hotkey="ctrl+shift+f12", log_level="debug"))
    assert config.load(path) == Config(False, False, "ctrl+shift+f12", "debug")


def test_parse_comments_and_unknown_keys():
    cfg = config.parse('overlay = false  # 끔\nfoo = 1\npause_hotkey = "ctrl+alt+p" # 주석\n')
    assert cfg.overlay is False and cfg.pause_hotkey == "ctrl+alt+p"
    assert any("foo" in w for w in cfg.warnings)


@pytest.mark.parametrize("text", [
    'overlay = "yes"', "overlay = 1", "preview = maybe", 'log_level = "loud"', "이상한 줄",
    'pause_hotkey = "p"', 'pause_hotkey = "ctrl+alt+??"',
])
def test_bad_values_fall_back_to_defaults(text):
    cfg = config.parse(text)
    assert cfg.warnings
    defaults = Config()
    assert cfg.overlay == defaults.overlay and cfg.preview == defaults.preview
    assert cfg.log_level == "error"


def test_empty_hotkey_means_none():
    cfg = config.parse('pause_hotkey = ""')
    assert cfg.pause_hotkey == "" and not cfg.warnings


def test_bom_is_accepted(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes("﻿overlay = false\n".encode("utf-8"))
    assert config.load(str(path)).overlay is False


@pytest.mark.parametrize("text, expected", [
    ("ctrl+alt+p", (frozenset({"ctrl", "alt"}), 0x50)),
    ("Ctrl + Alt + P", (frozenset({"ctrl", "alt"}), 0x50)),
    ("ctrl+shift+f12", (frozenset({"ctrl", "shift"}), 0x7B)),
    ("win+alt+1", (frozenset({"win", "alt"}), 0x31)),
    ("control+pause", (frozenset({"ctrl"}), 0x13)),
])
def test_parse_hotkey(text, expected):
    assert config.parse_hotkey(text) == expected


@pytest.mark.parametrize("text", ["p", "", "ctrl+", "ctrl+ctrl+p", "hyper+p", "ctrl+alt+f25", "ctrl+alt+enter"])
def test_parse_hotkey_rejects(text):
    assert config.parse_hotkey(text) is None
