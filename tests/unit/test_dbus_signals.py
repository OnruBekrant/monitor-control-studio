"""Kilit (ScreenSaver) D-Bus sinyalinin motora gerçekten ulaştığını izole oturumda doğrular."""
import os
import shutil
import subprocess
import sys
import textwrap

import pytest
from conftest import ROOT

pytestmark = pytest.mark.skipif(not shutil.which("dbus-run-session"), reason="dbus-run-session yok")

SCRIPT = textwrap.dedent(r'''
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtDBus import QDBusConnection, QDBusMessage
    from monitor_ddc import engine as E
    from monitor_ddc.config import Config
    E.Engine._start_udev_monitor = lambda self: None
    app = QCoreApplication([])
    bus = QDBusConnection.sessionBus()
    assert bus.registerService("org.freedesktop.ScreenSaver")
    e = E.Engine(Config())
    e._poll.stop()
    def emit(v):
        m = QDBusMessage.createSignal("/ScreenSaver", "org.freedesktop.ScreenSaver", "ActiveChanged")
        m.setArguments([v]); bus.send(m)
        end = time.time() + 2
        while time.time() < end: app.processEvents(); time.sleep(0.01)
    emit(True); a = e._locked
    emit(False); b = e._locked
    print("LOCK", a, b); e.shutdown()
''')


def test_screensaver_signal_reaches_engine(env, tmp_path):
    f = tmp_path / "s.py"
    f.write_text(SCRIPT)
    p = subprocess.run(["dbus-run-session", "--", sys.executable, str(f), str(ROOT)], capture_output=True,
                       text=True, timeout=60, env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    assert "LOCK True False" in p.stdout, p.stdout + p.stderr
