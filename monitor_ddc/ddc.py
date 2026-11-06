"""ddcutil üzerinden DDC/CI erişimi.

Neden CLI ve ctypes değil? libddcutil'in C API'si sürümler arasında değişiyor (2.x → 3.x
yapıları farklı); ctypes ile yanlış struct düzeni çökme demek. `ddcutil -b N` çağrısı
tek bir özellik için ~40-120 ms sürüyor, bu da bir kuyruk + birleştirme (coalescing)
ile kaydırıcılar için fazlasıyla yeterli.

Bu monitörde (RealTek RTK v0.1) gözlenen tuhaflıklar:
  * Tek `setvcp` komutunda birden çok kod gönderildiğinde sonuncusu kayboluyor →
    her özellik ayrı komutla, aralarında kısa bekleme ile yazılır.
  * 0x14 yazma doğrulaması başarısız oluyor ve geri okunan değer yazılandan farklı →
    her zaman --noverify kullanılır; doğrulama kendi tarafımızda, sadece C tipi
    kodlar için yapılır.
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
# Güvenlik: yalnızca sabit ddcutil yolu, shell=False ve argüman beyaz listesi (_SAFE_ARG) ile kullanılır.
import subprocess  # nosec B404  # nosemgrep: gitlab.bandit.B404
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Güvenlik: PATH'te önce gelen sahte bir 'ddcutil' çalıştırılmasın diye sistem yolu tercih edilir.
DDCUTIL = "/usr/bin/ddcutil" if os.access("/usr/bin/ddcutil", os.X_OK) else (shutil.which("ddcutil") or "ddcutil")
_lock = threading.Lock()  # süreç içi I2C serileştirme

# Girdi sınırları (DDC/CI: 8 bit kod, 16 bit değer)
MAX_BUS, MAX_CODE, MAX_VALUE = 1023, 0xFF, 0xFFFF


def _lock_path() -> str:
    """Süreçler arası I2C kilidinin yolu.

    Servis, CLI (--apply) ve testler aynı anda hatta yazmasın: eşzamanlı I2C işlemleri bu
    RealTek kartında paket bozulmasına ve renklerin fabrika değerine dönmesine yol açabiliyor.
    Güvenlik: herkesin yazabildiği /tmp kullanılmaz (sembolik bağ / önceden oluşturma saldırısı);
    yalnızca kullanıcıya ait ve başkalarının yazamadığı bir dizin kabul edilir.
    """
    rt = os.environ.get("XDG_RUNTIME_DIR", "")
    try:
        st = os.stat(rt) if rt else None
    except OSError:
        st = None
    if st and st.st_uid == os.getuid() and not (st.st_mode & 0o022):
        # systemd servisi bu dizini RuntimeDirectory= ile yazılabilir olarak oluşturur;
        # CLI ve testler de aynı yolu kullanır.
        d = os.path.join(rt, "monitor-ddc")
        os.makedirs(d, mode=0o700, exist_ok=True)
        return os.path.join(d, "i2c.lock")
    cache = os.path.join(os.path.expanduser("~"), ".cache", "monitor-ddc")
    os.makedirs(cache, mode=0o700, exist_ok=True)
    return os.path.join(cache, "i2c.lock")


def _open_lock() -> int:
    # O_NOFOLLOW: kilit yolu bir sembolik bağ ile başka bir dosyaya yönlendirilemez
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    return os.open(_lock_path(), flags, 0o600)


def _check_int(name: str, v: object, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= hi:
        raise DdcError(f"geçersiz {name}: {v!r} (0..{hi} tamsayı olmalı)")
    return v


class DdcError(RuntimeError):
    pass


@dataclass
class Display:
    bus: int
    connector: str = ""
    mfg: str = ""
    model: str = ""
    product_code: int = 0
    serial: str = ""
    bin_serial: int = 0
    year: str = ""
    vcp_version: str = ""
    dpms: str = "On"          # sysfs: On/Off — Off iken monitöre DDC trafiği gönderilmez
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """EDID tabanlı kalıcı kimlik (I2C bus numarası değişebilir, bu değişmez)."""
        ser = self.serial or str(self.bin_serial)
        return f"{self.mfg}:{self.model}:{self.product_code:04x}:{ser}"

    @property
    def title(self) -> str:
        return f"{self.mfg} {self.model}".strip() or f"i2c-{self.bus}"


_SAFE_ARG = re.compile(r"^(-b|--terse|--noverify|getvcp|setvcp|scs|detect|[0-9]{1,5}|[0-9a-f]{2})$")


def _run(args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    # Savunma derinliği: shell=False zaten enjeksiyonu engeller; ayrıca yalnızca beklenen
    # argüman biçimleri ddcutil'e iletilir (örn. '--edid' gibi seçenekler sızamaz).
    for a in args:
        if not _SAFE_ARG.match(a):
            raise DdcError(f"izin verilmeyen ddcutil argümanı: {a!r}")
    cmd = [DDCUTIL, *args]
    with _lock:
        try:
            fd = _open_lock()
        except OSError as e:
            raise DdcError(f"I2C kilidi açılamadı: {e}") from e
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)  # nosec B603
        except subprocess.TimeoutExpired as e:
            raise DdcError(f"zaman aşımı: {' '.join(cmd)}") from e
        except FileNotFoundError as e:
            raise DdcError("ddcutil bulunamadı (pacman -S ddcutil)") from e
        finally:
            os.close(fd)  # kapatmak kilidi de bırakır


DRM_ROOT = "/sys/class/drm"
# Dahili paneller DDC/CI desteklemez; I2C/AUX hatlarına erişmek Intel PSR'ı bozup
# dahili ekranda titremeye yol açabilir. Bu bağlantılara hiç dokunulmaz.
INTERNAL_PREFIXES = ("eDP", "LVDS", "DSI")


def parse_edid(e: bytes) -> dict:
    if len(e) < 128 or e[:8] != bytes.fromhex("00ffffffffffff00"):
        return {}
    m = (e[8] << 8) | e[9]
    mfg = "".join(chr(((m >> s) & 31) + 64) for s in (10, 5, 0))
    out = {"mfg": mfg, "product_code": e[10] | (e[11] << 8),
           "bin_serial": int.from_bytes(e[12:16], "little"),
           "year": f"{1990 + e[17]}, hafta {e[16]}", "model": "", "serial": ""}
    for i in range(54, 126, 18):
        d = e[i:i + 18]
        if d[0] == 0 and d[1] == 0 and d[3] in (0xFC, 0xFF):
            txt = d[5:18].split(b"\n")[0].decode("ascii", "replace").strip()
            out["model" if d[3] == 0xFC else "serial"] = txt
    return out


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def enumerate_connectors() -> list[Display]:
    """Bağlı harici monitörleri sysfs'ten bulur. HİÇ I2C trafiği üretmez (EDID çekirdekte önbellekli)."""
    out: list[Display] = []
    try:
        names = sorted(os.listdir(DRM_ROOT))
    except OSError:
        return out
    for name in names:
        if "-" not in name or not name.startswith("card"):
            continue
        conn = name.split("-", 1)[1]
        if conn.startswith(INTERNAL_PREFIXES):
            continue
        base = os.path.join(DRM_ROOT, name)
        if _read(os.path.join(base, "status")) != "connected":
            continue
        ddc_link = os.path.join(base, "ddc")
        m = re.search(r"i2c-(\d+)$", os.path.realpath(ddc_link)) if os.path.exists(ddc_link) else None
        if not m:
            continue  # DP-MST vb.: ddc bağlantısı yoksa atla
        try:
            with open(os.path.join(base, "edid"), "rb") as f:
                info = parse_edid(f.read())
        except OSError:
            info = {}
        if not info:
            continue
        out.append(Display(bus=int(m.group(1)), connector=name, dpms=_read(os.path.join(base, "dpms")) or "On",
                           **{k: info[k] for k in ("mfg", "model", "product_code", "serial", "bin_serial", "year")}))
    return out


