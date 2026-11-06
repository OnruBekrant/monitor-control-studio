# Security / Güvenlik

## 🇬🇧 English summary

**Reporting a vulnerability:** please use GitHub's *Report a vulnerability* (private security advisory) on this repository instead of a public issue.

**Threat model.** The app is a single-user desktop service. It opens no network sockets; its only endpoint is the per-user session D-Bus. Untrusted inputs are EDID strings and `ddcutil` output (from monitor firmware), `config.json`, and D-Bus/CLI arguments. All of them are validated:

* **Input validation:** VCP codes, values and bus numbers are range-checked, and `ddcutil` arguments go through an allow-list (`shell=False`). Profile names reject control and bidi characters (Trojan Source).
* **Untrusted text:** EDID strings and profile names are HTML-escaped before display.
* **Config:** a corrupted or malicious `config.json` is schema-checked and cannot crash the service.
* **I²C lock file:** private (`0600`, `O_NOFOLLOW`) and never placed in a world-writable directory.
* **systemd sandbox:** the user service runs sandboxed (`systemd-analyze security`: **1.4 OK**). Persistence locations (`~/.config/autostart`, `~/.config/systemd`, `~/.local`) are read-only and `~/.ssh` and `~/.gnupg` are inaccessible.
* **Risky raw VCP writes:** factory reset, power and input source require confirmation.
* **Continuous checks:** ruff (incl. bandit rules), mypy, bandit, semgrep, detect-secrets/gitleaks, pip-audit, shellcheck and 140 tests (fuzzing, D-Bus abuse, sandbox) run on every push via `scripts/check.sh`.

The detailed report below is in Turkish.

---

## 🇹🇷 Güvenlik Raporu ve Tehdit Modeli

Son denetim: 2026-11-07 · Sürüm 1.0.0

**Açık bildirmek için:** herkese açık bir issue yerine bu depodaki *Report a vulnerability* (özel güvenlik bildirimi) özelliğini kullanın.

## Tehdit modeli

Uygulama tek kullanıcılı bir masaüstü servisidir. Ağa açılmaz; tek dinleme noktası oturum D-Bus'ıdır ve ona yalnızca aynı kullanıcının süreçleri erişebilir. Bu yüzden güvenilmeyen girdiler şunlardır:

| Girdi | Kaynak | Risk |
|---|---|---|
| EDID metinleri (model, seri) | Monitör donanımı (kötü niyetli/bozuk monitör) | Arayüzde HTML enjeksiyonu, ayrıştırıcı çökmesi |
| `ddcutil` çıktısı | Monitör firmware'i | Ayrıştırıcı çökmesi |
| `config.json` | Elle düzenleme, bozulma, başka bir araç | Servisin çökmesi (otomasyonun durması), sınır dışı DDC değerleri |
| D-Bus çağrıları (`Apply`, `Show`, `Reapply`, `Quit`) | Aynı kullanıcının herhangi bir süreci | Girdi doğrulama, aşırı uzun ad, görsel aldatma |
| CLI argümanları | Kullanıcı / betikler | Girdi doğrulama |
| Kurulum yolu, `PATH`, çalışma dizini | Ortam | Yanlış ikilinin/modülün çalıştırılması |

## Kapatılan açıklar

