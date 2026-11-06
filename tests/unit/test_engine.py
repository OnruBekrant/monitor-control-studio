"""engine: otomasyon mantığının modül testleri (Qt offscreen, sahte ddcutil + sysfs)."""
import pytest
from conftest import pump, wait_until

from monitor_ddc import engine as eng_mod
from monitor_ddc import vcp
from monitor_ddc.config import Config

KEY = "BOE:NV156FHM-T06:2014:1"


@pytest.fixture
def engine(env, qapp, monkeypatch):
    monkeypatch.setattr(eng_mod, "_PROBE_BACKOFF", [50, 100, 150])
    monkeypatch.setattr(eng_mod.Engine, "_start_udev_monitor", lambda self: None)
    monkeypatch.setattr(eng_mod.Engine, "_connect_dbus_signals", lambda self: None)
    monkeypatch.setattr(eng_mod.Engine, "_query_lock_state", lambda self: None)
    cfg = Config()
    cfg.settings.update(verify_delays=[0.3], watchdog_enabled=False, write_gap_ms=0)
    e = eng_mod.Engine(cfg)
    e._poll.stop()
    e.msgs = []
    e.message.connect(lambda t, err: e.msgs.append(t))
    yield e
    e.shutdown()


def settle(qapp, e, extra=0.1):
    wait_until(qapp, e.worker.idle, 10)
    pump(qapp, extra)
    wait_until(qapp, e.worker.idle, 10)


def writes(env):
    return [c for c in env.monitor.calls() if "setvcp" in c]


def start(env, qapp, e):
    env.sysfs.add()
    e.sync("start")
    assert wait_until(qapp, lambda: KEY in e.displays and e.displays[KEY].vcp_version, 10)
    settle(qapp, e, 0.5)


def test_start_applies_default_profile(env, qapp, engine):
    start(env, qapp, engine)
    v = env.monitor.values()
    assert (v[0x16], v[0x18], v[0x1A], v[0x10]) == (52, 50, 40, 35)
    assert engine.cfg.monitor(KEY)["active_profile"] == "Günlük Doğal"


def test_unknown_monitor_without_profile_only_reads(env, qapp, engine):
    from conftest import make_edid
    env.sysfs.add(edid=make_edid(mfg="DEL", product=0x1234, model="U2720Q"))
    engine.sync("start")
    settle(qapp, engine, 0.5)
    assert writes(env) == []


def test_double_hotplug_event_applies_once(env, qapp, engine):
    start(env, qapp, engine)
    env.monitor.clear_calls()
    engine.sync("hotplug", force=True)  # udev olayı, yoklamanın hemen ardından
    settle(qapp, engine, 0.3)
    assert writes(env) == []


def test_hotplug_and_unplug(env, qapp, engine):
    start(env, qapp, engine)
    (env.sysfs.root / "card1-HDMI-A-1" / "status").write_text("disconnected\n")
    engine.sync("poll")
    assert engine.displays == {}
    (env.sysfs.root / "card1-HDMI-A-1" / "status").write_text("connected\n")
    env.monitor.set(c16=35, c18=35, c1a=47)
    env.monitor.clear_calls()
    engine.sync("poll")
    assert wait_until(qapp, lambda: env.monitor.values()[0x1A] == 40, 10)


def test_monitor_slow_to_wake_is_retried(env, qapp, engine):
    env.sysfs.add()
    env.monitor.write(offline=True)
    engine.sync("hotplug")
    pump(qapp, 0.08)
    env.monitor.write(offline=False)
    assert wait_until(qapp, lambda: env.monitor.values()[0x1A] == 40, 10)


def test_locked_means_no_ddc_and_deferred(env, qapp, engine):
    env.sysfs.add()
    engine._locked = True
    env.monitor.clear_calls()
    engine.sync("hotplug", force=True)
    engine._watchdog_tick()
    engine.verify_all("watchdog")
    settle(qapp, engine, 0.3)
    assert env.monitor.calls() == []
    assert KEY in engine._deferred
    engine._on_screensaver(False)
    assert wait_until(qapp, lambda: env.monitor.values()[0x1A] == 40, 10)
    assert KEY not in engine._deferred


def test_dpms_off_means_no_ddc(env, qapp, engine):
    start(env, qapp, engine)
    env.sysfs.set("card1-HDMI-A-1", dpms="Off")
    engine.sync("poll")
    env.monitor.clear_calls()
    engine._added_at[KEY] = 0  # 6 sn'lik çift-uygulama koruma penceresinin dışında
    engine._watchdog_tick()
    engine.sync("hotplug", force=True)
    settle(qapp, engine, 0.3)
    assert env.monitor.calls() == []
    assert KEY in engine._deferred
    env.sysfs.set("card1-HDMI-A-1", dpms="On")
    engine.sync("poll")
    assert wait_until(qapp, lambda: any("setvcp" in c for c in env.monitor.calls()), 10)


