"""PySide6 (Qt for Python, LGPLv3) ana pencere ve sistem tepsisi."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSlider, QSpinBox, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

from html import escape as esc

from . import ddc, vcp
from .config import Config, validate_profile_name
from .engine import Engine

APP_ICON = "preferences-desktop-display-color"

# Ham VCP yazımında onay istenen kodlar: fabrika sıfırlama, güç, giriş kaynağı, tuş kilidi vb.
# (yanlış değer monitörü sinyalsiz bırakabilir ya da fiziksel tuşları kilitleyebilir)
DANGEROUS_CODES = {0x01, 0x04, 0x05, 0x06, 0x08, 0x0A, 0x60, 0xB0, 0xCA, 0xCC, 0xD6, 0xDC} | set(range(0xE0, 0x100))
_HANDLE = ("QSlider::handle:horizontal { width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;"
           " background: palette(button); border: 1px solid palette(dark); }")


def app_icon() -> QIcon:
    for name in (APP_ICON, "video-display", "display"):
        ic = QIcon.fromTheme(name)
        if not ic.isNull():
            return ic
    return QIcon()


SHORT = {vcp.GAIN_R: "Kırmızı", vcp.GAIN_G: "Yeşil", vcp.GAIN_B: "Mavi", vcp.BLACK_R: "Siyah seviyesi",
         vcp.BRIGHTNESS: "Parlaklık", vcp.CONTRAST: "Kontrast", vcp.SHARPNESS: "Keskinlik", vcp.VOLUME: "Ses"}


def describe_codes(codes: dict[int, int]) -> str:
    short = SHORT
    parts = []
    for c in vcp.APPLY_ORDER + [c for c in codes if c not in vcp.APPLY_ORDER]:
        if c not in codes or c in (vcp.BLACK_G, vcp.BLACK_B):  # siyah seviyesi tek değer olarak gösterilir
            continue
        if c == vcp.PRESET:
            parts.append(f"Ön ayar {vcp.PRESETS.get(codes[c], hex(codes[c]))}")
        else:
            parts.append(f"{short.get(c, f'0x{c:02x}')} {codes[c]}")
    return " · ".join(parts)


class VcpSlider(QWidget):
    """Etiket + kaydırıcı + sayı kutusu. Kullanıcı hareketi `changed(code, value)` çağırır."""

    def __init__(self, feature: vcp.Feature, on_change, color: str | None = None):
        super().__init__()
        self.code = feature.code
        self._on_change = on_change
        self._mute = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(feature.name)
        self.label.setMinimumWidth(170)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, feature.max)
        if feature.max <= 10:
            self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
            self.slider.setTickInterval(1)
        if color:
            self.slider.setStyleSheet(
                "QSlider::groove:horizontal { height: 6px; border-radius: 3px; background: palette(mid); }"
                f"QSlider::sub-page:horizontal {{ background: {color}; border-radius: 3px; }}" + _HANDLE)
        self.spin = QSpinBox()
        self.spin.setRange(0, feature.max)
        self.spin.setFixedWidth(70)
        self.hw = QLabel("—")  # monitörden son okunan değer
        self.hw.setFixedWidth(40)
        self.hw.setToolTip("Monitörden son geri okunan değer")
        self.hw.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(self.label)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        lay.addWidget(self.hw)
        self.slider.valueChanged.connect(self._user)
        self.spin.valueChanged.connect(self._user)

    def _user(self, v: int) -> None:
        if self._mute:
            return
        self.set_value(v)
        self._on_change(self.code, v)

    def set_value(self, v: int, hw: bool = False) -> None:
        self._mute = True
        self.slider.setValue(v)
        self.spin.setValue(v)
        self._mute = False
        if hw:
            self.hw.setText(str(v))

    def value(self) -> int:
        return self.slider.value()


class SaveProfileDialog(QDialog):
    def __init__(self, parent, names: list[str], current: str | None):
        super().__init__(parent)
        self.setWindowTitle("Profili kaydet")
        lay = QFormLayout(self)
        self.name = QComboBox()
        self.name.setEditable(True)
        self.name.addItems(names)
        if current:
            self.name.setCurrentText(current)
        lay.addRow("Profil adı:", self.name)
        self.groups: dict[str, QCheckBox] = {}
        box = QGroupBox("Profile dahil edilecek ayarlar")
        bl = QVBoxLayout(box)
        defaults = {"preset": False, "gain": True, "black": False, "basic": True, "brightness": False,
                    "audio": False}
        for g, label in vcp.GROUPS.items():
            cb = QCheckBox(label)
            cb.setChecked(defaults[g])
            self.groups[g] = cb
            bl.addWidget(cb)
        hint = QLabel("İşaretlenmeyen ayarlara profil dokunmaz (örn. parlaklığı KDE'ye bırakmak için).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(placeholder-text);")
        bl.addWidget(hint)
        lay.addRow(box)
        self.make_default = QCheckBox("Varsayılan başlangıç profili yap")
        lay.addRow(self.make_default)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)


class MainWindow(QMainWindow):
    def __init__(self, engine: Engine, cfg: Config):
        super().__init__()
        self.engine, self.cfg = engine, cfg
        self.setWindowTitle("Monitör Kontrol Stüdyosu")
        self.setWindowIcon(app_icon())
        self.resize(900, 640)
        self.key: str | None = None
        self.preset_value: int | None = None
        self.quitting = False

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- üst şerit: monitör seçici + kimlik kartı
        top = QHBoxLayout()
        self.monitor_combo = QComboBox()
        self.monitor_combo.setMinimumWidth(260)
        self.monitor_combo.currentIndexChanged.connect(self._monitor_selected)
        top.addWidget(QLabel("Monitör:"))
        top.addWidget(self.monitor_combo)
        self.id_label = QLabel()
        self.id_label.setStyleSheet("color: palette(placeholder-text);")
        top.addWidget(self.id_label, 1)
        btn_refresh = QPushButton(QIcon.fromTheme("view-refresh"), "Yeniden tara")
        btn_refresh.clicked.connect(lambda: self.engine.sync("manual"))
        top.addWidget(btn_refresh)
        root.addLayout(top)

        # --- profil şeridi
        prow = QHBoxLayout()
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(220)
        self.profile_combo.currentIndexChanged.connect(self._show_profile_contents)
        prow.addWidget(QLabel("Profil:"))
        prow.addWidget(self.profile_combo)
        for text, icon, slot, tip in [
            ("Yükle", "dialog-ok-apply", self._apply_selected,
             "Seçili profilin kayıtlı değerlerini monitöre yaz"),
            ("Yeni…", "list-add", self._new_profile, "Şu anki ayarlardan yeni profil oluştur"),
            ("Değişiklikleri kaydet", "document-save", self._update_profile,
             "Seçili profili şu anki kaydırıcı değerleriyle güncelle (profilin içerdiği ayarlar)"),
            ("Yeniden adlandır", "edit-rename", self._rename_profile, ""),
            ("Varsayılan yap", "starred-symbolic", self._make_default,
             "Oturum açılışında bu profil uygulanır"),
            ("Sil", "edit-delete", self._delete_profile, ""),
        ]:
            b = QPushButton(QIcon.fromTheme(icon), text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            prow.addWidget(b)
        prow.addStretch(1)
        root.addLayout(prow)
        irow = QHBoxLayout()
        self.profile_contents = QLabel()
        self.profile_contents.setStyleSheet("color: palette(placeholder-text);")
        self.profile_contents.setWordWrap(True)
        irow.addWidget(self.profile_contents, 1)
        self.default_label = QLabel()
        irow.addWidget(self.default_label)
        root.addLayout(irow)

        # Profilden sapma şeridi: kaydırıcılar monitöre anında yazılır; kaydedilene kadar
        # profil değişmez. Fark varsa burada gösterilir.
        self.dirty_bar = QFrame()
        self.dirty_bar.setStyleSheet(
            "QFrame#dirty { background: rgba(245,166,35,0.15); border: 1px solid rgba(245,166,35,0.6);"
            " border-radius: 6px; }")
        self.dirty_bar.setObjectName("dirty")
        dl = QHBoxLayout(self.dirty_bar)
        dl.setContentsMargins(8, 4, 8, 4)
        self.dirty_label = QLabel()
        self.dirty_label.setWordWrap(True)
        dl.addWidget(self.dirty_label, 1)
        b = QPushButton(QIcon.fromTheme("edit-undo"), "Profile geri dön")
        b.setToolTip("Monitörü seçili profilin kayıtlı değerlerine döndür")
        b.clicked.connect(self._apply_selected)
        dl.addWidget(b)
        b = QPushButton(QIcon.fromTheme("document-save"), "Değişiklikleri kaydet")
        b.clicked.connect(self._update_profile)
        dl.addWidget(b)
        self.dirty_bar.hide()
        root.addWidget(self.dirty_bar)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.sliders: dict[int, VcpSlider] = {}
        self._build_color_tab()
        self._build_picture_tab()
        self._build_automation_tab()
        self._build_hardware_tab()
        self._build_log_tab()

        self.statusBar().showMessage("Hazır")

        engine.displaysChanged.connect(self._refresh_monitors)
        engine.valuesRead.connect(self._on_values)
        engine.message.connect(self._on_message)
        engine.profileApplied.connect(lambda k, n: self._refresh_profiles())
        self._dirty_timer = QTimer(self)
        self._dirty_timer.setSingleShot(True)
        self._dirty_timer.setInterval(150)
        self._dirty_timer.timeout.connect(self._update_dirty)
        self._refresh_monitors()

    # ------------------------------------------------------------------ sekmeler
    def _add_sliders(self, layout, codes, colors=None):
        for c in codes:
            s = VcpSlider(vcp.FEATURES[c], self._live, (colors or {}).get(c))
            self.sliders[c] = s
            layout.addWidget(s)

    def _build_color_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        pbox = QGroupBox("Renk ön ayarı (0x14)")
        pl = QHBoxLayout(pbox)
        self.preset_buttons: dict[int, QPushButton] = {}
        for val, name in vcp.PRESETS.items():
            b = QPushButton(name)
            b.setCheckable(True)
            b.clicked.connect(lambda _c, v=val: self._set_preset(v))
            self.preset_buttons[val] = b
            pl.addWidget(b)
        self.preset_readback = QLabel("Geri okunan: —")
        self.preset_readback.setToolTip(
            "Bu monitörün firmware'i 0x14 için yazılandan farklı bir değer geri bildirebiliyor.\n"
            "Doğru modu monitörün OSD menüsünden kontrol edin.")
        pl.addStretch(1)
        pl.addWidget(self.preset_readback)
        lay.addWidget(pbox)

        gbox = QGroupBox("Beyaz dengesi — RGB kazanç")
        gl = QVBoxLayout(gbox)
        self._add_sliders(gl, [vcp.GAIN_R, vcp.GAIN_G, vcp.GAIN_B],
                          {vcp.GAIN_R: "#e5484d", vcp.GAIN_G: "#30a46c", vcp.GAIN_B: "#3e63dd"})

        # Kelvin tabanlı sıcaklık kaydırıcısı (kazançlardan hesaplanır)
        krow = QHBoxLayout()
        kl = QLabel("Sıcaklık (göreli)")
        kl.setMinimumWidth(170)
        self.kelvin = QSlider(Qt.Orientation.Horizontal)
        self.kelvin.setRange(30, 93)  # ×100 K
        self.kelvin.setValue(65)
        self.kelvin.setStyleSheet(
            "QSlider::groove:horizontal { height: 6px; border-radius: 3px;"
            " background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #ff9b3d, stop:0.55 #fff4e8, stop:1 #9fc3ff); }"
            + _HANDLE)
        self.kelvin_label = QLabel("6500 K")
        self.kelvin_label.setFixedWidth(110)
        self.kelvin.valueChanged.connect(self._kelvin_changed)
        krow.addWidget(kl)
        krow.addWidget(self.kelvin, 1)
        krow.addWidget(self.kelvin_label)
        gl.addLayout(krow)

        qrow = QHBoxLayout()
        for text, delta in [("Mavi azalt", -3), ("Mavi artır", +3)]:
            b = QPushButton(text)
            b.clicked.connect(lambda _c, d=delta: self._nudge(vcp.GAIN_B, d))
            qrow.addWidget(b)
        b = QPushButton("Daha sıcak")
        b.clicked.connect(lambda: self._shift_kelvin(-3))
        qrow.addWidget(b)
        b = QPushButton("Daha soğuk")
        b.clicked.connect(lambda: self._shift_kelvin(+3))
        qrow.addWidget(b)
        b = QPushButton("Bunu 6500 K nötr kabul et")
        b.setToolTip("Mevcut R/G/B kazançlarını sıcaklık kaydırıcısının 6500 K referansı olarak sakla")
        b.clicked.connect(self._set_neutral)
        qrow.addWidget(b)
        qrow.addStretch(1)
        gl.addLayout(qrow)
        lay.addWidget(gbox)

        bbox = QGroupBox("Siyah seviyesi")
        bl = QVBoxLayout(bbox)
        # Monitörün OSD'sinde tek çubuk var; DDC'de ise R/G/B ayrı kodlar (0x6C/6E/70).
        # Tek kaydırıcı üçünü birlikte yazar.
        black = vcp.Feature(vcp.BLACK_R, "Siyah seviyesi", "black")
        s = VcpSlider(black, self._live_black)
        s.setToolTip("0x6C/0x6E/0x70 kodlarını birlikte yazar")
        self.sliders[vcp.BLACK_R] = s
        bl.addWidget(s)
        lay.addWidget(bbox)
        lay.addStretch(1)
        self.tabs.addTab(w, QIcon.fromTheme("color-management"), "Renk")

    def _build_picture_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        box = QGroupBox("Görüntü")
        bl = QVBoxLayout(box)
        self._add_sliders(bl, [vcp.BRIGHTNESS, vcp.CONTRAST, vcp.SHARPNESS])
        lay.addWidget(box)
        abox = QGroupBox("Ses")
        al = QVBoxLayout(abox)
        self._add_sliders(al, [vcp.VOLUME])
        lay.addWidget(abox)
        note = QLabel("Not: KDE Plasma da parlaklığı DDC/CI ile kontrol eder. Parlaklığı yalnızca KDE'den "
                      "yönetmek istiyorsanız profili kaydederken 'Parlaklık' grubunu işaretlemeyin.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(note)
        lay.addStretch(1)
        self.tabs.addTab(w, QIcon.fromTheme("video-display"), "Görüntü ve Ses")

    def _build_automation_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        s = self.cfg.settings

        def check(key, text):
            cb = QCheckBox(text)
            cb.setChecked(bool(s[key]))
            cb.toggled.connect(lambda v: self._setting(key, v))
            form.addRow(cb)
            return cb

        check("apply_on_start", "Oturum açılınca varsayılan profili uygula")
        check("apply_on_hotplug", "Monitör takılınca / gücü gelince aktif profili uygula (udev DRM)")
        check("apply_on_resume", "Uykudan dönünce aktif profili uygula (logind)")
        check("apply_on_unlock", "Ekran kilidi açılınca doğrula ve gerekirse uygula")
        check("watchdog_enabled", "Sıfırlanma koruması (watchdog): değerleri periyodik geri oku")
        check("enforce_profile", "Katı mod: OSD'den yapılan renk değişikliklerini de geri al "
                                 "(parlaklık/ses hariç)")
        check("start_hidden", "Uygulama başlarken pencereyi gizle (sadece tepsi)")

        sp = QSpinBox()
        sp.setRange(5, 3600)
        sp.setSuffix(" sn")
        sp.setValue(int(s["watchdog_interval"]))
        sp.valueChanged.connect(lambda v: self._setting("watchdog_interval", v))
        form.addRow("Watchdog aralığı:", sp)

        dl = QDoubleSpinBox()
        dl.setRange(0.2, 15)
        dl.setSingleStep(0.5)
        dl.setSuffix(" sn")
        dl.setValue(float(s["hotplug_delay"]))
        dl.valueChanged.connect(lambda v: self._setting("hotplug_delay", v))
        form.addRow("Takma sonrası bekleme:", dl)

        gap = QSpinBox()
        gap.setRange(0, 500)
        gap.setSuffix(" ms")
        gap.setValue(int(s["write_gap_ms"]))
        gap.valueChanged.connect(lambda v: self._setting("write_gap_ms", v))
        form.addRow("Yazmalar arası bekleme:", gap)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        form.addRow(sep)
        sig_help = QLabel(
            "Sıfırlanma imzası: monitör kendi kendine fabrika renk moduna döndüğünde kazançların aldığı "
            "değerler (bu monitörde Kırmızı 35 / Yeşil 35 / Mavi 47). Watchdog monitörde tam bu değerleri "
            "görürse \"monitör sıfırlandı\" der ve profili yeniden uygular. Parlaklık ve ses hiçbir zaman "
            "geri alınmaz; onlar KDE'ye bırakılır.")
        sig_help.setWordWrap(True)
        sig_help.setStyleSheet("color: palette(placeholder-text);")
        form.addRow(sig_help)
        self.sig_label = QLabel()
        form.addRow("Sıfırlanma imzası:", self.sig_label)
        row = QHBoxLayout()
        b0 = QPushButton("Fabrika değerleri (35/35/47)")
        b0.clicked.connect(lambda: self._set_signature(dict(vcp.FACTORY_SIGNATURE)))
        b1 = QPushButton("Şu anki kazançları imza yap")
        b1.setToolTip("Monitör kendini fabrika moduna aldığı anda basın.\n"
                      "Watchdog bu değerleri gördüğünde profili otomatik yeniden uygular.")
        b1.clicked.connect(self._capture_signature)
        b2 = QPushButton("İmzayı temizle")
        b2.clicked.connect(lambda: self._set_signature({}))
        row.addWidget(b0)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        form.addRow(row)
        self.tabs.addTab(w, QIcon.fromTheme("system-run"), "Otomasyon")

    def _build_hardware_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.hw_info = QLabel()
        self.hw_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.hw_info.setWordWrap(True)
        lay.addWidget(self.hw_info)

        row = QHBoxLayout()
        b = QPushButton(QIcon.fromTheme("document-save"), "Ayarları monitör belleğine kaydet (0x0C)")
        b.setToolTip("DDC/CI 'Save Current Settings'. Bu firmware güç kesilince yine unutabilir;\n"
                     "asıl kalıcılığı uygulamanın otomatik uygulaması sağlar.")
        b.clicked.connect(self._save_to_monitor)
        row.addWidget(b)
        b = QPushButton(QIcon.fromTheme("view-refresh"), "Değerleri monitörden oku")
        b.setToolTip("Tüm ayarları monitörden geri okur, kaydırıcıları günceller ve aşağıda listeler")
        b.clicked.connect(self._read_all)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        raw = QGroupBox("Ham VCP erişimi (ileri düzey)")
        gl = QGridLayout(raw)
        self.raw_code = QLineEdit()
        self.raw_code.setPlaceholderText("hex kod, örn. 14")
        self.raw_code.setFixedWidth(150)
        self.raw_val = QSpinBox()
        self.raw_val.setRange(0, 65535)
        b_get = QPushButton("Oku")
        b_set = QPushButton("Yaz")
        b_get.clicked.connect(self._raw_get)
        b_set.clicked.connect(self._raw_set)
        gl.addWidget(QLabel("Kod (hex):"), 0, 0)
        gl.addWidget(self.raw_code, 0, 1)
        gl.addWidget(QLabel("Değer:"), 0, 2)
        gl.addWidget(self.raw_val, 0, 3)
        gl.addWidget(b_get, 0, 4)
        gl.addWidget(b_set, 0, 5)
        self.raw_out = QPlainTextEdit()
        self.raw_out.setReadOnly(True)
        self.raw_out.setMaximumHeight(220)
        self.raw_out.setPlaceholderText("Ham kod okumak için hex kod yazıp 'Oku'ya basın (örn. 10 = parlaklık)")
        gl.addWidget(self.raw_out, 1, 0, 1, 6)
        lay.addWidget(raw)
        lay.addStretch(1)
        self.tabs.addTab(w, QIcon.fromTheme("hwinfo"), "Donanım")

    def _build_log_tab(self):
        self.log = QListWidget()
        self.log.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tabs.addTab(self.log, QIcon.fromTheme("view-history"), "Günlük")

    # ------------------------------------------------------------- yardımcılar
    def _setting(self, key, value):
        self.cfg.set_setting(key, value)
        self.engine.settings_changed()

    def _display(self) -> ddc.Display | None:
        return self.engine.displays.get(self.key) if self.key else None

    def _on_message(self, text: str, error: bool):
        self.statusBar().showMessage(text, 8000)
        it = QListWidgetItem(f"{time.strftime('%H:%M:%S')}  {text}")
        if error:
            it.setForeground(QColor("#e5484d"))
        self.log.insertItem(0, it)
        while self.log.count() > 500:
            self.log.takeItem(self.log.count() - 1)

    # --------------------------------------------------------------- monitörler
    def _refresh_monitors(self):
        cur = self.key
        self.monitor_combo.blockSignals(True)
        self.monitor_combo.clear()
        for k, d in self.engine.displays.items():
            self.monitor_combo.addItem(f"{d.title}  ({d.connector.split('-', 1)[-1]})", k)
        self.monitor_combo.blockSignals(False)
        keys = list(self.engine.displays)
        if not keys:
            self.key = None
            self.id_label.setText("DDC/CI destekli harici monitör bulunamadı")
            self.hw_info.setText("")
            self.tabs.setEnabled(False)
            self._refresh_profiles()
            return
        self.tabs.setEnabled(True)
        self.monitor_combo.setCurrentIndex(keys.index(cur) if cur in keys else 0)
        self._monitor_selected()

    def _monitor_selected(self, *_):
        self.key = self.monitor_combo.currentData()
        d = self._display()
        if not d:
            return
        self.id_label.setText(f"/dev/i2c-{d.bus} · {d.connector} · MCCS {d.vcp_version}")
        # EDID metinleri monitörden gelir (güvenilmeyen donanım girdisi): HTML olarak yorumlanmasın
        self.hw_info.setText(
            f"<b>{esc(d.title)}</b><br>Üretici: {esc(d.mfg)} · Model: {esc(d.model)} · "
            f"Ürün kodu: 0x{d.product_code:04x}<br>"
            f"Seri: {esc(d.serial) or '—'} (ikili: {d.bin_serial}) · Üretim: {esc(d.year)}<br>"
            f"I2C: /dev/i2c-{d.bus} · Bağlantı: {esc(d.connector)} · MCCS: {esc(d.vcp_version)}<br>"
            f"Profil anahtarı: <code>{esc(d.key)}</code>")
        mon = self.cfg.monitor(self.key)
        self._neutral = tuple(mon.get("neutral_gains", [50, 50, 50]))
        self._refresh_signature()
        self._refresh_profiles()
        self.engine.read_values(self.key)

    def _on_values(self, key: str, values: dict):
        if key != self.key:
            return
        for code, (val, _mx) in values.items():
            if code in self.sliders:
                self.sliders[code].set_value(val, hw=True)
            elif code == vcp.PRESET:
                name = vcp.PRESETS.get(val, f"0x{val:02x}")
                self.preset_readback.setText(f"Geri okunan: {name} (0x{val:02x})")
        if getattr(self, "_dump_next", False):
            self._dump_next = False
            self._dump_values(values)
        self._update_dirty()

    # ----------------------------------------------------------------- canlı
    def _live(self, code: int, value: int):
        if self.key:
            self.engine.set_live(self.key, code, value)
        self._dirty_timer.start()

    def _live_black(self, _code: int, value: int):
        for c in vcp.BLACK_CODES:
            self._live(c, value)

    def _set_preset(self, val: int):
        for v, b in self.preset_buttons.items():
            b.setChecked(v == val)
        self.preset_value = val
        if self.key:
            self.engine.set_live(self.key, vcp.PRESET, val)
            # scaler ön ayarla kazançları değiştirebilir; geri oku
            QTimer.singleShot(700, self._reread)

    def _reread(self) -> None:
        if self.key:
            self.engine.read_values(self.key)

    def _nudge(self, code: int, delta: int):
        s = self.sliders[code]
        s.set_value(max(0, min(100, s.value() + delta)))
        self._live(code, s.value())

    def _kelvin_changed(self, v: int):
        k = v * 100
        self.kelvin_label.setText(f"{k} K")
        r, g, b = vcp.gains_for_kelvin(k, self._neutral)
        for code, val in ((vcp.GAIN_R, r), (vcp.GAIN_G, g), (vcp.GAIN_B, b)):
            self.sliders[code].set_value(val)
            self._live(code, val)

    def _shift_kelvin(self, steps: int):
        self.kelvin.setValue(self.kelvin.value() + steps)

    def _set_neutral(self):
        if not self.key:
            return
        n = [self.sliders[c].value() for c in (vcp.GAIN_R, vcp.GAIN_G, vcp.GAIN_B)]
        self._neutral = tuple(n)
        self.cfg.set_field(self.key, "neutral_gains", n)
        self.kelvin.blockSignals(True)
        self.kelvin.setValue(65)
        self.kelvin_label.setText("6500 K")
        self.kelvin.blockSignals(False)
        self._on_message(f"Nötr referans kaydedildi: R{n[0]} G{n[1]} B{n[2]}", False)

    # ---------------------------------------------------------------- profiller
    def _refresh_profiles(self, select: str | None = None):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        if not self.key:
            self.profile_combo.blockSignals(False)
            self.default_label.setText("")
            self.profile_contents.setText("")
            return
        mon = self.cfg.monitor(self.key)
        for name in mon["profiles"]:
            star = "★ " if name == mon.get("default_profile") else ""
            self.profile_combo.addItem(star + name, name)
        act = mon.get("active_profile")
        i = self.profile_combo.findData(select or act)
        if i >= 0:
            self.profile_combo.setCurrentIndex(i)
        self.profile_combo.blockSignals(False)
        self.default_label.setText(
            f"Aktif: <b>{esc(act or '—')}</b> · Varsayılan: <b>{esc(mon.get('default_profile') or '—')}</b>")
        self._show_profile_contents()

    def _show_profile_contents(self, *_):
        name = self._selected_profile()
        codes = self.cfg.profile_codes(self.key, name) if (self.key and name) else None
        self.profile_contents.setText(f"İçerik: {describe_codes(codes)}" if codes else "")
        self._update_dirty()

    def profile_diff(self) -> list[tuple[int, int, int]]:
        """Seçili profil ile şu anki kaydırıcı değerleri arasındaki farklar: (kod, profil, şimdi)."""
        name = self._selected_profile()
        codes = self.cfg.profile_codes(self.key, name) if (self.key and name) else None
        out = []
        for c, want in (codes or {}).items():
            if c in (vcp.PRESET, vcp.BLACK_G, vcp.BLACK_B):
                continue  # ön ayar geri okuması güvenilmez; siyah tek değer
            s = self.sliders.get(c)
            if s and s.value() != want:
                out.append((c, want, s.value()))
        return out

    def _update_dirty(self):
        diff = self.profile_diff()
        if not diff:
            self.dirty_bar.hide()
            return
        parts = [f"{SHORT.get(c, f'0x{c:02x}')} {want}→{now}" for c, want, now in diff]
        self.dirty_label.setText(f"<b>Profilden farklı</b> ({esc(self._selected_profile() or '')}): "
                                 + esc(", ".join(parts)))
        self.dirty_bar.show()

    def _selected_profile(self) -> str | None:
        return self.profile_combo.currentData()

    def _apply_selected(self):
        name = self._selected_profile()
        if self.key and name:
            self.engine.apply_profile(self.key, name)

    def _current_codes(self, groups: set[str]) -> dict[int, int]:
        out = {}
        for code, s in self.sliders.items():
            if vcp.FEATURES[code].group in groups:
                out[code] = s.value()
        if vcp.BLACK_R in out:
            for c in vcp.BLACK_CODES:
                out[c] = out[vcp.BLACK_R]
        if "preset" in groups and self.preset_value is not None:
            out[vcp.PRESET] = self.preset_value
        return out

    def save_profile_as(self, name: str, groups: set[str], make_default: bool = False) -> str | None:
        """Şu anki kaydırıcı değerlerini profile yazar. Hata metni ya da None döner."""
        if not self.key:
            return "Monitör yok"
        try:
            name = validate_profile_name(name)
        except ValueError as e:
            return str(e)
        codes = self._current_codes(groups)
        if not codes:
            return "En az bir ayar grubu seçin"
        mon = self.cfg.monitor(self.key)
        self.cfg.save_profile(self.key, name, codes)
        self.cfg.set_field(self.key, "active_profile", name)
        if make_default or not mon.get("default_profile"):
            self.cfg.set_field(self.key, "default_profile", name)
        self._refresh_profiles(select=name)
        self.engine.profilesChanged.emit()
        self._on_message(f"Profil kaydedildi: {name} — {describe_codes(codes)}", False)
        return None

    def rename_profile_to(self, old: str, new: str) -> str | None:
        if not self.key:
            return "Monitör yok"
        try:
            new = validate_profile_name(new)
        except ValueError as e:
            return str(e)
        if new == old:
            return None
        if new in self.cfg.profiles(self.key):
            return f"'{esc(new)}' adında bir profil zaten var"
        self.cfg.rename_profile(self.key, old, new)
        self._refresh_profiles(select=new)
        self.engine.profilesChanged.emit()
        self._on_message(f"Profil yeniden adlandırıldı: {old} → {new}", False)
        return None

    def _warn(self, err: str | None) -> bool:
        if err:
            QMessageBox.warning(self, "Profil", err)
        return err is None

    def _new_profile(self):
        if not self.key:
            return
        dlg = SaveProfileDialog(self, list(self.cfg.profiles(self.key)), None)
        dlg.setWindowTitle("Yeni profil")
        dlg.name.setCurrentText("")
        while dlg.exec() == QDialog.DialogCode.Accepted:
            name = dlg.name.currentText().strip()
            if name in self.cfg.profiles(self.key) and QMessageBox.question(
                    self, "Üzerine yaz", f"'{esc(name)}' zaten var. Üzerine yazılsın mı?") \
                    != QMessageBox.StandardButton.Yes:
                continue
            groups = {g for g, cb in dlg.groups.items() if cb.isChecked()}
            if "preset" in groups and self.preset_value is None:
                QMessageBox.information(self, "Ön ayar",
                                        "Ön ayar dahil edilmedi: önce Renk sekmesinden bir ön ayar seçin.")
                groups.discard("preset")
            if self._warn(self.save_profile_as(name, groups, dlg.make_default.isChecked())):
                return

    def _update_profile(self):
        """Seçili profili, içerdiği ayar gruplarını koruyarak şu anki değerlerle günceller."""
        name = self._selected_profile()
        if not (self.key and name):
            return
        old = self.cfg.profile_codes(self.key, name) or {}
        groups = {vcp.FEATURES[c].group for c in old if c in vcp.FEATURES}
        if vcp.PRESET in old and self.preset_value is None:
            self.preset_value = old[vcp.PRESET]  # ön ayar seçilmediyse eskisini koru
        self._warn(self.save_profile_as(name, groups))

    def _make_default(self):
        name = self._selected_profile()
        if self.key and name:
            self.cfg.set_field(self.key, "default_profile", name)
            self._refresh_profiles(select=name)
            self.engine.profilesChanged.emit()

    def _rename_profile(self):
        old = self._selected_profile()
        if not (self.key and old):
            return
        new, ok = QInputDialog.getText(self, "Yeniden adlandır", "Yeni ad:", text=old)
        if ok:
            self._warn(self.rename_profile_to(old, new))

    def _delete_profile(self):
        name = self._selected_profile()
        if not (self.key and name):
            return
        if QMessageBox.question(self, "Sil", f"'{esc(name)}' profili silinsin mi?") == QMessageBox.StandardButton.Yes:
            self.cfg.delete_profile(self.key, name)
            self._refresh_profiles()
            self.engine.profilesChanged.emit()
            self._on_message(f"Profil silindi: {name}", False)

    # ------------------------------------------------------------------ imza
    def _refresh_signature(self):
        sig = self.cfg.reset_signature(self.key) if self.key else {}
        self.sig_label.setText(", ".join(f"0x{c:02x}={v}" for c, v in sorted(sig.items())) or "— (yok)")

    def _capture_signature(self):
        if not self.key:
            return
        sig = {c: self.sliders[c].value() for c in (vcp.GAIN_R, vcp.GAIN_G, vcp.GAIN_B)}
        name = self.cfg.monitor(self.key).get("active_profile")
        prof = self.cfg.profile_codes(self.key, name) or {} if name else {}
        if all(prof.get(c) == v for c, v in sig.items()):
            QMessageBox.warning(
                self, "Sıfırlanma imzası",
                f"Bu kazançlar aktif profil '{esc(name or '')}' ile aynı. İmza, monitör kendini sıfırladığında "
                "gördüğün DEĞİŞMİŞ değerler olmalı (bu monitörde 35/35/47); profille aynı olursa "
                "her küçük sapma sıfırlanma sanılır.")
            return
        self._set_signature(sig)

    def _set_signature(self, sig: dict[int, int]):
        if self.key:
            self.cfg.set_field(self.key, "reset_signature", {f"0x{c:02x}": v for c, v in sig.items()})
            self._refresh_signature()

    # --------------------------------------------------------------- donanım
    def _save_to_monitor(self):
        d = self._display()
        if d:
            self.engine.run_job(lambda: ddc.save_settings(d.bus),
                                lambda _r, e: self._on_message(f"Kaydetme: {e or 'tamam'}", bool(e)))

    def _read_all(self):
        if self.key:
            self._dump_next = True
            self.engine.read_values(self.key)

    def _dump_values(self, values: dict):
        lines = []
        for c in vcp.APPLY_ORDER:
            if c not in values:
                continue
            v = values[c][0]
            if c == vcp.PRESET:
                lines.append(f"Renk ön ayarı (0x14): {vcp.PRESETS.get(v, hex(v))}  "
                             "— bu firmware'de güvenilmez, OSD'ye bakın")
            elif c in vcp.FEATURES:
                lines.append(f"{vcp.FEATURES[c].name} (0x{c:02x}): {v}")
        self.raw_out.setPlainText("\n".join(lines))

    def _raw_code(self) -> int | None:
        try:
            return int(self.raw_code.text().strip().removeprefix("0x"), 16)
        except ValueError:
            self.raw_out.setPlainText("Geçersiz hex kod")
            return None

    def _raw_get(self):
        d, c = self._display(), self._raw_code()
        if d and c is not None:
            self.engine.run_job(lambda: ddc.raw_getvcp(d.bus, c),
                                lambda r, e: self.raw_out.setPlainText(str(e or r)))

    def _raw_set(self):
        d, c = self._display(), self._raw_code()
        if d and c is not None:
            v = self.raw_val.value()
            if c in DANGEROUS_CODES and QMessageBox.warning(
                    self, "Riskli VCP kodu",
                    f"0x{c:02x} kodu fabrika sıfırlaması, güç, giriş kaynağı ya da tuş kilidi gibi "
                    "monitörü sinyalsiz bırakabilecek bir ayar. Yine de yazılsın mı?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            def job() -> str:
                ddc.set_value(d.bus, c, v)
                return ddc.raw_getvcp(d.bus, c)

            self.engine.run_job(job,
                                lambda r, e: self.raw_out.setPlainText(str(e or f"Yazıldı. Geri okuma:\n{r}")))

    # ----------------------------------------------------------------- pencere
    def closeEvent(self, ev):
        if self.quitting or not QSystemTrayIcon.isSystemTrayAvailable():
            ev.accept()
        else:
            ev.ignore()
            self.hide()


class Tray(QSystemTrayIcon):
    def __init__(self, engine: Engine, cfg: Config, show_window, quit_app):
        super().__init__(app_icon())
        self.engine, self.cfg = engine, cfg
        self.setToolTip("Monitör Kontrol Stüdyosu")
        self.menu = QMenu()
        self.setContextMenu(self.menu)
        self._show, self._quit = show_window, quit_app
        self.activated.connect(self._activated)
        engine.displaysChanged.connect(self.rebuild)
        engine.profileApplied.connect(lambda *_: self.rebuild())
        engine.profilesChanged.connect(self.rebuild)
        self.rebuild()

    def _activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show()

    def rebuild(self):
        m = self.menu
        m.clear()
        if not self.engine.displays:
            a = m.addAction("Harici monitör yok")
            a.setEnabled(False)
        for key, d in self.engine.displays.items():
            mon = self.cfg.monitor(key)
            sec = m.addSection(d.title.replace("&", "&&"))
            group = QActionGroup(m)
            for name in mon["profiles"]:
                # '&' Qt menülerinde kısayol işaretidir; adı olduğu gibi göstermek için kaçır
                label = name.replace("&", "&&") + ("  ★" if name == mon.get("default_profile") else "")
                a = QAction(label, m)
                a.setCheckable(True)
                a.setChecked(name == mon.get("active_profile"))
                a.triggered.connect(lambda _c, k=key, n=name: self.engine.apply_profile(k, n))
                group.addAction(a)
                m.addAction(a)
            _ = sec
        m.addSeparator()
        m.addAction(QIcon.fromTheme("view-refresh"), "Ayarları şimdi yeniden uygula",
                    lambda: self.engine.sync("manual", force=True))
        m.addAction(QIcon.fromTheme("configure"), "Kontrol penceresini aç", self._show)
        m.addSeparator()
        m.addAction(QIcon.fromTheme("application-exit"), "Çıkış", self._quit)
