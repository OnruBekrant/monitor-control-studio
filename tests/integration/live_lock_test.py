"""Kilit / ekran kapalıyken DDC sessizliği testi (gerçek monitör, geçici config).

Ekranları ~6 sn karartır (kscreen-doctor --dpms off). Servisi önce durdurun:
  systemctl --user stop monitor-ddc && python3 tests/live_lock_test.py; systemctl --user start monitor-ddc
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="monitor-ddc-locktest-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from monitor_ddc import ddc  # noqa: E402
from monitor_ddc.config import Config  # noqa: E402
from monitor_ddc.engine import Engine  # noqa: E402

app = QApplication(sys.argv[:1])
calls: list[tuple[float, list[str]]] = []
_orig = ddc._run


def traced(args, timeout=10.0):
    calls.append((time.time(), list(args)))
    return _orig(args, timeout)


ddc._run = traced
fails = 0


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def check(cond, msg):
    global fails
    print(("  ✓ " if cond else "  ✗ ") + msg)
    fails += 0 if cond else 1


def writes_since(t):
    return [a for ts, a in calls if ts >= t and "setvcp" in a]


cfg = Config()
cfg.settings.update(watchdog_interval=5, hotplug_delay=0.5)
eng = Engine(cfg)
msgs = []
eng.message.connect(lambda t, e: msgs.append(t))
eng._locked = False
eng.sync("start")
pump(6)
key = next(iter(eng.displays), None)
assert key, "monitör yok"
check(any("uygulandı" in m for m in msgs), f"açılışta profil uygulandı ({eng.displays[key].title})")

print("1) Kilitli: watchdog ve hotplug DDC trafiği üretmemeli")
eng._on_screensaver(True)
t = time.time()
pump(1)
eng.sync("hotplug", force=True)  # kilitliyken kablo takıldı gibi
pump(11)  # 2 watchdog turu
c = [a for ts, a in calls if ts >= t]
check(not c, f"kilitliyken DDC çağrısı yok ({len(c)} çağrı)")
check(key in eng._deferred, "hotplug uygulaması ertelendi")

print("2) Kilit açıldı: ertelenen uygulama yapılmalı")
t = time.time()
eng._on_screensaver(False)
pump(6)
w = writes_since(t)
check(len(w) >= 3, f"kilit açılınca profil yazıldı ({len(w)} yazma)")
check(key not in eng._deferred, "erteleme kuyruğu boşaldı")

print("3) Ekran kapalı (DPMS Off): DDC trafiği olmamalı, açılınca doğrulanmalı")
pump(4)
real = subprocess.Popen(["kscreen-doctor", "--dpms", "off"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pump(1.0)
conn = eng.displays[key].connector
simulated = Path(f"/sys/class/drm/{conn}/dpms").read_text().strip() != "Off"
if simulated:
    # KWin kapatmayı reddetti (kullanıcı etkinliği / engelleyici): sysfs okumasını taklit et
    _enum = ddc.enumerate_connectors
    ddc.enumerate_connectors = lambda: [setattr(d, "dpms", "Off") or d for d in _enum()]
    print("    (KWin ekranı kapatmadı; DPMS Off sysfs düzeyinde taklit ediliyor)")
pump(2.5)  # sysfs yoklaması fark etsin
t = time.time()
check(eng.displays[key].dpms == "Off", "motor DPMS Off'u gördü")
pump(6)
c = [a for ts, a in calls if ts >= t]
check(not c, f"ekran kapalıyken DDC çağrısı yok ({len(c)} çağrı)")
if simulated:
    ddc.enumerate_connectors = _enum
subprocess.run(["kscreen-doctor", "--dpms", "on"], capture_output=True)
t = time.time()
pump(6)
check(eng.displays[key].dpms == "On", "motor DPMS On'u gördü")
c = [a for ts, a in calls if ts >= t and "getvcp" in a]
check(bool(c), "uyanınca doğrulama okuması yapıldı")

print("4) Dahili ekran hattına hiç dokunulmadı")
buses = {a[a.index("-b") + 1] for _, a in calls if "-b" in a}
check(all("detect" not in a for _, a in calls), "ddcutil detect hiç çalışmadı")
check(buses == {str(eng.displays[key].bus)}, f"yalnızca harici monitörün hattı kullanıldı: {buses}")

eng.shutdown()
print(f"\n{'BAŞARILI' if not fails else f'{fails} HATA'} — toplam {len(calls)} ddcutil çağrısı")
sys.exit(1 if fails else 0)
