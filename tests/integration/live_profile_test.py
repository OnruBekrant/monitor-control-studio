"""Gerçek monitörde profil yaşam döngüsü testi (geçici config ile, kullanıcı profillerine dokunmaz).

Servis kendi profilini doğrularken testin yazdıklarını geri alabileceği için önce durdurun:
  systemctl --user stop monitor-ddc && python3 tests/live_profile_test.py; systemctl --user start monitor-ddc
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="monitor-ddc-test-")
os.environ["XDG_CONFIG_HOME"] = tmp
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from monitor_ddc import ddc, vcp  # noqa: E402
from monitor_ddc.config import Config, CONFIG_FILE  # noqa: E402
from monitor_ddc.engine import Engine  # noqa: E402
from monitor_ddc.ui import MainWindow, Tray  # noqa: E402

app = QApplication(sys.argv[:1])
fails = 0
log: list[str] = []


def pump(sec: float):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def settle(extra: float = 0.3, timeout: float = 20):
    """İşçi kuyruğu boşalana kadar bekle (sabit bekleme yerine)."""
    end = time.time() + timeout
    pump(0.2)
    while time.time() < end and not eng.worker.idle():
        pump(0.05)
    pump(extra)


def check(cond, msg):
    global fails
    print(("  ✓ " if cond else "  ✗ ") + msg)
    fails += 0 if cond else 1


def disk_profiles():
    data = json.loads(CONFIG_FILE.read_text())
    return next(iter(data["monitors"].values()))


def hw(codes):
    return {c: v for c, (v, _m) in ddc.get(w._display().bus, codes).items()}


cfg = Config()
eng = Engine(cfg)
eng.message.connect(lambda t, e: log.append(t))
w = MainWindow(eng, cfg)
tray = Tray(eng, cfg, lambda: None, lambda: None)
eng.sync("start")
for _ in range(100):
    pump(0.1)
    if w.key:
        break
assert w.key, "monitör algılanamadı"
print(f"Monitör: {w._display().title} (i2c-{w._display().bus}), config: {CONFIG_FILE}")
settle()

print("1) Yeni profil oluştur ve kaydet")
for code, val in {vcp.GAIN_R: 58, vcp.GAIN_G: 47, vcp.GAIN_B: 36, vcp.BRIGHTNESS: 30}.items():
    w.sliders[code].set_value(val)
    w._live(code, val)
settle()
err = w.save_profile_as("Test Akşam", {"gain", "basic", "brightness"})
check(err is None, f"save_profile_as hatasız ({err})")
p = disk_profiles()["profiles"].get("Test Akşam")
check(p is not None, "profil diske yazıldı")
check(p and p["0x16"] == 58 and p["0x18"] == 47 and p["0x1a"] == 36 and p["0x10"] == 30,
      f"diskteki değerler doğru: {p}")
check("0x6c" not in (p or {}) and "0x62" not in (p or {}), "işaretlenmeyen gruplar (siyah, ses) profilde yok")
check(Config().profiles(w.key).get("Test Akşam") == p, "yeni Config nesnesi aynı profili okuyor")
check(w.profile_combo.currentData() == "Test Akşam", "açılır listede yeni profil seçili")
check("Test Akşam" in [a.text().replace("  ★", "") for a in tray.menu.actions()], "tepsi menüsünde görünüyor")

print("2) Başka profile geç, sonra yeni profili uygula ve monitörden doğrula")
eng.apply_profile(w.key, "Günlük Doğal")
settle()
v = hw([0x16, 0x18, 0x1a, 0x10])
check(v == {0x16: 52, 0x18: 50, 0x1a: 40, 0x10: 35}, f"'Günlük Doğal' monitörde: {v}")
log.clear()
eng.apply_profile(w.key, "Test Akşam")
settle()
pump(3.5)  # 3. saniye doğrulaması
settle()
v = hw([0x16, 0x18, 0x1a, 0x10, 0x12, 0x87])
check(v[0x16] == 58 and v[0x18] == 47 and v[0x1a] == 36 and v[0x10] == 30, f"'Test Akşam' monitörde: {v}")
check(any("doğrulandı" in m for m in log), "motor uygulama sonrası doğrulamayı bildirdi")
check(disk_profiles()["active_profile"] == "Test Akşam", "aktif profil diske kaydedildi")

print("2b) Uygulamadan hemen sonra elle değişiklik, bekleyen doğrulama tarafından geri alınmamalı")
eng.apply_profile(w.key, "Test Akşam")
settle()
w.sliders[vcp.GAIN_B].set_value(44)
w._live(vcp.GAIN_B, 44)
pump(9.0)  # 3 ve 8. saniye doğrulamaları geçsin
settle()
v = hw([0x1a])
check(v[0x1a] == 44, f"elle verilen mavi=44 korundu: {v}")

print("3) Yeniden adlandır")
err = w.rename_profile_to("Test Akşam", "Test Gece")
check(err is None, "yeniden adlandırma hatasız")
d = disk_profiles()
check("Test Gece" in d["profiles"] and "Test Akşam" not in d["profiles"], "diskte yeni ad var, eskisi yok")
check(d["active_profile"] == "Test Gece", "aktif profil adı da güncellendi")
check(w.profile_combo.currentData() == "Test Gece", "açılır listede yeniden adlandırılan seçili kaldı")
check(list(d["profiles"]).index("Test Gece") == 3, "profil sırası korundu")
check(w.rename_profile_to("Test Gece", "Günlük Doğal") is not None, "var olan ada yeniden adlandırma reddedildi")
check(w.rename_profile_to("Test Gece", "   ") is not None, "boş ad reddedildi")
check("Test Gece" in disk_profiles()["profiles"], "reddedilen denemeler profili bozmadı")

print("4) Güncelle (aynı gruplar, yeni değer)")
w.sliders[vcp.GAIN_B].set_value(33)
w.sliders[vcp.BRIGHTNESS].set_value(30)
w._update_profile()
d = disk_profiles()["profiles"]["Test Gece"]
check(d["0x1a"] == 33 and set(d) == {"0x16", "0x18", "0x1a", "0x10", "0x12", "0x87"},
      f"güncellenen profil: {d}")

print("4b) Sapma şeridi ve 'Profile geri dön'")
eng.apply_profile(w.key, "Test Gece")
settle()
pump(0.5)
check(not w.profile_diff(), f"profil yüklendikten sonra fark yok: {w.profile_diff()}")
w.sliders[vcp.GAIN_B].set_value(45)
w._live(vcp.GAIN_B, 45)
settle()
check(w.profile_diff() == [(vcp.GAIN_B, 33, 45)] and not w.dirty_bar.isHidden(),
      f"elle değişiklik şeritte görünüyor: {w.dirty_label.text()}")
w._apply_selected()  # 'Profile geri dön'
settle()
pump(0.5)
check(hw([0x1a])[0x1a] == 33 and not w.profile_diff() and w.dirty_bar.isHidden(),
      "'Profile geri dön' monitörü profile döndürdü ve şerit kayboldu")

print("4c) Siyah seviyesi tek çubuk: üç kodu birlikte yazar")
w.sliders[vcp.BLACK_R].set_value(70)
w._live_black(vcp.BLACK_R, 70)
settle()
v = hw([0x6c, 0x6e, 0x70])
check(v == {0x6c: 70, 0x6e: 70, 0x70: 70}, f"0x6C/6E/70 = 70: {v}")
w._live_black(vcp.BLACK_R, 80)
settle()

print("4d) Katı mod açıkken bile parlaklık (KDE) geri alınmaz; profille aynı imza sıfırlanma sayılmaz")
cfg.settings["enforce_profile"] = True
cfg.set_field(w.key, "reset_signature", {"0x16": 58, "0x18": 47, "0x1a": 33})  # profille aynı (yanlış imza)
log.clear()
ddc.set_value(w._display().bus, 0x10, 22)  # KDE parlaklığı değiştirdi
eng.verify(w.key, "watchdog")
settle()
pump(1.0)
settle()
check(hw([0x10])[0x10] == 22, "harici parlaklık değişikliği korundu")
check(not any("yeniden uygulanıyor" in m for m in log), f"yeniden uygulama tetiklenmedi: {log}")
ddc.set_value(w._display().bus, 0x16, 35)
ddc.set_value(w._display().bus, 0x18, 35)
ddc.set_value(w._display().bus, 0x1a, 47)
cfg.set_field(w.key, "reset_signature", {"0x16": 35, "0x18": 35, "0x1a": 47})
cfg.settings["enforce_profile"] = False
eng.verify(w.key, "watchdog")
settle()
pump(1.0)
settle()
v = hw([0x16, 0x18, 0x1a, 0x10])
check(v[0x16] == 58 and v[0x1a] == 33, f"gerçek sıfırlanma (35/35/47) düzeltildi: {v}")
check(v[0x10] == 22, "bu sırada parlaklığa yine dokunulmadı")

print("5) Varsayılan yap ve sil")
w._make_default()
check(disk_profiles()["default_profile"] == "Test Gece", "varsayılan diske yazıldı")
cfg.delete_profile(w.key, "Test Gece")
w._refresh_profiles()
d = disk_profiles()
check("Test Gece" not in d["profiles"] and d["default_profile"] is None, "silindi, varsayılan temizlendi")

eng.apply_profile(w.key, "Günlük Doğal")  # monitörü eski hâline getir
settle()
eng.shutdown()
print(f"\n{'BAŞARILI' if not fails else f'{fails} HATA'} — geçici config: {tmp}")
sys.exit(1 if fails else 0)
