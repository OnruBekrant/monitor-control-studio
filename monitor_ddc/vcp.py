"""MCCS VCP özellik tanımları ve renk sıcaklığı yardımcıları."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Feature:
    code: int
    name: str
    group: str
    max: int = 100
    continuous: bool = True  # C tipi (kaydırıcı) mı, NC (liste) mi


BRIGHTNESS, CONTRAST = 0x10, 0x12
PRESET = 0x14
GAIN_R, GAIN_G, GAIN_B = 0x16, 0x18, 0x1A
BLACK_R, BLACK_G, BLACK_B = 0x6C, 0x6E, 0x70
VOLUME, SHARPNESS = 0x62, 0x87
COLOR_TEMP_REQ = 0x0C
OSD_LANG, POWER = 0xCC, 0xD6

FEATURES: dict[int, Feature] = {f.code: f for f in [
    Feature(BRIGHTNESS, "Parlaklık", "brightness"),
    Feature(CONTRAST, "Kontrast", "basic"),
    Feature(SHARPNESS, "Keskinlik", "basic", max=4),
    Feature(VOLUME, "Ses", "audio"),
    Feature(PRESET, "Renk ön ayarı", "preset", continuous=False),
    Feature(GAIN_R, "Kırmızı kazanç", "gain"),
    Feature(GAIN_G, "Yeşil kazanç", "gain"),
    Feature(GAIN_B, "Mavi kazanç", "gain"),
    Feature(BLACK_R, "Kırmızı siyah seviyesi", "black"),
    Feature(BLACK_G, "Yeşil siyah seviyesi", "black"),
    Feature(BLACK_B, "Mavi siyah seviyesi", "black"),
]}

# Profil uygulanırken yazma sırası: önce ön ayar (kazançları ezebilir), sonra renk, en son ışık.
APPLY_ORDER = [PRESET, GAIN_R, GAIN_G, GAIN_B, BLACK_R, BLACK_G, BLACK_B,
               CONTRAST, BRIGHTNESS, SHARPNESS, VOLUME]

# İşletim sisteminin de değiştirdiği ayarlar: watchdog/doğrulama bunları asla geri almaz
USER_OWNED = {BRIGHTNESS, VOLUME}
BLACK_CODES = (BLACK_R, BLACK_G, BLACK_B)
FACTORY_SIGNATURE = {GAIN_R: 35, GAIN_G: 35, GAIN_B: 47}

# GUI açılışında okunan kodlar
READ_CODES = list(APPLY_ORDER)

GROUPS = {
    "preset": "Renk ön ayarı (0x14)",
    "gain": "RGB kazanç (0x16/18/1A)",
    "black": "RGB siyah seviyesi (0x6C/6E/70)",
    "basic": "Kontrast / Keskinlik",
    "brightness": "Parlaklık (işaretlemezsen parlaklık tamamen KDE'de kalır)",
    "audio": "Ses seviyesi",
}

PRESETS = {
    0x01: "sRGB",
    0x04: "5000 K",
    0x05: "6500 K",
    0x06: "7500 K",
    0x08: "9300 K",
    0x0B: "Kullanıcı",
}


def kelvin_to_rgb(kelvin: float) -> tuple[float, float, float]:
    """Kara cisim yaklaşımı (Tanner Helland). 0..1 aralığında RGB döndürür."""
    t = max(1000.0, min(40000.0, kelvin)) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * ((t - 60) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60) ** -0.0755148492)
        b = 255.0
    clamp = lambda v: max(0.0, min(255.0, v)) / 255.0  # noqa: E731
    return clamp(r), clamp(g), clamp(b)


def gains_for_kelvin(kelvin: float, neutral: tuple[int, int, int],
                     reference: float = 6500.0) -> tuple[int, int, int]:
    """Nötr (referans K'de doğru görünen) kazançlardan hedef sıcaklık için kazanç hesaplar.

    Monitörün kendi beyaz noktası bilinmediği için göreli çalışır: referans sıcaklıkta
    tam olarak `neutral` döner; daha düşük K'de mavi/yeşil kısılır, yüksekte kırmızı kısılır.
    En güçlü kanal nötr değerini korur, böylece parlaklık gereksiz düşmez.
    """
    tgt, ref = kelvin_to_rgb(kelvin), kelvin_to_rgb(reference)
    ratio = [t / r if r else 1.0 for t, r in zip(tgt, ref, strict=True)]
    peak = max(ratio) or 1.0
    return tuple(max(0, min(100, round(n * r / peak))) for n, r in zip(neutral, ratio, strict=True))  # type: ignore[return-value]
