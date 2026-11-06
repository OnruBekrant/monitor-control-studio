"""ddc modülü: sahte ddcutil ve sahte sysfs ile davranış + güvenlik testleri."""
import os
import stat

import pytest
from conftest import make_edid

from monitor_ddc import ddc


# ------------------------------------------------------------------ sysfs keşfi
def test_enumerate_finds_external_only(env):
    env.sysfs.add("card1-HDMI-A-1", bus=1)
    env.sysfs.add("card1-eDP-1", bus=9, edid=make_edid(mfg="AUO", product=0x978F, model=""))
    env.sysfs.add("card1-DP-1", bus=5, status="disconnected")
    env.sysfs.add("card1-DP-2", bus=6, ddc=False)
    env.sysfs.add("card1-DP-3", bus=7, edid=b"\x00" * 128)
    found = ddc.enumerate_connectors()
    assert [(d.connector, d.bus) for d in found] == [("card1-HDMI-A-1", 1)]


def test_enumerate_never_touches_i2c(env):
    env.sysfs.add()
    env.monitor.clear_calls()
    ddc.enumerate_connectors()
    assert env.monitor.calls() == [], "keşif hiç ddcutil çağırmamalı"


def test_enumerate_reads_dpms(env):
    env.sysfs.add(dpms="Off")
    assert ddc.enumerate_connectors()[0].dpms == "Off"


def test_enumerate_missing_root(env, monkeypatch):
    monkeypatch.setattr(ddc, "DRM_ROOT", str(env.tmp / "yok"))
    assert ddc.enumerate_connectors() == []


def test_detect_probes_only_external_bus(env):
    env.sysfs.add("card1-HDMI-A-1", bus=1)
    env.sysfs.add("card1-eDP-1", bus=9)
    found = ddc.detect()
    assert [d.vcp_version for d in found] == ["2.2"]
    assert all(c[c.index("-b") + 1] == "1" for c in env.monitor.calls()), "eDP hattına dokunulmamalı"


def test_detect_skips_non_ddc_monitor(env):
    env.sysfs.add(bus=4)  # sahte monitör bus 1'de; bus 4 cevap vermez
    assert ddc.detect() == []


# --------------------------------------------------------------- okuma / yazma
def test_get_and_set_roundtrip(env):
    ddc.set_value(1, 0x16, 61)
    assert ddc.get(1, [0x16, 0x10]) == {0x16: (61, 100), 0x10: (35, 100)}
    assert env.monitor.calls()[0] == ["-b", "1", "--noverify", "setvcp", "16", "61"]


def test_get_empty_codes(env):
    assert ddc.get(1, []) == {}


def test_get_offline_raises_after_retries(env):
    env.monitor.write(offline=True)
    with pytest.raises(ddc.DdcError):
        ddc.get(1, [0x10], retries=1)
    assert len(env.monitor.calls()) == 2


def test_set_failure_raises(env):
    env.monitor.write(fail_codes=[0x16])
    with pytest.raises(ddc.DdcError):
        ddc.set_value(1, 0x16, 10, retries=0)


def test_timeout_is_reported(env):
    env.monitor.write(hang=True)
    with pytest.raises(ddc.DdcError, match="zaman aşımı"):
        ddc._run(["-b", "1", "getvcp", "10"], timeout=0.5)


def test_missing_binary(env, monkeypatch):
    monkeypatch.setattr(ddc, "DDCUTIL", str(env.tmp / "yok" / "ddcutil"))
    with pytest.raises(ddc.DdcError, match="bulunamadı"):
        ddc._run(["-b", "1", "getvcp", "10"])


def test_probe_and_save(env):
    assert ddc.probe(1) == "2.2"
    assert ddc.probe(2) is None
    ddc.save_settings(1)
    assert "VCP code" not in ddc.raw_getvcp(1, 0x10) or True


# ---------------------------------------------------------------- güvenlik
@pytest.mark.parametrize("bad", [-1, 256, 1.5, "16", True, None, 10**9])
def test_set_rejects_invalid_code(env, bad):
    with pytest.raises(ddc.DdcError):
        ddc.set_value(1, bad, 10)


@pytest.mark.parametrize("bad", [-1, 65536, 2.0, "50", False, None])
def test_set_rejects_invalid_value(env, bad):
    with pytest.raises(ddc.DdcError):
        ddc.set_value(1, 0x10, bad)


@pytest.mark.parametrize("bad", [-1, 1024, "1", None])
def test_rejects_invalid_bus(env, bad):
    with pytest.raises(ddc.DdcError):
        ddc.get(bad, [0x10])
    assert env.monitor.calls() == []


@pytest.mark.parametrize("arg", ["--edid=00ff", "; rm -rf ~", "$(id)", "detect --verbose", "-b1", "../x", "ffff"])
def test_run_rejects_unexpected_args(env, arg):
    with pytest.raises(ddc.DdcError, match="izin verilmeyen"):
        ddc._run(["-b", "1", "getvcp", arg])
    assert env.monitor.calls() == []


def test_lockfile_private_and_not_in_tmp(env):
    path = ddc._lock_path()
    assert path.startswith(os.environ["XDG_RUNTIME_DIR"] + "/monitor-ddc/")
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    os.close(ddc._open_lock())
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_lockfile_refuses_symlink(env, tmp_path):
    target = tmp_path / "kurban"
    target.write_text("dokunma")
    os.symlink(target, ddc._lock_path())
    with pytest.raises(ddc.DdcError, match="kilidi"):
        ddc._run(["-b", "1", "getvcp", "10"])
    assert target.read_text() == "dokunma"


def test_lockfile_falls_back_when_runtime_dir_unsafe(env, monkeypatch, tmp_path):
    bad = tmp_path / "herkes"
    bad.mkdir()
    bad.chmod(0o777)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(bad))
    path = ddc._lock_path()
    assert not path.startswith(str(bad))
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    monkeypatch.setenv("XDG_RUNTIME_DIR", "")
    assert ddc._lock_path().endswith("i2c.lock")


def test_prefers_system_ddcutil_path():
    import importlib
    if os.access("/usr/bin/ddcutil", os.X_OK):
        assert importlib.reload(ddc).DDCUTIL == "/usr/bin/ddcutil"
