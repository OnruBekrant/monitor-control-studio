"""Profil ve ayar deposu: ~/.config/monitor-ddc/config.json

Yapı:
{
  "settings": {...},
  "monitors": {
     "<edid-key>": {
        "title": "BOE NV156FHM-T06",
        "default_profile": "Günlük Doğal",   # oturum açılışında
        "active_profile": "Gece",            # takma/uyanma/watchdog'da (son seçilen)
        "reset_signature": {"0x16": 35, ...}, # monitör kendini sıfırladığında görülen değerler
        "neutral_gains": [50, 50, 50],        # 6500K'de doğru görünen kazançlar
        "profiles": {"Günlük Doğal": {"0x16": 52, ...}}
     }
  }
}
Profil değerleri sadece içerdiği kodları değiştirir; örn. parlaklığı KDE'ye bırakmak
için profile 0x10 eklenmez.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "monitor-ddc"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_SETTINGS = {
    "apply_on_start": True,
    "apply_on_hotplug": True,
    "apply_on_resume": True,
    "apply_on_unlock": True,
    "watchdog_enabled": True,
    "watchdog_interval": 30,     # sn
    "enforce_profile": False,    # True: OSD'den yapılan değişiklikleri bile geri al
    "hotplug_delay": 2.0,        # sn; scaler'ın uyanması için
    "verify_delays": [3, 8],     # uygulamadan sonra doğrulama zamanları
    "write_gap_ms": 40,
    "start_hidden": False,
}

# Bu repo'nun hedef monitörü için hazır değerler (tasarım dokümanından)
KNOWN_MONITORS = {
    "BOE:NV156FHM-T06:2014": {
        "reset_signature": {"0x16": 35, "0x18": 35, "0x1a": 47},
        "neutral_gains": [50, 50, 50],
        "profiles": {
            "Günlük Doğal": {"0x16": 52, "0x18": 50, "0x1a": 40, "0x10": 35, "0x12": 50},
            "Gece / Göz Koruma": {"0x16": 55, "0x18": 48, "0x1a": 32, "0x10": 15},
            "Sinema / Film": {"0x10": 60, "0x12": 65, "0x87": 3},
        },
        "default_profile": "Günlük Doğal",
    }
}


MAX_PROFILE_NAME = 64
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")

# Ayar adı → (tip, alt, üst). Sınır dışı değerler varsayılana döner.
_SETTING_RULES: dict[str, tuple[type, float, float]] = {
    "watchdog_interval": (int, 5, 3600),
    "hotplug_delay": (float, 0.2, 15),
    "write_gap_ms": (int, 0, 500),
}


def validate_profile_name(name: object) -> str:
    """Profil adını temizler; geçersizse ValueError.

    Kontrol karakterleri ve yön değiştirici Unicode (Trojan Source tarzı görsel aldatma)
    reddedilir, uzunluk sınırlanır.
    """
    if not isinstance(name, str):
        raise ValueError("Profil adı metin olmalı")
    name = name.strip()
    if not name:
        raise ValueError("Profil adı boş olamaz")
    if _CTRL.search(name):
        raise ValueError("Profil adında kontrol/yön karakterleri olamaz")
    if len(name) > MAX_PROFILE_NAME:
        raise ValueError(f"Profil adı en fazla {MAX_PROFILE_NAME} karakter olabilir")
    return name


def sanitize_codes(raw: object) -> dict[str, int]:
    """{'0x16': 50} biçimini doğrular; geçersiz girdileri atar (çökme yerine)."""
    out: dict[str, int] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            code = int(str(k), 16)
        except ValueError:
            continue
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= code <= 0xFF or not 0 <= v <= 0xFFFF:
            continue
        out[_hex(code)] = v
    return out


def _sanitize_monitor(m: object) -> dict | None:
    if not isinstance(m, dict):
        return None
    profiles = {}
    for name, codes in (m.get("profiles") or {}).items() if isinstance(m.get("profiles"), dict) else []:
        try:
            profiles[validate_profile_name(name)] = sanitize_codes(codes)
        except ValueError:
            log.warning("geçersiz profil adı atlandı: %r", name)
    ng = m.get("neutral_gains")
    if not (isinstance(ng, list) and len(ng) == 3 and all(isinstance(x, int) and 0 <= x <= 100 for x in ng)):
        ng = [50, 50, 50]
    raw_title = m.get("title")
    title = raw_title if isinstance(raw_title, str) else ""

    def pname(field: str) -> str | None:
        v = m.get(field)
        return v if isinstance(v, str) and v in profiles else None

    return {
        "title": title[:128],
        "default_profile": pname("default_profile"),
        "active_profile": pname("active_profile"),
        "reset_signature": sanitize_codes(m.get("reset_signature")),
        "neutral_gains": ng,
        "profiles": profiles,
    }


def sanitize_settings(raw: object) -> dict:
    s = dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return s
    for k, default in DEFAULT_SETTINGS.items():
        if k not in raw:
            continue
        v = raw[k]
        if k in _SETTING_RULES:
            typ, lo, hi = _SETTING_RULES[k]
            if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
                s[k] = typ(v)
        elif k == "verify_delays":
            if isinstance(v, list) and len(v) <= 5 and all(
                    isinstance(x, (int, float)) and not isinstance(x, bool) and 0 < x <= 60 for x in v):
                s[k] = v
        elif isinstance(default, bool) and isinstance(v, bool):
            s[k] = v
    return s


def _hex(code: int) -> str:
    return f"0x{code:02x}"


def profile_to_codes(p: dict) -> dict[int, int]:
    return {int(k, 16): v for k, v in sanitize_codes(p).items()}


def codes_to_profile(d: dict[int, int]) -> dict[str, int]:
    return {_hex(k): int(v) for k, v in sorted(d.items())}


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or CONFIG_FILE  # çalışma anında çözülür (testler yolu değiştirebilsin)
        self.data: dict = {"settings": dict(DEFAULT_SETTINGS), "monitors": {}}
        self.load()

    # --- kalıcılık ---
    def load(self) -> None:
        try:
            if self.path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("config dosyası çok büyük")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("kök JSON nesnesi değil")
            mons = raw.get("monitors", {})
            self.data["monitors"] = {
                str(k)[:256]: sm for k, v in (mons.items() if isinstance(mons, dict) else [])
                if (sm := _sanitize_monitor(v)) is not None}
            self.data["settings"] = sanitize_settings(raw.get("settings", {}))
        except FileNotFoundError:
            pass
        except (ValueError, OSError, UnicodeDecodeError, RecursionError) as e:
            bak = self.path.with_suffix(".broken.json")
            with contextlib.suppress(OSError):
                self.path.rename(bak)
            log.error("config okunamadı (%s); %s olarak yedeklendi", e, bak)

    def save(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config-", suffix=".json")  # 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            Path(tmp).replace(self.path)  # atomik: güç kesilse bile bozuk dosya kalmaz
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
            raise

    # --- ayarlar ---
    @property
    def settings(self) -> dict:
        return self.data["settings"]

    def set_setting(self, key: str, value) -> None:
        self.settings[key] = value
        self.save()

    # --- monitörler ---
    def monitor(self, key: str, title: str = "") -> dict:
        mons = self.data["monitors"]
        if key not in mons:
            seed = {}
            for prefix, known in KNOWN_MONITORS.items():
                if key.startswith(prefix):
                    seed = json.loads(json.dumps(known))
            mons[key] = {
                "title": title,
                "default_profile": seed.get("default_profile"),
                "active_profile": seed.get("default_profile"),
                "reset_signature": seed.get("reset_signature", {}),
                "neutral_gains": seed.get("neutral_gains", [50, 50, 50]),
                "profiles": seed.get("profiles", {}),
            }
            self.save()
        elif title and not mons[key].get("title"):
            mons[key]["title"] = title
        return mons[key]

    def profiles(self, key: str) -> dict[str, dict]:
        return self.monitor(key)["profiles"]

    def profile_codes(self, key: str, name: str) -> dict[int, int] | None:
        p = self.profiles(key).get(name)
        return profile_to_codes(p) if p is not None else None

    def save_profile(self, key: str, name: str, codes: dict[int, int]) -> None:
        name = validate_profile_name(name)
        self.profiles(key)[name] = sanitize_codes(codes_to_profile(codes))
        self.save()

    def delete_profile(self, key: str, name: str) -> None:
        m = self.monitor(key)
        m["profiles"].pop(name, None)
        for f in ("default_profile", "active_profile"):
            if m.get(f) == name:
                m[f] = None
        self.save()

    def rename_profile(self, key: str, old: str, new: str) -> None:
        m = self.monitor(key)
        new = validate_profile_name(new)
        if old not in m["profiles"] or new in m["profiles"]:
            return
        m["profiles"] = {(new if k == old else k): v for k, v in m["profiles"].items()}
        for f in ("default_profile", "active_profile"):
            if m.get(f) == old:
                m[f] = new
        self.save()

    def set_field(self, key: str, field: str, value) -> None:
        self.monitor(key)[field] = value
        self.save()

    def reset_signature(self, key: str) -> dict[int, int]:
        return profile_to_codes(self.monitor(key).get("reset_signature", {}))
