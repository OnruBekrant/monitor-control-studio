"""ui: pencere davranışı ve arayüz güvenliği (HTML enjeksiyonu, riskli yazım onayı)."""
import pytest
from conftest import pump, wait_until
from PySide6.QtWidgets import QMessageBox

from monitor_ddc import vcp
from monitor_ddc.config import Config

KEY = "BOE:NV156FHM-T06:2014:1"


@pytest.fixture
def win(env, qapp, monkeypatch):
    from monitor_ddc import engine as eng_mod
    from monitor_ddc.ui import MainWindow, Tray
    monkeypatch.setattr(eng_mod.Engine, "_start_udev_monitor", lambda self: None)
    monkeypatch.setattr(eng_mod.Engine, "_connect_dbus_signals", lambda self: None)
    monkeypatch.setattr(eng_mod.Engine, "_query_lock_state", lambda self: None)
    cfg = Config()
    cfg.settings.update(verify_delays=[0.3], watchdog_enabled=False, write_gap_ms=0)
    e = eng_mod.Engine(cfg)
    e._poll.stop()
    w = MainWindow(e, cfg)
    t = Tray(e, cfg, lambda: None, lambda: None)
    env.sysfs.add()
    e.sync("start")
    assert wait_until(qapp, lambda: w.key == KEY and e.displays[KEY].vcp_version, 10)
    settle(qapp, e)
    yield type("W", (), {"w": w, "e": e, "tray": t, "cfg": cfg})
    e.shutdown()


def settle(qapp, e):
    wait_until(qapp, e.worker.idle, 10)
    pump(qapp, 0.3)
    wait_until(qapp, e.worker.idle, 10)


def test_sliders_reflect_monitor(win, env):
    assert win.w.sliders[vcp.GAIN_B].value() == 40
    assert win.w.sliders[vcp.BLACK_R].value() == 80


def test_save_profile_and_reload(win, env, qapp):
    w = win.w
    w.sliders[vcp.GAIN_R].set_value(60)
    w._live(vcp.GAIN_R, 60)
    settle(qapp, win.e)
    assert w.save_profile_as("Yeni", {"gain"}) is None
    assert Config().profile_codes(KEY, "Yeni") == {0x16: 60, 0x18: 50, 0x1A: 40}
    assert "Yeni" in [a.text() for a in win.tray.menu.actions()]


@pytest.mark.parametrize("bad", ["", "a\x00", "\u202eexe", "x" * 100])
def test_save_rejects_bad_names(win, bad):
    assert win.w.save_profile_as(bad, {"gain"}) is not None
    assert win.w.rename_profile_to("Günlük Doğal", bad) is not None


def test_html_in_names_is_escaped(win, qapp):
    w = win.w
    evil = "<img src=x><b>X</b>"
    assert w.save_profile_as(evil, {"gain"}) is None
    w._update_dirty()
    assert "<img" not in w.default_label.text() and "&lt;img" in w.default_label.text()
    w.sliders[vcp.GAIN_B].set_value(1)
    w._update_dirty()
    assert "<img" not in w.dirty_label.text()


def test_edid_strings_escaped(win, env, qapp):
    d = win.e.displays[KEY]
    d.model = "<a href=x>m</a>"
    w = win.w
    w._monitor_selected()
    assert "<a href" not in w.hw_info.text()


def test_ampersand_in_tray(win):
    win.w.save_profile_as("R&D", {"gain"})
    assert "R&&D" in [a.text() for a in win.tray.menu.actions()]


def test_black_slider_writes_three_codes(win, env, qapp):
    win.w._live_black(vcp.BLACK_R, 70)
    settle(qapp, win.e)
    v = env.monitor.values()
    assert v[0x6C] == v[0x6E] == v[0x70] == 70


def test_dirty_bar_and_revert(win, env, qapp):
    w = win.w
    w.sliders[vcp.GAIN_B].set_value(45)
    w._live(vcp.GAIN_B, 45)
    settle(qapp, win.e)
    assert w.profile_diff() == [(vcp.GAIN_B, 40, 45)] and not w.dirty_bar.isHidden()
    w._apply_selected()
    settle(qapp, win.e)
    assert env.monitor.values()[0x1A] == 40 and w.dirty_bar.isHidden()


def test_signature_equal_to_profile_refused(win, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a))
    win.cfg.set_field(KEY, "reset_signature", {})
    win.w._capture_signature()
    assert warned and win.cfg.reset_signature(KEY) == {}


@pytest.mark.parametrize("code,answer,expect_write", [
    (0x04, QMessageBox.StandardButton.No, False),
    (0xD6, QMessageBox.StandardButton.No, False),
    (0x04, QMessageBox.StandardButton.Yes, True),
    (0x16, None, True),
])
def test_dangerous_raw_write_needs_confirmation(win, env, qapp, monkeypatch, code, answer, expect_write):
    asked = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: asked.append(code) or answer)
    win.w.raw_code.setText(f"{code:02x}")
    win.w.raw_val.setValue(1)
    env.monitor.clear_calls()
    win.w._raw_set()
    settle(qapp, win.e)
    wrote = any("setvcp" in c and c[-2] == f"{code:02x}" for c in env.monitor.calls())
    assert wrote == expect_write
    assert bool(asked) == (answer is not None)


