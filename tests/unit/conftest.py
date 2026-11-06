"""Ortak test altyapısı: sahte ddcutil, sahte sysfs, yalıtılmış config/runtime dizinleri.

Gerçek monitöre, gerçek I2C'ye ya da kullanıcının config'ine dokunulmaz.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FAKE_DDCUTIL = ROOT / "tests" / "fakes" / "ddcutil"

DEFAULT_VALUES = {0x10: 35, 0x12: 50, 0x14: 0x08, 0x16: 50, 0x18: 50, 0x1A: 50,
                  0x6C: 80, 0x6E: 80, 0x70: 80, 0x87: 2, 0x62: 0, 0xDF: 0x0202}


def make_edid(mfg="BOE", product=0x2014, serial=1, model="NV156FHM-T06", serial_str="") -> bytes:
    e = bytearray(128)
    e[0:8] = bytes.fromhex("00ffffffffffff00")
    m = sum((ord(ch) - 64) << s for ch, s in zip(mfg, (10, 5, 0), strict=True))
    e[8], e[9] = m >> 8, m & 0xFF
    e[10], e[11] = product & 0xFF, product >> 8
    e[12:16] = serial.to_bytes(4, "little")
    e[16], e[17] = 42, 34
    descs = [(0xFC, model)] + ([(0xFF, serial_str)] if serial_str else [])
    for i, (tag, txt) in enumerate(descs):
        off = 54 + 18 * (i + 1)
        e[off:off + 5] = bytes([0, 0, 0, tag, 0])
        e[off + 5:off + 18] = (txt.encode()[:13] + b"\n").ljust(13, b" ")[:13]
    return bytes(e)


class FakeSysfs:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir()

    def add(self, name="card1-HDMI-A-1", bus=1, status="connected", dpms="On", edid=None, ddc=True):
        d = self.root / name
        d.mkdir()
        (d / "status").write_text(status + "\n")
        (d / "dpms").write_text(dpms + "\n")
        (d / "edid").write_bytes(make_edid() if edid is None else edid)
        if ddc:
            (self.root / f"i2c-{bus}").mkdir(exist_ok=True)
            (d / "ddc").symlink_to(f"../i2c-{bus}")
        return d

    def set(self, name, **kw):
        for k, v in kw.items():
            (self.root / name / k).write_text(v + "\n")


class FakeMonitor:
    def __init__(self, path: Path):
        self.path = path
        self.write(bus=1, values={str(k): v for k, v in DEFAULT_VALUES.items()}, calls=[])

    def _locked(self):
        import contextlib
        import fcntl

        @contextlib.contextmanager
        def cm():
            with open(str(self.path) + ".lock", "a") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                yield
        return cm()

    def _save(self, st):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(self.path)

    def read(self) -> dict:
        with self._locked():
            return json.loads(self.path.read_text())

    def write(self, **kw):
        with self._locked():
            st = json.loads(self.path.read_text()) if self.path.exists() else {}
            st.update(kw)
            self._save(st)

    def values(self) -> dict[int, int]:
        return {int(k): v for k, v in self.read()["values"].items()}

    def set(self, **codes):
        with self._locked():
            st = json.loads(self.path.read_text())
            for k, v in codes.items():
                st["values"][str(int(k.removeprefix("c"), 16))] = v
            self._save(st)

    def calls(self) -> list[list[str]]:
        return self.read()["calls"]

    def clear_calls(self):
        self.write(calls=[])


@pytest.fixture
def env(tmp_path, monkeypatch):
    from monitor_ddc import ddc
    rt = tmp_path / "runtime"
    rt.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FAKE_DDC_STATE", str(tmp_path / "monitor.json"))
    monkeypatch.setattr(ddc, "DDCUTIL", str(FAKE_DDCUTIL))
    sysfs = FakeSysfs(tmp_path / "drm")
    monkeypatch.setattr(ddc, "DRM_ROOT", str(sysfs.root))
    from monitor_ddc import config
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config" / "monitor-ddc")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config" / "monitor-ddc" / "config.json")
    mon = FakeMonitor(tmp_path / "monitor.json")
    return type("Env", (), {"sysfs": sysfs, "monitor": mon, "tmp": tmp_path,
                            "config_file": tmp_path / "config" / "monitor-ddc" / "config.json"})


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(["test"])


def pump(app, sec: float):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_until(app, cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False