def test_brightness_never_enforced(env, qapp, engine):
    start(env, qapp, engine)
    engine.cfg.settings["enforce_profile"] = True
    env.monitor.set(c10=80, c62=60)  # KDE parlaklığı ve sesi değiştirdi
    env.monitor.clear_calls()
    engine.verify(KEY, "watchdog")
    settle(qapp, engine, 0.5)
    assert writes(env) == []
    assert env.monitor.values()[0x10] == 80


def test_enforce_reverts_color_only(env, qapp, engine):
    start(env, qapp, engine)
    engine.cfg.settings["enforce_profile"] = True
    env.monitor.set(c1a=60, c10=80)
    env.monitor.clear_calls()
    engine.verify(KEY, "watchdog")
    settle(qapp, engine, 0.5)
    assert [c[-2] for c in writes(env)] == ["1a"]
    assert env.monitor.values()[0x10] == 80


def test_reset_signature_triggers_fix(env, qapp, engine):
    start(env, qapp, engine)
    env.monitor.set(c16=35, c18=35, c1a=47, c10=70)
    env.monitor.clear_calls()
    engine.verify(KEY, "watchdog")
    settle(qapp, engine, 0.5)
    v = env.monitor.values()
    assert (v[0x16], v[0x18], v[0x1A]) == (52, 50, 40) and v[0x10] == 70
    assert any("sıfırlanma" in m for m in engine.msgs)


def test_signature_equal_to_profile_is_not_reset(env, qapp, engine):
    start(env, qapp, engine)
    engine.cfg.set_field(KEY, "reset_signature", {"0x16": 52, "0x18": 50, "0x1a": 40})
    env.monitor.set(c10=80)
    env.monitor.clear_calls()
    engine.verify(KEY, "watchdog")
    settle(qapp, engine, 0.5)
    assert writes(env) == []


def test_osd_change_left_alone_without_enforce(env, qapp, engine):
    start(env, qapp, engine)
    env.monitor.set(c1a=44)
    env.monitor.clear_calls()
    engine.verify(KEY, "watchdog")
    settle(qapp, engine, 0.5)
    assert writes(env) == []


def test_reapply_gives_up_after_three(env, qapp, engine):
    start(env, qapp, engine)
    env.monitor.write(readonly=[0x16, 0x18, 0x1A])
    env.monitor.set(c16=35, c18=35, c1a=47)
    for _ in range(5):
        engine.verify(KEY, "watchdog")
        settle(qapp, engine, 0.1)
    assert any("vazgeçildi" in m for m in engine.msgs)
    assert sum(1 for m in engine.msgs if "yeniden uygulanıyor" in m) == 3


def test_live_writes_are_coalesced(env, qapp, engine):
    start(env, qapp, engine)
    env.monitor.clear_calls()
    engine.worker.submit(lambda: __import__("time").sleep(0.4))  # işçi meşgulken sürükleme
    for v in range(101):
        engine.set_live(KEY, vcp.GAIN_B, v)
    settle(qapp, engine, 0.2)
    w = [c for c in writes(env) if c[-2] == "1a"]
    assert len(w) <= 3 and w[-1][-1] == "100"


def test_stale_post_apply_verify_does_not_revert_user_edit(env, qapp, engine):
    start(env, qapp, engine)
    engine.apply_profile(KEY, "Günlük Doğal")
    settle(qapp, engine)
    engine.set_live(KEY, vcp.GAIN_B, 33)
    pump(qapp, 0.8)  # 0.3 sn doğrulama zamanı geçer
    settle(qapp, engine)
    assert env.monitor.values()[0x1A] == 33


def test_stale_read_not_emitted(env, qapp, engine):
    start(env, qapp, engine)
    got = []
    engine.valuesRead.connect(lambda k, v: got.append(v))
    engine.worker.submit(lambda: __import__("time").sleep(0.3))
    engine.read_values(KEY)
    engine.set_live(KEY, vcp.GAIN_B, 20)
    settle(qapp, engine)
    assert got == []


def test_apply_unknown_profile_reports(env, qapp, engine):
    start(env, qapp, engine)
    engine.apply_profile(KEY, "yok")
    assert any("uygulanamadı" in m for m in engine.msgs)


def test_job_exception_does_not_kill_worker(env, qapp, engine):
    errs = []
    engine.run_job(lambda: 1 / 0, lambda r, e: errs.append(e))
    settle(qapp, engine)
    assert isinstance(errs[0], ZeroDivisionError)
    engine.run_job(lambda: 5, lambda r, e: errs.append(r))
    settle(qapp, engine)
    assert errs[-1] == 5
