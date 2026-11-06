"""Dinamik testler: gerçek süreç, izole D-Bus oturumu, kötü niyetli D-Bus girdileri ve CLI."""
import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest
from conftest import ROOT

pytestmark = pytest.mark.skipif(not shutil.which("dbus-run-session") or not shutil.which("gdbus"),
                                reason="dbus-run-session/gdbus yok")

DRIVER = textwrap.dedent(r'''
    import json, os, subprocess, sys, time
    ROOT, DRM = sys.argv[1], sys.argv[2]
    run = [sys.executable, f"{ROOT}/tests/fakes/run_app.py", DRM]
    res = {}
    def call(method, *args):
        p = subprocess.run(["gdbus", "call", "--session", "--dest", "io.github.MonitorDDC",
                            "--object-path", "/", "--method", f"io.github.MonitorDDC.{method}", *args],
                           capture_output=True, text=True, timeout=10)
        return p.returncode, (p.stdout + p.stderr).strip()
    def has_name():
        p = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
                            "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.NameHasOwner",
                            "io.github.MonitorDDC"], capture_output=True, text=True)
        return "true" in p.stdout
    app = subprocess.Popen(run + ["--background"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        if has_name(): break
        time.sleep(0.1)
    res["registered"] = has_name()
    time.sleep(1.5)
    res["second_instance_rc"] = subprocess.run(run + ["--background"], timeout=15).returncode
    res["apply_ok"] = call("Apply", "Gece / Göz Koruma")
    time.sleep(1.0)
    res["apply_missing"] = call("Apply", "yok")
    res["apply_rtl"] = call("Apply", "\u202eabc")
    res["apply_huge"] = call("Apply", "A" * 100000)
    res["apply_wrongtype"] = call("Apply", "42")
    res["unknown_method"] = call("Format", "c")
    res["introspect"] = subprocess.run(["gdbus", "introspect", "--session", "--dest", "io.github.MonitorDDC",
                                        "--object-path", "/"], capture_output=True, text=True).stdout
    res["cli_apply_rc"] = subprocess.run(run + ["--apply", "Günlük Doğal"], timeout=15).returncode
    time.sleep(1.0)
    res["cli_bad_name_rc"] = subprocess.run(run + ["--apply", "\x01x"], capture_output=True, timeout=15).returncode
    res["alive_after_abuse"] = app.poll() is None
    res["quit"] = call("Quit")
    try:
        res["exit_code"] = app.wait(timeout=10)
    except subprocess.TimeoutExpired:
        app.kill(); res["exit_code"] = "timeout"
    res["log"] = app.stdout.read()[-3000:]
    print("RESULT" + json.dumps(res))
''')


def test_dbus_interface_end_to_end(env, tmp_path):
    env.sysfs.add()
    drv = tmp_path / "driver.py"
    drv.write_text(DRIVER)
    e = dict(os.environ, PYTHONPATH=str(ROOT))
    p = subprocess.run(["dbus-run-session", "--", sys.executable, str(drv), str(ROOT), str(env.sysfs.root)],
                       capture_output=True, text=True, timeout=180, env=e)
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT")), None)
    assert line, p.stdout + p.stderr
    r = json.loads(line[6:])
    log = r["log"]
    assert r["registered"], log
    assert r["second_instance_rc"] == 0, "tek örnek: ikinci başlatma sessizce çıkmalı"
    assert r["apply_ok"] == [0, "(true,)"], r["apply_ok"]
    assert r["apply_missing"] == [0, "(false,)"]
    assert r["apply_rtl"] == [0, "(false,)"], "yön değiştirici karakterli ad reddedilmeli"
    assert r["apply_huge"] == [0, "(false,)"], "aşırı uzun ad reddedilmeli"
    assert r["unknown_method"][0] != 0
    assert "Format" not in r["introspect"] and "Apply" in r["introspect"]
    assert r["cli_apply_rc"] == 0
    assert r["cli_bad_name_rc"] == 2, "CLI geçersiz adı reddetmeli"
    assert r["alive_after_abuse"], log
    assert r["exit_code"] == 0, log
    assert "Traceback" not in log, log
    v = env.monitor.values()
    assert v[0x1A] == 40, "son uygulanan 'Günlük Doğal' olmalı (CLI → D-Bus → çalışan örnek)"


def test_cli_list_without_running_instance(env):
    env.sysfs.add()
    p = subprocess.run([sys.executable, str(ROOT / "tests/fakes/run_app.py"), str(env.sysfs.root), "--list"],
                       capture_output=True, text=True, timeout=30, env=dict(os.environ))
    assert p.returncode == 0 and "NV156FHM-T06" in p.stdout and "Günlük Doğal" in p.stdout


def test_cli_direct_apply_without_dbus(env, tmp_path):
    env.sysfs.add()
    e = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=f"unix:path={tmp_path}/yok")
    p = subprocess.run([sys.executable, str(ROOT / "tests/fakes/run_app.py"), str(env.sysfs.root),
                        "--apply", "Gece / Göz Koruma"], capture_output=True, text=True, timeout=60, env=e)
    assert p.returncode == 0, p.stdout + p.stderr
    assert env.monitor.values()[0x1A] == 32
