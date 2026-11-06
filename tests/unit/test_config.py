"""config: kalıcılık, atomiklik, izinler ve bozuk/kötü niyetli girdiye dayanıklılık."""
import json
import os
import stat

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from monitor_ddc import config
from monitor_ddc.config import Config, sanitize_codes, sanitize_settings, validate_profile_name

KEY = "BOE:NV156FHM-T06:2014:1"


def test_missing_file_gives_defaults(env):
    c = Config()
    assert c.settings == config.DEFAULT_SETTINGS and c.data["monitors"] == {}


def test_known_monitor_seeded(env):
    c = Config()
    m = c.monitor(KEY, "BOE NV156FHM-T06")
    assert m["default_profile"] == "Günlük Doğal" and m["reset_signature"]["0x16"] == 35
    assert c.monitor("XYZ:foo:0001:0")["profiles"] == {}


def test_save_is_private_and_atomic(env):
    c = Config()
    c.save_profile(KEY, "A", {0x16: 40})
    assert stat.S_IMODE(os.stat(env.config_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(env.config_file.parent).st_mode) == 0o700
    assert not [p for p in env.config_file.parent.iterdir() if p.name.startswith(".config-")]
    assert Config().profile_codes(KEY, "A") == {0x16: 40}


def test_save_failure_leaves_old_file(env, monkeypatch):
    c = Config()
    c.save_profile(KEY, "A", {0x16: 40})
    before = env.config_file.read_text()
    monkeypatch.setattr(config.json, "dump", lambda *a, **k: (_ for _ in ()).throw(OSError("disk dolu")))
    with pytest.raises(OSError):
        c.save_profile(KEY, "B", {0x16: 41})
    assert env.config_file.read_text() == before
    assert not [p for p in env.config_file.parent.iterdir() if p.name.startswith(".config-")]


def test_corrupt_file_is_backed_up(env):
    env.config_file.parent.mkdir(parents=True)
    env.config_file.write_text("{bozuk json")
    c = Config()
    assert c.data["monitors"] == {}
    assert env.config_file.with_suffix(".broken.json").exists()


@pytest.mark.parametrize("payload", ["[]", "null", "42", '"x"', '{"monitors": []}', '{"monitors": {"k": 5}}'])
def test_wrong_shapes_do_not_crash(env, payload):
    env.config_file.parent.mkdir(parents=True)
    env.config_file.write_text(payload)
    Config()


def test_huge_file_rejected(env):
    env.config_file.parent.mkdir(parents=True)
    env.config_file.write_text(" " * (3 * 1024 * 1024))
    assert Config().data["monitors"] == {}


def test_deeply_nested_json_does_not_crash(env):
    env.config_file.parent.mkdir(parents=True)
    env.config_file.write_text("[" * 100000 + "]" * 100000)
    Config()


def test_malicious_profile_values_are_dropped(env):
    env.config_file.parent.mkdir(parents=True)
    env.config_file.write_text(json.dumps({
        "settings": {"watchdog_interval": -5, "hotplug_delay": "rm -rf", "write_gap_ms": 10**9,
                     "enforce_profile": "yes", "verify_delays": [1e9], "unknown": 1},
        "monitors": {KEY: {
            "profiles": {"ok": {"0x16": 50, "0xzz": 1, "0x10": -1, "0x12": 70000, "0x87": True, "0x1a": "5"},
                         "\u202ekötü": {"0x16": 1}, "x" * 500: {}},
            "default_profile": "yok", "active_profile": "ok",
            "neutral_gains": [999, 0, 0], "reset_signature": {"0x16": "a"}, "title": 5}}}))
    c = Config()
    assert c.settings == config.DEFAULT_SETTINGS
    m = c.monitor(KEY)
    assert m["profiles"] == {"ok": {"0x16": 50}}
    assert m["default_profile"] is None and m["active_profile"] == "ok"
    assert m["neutral_gains"] == [50, 50, 50] and m["reset_signature"] == {} and m["title"] == ""


@pytest.mark.parametrize("name", ["", "   ", "a\x00b", "a\nb", "x" * 65, "\u202egnp.exe", "ab\u200bc", 5, None])
def test_invalid_profile_names(name):
    with pytest.raises(ValueError):
        validate_profile_name(name)


@pytest.mark.parametrize("name", ["Gece / Göz Koruma", "<b>html</b>", "Ünlü & İsim", "x" * 64, "  boşluk  "])
def test_valid_profile_names(name):
    assert validate_profile_name(name) == name.strip()


def test_rename_and_delete_semantics(env):
    c = Config()
    c.monitor(KEY)
    c.save_profile(KEY, "A", {0x16: 1})
    c.set_field(KEY, "default_profile", "A")
    c.set_field(KEY, "active_profile", "A")
    c.rename_profile(KEY, "A", "B")
    m = Config().monitor(KEY)
    assert "B" in m["profiles"] and "A" not in m["profiles"]
    assert m["default_profile"] == m["active_profile"] == "B"
    c.rename_profile(KEY, "B", "Günlük Doğal")  # var olan ad: değişmez
    assert "B" in c.profiles(KEY)
    with pytest.raises(ValueError):
        c.rename_profile(KEY, "B", "\x00")
    c.delete_profile(KEY, "B")
    m = Config().monitor(KEY)
    assert "B" not in m["profiles"] and m["default_profile"] is None


@settings(max_examples=300)
@given(st.dictionaries(st.text(max_size=6), st.one_of(st.integers(), st.text(), st.none(), st.booleans()),
                       max_size=20))
def test_sanitize_codes_fuzz(raw):
    out = sanitize_codes(raw)
    for k, v in out.items():
        assert 0 <= int(k, 16) <= 0xFF and isinstance(v, int) and 0 <= v <= 0xFFFF


@settings(max_examples=200)
@given(st.dictionaries(st.sampled_from(list(config.DEFAULT_SETTINGS) + ["x"]),
                       st.one_of(st.integers(), st.floats(allow_nan=True), st.text(), st.booleans(),
                                 st.lists(st.integers()), st.none())))
def test_sanitize_settings_fuzz(raw):
    s = sanitize_settings(raw)
    assert set(s) == set(config.DEFAULT_SETTINGS)
    assert 5 <= s["watchdog_interval"] <= 3600 and 0 <= s["write_gap_ms"] <= 500
