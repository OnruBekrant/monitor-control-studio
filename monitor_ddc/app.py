"""Giriş noktası: tek örnek (D-Bus), tepsi + motor + pencere.

  monitor-ddc                 pencereyi aç (çalışıyorsa mevcut örneği öne getir)
  monitor-ddc --background    sadece tepsi + otomasyon (systemd --user servisi bunu kullanır)
  monitor-ddc --apply NAME    çalışan örneğe profil uygulat (yoksa doğrudan uygular)
  monitor-ddc --reapply       aktif profili yeniden uygula
  monitor-ddc --list          monitörleri ve profilleri listele
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from PySide6.QtCore import ClassInfo, QObject, QTimer, Slot
from PySide6.QtDBus import QDBusAbstractAdaptor, QDBusConnection, QDBusInterface
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from . import ddc, vcp
from .config import Config, validate_profile_name

log = logging.getLogger("monitor_ddc")


def _excepthook(exc_type, exc, tb) -> None:
    """Yakalanmamış istisnalar arka plan servisini durdurmasın (Qt bağlamalarının bazı sürümleri
    abort() ile süreci öldürür); günlüğe yaz ve çalışmaya devam et."""
    log.error("yakalanmamış hata", exc_info=(exc_type, exc, tb))

SERVICE = "io.github.MonitorDDC"
PATH = "/"
IFACE = "io.github.MonitorDDC"


@ClassInfo(**{"D-Bus Interface": IFACE})
class Adaptor(QDBusAbstractAdaptor):
    def __init__(self, controller: Controller):
        super().__init__(controller)
        self.c = controller

    @Slot()
    def Show(self):
        self.c.show_window()

    @Slot(str, result=bool)
    def Apply(self, name: str) -> bool:
        # D-Bus girdisi: aynı kullanıcının herhangi bir süreci çağırabilir → doğrula
        try:
            name = validate_profile_name(name)
        except ValueError:
            return False
        return self.c.apply_by_name(name)

    @Slot()
    def Reapply(self):
        self.c.engine.sync("manual", force=True)

    @Slot()
    def Quit(self):
        self.c.quit()


class Controller(QObject):
    def __init__(self, app: QApplication, background: bool):
        super().__init__()
        from .engine import Engine
        from .ui import MainWindow, Tray

        self.app = app
        self.cfg = Config()
        self.engine = Engine(self.cfg)
        self.window = MainWindow(self.engine, self.cfg)
        self.tray = Tray(self.engine, self.cfg, self.show_window, self.quit)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self.adaptor = Adaptor(self)
        QDBusConnection.sessionBus().registerObject(PATH, self)
        if not (background or self.cfg.settings["start_hidden"]):
            self.show_window()
        # ilk algılama + oturum açılışı uygulaması
        QTimer.singleShot(0, lambda: self.engine.sync("start"))

    def show_window(self):
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def apply_by_name(self, name: str) -> bool:
        ok = False
        for key in self.engine.displays:
            if name in self.cfg.profiles(key):
                self.engine.apply_profile(key, name)
                ok = True
        return ok

    def quit(self):
        self.window.quitting = True
        self.tray.hide()
        self.engine.shutdown()
        self.app.quit()


def _remote(method: str, *args) -> bool:
    """Çalışan örnek varsa ona ilet."""
    iface = QDBusInterface(SERVICE, PATH, IFACE, QDBusConnection.sessionBus())
    if not iface.isValid():
        return False
    reply = iface.call(method, *args)
    if reply.errorName():
        print(f"D-Bus hatası: {reply.errorMessage()}", file=sys.stderr)
        return False
    vals = reply.arguments()
    if method == "Apply" and vals and not vals[0]:
        print(f"Profil bulunamadı: {args[0]}", file=sys.stderr)
    return True


def _direct_apply(name: str | None) -> int:
    """GUI olmadan (örn. TTY/script) doğrudan uygula."""
    cfg = Config()
    rc = 1
    for d in ddc.detect():
        mon = cfg.monitor(d.key, d.title)
        pname = name or mon.get("active_profile") or mon.get("default_profile")
        codes = cfg.profile_codes(d.key, pname) if pname else None
        if not codes:
            continue
        for c in [c for c in vcp.APPLY_ORDER if c in codes]:
            ddc.set_value(d.bus, c, codes[c])
            time.sleep(0.35 if c == vcp.PRESET else 0.05)
        print(f"{d.title}: '{pname}' uygulandı")
        rc = 0
    return rc


def _list() -> int:
    cfg = Config()
    for d in ddc.detect():
        mon = cfg.monitor(d.key, d.title)
        print(f"{d.title}  i2c-{d.bus}  {d.connector}  [{d.key}]")
        for n, p in mon["profiles"].items():
            flags = ("★" if n == mon.get("default_profile") else " ") + \
                    ("▶" if n == mon.get("active_profile") else " ")
            print(f"   {flags} {n}: {p}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="monitor-ddc", description="DDC/CI monitör kontrol stüdyosu")
    ap.add_argument("--background", action="store_true", help="pencere açmadan tepside başla")
    ap.add_argument("--apply", metavar="PROFIL", help="profili uygula")
    ap.add_argument("--reapply", action="store_true", help="aktif profili yeniden uygula")
    ap.add_argument("--list", action="store_true", help="monitörleri ve profilleri listele")
    ap.add_argument("--quit", action="store_true", help="çalışan örneği kapat")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.excepthook = _excepthook
    if a.apply is not None:
        try:
            a.apply = validate_profile_name(a.apply)
        except ValueError as e:
            ap.error(str(e))

    if a.list:
        return _list()

    # D-Bus istemcisi için QCoreApplication yeterli değil ama QApplication oluşturmak da ucuz
    app = QApplication(sys.argv[:1])
    app.setApplicationName("monitor-ddc")
    app.setApplicationDisplayName("Monitör Kontrol Stüdyosu")
    app.setDesktopFileName("io.github.MonitorDDC")
    app.setQuitOnLastWindowClosed(False)

    if a.quit:
        return 0 if _remote("Quit") else 1
    if a.apply or a.reapply:
        if a.apply and _remote("Apply", a.apply):
            return 0
        if a.reapply and _remote("Reapply"):
            return 0
        return _direct_apply(a.apply)

    bus = QDBusConnection.sessionBus()
    if not bus.registerService(SERVICE):
        # zaten çalışıyor → öne getir
        if not a.background:
            _remote("Show")
        return 0

    signal.signal(signal.SIGINT, lambda *_: ctl.quit())
    signal.signal(signal.SIGTERM, lambda *_: ctl.quit())
    ctl = Controller(app, a.background)
    # Python sinyal işleyicilerinin çalışabilmesi için olay döngüsünü ara ara uyandır
    tick = QTimer()
    tick.start(500)
    tick.timeout.connect(lambda: None)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