def probe(bus: int) -> str | None:
    """Monitör DDC/CI'ya cevap veriyor mu? Cevap verirse MCCS sürümünü döndürür."""
    _check_int("bus", bus, MAX_BUS)
    p = _run(["-b", str(bus), "--terse", "getvcp", "df", "10"], timeout=8)
    res = parse_terse(p.stdout)
    if 0x10 not in res:
        return None
    v = res.get(0xDF)
    return f"{v[0] >> 8}.{v[0] & 0xFF}" if v else "?"


def detect() -> list[Display]:
    """CLI için: sysfs'ten bul, sadece o hatları DDC/CI ile yokla."""
    found = []
    for d in enumerate_connectors():
        ver = probe(d.bus)
        if ver:
            d.vcp_version = ver
            found.append(d)
    return found


_TERSE = re.compile(r"^VCP\s+([0-9A-Fa-f]{2})\s+(\S+)\s*(.*)$")


def parse_terse(text: str) -> dict[int, tuple[int, int | None]]:
    """`getvcp --terse` çıktısını {kod: (değer, max|None)} sözlüğüne çevirir."""
    out: dict[int, tuple[int, int | None]] = {}
    for line in text.splitlines():
        m = _TERSE.match(line.strip())
        if not m:
            continue
        code, typ, rest = int(m.group(1), 16), m.group(2), m.group(3).split()
        try:
            if typ == "C" and len(rest) >= 2:
                out[code] = (int(rest[0]), int(rest[1]))
            elif typ in ("CNC", "SNC") and rest:
                # CNC: mh ml sh sl → değer sl (bazı kodlarda sh<<8|sl)
                vals = [int(x.lstrip("x"), 16) for x in rest]
                val = vals[-1] if len(vals) < 4 else (vals[2] << 8) | vals[3]
                out[code] = (val, None)
        except ValueError:
            continue
    return out