| # | Önem | Açık | Çözüm | Test |
|---|---|---|---|---|
| 1 | Orta | I2C kilit dosyası `XDG_RUNTIME_DIR` yoksa herkesin yazabildiği `/tmp`'ye düşüyordu ve sembolik bağı takip ediyordu (CWE-377/59) | Yalnızca kullanıcıya ait, başkalarının yazamadığı bir dizin kabul ediliyor; değilse `~/.cache/monitor-ddc` (0700) kullanılıyor; dosya `O_NOFOLLOW` ve 0600 ile açılıyor | `test_lockfile_*` |
| 2 | Orta | Bozuk ya da kötü niyetli `config.json` servisi çökertebiliyordu (DoS); sınır dışı değerler monitöre yazılabiliyordu | Dosya yüklenirken şema doğrulanıyor: kod 0–255, değer 0–65535, ayarlar sınırlı; geçersiz girdiler atılıyor; 2 MB boyut sınırı; JSON özyineleme koruması var | `test_malicious_profile_values_are_dropped`, fuzz testleri |
| 3 | Orta | EDID'den gelen model/seri ve profil adları zengin metin (HTML) olarak işleniyordu (CWE-79 benzeri) | Tüm dış metinler `html.escape` ile kaçırılıyor; tepsi menüsünde `&` kaçırılıyor | `test_html_in_names_is_escaped`, `test_edid_strings_escaped` |
| 4 | Orta | Profil adlarında kontrol karakteri ve yön değiştirici Unicode (Trojan Source tarzı görsel aldatma) kabul ediliyordu | `validate_profile_name`: kontrol ve yön karakterleri reddediliyor, 64 karakter sınırı var; D-Bus, CLI, arayüz ve config'de uygulanıyor | `test_invalid_profile_names`, D-Bus testi |
| 5 | Düşük | `ddcutil`'e iletilen değerlerin tipi ve aralığı doğrulanmıyordu | `bus`/`kod`/`değer` doğrulanıyor; argümanlar bir beyaz listeden geçiyor (`--edid` gibi seçenekler sızamaz); `shell=False` | `test_run_rejects_unexpected_args`, `test_set_rejects_*` |
| 6 | Düşük | `ddcutil`, `PATH` sırasına göre bulunuyordu | Varsa `/usr/bin/ddcutil` sabit yolu tercih ediliyor | `test_prefers_system_ddcutil_path` |
| 7 | Düşük | `python -m`, çalışma dizinini (`~`) `sys.path`'in başına ekliyordu; oraya bırakılmış bir modül yüklenebilirdi | Başlatıcı `python3 -P` ile çalışıyor | — |
| 8 | Orta | Yakalanmamış bir istisna (Qt bağlamalarının bazı sürümlerinde) `abort()` ile tüm servisi öldürebiliyordu (DoS) | `sys.excepthook` istisnayı günlüğe yazıyor; ayrıca `Restart=on-failure` var | `test_excepthook_logs_instead_of_abort` |
| 9 | Düşük | Ham VCP yazımı, fabrika sıfırlama, güç, giriş kaynağı ve tuş kilidi kodlarına onaysız izin veriyordu | Riskli kodlar (0x04, 0x08, 0x60, 0xCA, 0xD6, 0xE0–0xFF…) için onay isteniyor | `test_dangerous_raw_write_needs_confirmation` |
| 10 | Yüksek | systemd servisi hiç sertleştirilmemişti (exposure **9.4 UNSAFE**) | Sandbox eklendi: salt okunur sistem ve ev dizini, kalıcılık noktaları (`autostart`, `systemd`, `environment.d`, `~/.local`) kilitli, `~/.ssh` ve `~/.gnupg` erişilemez, syscall filtresi, `AF_UNIX`/`AF_NETLINK` dışında soket yok, `NoNewPrivileges`. Sonuç: **1.4 OK** | `scripts/check.sh` (`systemd-analyze security`) |
| 11 | Düşük | `install.sh`, `sed` ile yola değer yerleştirirken özel karakterleri kontrol etmiyordu; herkesin yazabildiği kod dizinine kurulum yapılabiliyordu | `\|`, `&`, `\` ve satır sonu içeren yollar reddediliyor; herkesin yazabildiği dosya varsa kurulum duruyor; `umask 077`; unit dosyası doğrulanıyor | `shellcheck` |
| 12 | Düşük | Config yazımı başarısız olursa geçici dosya kalıyordu; config dizini 0755 izinle oluşuyordu | Hata olursa geçici dosya siliniyor, `fsync` yapılıyor; dizin 0700, dosya 0600 | `test_save_*` |

## Lisans uyumluluğu

Arayüz önce PyQt6 (yalnızca GPLv3) ile yazılmıştı. GPLv3, "ticari satış yasak" gibi ek kısıtlamalara izin vermez. Proje bu yüzden **PySide6**'ya (LGPLv3) taşındı ve **PolyForm Noncommercial 1.0.0** ile lisanslandı. PySide6 değiştirilmeden, dinamik olarak yüklenen bir sistem kütüphanesi olarak kullanılıyor. ddcutil (GPLv2) ayrı bir program olarak çağrılıyor ve paketlenmiyor.

## Kabul edilen riskler

- **D-Bus erişimi:** Aynı kullanıcının herhangi bir süreci `Apply` ve `Quit` çağırabilir. Bu, masaüstü oturumundaki güven sınırıdır; o kullanıcıyla kod çalıştırabilen bir saldırgan zaten çok daha fazlasını yapabilir.
- **`~/.config` ve `~/.cache` yazılabilir:** Qt/KDE tema eklentisi KConfig kilit dosyaları için bu dizinlere yazmak zorunda; ev dizini salt okunur yapılınca arayüz takılıyor (test edildi). Oturum açılışında kod çalıştırabilecek alt dizinler salt okunur bırakıldı.
- **semgrep geliştirme bağımlılıkları:** semgrep 1.184.0, `mcp` ve `opentelemetry` paketlerinin bilinen açıklı sürümlerine sabitli. Bu yüzden semgrep ayrı bir ortamda (`.venv-semgrep`) çalışıyor ve uygulamayla dağıtılmıyor.

## Çalıştırılan testler

| Tür | Araç | Sonuç |
|---|---|---|
| Lint ve güvenlik kuralları | ruff (pyflakes, bugbear, bandit-S, pylint, Trojan Source PLE2502) | Temiz |
| Tip denetimi | mypy | Temiz |
| SAST | bandit, semgrep (p/python, p/bandit, p/secrets) | Temiz (subprocess kullanımı gerekçesiyle işaretli) |
| Gizli anahtar taraması | detect-secrets (CI'da ayrıca gitleaks) | Temiz |
| Bağımlılık açığı (SCA) | pip-audit: uygulama (PySide6 6.12.0) ve geliştirme ortamı | Açık yok |
| SBOM | CycloneDX (`sbom.cdx.json`) | Üretildi |
| Kabuk betikleri | shellcheck | Temiz |
| Ölü kod | vulture | Temiz |
| Paketleme | desktop-file-validate, systemd-analyze verify/security | Geçti, 1.4 OK |
| Birim | pytest (vcp, parser, config, ddc) | Geçti |
| Özellik tabanlı / fuzz | hypothesis: EDID, ddcutil çıktısı, config, ayarlar (binlerce rastgele girdi) | Geçti |
| Modül / entegrasyon | Motor + arayüz, sahte ddcutil ve sahte sysfs ile (Qt offscreen) | Geçti |
| Dinamik / DAST | Gerçek süreç, izole D-Bus oturumu, kötü niyetli D-Bus girdileri, CLI | Geçti |
| Sandbox doğrulaması | Servis içinden kalıcılık noktalarına yazma denemesi | Engellendi |
| Kararlılık | 6 paralel çalıştırma | 6/6 geçti |
| Gerçek donanım | `tests/integration/live_*.py` | `scripts/check.sh --live` ile ayrıca çalıştırılır |

Toplam: 140 otomatik test, %91 satır+dal kapsamı. Tümünü çalıştırmak için:

```sh
scripts/check.sh            # statik + güvenlik + birim/modül/dinamik
scripts/check.sh --live     # + gerçek monitörde (servis geçici olarak durur)
```