def test_raw_invalid_hex(win):
    win.w.raw_code.setText("zz")
    win.w._raw_get()
    assert "Geçersiz" in win.w.raw_out.toPlainText()


def test_close_hides_instead_of_quitting(win, monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    win.w.show()
    win.w.close()
    assert not win.w.isVisible()


# ------------------------------------------------------------ diyalog yolları
def test_new_profile_dialog_flow(win, env, qapp, monkeypatch):
    from monitor_ddc import ui
    from PySide6.QtWidgets import QDialog

    def fake_exec(self):
        self.name.setCurrentText("Diyalogdan")
        for g, cb in self.groups.items():
            cb.setChecked(g == "gain")
        self.make_default.setChecked(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ui.SaveProfileDialog, "exec", fake_exec)
    win.w._new_profile()
    m = win.cfg.monitor(KEY)
    assert "Diyalogdan" in m["profiles"] and m["default_profile"] == "Diyalogdan"


def test_new_profile_overwrite_declined_then_cancel(win, monkeypatch):
    from monitor_ddc import ui
    from PySide6.QtWidgets import QDialog
    calls = iter([QDialog.DialogCode.Accepted, QDialog.DialogCode.Rejected])

    def fake_exec(self):
        self.name.setCurrentText("Günlük Doğal")
        return next(calls)

    monkeypatch.setattr(ui.SaveProfileDialog, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    before = dict(win.cfg.profiles(KEY)["Günlük Doğal"])
    win.w._new_profile()
    assert win.cfg.profiles(KEY)["Günlük Doğal"] == before


def test_rename_make_default_delete_via_ui(win, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    w = win.w
    w.save_profile_as("Geçici", {"gain"})
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Kalıcı", True))
    w._rename_profile()
    assert "Kalıcı" in win.cfg.profiles(KEY)
    w._make_default()
    assert win.cfg.monitor(KEY)["default_profile"] == "Kalıcı"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    w._delete_profile()
    assert "Kalıcı" not in win.cfg.profiles(KEY)


def test_update_profile_keeps_groups(win, env, qapp):
    w = win.w
    w.sliders[vcp.GAIN_B].set_value(30)
    w._update_profile()
    codes = win.cfg.profile_codes(KEY, "Günlük Doğal")
    assert codes[vcp.GAIN_B] == 30 and vcp.VOLUME not in codes


def test_color_controls(win, env, qapp):
    w = win.w
    w._set_preset(0x05)
    assert w.preset_buttons[0x05].isChecked() and w.preset_value == 0x05
    w._nudge(vcp.GAIN_B, -3)
    w.kelvin.setValue(50)
    assert w.sliders[vcp.GAIN_B].value() < w.sliders[vcp.GAIN_R].value()
    w._shift_kelvin(+3)
    w._set_neutral()
    assert win.cfg.monitor(KEY)["neutral_gains"] == [w.sliders[c].value() for c in (vcp.GAIN_R, vcp.GAIN_G, vcp.GAIN_B)]
    settle(qapp, win.e)
    assert any("setvcp" in c and c[-2] == "14" for c in env.monitor.calls())


def test_hardware_tab_actions(win, env, qapp):
    w = win.w
    w._read_all()
    settle(qapp, win.e)
    assert "Kırmızı kazanç (0x16)" in w.raw_out.toPlainText()
    w.raw_code.setText("10")
    w._raw_get()
    settle(qapp, win.e)
    w._save_to_monitor()
    settle(qapp, win.e)
    assert any(c[-1] == "scs" for c in env.monitor.calls())


def test_settings_and_signature_buttons(win):
    w = win.w
    w._setting("watchdog_enabled", True)
    w._setting("watchdog_interval", 15)
    assert win.e.watchdog.isActive() and win.e.watchdog.interval() == 15000
    w._setting("watchdog_enabled", False)
    assert not win.e.watchdog.isActive()
    w._set_signature(dict(vcp.FACTORY_SIGNATURE))
    assert "0x16=35" in w.sig_label.text()
    w._set_signature({})
    assert "yok" in w.sig_label.text()


def test_monitor_removed_disables_tabs(win, env, qapp):
    (env.sysfs.root / "card1-HDMI-A-1" / "status").write_text("disconnected\n")
    win.e.sync("poll")
    assert win.w.key is None and not win.w.tabs.isEnabled()
    assert "Harici monitör yok" in [a.text() for a in win.tray.menu.actions()]


def test_log_tab_caps_entries(win):
    for i in range(520):
        win.w._on_message(f"m{i}", i % 2 == 0)
    assert win.w.log.count() == 500