def get(bus: int, codes: list[int], retries: int = 2) -> dict[int, tuple[int, int | None]]:
    """Birden çok kodu tek çağrıda okur. Okunamayan kodlar sonuçta yer almaz."""
    _check_int("bus", bus, MAX_BUS)
    if not codes:
        return {}
    for c in codes:
        _check_int("kod", c, MAX_CODE)
    args = ["-b", str(bus), "--terse", "getvcp", *[f"{c:02x}" for c in codes]]
    last = ""
    for attempt in range(retries + 1):
        p = _run(args)
        res = parse_terse(p.stdout)
        if res:
            return res
        last = (p.stderr or p.stdout).strip()
        time.sleep(0.25 * (attempt + 1))
    raise DdcError(f"i2c-{bus} okunamadı: {last[:200]}")


def set_value(bus: int, code: int, value: int, retries: int = 2) -> None:
    _check_int("bus", bus, MAX_BUS)
    _check_int("kod", code, MAX_CODE)
    _check_int("değer", value, MAX_VALUE)
    args = ["-b", str(bus), "--noverify", "setvcp", f"{code:02x}", str(int(value))]
    last = ""
    for attempt in range(retries + 1):
        p = _run(args)
        if p.returncode == 0:
            return
        last = (p.stderr or p.stdout).strip()
        time.sleep(0.2 * (attempt + 1))
    raise DdcError(f"i2c-{bus} 0x{code:02x}={value} yazılamadı: {last[:200]}")


def save_settings(bus: int) -> None:
    _check_int("bus", bus, MAX_BUS)
    p = _run(["-b", str(bus), "scs"])
    if p.returncode != 0:
        raise DdcError((p.stderr or p.stdout).strip()[:200])


def raw_getvcp(bus: int, code: int) -> str:
    """Donanım sekmesi için insan okunur çıktı."""
    _check_int("bus", bus, MAX_BUS)
    _check_int("kod", code, MAX_CODE)
    p = _run(["-b", str(bus), "getvcp", f"{code:02x}"])
    return (p.stdout + p.stderr).strip()
