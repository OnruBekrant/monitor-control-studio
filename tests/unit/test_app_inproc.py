"""app modülünün süreç içi testleri (argüman doğrulama, Adaptor, Controller)."""
import pytest
from conftest import wait_until

from monitor_ddc import app

KEY = "BOE:NV156FHM-T06:2014:1"


def test_cli_rejects_bad_apply_name(env):
    with pytest.raises(SystemExit) as e:
        app.main(["--apply", "a\x00b"])
    assert e.value.code == 2


def test_cli_list(env, capsys):
    env.sysfs.add()
    assert app.main(["--list"]) == 0
    assert "NV156FHM-T06" in capsys.readouterr().out


def test_direct_apply_unknown_profile(env):
    env.sysfs.add()
    assert app._direct_apply("yok") == 1


def test_excepthook_logs_instead_of_abort(caplog):
    try:
        raise ValueError("deneme")
    except ValueError as exc:
        app._excepthook(type(exc), exc, exc.__traceback__)
    assert "yakalanmamış" in caplog.text


def test_controller_and_adaptor(env, qapp, monkeypatch):
    from monitor_ddc import engine as eng_mod
    monkeypatch.setattr(eng_mod.Engine, "_start_udev_monitor", lambda self: None)
    monkeypatch.setattr(eng_mod.Engine, "_connect_dbus_signals", lambda self: None)
    monkeypatch.setattr(eng_mod.Engine, "_query_lock_state", lambda self: None)
    monkeypatch.setattr(qapp, "quit", lambda: None)
    env.sysfs.add()
    ctl = app.Controller(qapp, background=True)
    try:
        assert wait_until(qapp, lambda: KEY in ctl.engine.displays, 10)
        ad = ctl.adaptor
        assert ad.Apply("Gece / Göz Koruma") is True
        assert ad.Apply("yok") is False
        assert ad.Apply("\u202ex") is False
        assert ad.Apply("x" * 10000) is False
        ad.Show()
        assert ctl.window.isVisible()
        ad.Reapply()
        wait_until(qapp, ctl.engine.worker.idle, 10)
    finally:
        ctl.adaptor.Quit()
    assert ctl.window.quitting
