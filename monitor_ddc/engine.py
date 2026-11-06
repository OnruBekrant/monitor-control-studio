"""Uygulama motoru: tek I2C sahibi, olay kaynakları ve otomatik uygulama.

GUI, tepsi ve D-Bus komutları aynı süreçte bu motoru kullanır; böylece iki ayrı süreç
(GUI + daemon) aynı I2C hattında yarışmaz. Tüm I2C trafiği tek bir işçi iş parçacığında
sırayla akar.

Otomatik uygulama tetikleyicileri:
  1. Oturum açılışı (ilk algılama)                → varsayılan profil
  2. udev DRM 'change' olayı (kablo / güç)        → aktif profil, kademeli yeniden deneme
  3. logind PrepareForSleep(false) (uykudan dönüş) → aktif profil
  4. Ekran kilidi açılması / DPMS uyanışı          → doğrula, gerekirse uygula
  5. Watchdog: periyodik geri okuma; sıfırlanma imzası görülürse yeniden uygula
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable

from PySide6.QtCore import SLOT, QObject, QProcess, QThread, QTimer, Signal, Slot
from PySide6.QtDBus import QDBusConnection, QDBusInterface

from . import ddc, vcp
from .config import Config

log = logging.getLogger(__name__)


class DdcWorker(QThread):
    """Sıralı I2C işçisi.

    * set_live(): kaydırıcı değerleri; aynı (bus, kod) için bekleyen değer üzerine yazılır,
      yani sürükleme sırasında sadece en güncel değer gönderilir (doğal debounce/throttle).
    * submit(): sıradan iş (algılama, profil uygulama, okuma). Sonuç ana iş parçacığına
      sinyal ile döner.
    """

    jobDone = Signal(object, object, object)  # callback, sonuç, hata

    def __init__(self, gap_ms: int = 40):
        super().__init__()
        self._cv = threading.Condition()
        self._live: OrderedDict[tuple[int, int], int] = OrderedDict()
        self._jobs: deque = deque()
        self._stop = False
        self._busy = False
        self.gap = gap_ms / 1000.0

    def idle(self) -> bool:
        with self._cv:
            return not (self._busy or self._live or self._jobs)

    def set_live(self, bus: int, code: int, value: int) -> None:
        with self._cv:
            self._live[(bus, code)] = value
            self._live.move_to_end((bus, code))
            self._cv.notify()

    def submit(self, fn: Callable, callback: Callable | None = None) -> None:
        with self._cv:
            self._jobs.append((fn, callback))
            self._cv.notify()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()
        self.wait(5000)

    def run(self) -> None:
        while True:
            with self._cv:
                while not (self._stop or self._live or self._jobs):
                    self._cv.wait()
                if self._stop:
                    return
                self._busy = True
                if self._live:  # canlı değerler öncelikli: arayüz tepkisi
                    (bus, code), value = self._live.popitem(last=False)
                    job = None
                else:
                    job = self._jobs.popleft()
            if job is None:
                try:
                    ddc.set_value(bus, code, value, retries=1)
                except ddc.DdcError as e:
                    self.jobDone.emit(None, None, e)
                time.sleep(self.gap)
                self._busy = False
                continue
            fn, cb = job
            try:
                res, err = fn(), None
            except Exception as e:  # noqa: BLE001 - her hatayı UI'ye taşı
                res, err = None, e
            self._busy = False
            self.jobDone.emit(cb, res, err)


class Engine(QObject):
    displaysChanged = Signal()
    # dict değil object: PySide6 dict'i QVariantMap'e çevirir ve tamsayı anahtarları kaybeder
    valuesRead = Signal(str, object)     # monitör anahtarı, {kod: (değer, max)}
    profileApplied = Signal(str, str)    # monitör anahtarı, profil adı
    profilesChanged = Signal()
    message = Signal(str, bool)          # metin, hata mı

    def __init__(self, config: Config):
        super().__init__()
        self.cfg = config
        self.displays: dict[str, ddc.Display] = {}
        self._locked = False
        self._deferred: dict[str, str] = {}   # kilit/DPMS nedeniyle ertelenen uygulamalar
        self._probing: set[str] = set()
        self._added_at: dict[str, float] = {}
        self._verify_attempts: dict[str, int] = {}
        self._last_applied: dict[str, dict[int, int]] = {}
        self._confirmed: dict[str, bool] = {}
        # her yazma (profil ya da canlı) nesli artırır; eskimiş doğrulamalar iptal edilir
        self._gen: dict[str, int] = {}
        self._applied_gen: dict[str, int] = {}

        self.worker = DdcWorker(self.cfg.settings["write_gap_ms"])
        self.worker.jobDone.connect(self._on_job_done)
        self.worker.start()

        self._hotplug_timer = QTimer(self)
        self._hotplug_timer.setSingleShot(True)
        self._hotplug_timer.timeout.connect(lambda: self.sync("hotplug", force=True))

        # Bağlantı/DPMS durumunu sysfs'ten yokla: ucuz, I2C trafiği yok. udev olayı
        # kaçsa ya da monitör bekleme modundan çıksa bile 2 sn içinde fark edilir.
        self._poll = QTimer(self)
        self._poll.timeout.connect(lambda: self.sync("poll"))
        self._poll.start(2000)

        self.watchdog = QTimer(self)
        self.watchdog.timeout.connect(self._watchdog_tick)
        self._update_watchdog()

        self._udev: QProcess | None = None
        self._start_udev_monitor()
        self._connect_dbus_signals()
        self._query_lock_state()

    # ------------------------------------------------------------------ altyapı
    def shutdown(self) -> None:
        if self._udev:
            self._udev.kill()
            self._udev.waitForFinished(1000)
        self.worker.stop()

    @Slot(object, object, object)
    def _on_job_done(self, cb, res, err) -> None:
        if err is not None and cb is None:
            self.message.emit(str(err), True)
            return
        if cb:
            cb(res, err)

    def _say(self, text: str, error: bool = False) -> None:
        (log.warning if error else log.info)(text)
        self.message.emit(text, error)

    def _update_watchdog(self) -> None:
        s = self.cfg.settings
        if s["watchdog_enabled"]:
            self.watchdog.start(max(5, int(s["watchdog_interval"])) * 1000)
        else:
            self.watchdog.stop()

    def settings_changed(self) -> None:
        self.worker.gap = self.cfg.settings["write_gap_ms"] / 1000.0
        self._update_watchdog()

    # ------------------------------------------------------------ olay kaynakları
    def _start_udev_monitor(self) -> None:
        p = QProcess(self)
        p.setProgram("udevadm")
        p.setArguments(["monitor", "--udev", "--subsystem-match=drm"])
        p.readyReadStandardOutput.connect(self._on_udev_output)
        p.start()
        if not p.waitForStarted(2000):
            self._say("udevadm başlatılamadı; hotplug yalnızca watchdog ile algılanacak", True)
            return
        self._udev = p

    def _on_udev_output(self) -> None:
        if self._udev is None:
            return
        data = self._udev.readAllStandardOutput().data().decode(errors="replace")
        if ("change" in data or " add " in data) and self.cfg.settings["apply_on_hotplug"]:
            # olay fırtınasını tek bir uygulamaya indir; scaler'ın uyanmasını bekle
            self._hotplug_timer.start(int(self.cfg.settings["hotplug_delay"] * 1000))

    def _connect_dbus_signals(self) -> None:
        sysbus = QDBusConnection.systemBus()
        ok = sysbus.connect("org.freedesktop.login1", "/org/freedesktop/login1",
                            "org.freedesktop.login1.Manager", "PrepareForSleep",
                            self, SLOT("_on_prepare_for_sleep(bool)"))
        if not ok:
            self._say("logind uyku sinyaline bağlanılamadı", True)
        sesbus = QDBusConnection.sessionBus()
        if not sesbus.connect("org.freedesktop.ScreenSaver", "/ScreenSaver",
                              "org.freedesktop.ScreenSaver", "ActiveChanged",
                              self, SLOT("_on_screensaver(bool)")):
            self._say("Ekran kilidi sinyaline bağlanılamadı", True)

    @Slot(bool)
    def _on_prepare_for_sleep(self, going_to_sleep: bool) -> None:
        if not going_to_sleep and self.cfg.settings["apply_on_resume"]:
            self._say("Uykudan dönüldü; profil yeniden uygulanacak")
            QTimer.singleShot(3000, lambda: self.sync("resume", force=True))

    def _query_lock_state(self) -> None:
        iface = QDBusInterface("org.freedesktop.ScreenSaver", "/ScreenSaver",
                               "org.freedesktop.ScreenSaver", QDBusConnection.sessionBus())
        if iface.isValid():
            r = iface.call("GetActive")
            args = r.arguments()
            self._locked = bool(args[0]) if args else False

    @Slot(bool)
    def _on_screensaver(self, active: bool) -> None:
        self._locked = active
        if active:
            log.info("Ekran kilitlendi; DDC trafiği duraklatıldı")
            return
        log.info("Kilit açıldı")
        QTimer.singleShot(2500, self._after_unlock)

    def _after_unlock(self) -> None:
        if self._locked:
            return
        self._run_deferred()
        if self.cfg.settings["apply_on_unlock"]:
            self.verify_all("unlock")

    # ---------------------------------------------------------------- algılama
    def can_talk(self, key: str) -> bool:
        """Monitöre DDC/CI trafiği göndermek güvenli mi?

        Kilitliyken ya da çıkış DPMS-Off iken (monitör bekleme modunda) DDC trafiği bazı
        monitörleri uyandırıp 'sinyal yok' ekranına düşürür; bu taşınabilir monitörde
        HPD'nin düşmesine ve KWin'in ekran düzenini yeniden kurmasına (titreme) yol açabilir.
        """
        d = self.displays.get(key)
        return d is not None and not self._locked and d.dpms == "On"

    def sync(self, reason: str, force: bool = False) -> None:
        """sysfs'ten bağlı monitörleri oku (I2C trafiği yok); değişiklikleri işle.

        force=True ise zaten bağlı monitörlere de profil yeniden uygulanır (hotplug/uyanma).
        """
        conns = {d.key: d for d in ddc.enumerate_connectors()}
        changed = False
        for k in set(self.displays) - set(conns):
            d = self.displays.pop(k)
            self._deferred.pop(k, None)
            self._say(f"Monitör ayrıldı: {d.title}")
            changed = True
        for k, new in conns.items():
            cur = self.displays.get(k)
            if cur is None:
                self.cfg.monitor(k, new.title)
                self.displays[k] = new
                self._added_at[k] = time.monotonic()
                changed = True
                if reason not in ("start", "manual"):
                    self._say(f"Monitör bağlandı: {new.title} ({new.connector.split('-', 1)[-1]})")
                self._request_apply(k, "hotplug" if reason == "poll" else reason)
                continue
            woke = cur.dpms != "On" and new.dpms == "On"
            if cur.bus != new.bus or cur.connector != new.connector:
                cur.bus, cur.connector = new.bus, new.connector
                changed = True
            cur.dpms = new.dpms
            if force and time.monotonic() - self._added_at.get(k, 0) > 6:
                cur.vcp_version = ""  # monitör yeniden başlıyor olabilir: hazır olmasını bekle
                self._request_apply(k, reason)
            elif woke:
                log.info("%s bekleme modundan çıktı", cur.title)
                self._run_deferred(k)
                QTimer.singleShot(3000, lambda k=k: self.verify(k, "wake"))
        if changed:
            self.displaysChanged.emit()

    def _request_apply(self, key: str, reason: str) -> None:
        if reason == "hotplug" and not self.cfg.settings["apply_on_hotplug"]:
            return
        if self.can_talk(key):
            self._auto_apply(key, reason)
        else:
            log.info("%s için uygulama ertelendi (kilitli ya da ekran kapalı)", key)
            self._deferred[key] = reason

    def _run_deferred(self, key: str | None = None) -> None:
        for k in [key] if key else list(self._deferred):
            if k in self._deferred and self.can_talk(k):
                self._auto_apply(k, self._deferred.pop(k))

    def _when_ready(self, key: str, then: Callable[[], None], attempt: int = 0) -> None:
        """Monitör DDC/CI'ya cevap verene kadar (yeni takıldıysa scaler açılıyor olabilir) bekle."""
        d = self.displays.get(key)
        if not d:
            return
        if d.vcp_version:
            then()
            return
        if key in self._probing and attempt == 0:
            return
        self._probing.add(key)

        def done(ver, err):
            dd = self.displays.get(key)
            if not dd:
                self._probing.discard(key)
                return
            if ver:
                self._probing.discard(key)
                dd.vcp_version = ver
                self.displaysChanged.emit()
                then()
            elif attempt < len(_PROBE_BACKOFF) and self.can_talk(key):
                QTimer.singleShot(_PROBE_BACKOFF[attempt], lambda: self._when_ready(key, then, attempt + 1))
            else:
                self._probing.discard(key)
                if self.can_talk(key):
                    self._say(f"{dd.title} DDC/CI'ya cevap vermiyor", True)

        self.worker.submit(lambda: ddc.probe(d.bus), done)

    def _auto_apply(self, key: str, reason: str) -> None:
        s = self.cfg.settings
        if reason == "start" and not s["apply_on_start"]:
            self._when_ready(key, lambda: self.read_values(key))
            return
        mon = self.cfg.monitor(key)
        name = mon.get("default_profile") if reason == "start" else (
            mon.get("active_profile") or mon.get("default_profile"))
        if reason == "start" and name:
            self.cfg.set_field(key, "active_profile", name)
        if name and name in mon["profiles"]:
            self._when_ready(key, lambda: self.apply_profile(key, name, reason))
        else:
            self._when_ready(key, lambda: self.read_values(key))

    # --------------------------------------------------------------- uygulama
    def apply_profile(self, key: str, name: str, reason: str = "manual") -> None:
        d = self.displays.get(key)
        codes = self.cfg.profile_codes(key, name)
        if d is None or codes is None:
            self._say(f"'{name}' uygulanamadı: monitör ya da profil yok", True)
            return
        if reason == "manual":
            self.cfg.set_field(key, "active_profile", name)
        self._verify_attempts[key] = 0
        self._write_codes(d, codes, name, reason)

    def _write_codes(self, d: ddc.Display, codes: dict[int, int], name: str, reason: str) -> None:
        order = [c for c in vcp.APPLY_ORDER if c in codes] + \
                [c for c in codes if c not in vcp.APPLY_ORDER]
        bus, gap = d.bus, self.worker.gap
        gen = self._gen[d.key] = self._gen.get(d.key, 0) + 1
        self._applied_gen[d.key] = gen

        def job():
            errors = []
            for c in order:
                try:
                    ddc.set_value(bus, c, codes[c])
                except ddc.DdcError as e:
                    errors.append(str(e))
                # ön ayar değişince scaler kazançları yeniden yükler; biraz daha bekle
                time.sleep(0.35 if c == vcp.PRESET else gap)
            return errors

        def done(errors, err):
            if err or errors:
                self._say(f"'{name}' kısmen uygulandı: {err or errors[0]}", True)
            else:
                self._say(f"'{name}' uygulandı ({_REASONS.get(reason, reason)})")
            self._last_applied[d.key] = codes
            self._confirmed[d.key] = False
            self.profileApplied.emit(d.key, name)
            self.read_values(d.key)
            for delay in self.cfg.settings["verify_delays"]:
                QTimer.singleShot(int(delay * 1000),
                                  lambda k=d.key: self.verify(k, "post-apply", gen, codes, name))

        self.worker.submit(job, done)

    def set_live(self, key: str, code: int, value: int) -> None:
        d = self.displays.get(key)
        if d:
            self._gen[key] = self._gen.get(key, 0) + 1  # kullanıcı elle değiştiriyor
            self.worker.set_live(d.bus, code, value)

    def read_values(self, key: str, codes: list[int] | None = None) -> None:
        d = self.displays.get(key)
        if not d:
            return
        codes = codes or vcp.READ_CODES
        gen = self._gen.get(key, 0)

        def done(res, err):
            if err:
                self._say(str(err), True)
            else:
                self._emit_values(key, res, gen)

        self.worker.submit(lambda: ddc.get(d.bus, codes), done)

    def _emit_values(self, key: str, res: dict, gen: int) -> None:
        """Okuma sürerken kullanıcı kaydırıcı oynattıysa sonuç eskimiştir; arayüze gönderme
        (yoksa kaydırıcı eski değere geri zıplar)."""
        if self._gen.get(key, 0) == gen:
            self.valuesRead.emit(key, res)

    def run_job(self, fn: Callable, callback: Callable | None = None) -> None:
        self.worker.submit(fn, callback)

    # ------------------------------------------------------------- doğrulama
    def verify_all(self, reason: str) -> None:
        for k in list(self.displays):
            self.verify(k, reason)

    def verify(self, key: str, reason: str, gen: int | None = None,
               target: dict[int, int] | None = None, name: str | None = None) -> None:
        """Geri oku; monitör kendini sıfırladıysa profili yeniden çak."""
        d = self.displays.get(key)
        mon = self.cfg.monitor(key)
        if gen is not None and gen != self._gen.get(key):
            return  # bu uygulamadan sonra başka bir yazma oldu; eski doğrulama geçersiz
        if not self.can_talk(key):
            return
        if target is None:
            name = mon.get("active_profile")
            target = self.cfg.profile_codes(key, name) if name else None
        sig = self.cfg.reset_signature(key)
        # sadece sürekli (C) kodlar doğrulanır; 0x14'ün geri okuması bu firmware'de güvenilmez
        # Parlaklık ve ses işletim sistemine (KDE) bırakılır: profil uygularken yazılır ama
        # sonradan asla "düzeltilmez".
        check = [c for c in (target or {})
                 if vcp.FEATURES.get(c, vcp.Feature(c, "", "")).continuous and c not in vcp.USER_OWNED]
        codes = sorted(set(check) | set(sig))
        if not d or not codes:
            return

        read_gen = self._gen.get(key, 0)

        def done(res, err):
            if err:
                log.debug("doğrulama okunamadı (%s): %s", reason, err)
                return
            self._emit_values(key, res, read_gen)
            if not target or (gen is not None and gen != self._gen.get(key)):
                return
            now = {c: v for c, (v, _m) in res.items()}
            # İmza ancak profilden FARKLIYSA anlamlıdır; aynıysa her sapma sıfırlanma sanılırdı.
            is_reset = (bool(sig) and all(now.get(c) == v for c, v in sig.items())
                        and any(target.get(c) != v for c, v in sig.items()))
            drift = [c for c in check if c in now and now[c] != target[c]]
            if not drift:
                self._verify_attempts[key] = 0
                if reason == "post-apply" and not self._confirmed.get(key):
                    self._confirmed[key] = True
                    self._say(f"'{name}' doğrulandı: monitör değerleri profille aynı ✓")
                return
            # GUI'den elle değiştirilmiş değerleri katı mod bile geri almaz (sadece sıfırlanma)
            edited = self._gen.get(key) != self._applied_gen.get(key)
            enforce = (self.cfg.settings["enforce_profile"] and not edited) or reason == "post-apply"
            if is_reset or enforce:
                n = self._verify_attempts.get(key, 0)
                if n >= 3:
                    if n == 3:
                        self._say("Monitör değerleri tutmuyor; 3 denemeden sonra vazgeçildi", True)
                        self._verify_attempts[key] = n + 1
                    return
                self._verify_attempts[key] = n + 1
                why = "sıfırlanma algılandı" if is_reset else "değerler sapmış"
                self._say(f"{why} ({', '.join(f'0x{c:02x}' for c in drift)}); yeniden uygulanıyor")
                # Sadece bozulan renk kodlarını (ve ön ayarı) geri yaz; parlaklık/ses KDE'nindir.
                fix = {c: target[c] for c in drift}
                if vcp.PRESET in target and is_reset:
                    fix = {vcp.PRESET: target[vcp.PRESET], **fix}
                self._write_codes(d, fix, name or "?", "watchdog")

        self.worker.submit(lambda: ddc.get(d.bus, codes, retries=1), done)

    def _watchdog_tick(self) -> None:
        # varlık kontrolü sysfs yoklamasında; burada sadece güvenliyse DDC ile geri oku
        for k in list(self.displays):
            if self.can_talk(k) and self.displays[k].vcp_version:
                self.verify(k, "watchdog")


_PROBE_BACKOFF = [1500, 2500, 4000, 6000, 8000]  # ms; toplam ~22 sn

_REASONS = {
    "start": "oturum açılışı",
    "hotplug": "monitör takıldı",
    "resume": "uykudan dönüş",
    "watchdog": "sıfırlanma koruması",
    "manual": "elle",
    "unlock": "kilit açıldı",
    "wake": "ekran uyandı",
    "poll": "monitör takıldı",
}
